# tlwatch

Who is approaching my aid station?

Trackleaders already projects every runner's SPOT fix onto the course and
publishes it as "Route mile". `tlwatch` polls that feed for you, lists
everyone within a stretch of miles before your aid station (closest to
arrive first), and can project when each of them will roll in.

Built around the [Bigfoot 200](https://www.bigfootultra.com/) but works for
any event Trackleaders tracks.

## Quick start

```bash
# everyone in the 6 miles before mile 171
./tlwatch.py --event bigfoot200-26 --station 171 --back 6

# keep refreshing every 5 minutes, flag new arrivals into the window
./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --refresh 5

# also show runners up to 2 miles past the station (just departed)
./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --past 2

# crew-planning forecast: half-hour arrival bins for the next 6 hours
./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --forecast

# two-pane GUI dashboard (forecast + sortable runner table)
./tlwatch.py --event bigfoot200-26 --station 171 --back 6 --gui
```

`--event` is the Trackleaders event slug, taken from the tracker URL
(`trackleaders.com/<event>f.php`) -- e.g. `bigfoot200-26`.

## Features

- **Windowed roster** -- lists runners whose projected course mile falls in
  `[station - back, station + past]`, closest first.
- **Arrival forecast** -- buckets projected arrivals into configurable
  time bins for staffing an aid station.
- **HF/Winlink mode** (`--hf-forecast`) -- a byte-minimized ASCII forecast
  format for sending over VARA HF Winlink when there's no internet at the
  station, plus a `--bytes` flag to estimate airtime before you transmit.
- **Persistent cache** -- only re-fetches a runner once they're actually due
  for a new SPOT fix, and survives a laptop reboot mid-shift.
- **`--refresh MINUTES`** -- polls on an interval and flags runners newly
  arrived in the window since the last cycle.
- **Optional GUI** (`--gui`) -- a two-pane Tk dashboard (forecast on the
  left, sortable runner detail on the right) with light/dark themes, for
  leaving up on a laptop at the aid station.
- **Tuned for satellite backhaul** -- pooled, keep-alive HTTP/2 connections
  (via `httpx`) instead of one handshake per request, generous timeouts,
  and jittered retry/backoff, so a high-latency, occasionally lossy link
  (Starlink, BGAN, geostationary VSAT) doesn't get mistaken for a dead one.
  See `--timeout`, `--retries`, and `--http1` if a satellite proxy/PEP on
  your link needs different handling.

Run `./tlwatch.py --help` for the full option list.

## Requirements

- Python 3.11+ and [`httpx`](https://www.python-httpx.org/) with its
  HTTP/2 extra: `pip install -r requirements.txt` (or
  `pip install "httpx[http2]"`). Not needed if you're running the
  [standalone `.exe`](#standalone-windows-executable) -- it's bundled in.
- `--gui` additionally needs `tkinter`, which ships with the python.org
  Windows/macOS installers but is sometimes missing from the Microsoft
  Store build of Python (`sudo apt install python3-tk` on Debian/Ubuntu,
  `brew install python-tk` on macOS).
- On Windows, if the system has no timezone database, `pip install tzdata`
  gets you correct event-local clock display; otherwise the tool falls
  back to the machine's local time.

## Standalone Windows executable

Every tagged release is built into a single-file Windows `.exe` by the
[`build-windows.yml`](.github/workflows/build-windows.yml) GitHub Actions
workflow. It bundles Python, `httpx`, and every dependency `httpx` needs
(including certifi's CA bundle) directly into the executable, so it's fully
self-contained -- no Python install and no separate downloads at the point
of use. Grab it from the [Releases](../../releases) page, or trigger the
workflow manually from the Actions tab to get a build artifact from any
branch.

## License

Apache License 2.0 -- see [LICENSE](LICENSE).
