# Speed Tracker

A dependency-free macOS network speed and health tracker. It records full
`networkQuality` results plus packet loss, DNS, and HTTPS probes in a private
local SQLite database. A sanitized static snapshot is published to GitHub Pages
at <https://net.noventayocho.work>.

## Use

```sh
python3 speed_tracker.py collect
python3 speed_tracker.py status
python3 speed_tracker.py serve
```

Open <http://127.0.0.1:8765> after starting the server.

Install hourly collection, GitHub Pages publishing, and the persistent local
dashboard:

```sh
python3 speed_tracker.py install
```

The collection LaunchAgent runs at minute `00` each hour and once when loaded.
If the Mac is asleep, macOS coalesces missed calendar events into one run after
wake. Each completed run updates `site/data.json`, commits it, and pushes it to
the private GitHub repository. GitHub Actions then deploys the static dashboard.
Full speed tests consume data and can affect active network traffic.

Remove both LaunchAgents while preserving all history:

```sh
python3 speed_tracker.py uninstall
```

## Data and logs

- Database: `~/Library/Application Support/Speed Tracker/network_health.sqlite`
- Logs: `~/Library/Logs/SpeedTracker/`
- LaunchAgents: `~/Library/LaunchAgents/com.local.speed-tracker.*.plist`

The application does not collect public IP addresses or Wi-Fi SSIDs. The
local dashboard accepts connections only from this Mac. The public snapshot
also excludes interface names and raw diagnostic errors. GitHub Pages itself is
public even though the source repository is private.

## Validation

```sh
python3 -m unittest discover -v
```
