#!/usr/bin/env python3
"""
tlwatch.py -- who is approaching my aid station.

Trackleaders already projects every runner's SPOT fix onto the course and
publishes it as "Route mile". Give it your station's mileage and how far back
you want to look, and it lists everyone in that stretch, closest first.

    # everyone in the 6 miles before mile 171
    ./tlwatch.py --event bigfoot200-26 --station 171 --back 6

    # keep refreshing every 5 minutes, announce new arrivals into the window
    ./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --watch

    # also show runners up to 2 miles past the station (just departed)
    ./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --past 2

Setup:
    Needs the 'httpx' package: py -3 -m pip install "httpx[http2]"
    (the compiled tlwatch.exe bundles this already -- only matters when
    running tlwatch.py directly from source). Runs on any stock Python 3.11+.
    The optional --gui mode needs tkinter, which ships with the python.org
    Windows installer (it is sometimes absent from Microsoft Store builds).
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

MIN_PY = (3, 11)


def _preflight() -> None:
    """Fail loudly and actionably instead of dumping a bare traceback.

    This runs before anything else. Someone standing up a go-kit at 0200 on a
    freshly imaged laptop should get the exact command to run, pinned to the
    interpreter actually executing this file -- not a generic ImportError.
    """
    if sys.version_info < MIN_PY:
        sys.exit(
            f"tlwatch needs Python {MIN_PY[0]}.{MIN_PY[1]}+; this is "
            f"{sys.version.split()[0]} at {sys.executable}\n"
            "Install a current Python from python.org and re-run."
        )

    # Applications that bundle a private Python (Inkscape, GIMP, Blender, QGIS,
    # ArcGIS) often land on PATH ahead of the real install. Packages then get
    # installed into the vendored copy, which is usually stripped down and will
    # fail in confusing ways later.
    exe_l = (sys.executable or "").lower()
    for app in ("inkscape", "gimp", "blender", "qgis", "arcgis", "krita",
                "freecad"):
        if app in exe_l:
            print(f"WARNING: running under {app.title()}'s bundled Python:\n"
                  f"  {sys.executable}\n"
                  f"This is probably not what you want -- it shadows your real "
                  f"install on PATH.\nList your actual Pythons with:  py -0p\n"
                  f"Then run:  py -3 tlwatch.py ...\n",
                  file=sys.stderr)
            break

    # A frozen build (PyInstaller) always has httpx bundled; only running
    # from source can be missing it.
    if not getattr(sys, "frozen", False):
        try:
            import httpx  # noqa: F401
        except ImportError as e:
            print("tlwatch needs the 'httpx' package, which this Python "
                  f"lacks.\n\n  interpreter: {sys.executable}\n  error: {e}\n"
                  "\nInstall it with:\n"
                  f"  {sys.executable} -m pip install \"httpx[http2]\"\n",
                  file=sys.stderr)
            sys.exit(1)

    # tkinter ships with python.org builds but is routinely absent from the
    # Microsoft Store build and from stock Linux distro Python. Only checked
    # when actually needed, so console use never pays for it.
    if "--gui" in sys.argv:
        try:
            import tkinter  # noqa: F401
            from tkinter import ttk  # noqa: F401
        except ImportError as e:
            exe = sys.executable or "python"
            print("tlwatch: --gui needs tkinter, which this Python lacks.\n",
                  file=sys.stderr)
            print(f"  interpreter: {exe}", file=sys.stderr)
            print(f"  error: {e}\n", file=sys.stderr)
            if sys.platform == "win32":
                print("Windows: the Microsoft Store build of Python often "
                      "omits tcl/tk.\n"
                      "  Install Python from python.org and make sure the "
                      "'tcl/tk and IDLE'\n"
                      "  optional feature is checked, then re-run with that "
                      "interpreter.",
                      file=sys.stderr)
            elif sys.platform == "darwin":
                print("macOS:  brew install python-tk", file=sys.stderr)
            else:
                print("Linux:  sudo apt install python3-tk   "
                      "(or python3-tkinter on Fedora/RHEL)", file=sys.stderr)
            print("\nThe console modes work without it -- drop --gui to "
                  "continue now.", file=sys.stderr)
            sys.exit(1)
    return


_preflight()

import httpx  # noqa: E402 -- after _preflight() so a missing install fails
              # with the actionable message above, not a bare traceback.


def _load_tz(key: str):
    """Event timezone, degrading gracefully.

    Windows ships no system tz database, so stdlib zoneinfo needs the 'tzdata'
    package. Rather than hard-fail over clock formatting, fall back to the
    machine's local timezone -- which is correct anyway whenever you're
    deployed in the same zone as the event. Warn only when the two could
    plausibly differ.
    """
    try:
        return ZoneInfo(key)
    except Exception:
        local = dt.datetime.now().astimezone().tzinfo
        print(f"note: no tz database for {key!r} "
              f"(pip install tzdata to fix); using system local time "
              f"({dt.datetime.now().astimezone():%Z%z}). Times will be wrong "
              f"if this machine is not in the event's timezone.",
              file=sys.stderr)
        return local or dt.timezone.utc

LOG = logging.getLogger("tlwatch")

BASE = "https://trackleaders.com"
UA = "tlwatch/3.0 (personal race-monitoring)"
EVENT_TZ = _load_tz("America/Los_Angeles")

# SPOT units go quiet under canopy constantly; on mountain courses this is
# normal, not alarming. Flag it so you can weight the position accordingly.
STALE_MIN = 45

# Tuned for satellite backhaul (Starlink, BGAN, geostationary VSAT), where a
# single round trip can run 600ms-2000ms+ (or spike far higher under rain
# fade) and terrestrial-tuned defaults read a merely-slow link as a dead one.
DEFAULT_TIMEOUT_S = 45.0
DEFAULT_RETRIES = 5
BACKOFF_BASE_S = 3.0
BACKOFF_MAX_S = 60.0


# --------------------------------------------------------------------------
# scraping
# --------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\xa0]+")
_ROSTER = re.compile(r"i\.php\?name=([A-Za-z0-9_\-]+)")


def detag(raw: str) -> str:
    """HTML -> flat text. Label-anchored parsing survives layout changes."""
    txt = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", txt)))


def _field(text: str, label: str, pattern: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s*[:|]?\s*{pattern}", text, re.I)
    return m.group(1).strip() if m else None


def _parse_ts(s: str | None) -> dt.datetime | None:
    """'08:39:54 AM (PDT) 08/17/26' -> aware datetime."""
    if not s:
        return None
    m = re.match(
        r"(\d{1,2}:\d{2}:\d{2}\s*[AP]M)\s*\([A-Z]{2,5}\)\s*(\d{2}/\d{2}/\d{2})",
        s.strip(), re.I)
    if not m:
        return None
    try:
        naive = dt.datetime.strptime(f"{m.group(2)} {m.group(1)}",
                                     "%m/%d/%y %I:%M:%S %p")
    except ValueError:
        return None
    return naive.replace(tzinfo=EVENT_TZ)


@dataclass(slots=True)
class Runner:
    slug: str
    name: str
    bib: str | None = None
    status: str | None = None
    mile: float | None = None
    mph: float | None = None
    last_update: dt.datetime | None = None
    next_wpt: str | None = None

    @property
    def active(self) -> bool:
        return (self.status or "").strip().lower() == "active"

    @property
    def stale_min(self) -> float | None:
        if self.last_update is None:
            return None
        return (dt.datetime.now(dt.timezone.utc)
                - self.last_update).total_seconds() / 60

    @property
    def last_clock(self) -> str:
        """Last ping as event-local wall clock."""
        if self.last_update is None:
            return "--:--:--"
        return self.last_update.astimezone(EVENT_TZ).strftime("%H:%M:%S")

    @property
    def last_age(self) -> str:
        """Elapsed since last ping, compact."""
        m = self.stale_min
        if m is None:
            return "  --  "
        if m < 60:
            return f"{int(m):>3}m  "
        h, rem = divmod(int(m), 60)
        return f"{h:>2}h{rem:02d}m"


# Bib is anchored on the stats table header, which renders as
# "Elke Albright (3)" immediately followed by the "Status" column heading.
# Bibs are not always numeric -- sweeps are S1..S5 and the Bigfoot mascot
# tracker is BF -- so accept short alphanumerics rather than digits only.
_BIB_HDR = re.compile(r"\(([A-Za-z0-9]{1,5})\)\s*Status")
_BIB_DASH = re.compile(r"-\s+([A-Za-z0-9]{1,5})\s*$", re.M)

# A plausible display-name token. Deliberately excludes underscores, which is
# what distinguishes a rendered name ("Chris Townsley") from the URL slug that
# appears in the page breadcrumb ("Chris_Townsley Full History").
_NAME_TOK = re.compile(r"^[A-Za-z][A-Za-z'\-\.]*$")
_NAME_STOP = {"full", "history", "tracker", "status", "the", "for", "in"}


def _name_before(text: str, end: int, max_tokens: int = 5) -> str | None:
    """Walk backwards from `end` collecting name-shaped tokens.

    Anchoring on the bib and reading leftwards avoids the failure mode where a
    forward regex starts inside the breadcrumb and drags slug text and a line
    break into the captured name.
    """
    toks = text[:end].split()
    out: list[str] = []
    for tok in reversed(toks[-(max_tokens + 3):]):
        if len(out) >= max_tokens:
            break
        if not _NAME_TOK.match(tok) or tok.lower() in _NAME_STOP:
            break
        out.append(tok)
    return " ".join(reversed(out)) or None


def parse_runner(slug: str, raw: str) -> Runner:
    t = detag(raw)
    r = Runner(slug=slug, name=slug.replace("_", " "))

    if m := _BIB_HDR.search(t):
        r.bib = m.group(1).strip()
        if name := _name_before(t, m.start()):
            r.name = name
    elif m := _BIB_DASH.search(t):
        r.bib = m.group(1).strip()

    # Never let stray whitespace out of the parser; it breaks table alignment.
    r.name = " ".join(r.name.split())

    st = _field(t, "Race Status", r"(\w[\w\s]*?)\s+(?:Last Update|Current|$)")
    r.status = st.split("Last")[0].strip() if st else None
    if v := _field(t, "Route mile", r"([\d.]+)\s*mi"):
        r.mile = float(v)
    if v := _field(t, "Current speed", r"([\d.]+)\s*mph"):
        r.mph = float(v)
    r.last_update = _parse_ts(
        _field(t, "Last Update Rec'd", r"([\d:]+\s*[AP]M\s*\([A-Z]+\)\s*[\d/]+)"))
    r.next_wpt = _field(t, "Next waypoint", r"([\w\-\. ]+?)\s+Distance to next")
    return r


def _backoff(attempt: int, floor: float = 1.0) -> float:
    """Exponential backoff with jitter, capped.

    Satellite links tend to fail in bursts -- rain fade, a brief obstruction
    of the line of sight -- rather than cleanly, so the cap is generous and
    the jitter keeps every worker thread from retrying in lockstep the
    instant the link comes back.
    """
    base = min(BACKOFF_MAX_S, floor * (BACKOFF_BASE_S ** attempt))
    return base * (0.5 + random.random())


class Client:
    """Polite fetcher, tuned for satellite backhaul.

    Trackleaders is a small operation carrying live, safety-relevant traffic
    during an active race. Low concurrency plus conditional GETs keeps repeat
    polls cheap (mostly 304s) -- and getting throttled mid-deployment would be
    worse than a slow poll.

    Backed by a single pooled, keep-alive httpx.Client shared across the
    worker threads rather than one connection per request. On a GEO
    satellite link a single TCP+TLS handshake can cost several round trips
    -- multiple seconds -- before the first byte of an actual request goes
    out; reusing pooled connections for the ~180 requests in a poll cycle
    amortizes that cost to roughly once per pooled connection instead of
    once per runner. HTTP/2 multiplexes several in-flight requests over each
    of those connections, which matters more than raw throughput on a link
    that is high-latency and occasionally lossy rather than narrowband.
    """

    def __init__(self, event: str, concurrency: int = 4,
                 timeout: float = DEFAULT_TIMEOUT_S,
                 max_retries: int = DEFAULT_RETRIES,
                 http2: bool = True):
        self.event = event
        self.concurrency = max(1, concurrency)
        self.max_retries = max(1, max_retries)
        self._etags: dict[str, str] = {}
        self._lock = threading.Lock()
        limits = httpx.Limits(
            max_connections=self.concurrency * 2,
            max_keepalive_connections=self.concurrency,
            # A poll cycle is bursty (all ~180 requests, then nothing until
            # the next refresh); holding connections open well past that
            # burst is cheap and saves the next cycle a fresh handshake.
            keepalive_expiry=120.0,
        )
        try:
            self._client = httpx.Client(
                http2=http2, headers={"User-Agent": UA}, timeout=timeout,
                limits=limits, follow_redirects=True)
        except ImportError:
            # http2=True needs the optional 'h2' package; degrade instead of
            # crashing a field deployment over a missing extra.
            LOG.warning("HTTP/2 requested but 'h2' is not installed "
                        "(pip install \"httpx[http2]\"); using HTTP/1.1")
            self._client = httpx.Client(
                http2=False, headers={"User-Agent": UA}, timeout=timeout,
                limits=limits, follow_redirects=True)

    def __enter__(self):
        return self

    def __exit__(self, *e):
        self._client.close()
        return False

    def _get(self, url: str) -> str | None:
        """Fetch a page. Returns None for 304 (unchanged) or permanent failure."""
        with self._lock:
            tag = self._etags.get(url)
        hdr = {"If-None-Match": tag} if tag else {}

        for attempt in range(self.max_retries):
            try:
                resp = self._client.get(url, headers=hdr)
            except httpx.TimeoutException as e:
                LOG.debug("%s: timeout (retry %d): %s", url, attempt + 1, e)
                time.sleep(_backoff(attempt))
                continue
            except httpx.TransportError as e:
                LOG.debug("%s: %s (retry %d)", url, e, attempt + 1)
                time.sleep(_backoff(attempt))
                continue

            if resp.status_code == 304:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(_backoff(attempt, floor=2.0))
                continue
            if resp.status_code >= 400:
                LOG.debug("%s: HTTP %s", url, resp.status_code)
                return None

            if et := resp.headers.get("ETag"):
                with self._lock:
                    self._etags[url] = et
            return _decode(resp)
        LOG.warning("giving up on %s", url)
        return None

    def roster(self) -> list[str]:
        raw = self._get(f"{BASE}/{self.event}f.php")
        if not raw:
            return []
        seen: dict[str, None] = {}
        for s in _ROSTER.findall(raw):
            seen.setdefault(s, None)
        return list(seen)

    def runner(self, slug: str) -> Runner | None:
        raw = self._get(f"{BASE}/{self.event}i.php?name={slug}")
        return parse_runner(slug, raw) if raw else None

    def all_runners(self, slugs: list[str]) -> list[Runner]:
        out: list[Runner] = []
        if not slugs:
            return out
        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futures = {ex.submit(self.runner, s): s for s in slugs}
            for fut in as_completed(futures):
                slug = futures[fut]
                try:
                    r = fut.result()
                except Exception as e:
                    LOG.debug("%s: %s", slug, e)
                    continue
                if r is not None:
                    out.append(r)
        return out


def _decode(resp: "httpx.Response") -> str:
    """Decode a response body. httpx already handles Content-Encoding."""
    charset = resp.encoding or "utf-8"
    return resp.content.decode(charset, errors="replace")


# --------------------------------------------------------------------------
# the query
# --------------------------------------------------------------------------


def in_window(runners: list[Runner], station: float, back: float,
              past: float = 0.0, all_status: bool = False) -> list[Runner]:
    """Runners whose route mile lies in [station - back, station + past].

    Sorted closest-to-station first, i.e. next to arrive at the top.
    """
    lo, hi = station - back, station + past
    hits = [r for r in runners
            if r.mile is not None and lo <= r.mile <= hi
            and (all_status or r.active)]
    return sorted(hits, key=lambda r: -r.mile)


DEFAULT_FLOOR_MPH = 1.5


def arrival_time(r: Runner, station: float,
                 floor_mph: float = DEFAULT_FLOOR_MPH) -> dt.datetime | None:
    """Projected wall-clock arrival at the station.

    Crucially this projects forward from the LAST PING TIME, not from now. A
    runner whose fix is 50 minutes old has been moving that whole time, so
    measuring from now would systematically report them as further out than
    they are -- exactly the runners you'd get caught short on.

    Pace is floored so someone stopped in an aid station doesn't project to
    infinity. This is a planning number, not a promise: ultra pace decays
    nonlinearly and the back half of a 200 is the slow half.
    """
    if r.mile is None or r.mile >= station:
        return None
    base = r.last_update or dt.datetime.now(dt.timezone.utc)
    hours = (station - r.mile) / max(r.mph or 0.0, floor_mph)
    return base + dt.timedelta(hours=hours)


def eta_hours(r: Runner, station: float,
              floor_mph: float = DEFAULT_FLOOR_MPH) -> float | None:
    """Hours from now until projected arrival. Negative means overdue."""
    at = arrival_time(r, station, floor_mph)
    if at is None:
        return None
    return (at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600


def forecast(hits: list[Runner], station: float, hours: float,
             bin_min: int = 30,
             floor_mph: float = DEFAULT_FLOOR_MPH
             ) -> tuple[list[tuple[dt.datetime, list[Runner]]],
                        list[Runner], list[Runner]]:
    """Bucket projected arrivals into fixed windows.

    Returns (bins, due_now, beyond_horizon). `due_now` holds runners whose
    projected arrival has already passed -- typically those with stale pings
    who have likely walked in since their last fix and should be treated as
    imminent rather than dropped from the count.
    """
    now = dt.datetime.now(dt.timezone.utc)
    step = dt.timedelta(minutes=bin_min)

    # Anchor bins on the wall clock (:00 / :30) so they line up with how a
    # crew actually talks about time, rather than drifting off the poll moment.
    local = now.astimezone(EVENT_TZ)
    floor_local = local.replace(
        minute=(local.minute // bin_min) * bin_min, second=0, microsecond=0)
    start = floor_local.astimezone(dt.timezone.utc)

    n_bins = max(1, int(hours * 60 / bin_min))
    edges = [start + i * step for i in range(n_bins + 1)]
    buckets: list[tuple[dt.datetime, list[Runner]]] = [(e, []) for e in edges[:-1]]

    due: list[Runner] = []
    beyond: list[Runner] = []
    for r in hits:
        at = arrival_time(r, station, floor_mph)
        if at is None:          # already past the station
            continue
        if at < now:
            due.append(r)
            continue
        if at >= edges[-1]:
            beyond.append(r)
            continue
        idx = min(int((at - start) / step), n_bins - 1)
        buckets[idx][1].append(r)

    for _, lst in buckets:
        lst.sort(key=lambda x: arrival_time(x, station, floor_mph) or now)
    due.sort(key=lambda x: x.mile or 0, reverse=True)
    beyond.sort(key=lambda x: arrival_time(x, station, floor_mph) or now)
    return buckets, due, beyond


def render_forecast(hits: list[Runner], station: float, hours: float,
                    bin_min: int = 30, names: bool = True) -> str:
    buckets, due, beyond = forecast(hits, station, hours, bin_min)
    step = dt.timedelta(minutes=bin_min)

    total = sum(len(b) for _, b in buckets) + len(due)
    out = [f"\nARRIVAL FORECAST — mile {station:g}, next {hours:g}h "
           f"({bin_min}-min bins)",
           f"{'window':<15} {'n':>3} {'cum':>4}  {'load':<12} runners",
           "-" * 76]

    stale_n = sum(1 for r in hits
                  if (sm := r.stale_min) is not None and sm > STALE_MIN)

    if due:
        who = ", ".join(f"#{r.bib or '?'}" for r in due) if names else ""
        out.append(f"{'DUE NOW':<15} {len(due):>3} {len(due):>4}  "
                   f"{'#' * min(len(due), 12):<12} {who}")

    cum = len(due)
    peak = max((len(b) for _, b in buckets), default=0)
    for edge, lst in buckets:
        cum += len(lst)
        lo = edge.astimezone(EVENT_TZ)
        hi = (edge + step).astimezone(EVENT_TZ)
        label = f"{lo:%H:%M}-{hi:%H:%M}"
        bar = "#" * min(len(lst), 12)
        who = ", ".join(f"#{r.bib or '?'}" for r in lst) if names else ""
        out.append(f"{label:<15} {len(lst):>3} {cum:>4}  {bar:<12} {who}")

    out.append("-" * 76)
    out.append(f"{'TOTAL':<15} {total:>3}        in window; "
               f"peak {peak}/{bin_min}min")
    if beyond:
        out.append(f"{'beyond ' + f'{hours:g}h':<15} {len(beyond):>3}")
    if stale_n:
        out.append(f"\nnote: {stale_n} of {len(hits)} runners have pings older "
                   f"than {STALE_MIN} min. Their arrivals are projected from\n"
                   f"      the ping timestamp, so they are already advanced "
                   f"past the mileage shown above.")
    out.append("      Projections assume current pace holds; late-race pace "
               "usually decays, so treat\n      these as earliest-plausible "
               "arrivals and staff the peak bin with margin.")
    return "\n".join(out) + "\n"
def render_hf_forecast(hits: list[Runner], station: float, hours: float,
                       bin_min: int = 30, wrap: int = 0) -> str:
    """Byte-minimized forecast for HF Winlink transport.

    Same data as render_forecast, stripped of everything derivable or
    decorative. Empty bins are omitted entirely -- absence means zero. At VARA
    HF 500 Hz SL1 (~40-50 bps net) the pretty format is ~100 s of payload
    before handshake and ACK turnarounds; this is roughly a third of that.

    ASCII-only by construction: a multi-byte char costs 2-3x on the wire and
    some gateways mangle it. Enforced below rather than assumed.
    """
    buckets, due, beyond = forecast(hits, station, hours, bin_min)

    def bibs(rs: list[Runner]) -> str:
        # No '#' prefix -- unambiguous in context, and it's one byte per runner.
        return " ".join((r.bib or "?") for r in rs)

    lines = [f"FCST {station:g} {hours:g}H {bin_min}MIN BINS"]
    if due:
        lines.append(f"DUE {bibs(due)}")

    peak = 0
    total = len(due)
    for edge, lst in buckets:
        if not lst:
            continue                      # omit empty bins
        peak = max(peak, len(lst))
        total += len(lst)
        lines.append(f"{edge.astimezone(EVENT_TZ):%H%M} {bibs(lst)}")

    # PEAK is max-per-bin per spec; DUE is not a time bin and is excluded
    # even when it is the larger surge. It still counts toward TOT.
    lines.append(f"TOT {total} PEAK {peak} BEYOND {len(beyond)}")

    if wrap:
        lines = _soft_wrap(lines, wrap)

    out = "\n".join(ln.rstrip() for ln in lines) + "\n"

    # Cheap insurance: strip anything non-ASCII rather than emit bytes that a
    # gateway might mangle or a receiving client might render as garbage.
    if not out.isascii():
        out = out.encode("ascii", "ignore").decode("ascii")
    return out


def _soft_wrap(lines: list[str], width: int) -> list[str]:
    """Wrap long bib lists, continuing with a '+' key.

    Winlink itself handles long lines fine; this exists only for readability
    on the receiving end. A continuation line starts with '+' so a parser can
    still whitespace-split and append to the previous key.
    """
    out: list[str] = []
    for ln in lines:
        if len(ln) <= width:
            out.append(ln)
            continue
        toks = ln.split()
        cur = toks[0]
        for tok in toks[1:]:
            if len(cur) + 1 + len(tok) > width:
                out.append(cur)
                cur = "+ " + tok
            else:
                cur += " " + tok
        out.append(cur)
    return out


def render(hits: list[Runner], station: float, back: float, past: float,
           new: set[str] | None = None, stamp: bool = False) -> str:
    new = new or set()
    hdr = (f"\nMile {station:g} — {len(hits)} runner(s) in "
           f"[{station - back:g}, {station + past:g}]")
    if stamp:
        hdr += f"   {dt.datetime.now(EVENT_TZ):%H:%M:%S}"
    if not hits:
        return hdr + "\n  (none)\n"
    def clean(s: str | None, limit: int) -> str:
        """Collapse whitespace and clamp width.

        Belt-and-suspenders: a single stray newline from the parser would
        otherwise shred the whole table mid-shift.
        """
        s = " ".join((s or "").split())
        return s[: limit - 1] + "…" if len(s) > limit else s

    names = {r.slug: clean(r.name, 26) for r in hits}
    bibs = {r.slug: clean(r.bib, 5) or "?" for r in hits}
    w = max(len(v) for v in names.values())
    b = max(3, max(len(v) for v in bibs.values()))
    lines = [hdr, "",
             f"  {'BIB':<{b}}  {'':<{w}}{'MILE':>8}  {'OFF':>5}  {'PACE':>8}  "
             f"{'ETA':>9}   {'LAST PING':<9} {'AGE':>6}",
             f"  {'-' * (b + w + 56)}"]
    for r in hits:
        delta = r.mile - station
        eta = eta_hours(r, station)
        if eta is None:
            eta_s = "   past  "
        elif eta < 0:
            eta_s = "  DUE NOW"
        else:
            eta_s = f"{eta:5.1f}h out"
        flag = ">" if r.slug in new else " "
        stale = ""
        if (sm := r.stale_min) is not None and sm > STALE_MIN:
            stale = "  STALE"
        lines.append(f"{flag} {bibs[r.slug]:<{b}}  {names[r.slug]:<{w}}  "
                     f"{r.mile:6.1f}  {delta:+5.1f}  "
                     f"{(r.mph or 0):4.1f} mph  {eta_s}   "
                     f"{r.last_clock:<9} {r.last_age}{stale}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------


class Cache:
    """Persistent runner cache with ping-age-based fetch suppression.

    SPOT trackers transmit on a fixed interval (2.5-10 min depending on plan).
    Re-requesting a runner whose last fix is only a few minutes old cannot
    return anything new -- the satellite hasn't heard from them yet. So each
    refresh only fetches runners who are actually due for a new fix. On a
    320-runner roster mid-race this typically cuts request volume by well over
    half, which matters on metered backhaul.

    State survives process restarts, so a laptop reboot mid-shift resumes with
    a warm cache instead of re-scraping the field.
    """

    def __init__(self, path: Path | None, event: str):
        self.path = path
        self.event = event
        self.slugs: list[str] = []
        self.runners: dict[str, Runner] = {}
        self.etags: dict[str, str] = {}
        self.last_try: dict[str, dt.datetime] = {}
        if path:
            self._load()

    # -- persistence ------------------------------------------------------
    def _load(self) -> None:
        try:
            blob = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if blob.get("event") != self.event:
            return          # different race; start clean
        self.slugs = blob.get("slugs", [])
        self.etags = blob.get("etags", {})
        for slug, d in blob.get("runners", {}).items():
            self.runners[slug] = Runner(
                slug=slug, name=d["name"], bib=d.get("bib"),
                status=d.get("status"), mile=d.get("mile"), mph=d.get("mph"),
                last_update=(dt.datetime.fromisoformat(d["last_update"])
                             if d.get("last_update") else None),
                next_wpt=d.get("next_wpt"))
        for slug, ts in blob.get("last_try", {}).items():
            try:
                self.last_try[slug] = dt.datetime.fromisoformat(ts)
            except ValueError:
                pass
        LOG.info("cache: loaded %d runners from %s",
                 len(self.runners), self.path)

    def save(self) -> None:
        if not self.path:
            return
        blob = {
            "event": self.event,
            "saved": dt.datetime.now(dt.timezone.utc).isoformat(),
            "slugs": self.slugs,
            "etags": self.etags,
            "last_try": {k: v.isoformat() for k, v in self.last_try.items()},
            "runners": {
                s: {"name": r.name, "bib": r.bib, "status": r.status,
                    "mile": r.mile, "mph": r.mph,
                    "last_update": (r.last_update.isoformat()
                                    if r.last_update else None),
                    "next_wpt": r.next_wpt}
                for s, r in self.runners.items()},
        }
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(blob))
            tmp.replace(self.path)      # atomic; survives a kill mid-write
        except OSError as e:
            LOG.warning("cache save failed: %s", e)

    # -- scheduling -------------------------------------------------------
    def due(self, slugs: list[str], min_age_min: float,
            retry_min: float) -> list[str]:
        """Slugs worth fetching now."""
        now = dt.datetime.now(dt.timezone.utc)
        out = []
        for s in slugs:
            r = self.runners.get(s)
            if r is None:
                out.append(s)                       # never seen
                continue
            if r.status and r.status.strip().lower() != "active":
                # Finished/scratched runners don't move. Poll rarely, just in
                # case a status gets corrected.
                last = self.last_try.get(s)
                if last is None or (now - last).total_seconds() / 60 >= 60:
                    out.append(s)
                continue
            age = r.stale_min
            if age is None:
                out.append(s)
                continue
            if age < min_age_min:
                continue                            # can't have a new fix yet
            last = self.last_try.get(s)
            if last and (now - last).total_seconds() / 60 < retry_min:
                continue                            # just asked; don't hammer
            out.append(s)
        return out


def fetch(event: str, concurrency: int, cache: Cache,
          min_age_min: float = 10.0,
          retry_min: float = 2.0,
          timeout: float = DEFAULT_TIMEOUT_S,
          max_retries: int = DEFAULT_RETRIES,
          http2: bool = True) -> list[Runner]:
    with Client(event, concurrency, timeout=timeout,
                max_retries=max_retries, http2=http2) as c:
        c._etags = cache.etags
        if not cache.slugs:
            cache.slugs = c.roster()
            LOG.info("roster: %d runners", len(cache.slugs))
        if not cache.slugs:
            LOG.error("empty roster — check --event %r", event)
            return list(cache.runners.values())

        due = cache.due(cache.slugs, min_age_min, retry_min)
        skipped = len(cache.slugs) - len(due)
        if due:
            now = dt.datetime.now(dt.timezone.utc)
            fresh = c.all_runners(due)
            for s in due:
                cache.last_try[s] = now
            # Conditional GETs return None for unchanged pages, so anyone who
            # didn't move simply keeps their cached record.
            for r in fresh:
                cache.runners[r.slug] = r
            LOG.info("fetched %d, cached %d (fix younger than %g min)",
                     len(fresh), skipped, min_age_min)
        else:
            LOG.info("no runners due; all %d served from cache", skipped)
        cache.save()
        return list(cache.runners.values())


def report_size(payload: str) -> None:
    """Tell the operator what they're about to push over a slow link.

    The compressed figure is a zlib estimate, not Winlink's actual B2F/LZHUF,
    so treat it as order-of-magnitude. Airtime shown is payload only --
    connect handshake, proposal/accept, ACK turnarounds and disconnect add
    40-90 s at low speed levels, so budget the session at several times this.
    """
    import zlib
    raw = len(payload.encode("ascii"))
    comp = len(zlib.compress(payload.encode("ascii"), 9))
    print(f"[hf] payload {raw} B raw, ~{comp} B compressed (zlib estimate)",
          file=sys.stderr)
    for label, bps in (("VARA HF 500Hz SL1", 45), ("VARA HF 500Hz SL3", 200),
                       ("VARA FM narrow", 3000)):
        print(f"[hf]   {label:<20} ~{comp * 8 / bps:5.1f} s payload",
              file=sys.stderr)
    print("[hf]   plus 40-90 s handshake/ACK/disconnect at low SL",
          file=sys.stderr)



# GUI colour palettes. Kept as plain data so a theme switch is one lookup and
# nothing else in the GUI needs to know which mode it is in.
PALETTES = {
    "light": {
        "bg":        "#f4f5f7",   # window / toolbar
        "fg":        "#1c1f23",   # normal text
        "field":     "#ffffff",   # text panes, entries, table body
        "field_fg":  "#1c1f23",
        "sel":       "#2f6fb3",   # selection background
        "sel_fg":    "#ffffff",
        "head":      "#e4e7ec",   # table headings
        "border":    "#c3c8d0",
        "stripe":    "#f0f0f0",   # default alternating row shade
        "stale":     "#c2600a",
        "due":       "#b32020",
        "new":       "#1d7a34",
        "hdr":       "#1a4f8a",   # forecast-pane header lines
    },
    "dark": {
        "bg":        "#161a1f",
        "fg":        "#d8dee4",
        "field":     "#101418",
        "field_fg":  "#d8dee4",
        "sel":       "#2b4f76",
        "sel_fg":    "#ffffff",
        "head":      "#20262e",
        "border":    "#2b323c",
        "stripe":    "#1b2430",
        "stale":     "#e5a15c",
        "due":       "#e06c6c",
        "new":       "#7fd88f",
        "hdr":       "#8ab4f8",
    },
}


def run_gui(args, cache) -> int:
    """Two-pane Tk dashboard: forecast left, runner detail right.

    Deliberately a thin shell over the same render functions the console uses,
    so there is exactly one implementation of the formatting and the GUI cannot
    drift from what gets sent over the air.

    Fetches run on a worker thread and hand results back through a queue that
    the Tk main loop drains -- a blocking poll of ~320 pages would otherwise
    freeze the window for the duration.
    """
    import queue
    import threading
    import tkinter as tk
    from tkinter import font as tkfont
    from tkinter import ttk

    results: "queue.Queue" = queue.Queue()
    stop = threading.Event()
    state = {"busy": False, "next_at": None, "last_ok": None, "err": None}

    root = tk.Tk()
    root.title(f"tlwatch — {args.event} — mile {args.station:g}")
    root.geometry("1500x900")
    root.minsize(900, 500)

    mono = tkfont.nametofont("TkFixedFont").copy()
    mono.configure(size=args.font_size)

    # -- toolbar ----------------------------------------------------------
    bar = ttk.Frame(root, padding=(8, 6))
    bar.pack(side="top", fill="x")

    ttk.Label(bar, text="Station mi").pack(side="left")
    v_station = tk.StringVar(value=f"{args.station:g}")
    ttk.Entry(bar, textvariable=v_station, width=7).pack(side="left", padx=(4, 10))

    ttk.Label(bar, text="Back").pack(side="left")
    v_back = tk.StringVar(value=f"{args.back:g}")
    ttk.Entry(bar, textvariable=v_back, width=5).pack(side="left", padx=(4, 10))

    ttk.Label(bar, text="Past").pack(side="left")
    v_past = tk.StringVar(value=f"{args.past:g}")
    ttk.Entry(bar, textvariable=v_past, width=5).pack(side="left", padx=(4, 10))

    ttk.Label(bar, text="Horizon h").pack(side="left")
    v_hours = tk.StringVar(value=f"{args.forecast_hours:g}")
    ttk.Entry(bar, textvariable=v_hours, width=5).pack(side="left", padx=(4, 10))

    ttk.Label(bar, text="Bin min").pack(side="left")
    v_bin = tk.StringVar(value=str(args.bin))
    ttk.Entry(bar, textvariable=v_bin, width=5).pack(side="left", padx=(4, 10))

    v_hf = tk.BooleanVar(value=args.hf_forecast)
    ttk.Checkbutton(bar, text="HF format", variable=v_hf).pack(side="left",
                                                               padx=(0, 10))
    v_auto = tk.BooleanVar(value=args.refresh is not None)
    ttk.Checkbutton(bar, text="Auto", variable=v_auto).pack(side="left")

    ttk.Label(bar, text="every").pack(side="left", padx=(6, 2))
    v_every = tk.StringVar(value=f"{args.refresh or 10:g}")
    ttk.Entry(bar, textvariable=v_every, width=4).pack(side="left")
    ttk.Label(bar, text="min").pack(side="left", padx=(2, 10))

    btn = ttk.Button(bar, text="Refresh now")
    btn.pack(side="left", padx=(0, 8))
    copy_btn = ttk.Button(bar, text="Copy forecast")
    copy_btn.pack(side="left")

    # Countdown lives on the right of the toolbar, separate from the status
    # bar so transient messages ("copied 3 rows") can't clobber it.
    countdown = ttk.Label(bar, text="", width=22, anchor="e")
    countdown.pack(side="right")

    # -- panes ------------------------------------------------------------
    panes = ttk.PanedWindow(root, orient="horizontal")
    panes.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    left = ttk.LabelFrame(panes, text="Arrival forecast", padding=4)
    right = ttk.LabelFrame(panes, text="Runner detail", padding=4)
    panes.add(left, weight=1)
    panes.add(right, weight=2)

    text_panes: list = []

    def textpane(parent):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        yb = ttk.Scrollbar(wrap, orient="vertical")
        xb = ttk.Scrollbar(wrap, orient="horizontal")
        t = tk.Text(wrap, font=mono, wrap="none", undo=False,
                    yscrollcommand=yb.set, xscrollcommand=xb.set,
                    borderwidth=0, highlightthickness=0)
        yb.config(command=t.yview)
        xb.config(command=t.xview)
        yb.pack(side="right", fill="y")
        xb.pack(side="bottom", fill="x")
        t.pack(side="left", fill="both", expand=True)
        t.configure(state="disabled")
        text_panes.append(t)
        return t

    t_fcst = textpane(left)

    # -- detail table -----------------------------------------------------
    # Columns carry a raw sort key alongside the display string. Sorting on
    # the rendered text would order "1h34m" before "51m" and " 0.4h out"
    # lexically -- both wrong. Every column sorts on its native value.
    COLS = [
        ("bib",   "BIB",       60,  "w"),
        ("name",  "NAME",      190, "w"),
        ("mile",  "MILE",      70,  "e"),
        ("off",   "OFF",       65,  "e"),
        ("pace",  "PACE",      75,  "e"),
        ("eta",   "ETA",       85,  "e"),
        ("ping",  "LAST PING", 90,  "e"),
        ("age",   "AGE",       70,  "e"),
    ]

    tree_wrap = ttk.Frame(right)
    tree_wrap.pack(fill="both", expand=True)
    tv_y = ttk.Scrollbar(tree_wrap, orient="vertical")
    tree = ttk.Treeview(tree_wrap, columns=[c[0] for c in COLS],
                        show="headings", selectmode="extended",
                        yscrollcommand=tv_y.set)
    tv_y.config(command=tree.yview)
    tv_y.pack(side="right", fill="y")
    tree.pack(side="left", fill="both", expand=True)

    for key, label, width, anchor in COLS:
        tree.column(key, width=width, anchor=anchor, stretch=(key == "name"))

    # Tk 8.6.9 has a regression where Treeview ignores tag colours entirely.
    # Fixed in 8.6.10+. Detect it and fall back to a text marker so the stale
    # flag never silently disappears.
    tk_patch = root.tk.call("info", "patchlevel")
    tags_broken = tk_patch == "8.6.9"

    # A stripe colour given explicitly on the command line wins over the
    # theme's default and follows the operator across theme switches.
    stripe = {"color": args.stripe_color, "on": not args.no_stripe,
              "user_set": args.stripe_color is not None}

    style = ttk.Style(root)
    native_theme = style.theme_use()
    theme = {"name": "dark" if args.dark else "light"}

    def _luminance(hexcolor: str) -> float:
        """Rough perceived brightness, 0..1. Used to pick readable text."""
        h = (hexcolor or "").lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        try:
            r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        except ValueError:
            return 1.0
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def apply_tags() -> None:
        """(Re)configure row tags for the current theme and stripe colour.

        Stripe sets background only and status tags set foreground only, so a
        row carries both cleanly -- except on a dark stripe, where the stripe
        also supplies light text and the status tag (applied last) still wins
        for stale/due rows.
        """
        if tags_broken:
            return
        pal = PALETTES[theme["name"]]
        tree.tag_configure("stripe", background=stripe["color"])
        if _luminance(stripe["color"]) < 0.5:
            tree.tag_configure("stripe", foreground=pal["field_fg"])
        tree.tag_configure("stale", foreground=pal["stale"])
        tree.tag_configure("due", foreground=pal["due"])
        tree.tag_configure("new", foreground=pal["new"])

    def apply_theme(redraw_after: bool = True) -> None:
        """Repaint every widget for the active palette.

        ttk's native Windows themes ('vista'/'xpnative') hard-code widget
        colours and ignore most style configuration, so dark mode switches to
        'clam', which honours it fully. Light mode goes back to the native
        theme so the app still looks like a Windows app by default.
        """
        pal = PALETTES[theme["name"]]
        dark = theme["name"] == "dark"

        try:
            style.theme_use("clam" if dark else native_theme)
        except tk.TclError:
            pass

        root.configure(background=pal["bg"])
        for wname in ("TFrame", "TLabelframe", "TPanedWindow"):
            style.configure(wname, background=pal["bg"])
        style.configure("TLabelframe.Label", background=pal["bg"],
                        foreground=pal["fg"])
        style.configure("TLabel", background=pal["bg"], foreground=pal["fg"])
        style.configure("TCheckbutton", background=pal["bg"],
                        foreground=pal["fg"])
        style.map("TCheckbutton", background=[("active", pal["bg"])])
        if dark:
            style.configure("TButton", background=pal["head"],
                            foreground=pal["fg"], bordercolor=pal["border"])
            style.map("TButton",
                      background=[("active", pal["sel"]),
                                  ("disabled", pal["bg"])],
                      foreground=[("disabled", pal["border"])])
            style.configure("TEntry", fieldbackground=pal["field"],
                            foreground=pal["field_fg"],
                            insertcolor=pal["field_fg"])
            style.configure("TCheckbutton", indicatorcolor=pal["field"])

        style.configure("Treeview", background=pal["field"],
                        fieldbackground=pal["field"],
                        foreground=pal["field_fg"], bordercolor=pal["border"])
        style.map("Treeview",
                  background=[("selected", pal["sel"])],
                  foreground=[("selected", pal["sel_fg"])])
        style.configure("Treeview.Heading", background=pal["head"],
                        foreground=pal["fg"])
        style.map("Treeview.Heading", background=[("active", pal["sel"])])

        for t in text_panes:
            t.configure(background=pal["field"], foreground=pal["field_fg"],
                        insertbackground=pal["field_fg"])
            t.tag_configure("stale", foreground=pal["stale"])
            t.tag_configure("due", foreground=pal["due"])
            t.tag_configure("new", foreground=pal["new"])
            t.tag_configure("hdr", foreground=pal["hdr"])

        if not stripe["user_set"]:
            stripe["color"] = pal["stripe"]
        apply_tags()
        if redraw_after:
            redraw()

    def toggle_theme(*_) -> None:
        theme["name"] = "dark" if v_dark.get() else "light"
        apply_theme()

    def pick_stripe() -> None:
        from tkinter import colorchooser
        rgb, hexval = colorchooser.askcolor(
            color=stripe["color"], title="Alternating row shade", parent=root)
        if hexval:
            stripe["color"] = hexval
            stripe["user_set"] = True
            apply_tags()
            redraw()
            status.config(text=f"row shade set to {hexval}")

    shade_btn = ttk.Button(bar, text="Row shade", command=pick_stripe)
    shade_btn.pack(side="left", padx=(8, 0))

    v_stripe = tk.BooleanVar(value=stripe["on"])

    def toggle_stripe(*_):
        stripe["on"] = v_stripe.get()
        redraw()

    ttk.Checkbutton(bar, text="Shade", variable=v_stripe,
                    command=toggle_stripe).pack(side="left", padx=(4, 0))

    v_dark = tk.BooleanVar(value=(theme["name"] == "dark"))
    ttk.Checkbutton(bar, text="Dark", variable=v_dark,
                    command=toggle_theme).pack(side="left", padx=(4, 0))

    # Descending on OFF puts the largest offset first: runners who have passed
    # the station (+0.5) at the top, then those closest to arriving, with the
    # far-back tail at the bottom.
    sort_state = {"col": "off", "desc": True}

    def sort_by(col: str) -> None:
        if sort_state["col"] == col:
            sort_state["desc"] = not sort_state["desc"]
        else:
            sort_state["col"], sort_state["desc"] = col, False
        redraw()

    for key, label, _w, _a in COLS:
        tree.heading(key, text=label, anchor="w",
                     command=lambda k=key: sort_by(k))

    def natkey(s: str):
        """Sort bibs so 7 < 32 < 289, and S1/S3/BF group together after."""
        s = s or ""
        return (0, int(s), "") if s.isdigit() else (1, 0, s.upper())

    def sortkey(r: Runner, col: str, station: float):
        if col == "bib":
            return natkey(r.bib)
        if col == "name":
            return (r.name or "").upper()
        if col == "mile":
            return r.mile if r.mile is not None else -1e9
        if col == "off":
            return (r.mile - station) if r.mile is not None else -1e9
        if col == "pace":
            return r.mph if r.mph is not None else -1.0
        if col == "eta":
            e = eta_hours(r, station)
            return e if e is not None else -1e9
        if col in ("ping", "age"):
            # Both order by recency; age is just the same axis inverted.
            m = r.stale_min
            return m if m is not None else 1e9
        return 0

    def fill_tree(hits: list[Runner], station: float,
                  new_slugs: set[str]) -> None:
        col = sort_state["col"]
        # Stable secondary sort on distance-to-station keeps ties in a
        # deterministic order across refreshes.
        rows = sorted(hits, key=lambda r: (r.mile if r.mile is not None else 0))
        rows.sort(key=lambda r: sortkey(r, col, station),
                  reverse=sort_state["desc"])

        sel = set(tree.selection())
        yview = tree.yview()
        tree.delete(*tree.get_children())

        for idx, r in enumerate(rows):
            delta = (r.mile - station) if r.mile is not None else 0.0
            e = eta_hours(r, station)
            eta_s = "past" if e is None else ("DUE NOW" if e < 0
                                              else f"{e:.1f}h out")
            sm = r.stale_min
            stale = sm is not None and sm > STALE_MIN
            age = r.last_age.strip()
            if stale and tags_broken:
                age += " !"
            # Stripe first so a status tag applied after it wins on foreground.
            tags = []
            if stripe["on"] and idx % 2:
                tags.append("stripe")
            if stale:
                tags.append("stale")
            elif e is not None and e < 0:
                tags.append("due")
            elif r.slug in new_slugs:
                tags.append("new")
            tree.insert("", "end", iid=r.slug, tags=tags, values=(
                r.bib or "?", " ".join((r.name or "").split()),
                f"{r.mile:.1f}" if r.mile is not None else "-",
                f"{delta:+.1f}", f"{(r.mph or 0):.1f}",
                eta_s, r.last_clock, age))

        for key, label, _w, _a in COLS:
            mark = ""
            if key == col:
                mark = "  v" if sort_state["desc"] else "  ^"
            tree.heading(key, text=label + mark)

        keep = [s for s in sel if tree.exists(s)]
        if keep:
            tree.selection_set(keep)
        tree.yview_moveto(yview[0])

    def copy_rows(_evt=None) -> None:
        """Ctrl-C on a Treeview copies nothing by default; make it TSV."""
        rows = tree.selection() or tree.get_children()
        lines = ["\t".join(c[1] for c in COLS)]
        lines += ["\t".join(str(v) for v in tree.item(i, "values"))
                  for i in rows]
        root.clipboard_clear()
        root.clipboard_append("\n".join(lines))
        status.config(text=f"copied {len(rows)} row(s) as TSV")

    tree.bind("<Control-c>", copy_rows)

    status = ttk.Label(root, anchor="w", padding=(10, 4))
    status.pack(side="bottom", fill="x")

    def paint(widget, text: str) -> None:
        """Replace contents, preserving scroll position and tagging rows."""
        widget.configure(state="normal")
        yview = widget.yview()
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        for i, line in enumerate(text.split("\n"), start=1):
            if "STALE" in line:
                widget.tag_add("stale", f"{i}.0", f"{i}.end")
            elif "DUE NOW" in line or line.startswith("DUE "):
                widget.tag_add("due", f"{i}.0", f"{i}.end")
            elif line.startswith(">"):
                widget.tag_add("new", f"{i}.0", f"{i}.end")
            elif line.startswith(("  BIB", "FCST", "window", "ARRIVAL", "Mile")):
                widget.tag_add("hdr", f"{i}.0", f"{i}.end")
        widget.configure(state="disabled")
        widget.yview_moveto(yview[0])

    def params():
        """Read the toolbar, falling back to the CLI value on bad input."""
        def num(var, default, cast=float):
            try:
                return cast(var.get())
            except (ValueError, tk.TclError):
                return default
        return dict(
            station=num(v_station, args.station),
            back=num(v_back, args.back),
            past=num(v_past, args.past),
            hours=num(v_hours, args.forecast_hours),
            bin=max(1, num(v_bin, args.bin, int)),
        )

    seen: set[str] = set()

    def redraw() -> None:
        p = params()
        runners = list(cache.runners.values())
        hits = in_window(runners, p["station"], p["back"], p["past"], args.all)
        nonlocal_new = {r.slug for r in hits} - seen if seen else set()

        if v_hf.get():
            fc = render_hf_forecast(hits, p["station"], p["hours"], p["bin"],
                                    args.hf_wrap)
        else:
            fc = render_forecast(hits, p["station"], p["hours"], p["bin"])
        paint(t_fcst, fc)
        fill_tree(hits, p["station"], nonlocal_new)
        seen.clear()
        seen.update(r.slug for r in hits)

        stale = sum(1 for r in hits
                    if (sm := r.stale_min) is not None and sm > STALE_MIN)
        bits = [f"{len(hits)} in window", f"{len(runners)} cached",
                f"{stale} stale"]
        if state["last_ok"]:
            bits.append(f"updated {state['last_ok'].astimezone(EVENT_TZ):%H:%M:%S}")
        if state["err"]:
            bits.append(f"LAST FETCH FAILED: {state['err']}")
        status.config(text="   |   ".join(bits))

    # -- fetching ---------------------------------------------------------
    def worker() -> None:
        try:
            runners = fetch(args.event, args.concurrency, cache,
                            args.min_age, args.retry_after,
                            args.timeout, args.retries, not args.http1)
            results.put(("ok", runners))
        except Exception as e:            # keep the window alive on any failure
            results.put(("err", e))

    def kick() -> None:
        if state["busy"]:
            return
        state["busy"] = True
        btn.config(text="Refreshing...", state="disabled")
        paint_countdown()
        threading.Thread(target=worker, daemon=True).start()

    def paint_countdown() -> None:
        if state["busy"]:
            countdown.config(text="refreshing now...")
        elif not v_auto.get():
            countdown.config(text="auto refresh off")
        else:
            nxt = state["next_at"]
            if nxt is None:
                countdown.config(text="refresh pending")
            else:
                secs = int((nxt - dt.datetime.now(dt.timezone.utc))
                           .total_seconds())
                if secs <= 0:
                    countdown.config(text="refresh due")
                else:
                    m, s = divmod(secs, 60)
                    countdown.config(text=f"refresh in {m:d}:{s:02d}")

    def tick() -> None:
        """Drive the countdown at 1 Hz, independent of the fetch loop.

        Separate timer from drain() so the display stays live while a fetch is
        in flight. Painting is split out so state changes (auto toggled,
        interval edited, refresh started) can repaint immediately rather than
        leaving a stale value on screen for up to a second.
        """
        paint_countdown()
        root.after(1000, tick)

    def drain() -> None:
        try:
            while True:
                kind, payload = results.get_nowait()
                state["busy"] = False
                btn.config(text="Refresh now", state="normal")
                if kind == "ok":
                    state["last_ok"] = dt.datetime.now(dt.timezone.utc)
                    state["err"] = None
                else:
                    state["err"] = str(payload)[:80]
                    LOG.warning("fetch failed: %s", payload)
                redraw()
                if v_auto.get():
                    try:
                        mins = max(0.5, float(v_every.get()))
                    except ValueError:
                        mins = 10.0
                    state["next_at"] = (dt.datetime.now(dt.timezone.utc)
                                        + dt.timedelta(minutes=mins))
        except queue.Empty:
            pass

        if v_auto.get() and not state["busy"]:
            nxt = state["next_at"]
            if nxt is None or dt.datetime.now(dt.timezone.utc) >= nxt:
                kick()
        root.after(500, drain)

    def copy_forecast() -> None:
        root.clipboard_clear()
        root.clipboard_append(t_fcst.get("1.0", "end-1c"))
        status.config(text="forecast copied to clipboard")

    btn.config(command=kick)
    copy_btn.config(command=copy_forecast)

    # Repaint on control changes without re-fetching. All the forecast inputs
    # are derived from cached data, so changing the horizon or bin size is
    # instant and costs no network -- important on a metered link.
    for var in (v_station, v_back, v_past, v_hours, v_bin):
        var.trace_add("write", lambda *_: redraw())
    v_hf.trace_add("write", lambda *_: redraw())

    root.bind("<F5>", lambda e: kick())
    root.bind("<Control-r>", lambda e: kick())
    root.bind("<Control-q>", lambda e: root.destroy())

    def reschedule(*_) -> None:
        """Re-anchor the countdown when the operator changes the interval."""
        if not v_auto.get():
            state["next_at"] = None
            paint_countdown()
            return
        try:
            mins = max(0.5, float(v_every.get()))
        except ValueError:
            return
        base = state["last_ok"] or dt.datetime.now(dt.timezone.utc)
        state["next_at"] = base + dt.timedelta(minutes=mins)
        paint_countdown()

    v_every.trace_add("write", reschedule)
    v_auto.trace_add("write", reschedule)

    apply_theme(redraw_after=False)
    redraw()          # show whatever the cache already holds, immediately
    kick()
    root.after(500, drain)
    tick()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    stop.set()
    cache.save()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--event", required=True, help="e.g. bigfoot200-26")
    p.add_argument("--station", type=float, required=True,
                   help="your aid station's course mile")
    p.add_argument("--back", type=float, default=6.0,
                   help="miles before the station to include (default 6)")
    p.add_argument("--past", type=float, default=0.0,
                   help="miles past the station to also include")
    p.add_argument("--all", action="store_true",
                   help="include finished/scratched, not just active")
    p.add_argument("--forecast", action="store_true",
                   help="show half-hour arrival counts for crew planning")
    p.add_argument("--hf-forecast", action="store_true",
                   help="emit the forecast in byte-minimized ASCII for HF "
                        "Winlink transport (~1/3 the bytes of the screen "
                        "format). Use this over VARA HF; send the full-width "
                        "version over Starlink or P2P VARA FM instead.")
    p.add_argument("--hf-wrap", type=int, default=0, metavar="COLS",
                   help="soft-wrap long bib lists at COLS with '+' "
                        "continuation lines (0 = no wrap, default)")
    p.add_argument("--bytes", action="store_true",
                   help="report payload size on stderr before transmit")
    p.add_argument("--forecast-hours", type=float, default=6.0,
                   help="forecast horizon in hours (default 6)")
    p.add_argument("--bin", type=int, default=30, metavar="MIN",
                   help="forecast bin size in minutes (default 30)")
    p.add_argument("--gui", action="store_true",
                   help="open a two-pane window (forecast left, runner detail "
                        "right) instead of printing to the console")
    p.add_argument("--dark", action="store_true",
                   help="start the GUI in dark mode (toggleable at runtime "
                        "with the Dark checkbox)")
    p.add_argument("--stripe-color", default=None, metavar="HEX",
                   help="alternating row shade in the GUI detail table "
                        "(default light gray, or a dark shade in dark mode). "
                        "Changeable at runtime via the Row shade button.")
    p.add_argument("--no-stripe", action="store_true",
                   help="disable alternating row shading")
    p.add_argument("--font-size", type=int, default=10, metavar="PT",
                   help="GUI monospace font size (default 10)")
    p.add_argument("--detail", action="store_true",
                   help="show the per-runner table. Implied when no forecast "
                        "mode is selected; otherwise the forecast alone is "
                        "printed.")
    p.add_argument("--min-age", type=float, default=10.0, metavar="MIN",
                   help="don't re-request a runner whose last fix is younger "
                        "than MIN minutes (default 10; SPOT can't have sent "
                        "a new one yet)")
    p.add_argument("--retry-after", type=float, default=2.0, metavar="MIN",
                   help="minimum gap between requests for the same runner "
                        "(default 2)")
    p.add_argument("--cache", default="tlwatch-cache.json", metavar="FILE",
                   help="persistent cache file (default tlwatch-cache.json)")
    p.add_argument("--no-cache", action="store_true",
                   help="keep cache in memory only; don't write to disk")
    p.add_argument("--refresh", type=float, metavar="MINUTES",
                   help="re-poll and redraw every MINUTES (omit for one shot)")
    p.add_argument("--clear", action="store_true",
                   help="clear the screen between refreshes")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                   metavar="SEC",
                   help="per-request timeout in seconds (default "
                        f"{DEFAULT_TIMEOUT_S:g}; raised well above "
                        "terrestrial norms for satellite RTT)")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES,
                   metavar="N",
                   help="retry attempts per request on timeout/5xx/429 "
                        f"before giving up (default {DEFAULT_RETRIES})")
    p.add_argument("--http1", action="store_true",
                   help="force HTTP/1.1 instead of HTTP/2 -- use if a "
                        "satellite proxy/PEP breaks HTTP/2")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")

    cache = Cache(None if args.no_cache else Path(args.cache), args.event)

    if args.gui:
        return run_gui(args, cache)

    def cycle(seen: set[str]) -> set[str]:
        runners = fetch(args.event, args.concurrency, cache,
                        args.min_age, args.retry_after,
                        args.timeout, args.retries, not args.http1)
        hits = in_window(runners, args.station, args.back, args.past, args.all)
        current = {r.slug for r in hits}
        # The table is the whole point when no forecast was asked for, so it
        # stays the default in that case. Once a forecast is requested the
        # table becomes opt-in -- 77 rows scrolling the forecast off screen
        # every refresh is not what you want on a shift.
        show_table = args.detail or not (args.forecast or args.hf_forecast)
        if show_table:
            print(render(hits, args.station, args.back, args.past,
                         new=current - seen if seen else set(),
                         stamp=args.refresh is not None), flush=True)
        if args.forecast:
            print(render_forecast(hits, args.station, args.forecast_hours,
                                  args.bin), flush=True)
        if args.hf_forecast:
            payload = render_hf_forecast(hits, args.station,
                                         args.forecast_hours, args.bin,
                                         args.hf_wrap)
            sys.stdout.write(payload)
            sys.stdout.flush()
            if args.bytes:
                report_size(payload)
        for slug in seen - current:
            if r := cache.runners.get(slug):
                print(f"  (cleared: #{r.bib or '?'} {r.name} "
                      f"now at mi {r.mile:.1f})",
                      flush=True)
        return current

    if args.refresh is None:
        cycle(set())
        return 0

    seen: set[str] = set()
    first = True
    try:
        while True:
            if args.clear and not first:
                print("\033[2J\033[H", end="")
            seen = cycle(seen)
            first = False
            print(f"  next refresh in {args.refresh:g} min — Ctrl-C to stop",
                  flush=True)
            time.sleep(args.refresh * 60)
    except KeyboardInterrupt:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
