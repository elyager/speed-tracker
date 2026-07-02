# Speed Tracker

A dependency-free macOS network speed and health tracker. It records low-impact
continuous health samples plus hourly complete speed and diagnostic runs in a
private local SQLite database. A sanitized static snapshot is published to
GitHub Pages at <https://net.noventayocho.work>.

## Use

```sh
python3 speed_tracker.py collect
python3 speed_tracker.py monitor
python3 speed_tracker.py status
python3 speed_tracker.py serve
```

Open <http://127.0.0.1:8765> after starting the server.

Install low-impact monitoring, hourly collection, GitHub Pages publishing, and
the persistent local dashboard:

```sh
python3 speed_tracker.py install
```

The monitor LaunchAgent runs continuously and samples:

- router ping and internet ping every 15 seconds
- IPv6 ping every 60 seconds when an IPv6 default route exists
- DNS, HTTPS/TLS timings, and route state every 5 minutes

The collection LaunchAgent runs a complete test every 60 minutes by default and
once when loaded. The frequency can be changed from 15 to 1440 minutes in the
local dashboard. Each completed run updates `site/data.json`, commits it, and
pushes it to the private GitHub repository. GitHub Actions then deploys the
static dashboard. Full speed tests consume data and can affect active network
traffic, so use shorter intervals carefully.

## Speed providers

Hourly collection uses Apple `networkQuality` in sequential mode for download
and upload, plus responsiveness, idle latency, and loaded latency. If it cannot
produce a speed result, the run is failed.

Remove all LaunchAgents while preserving history:

```sh
python3 speed_tracker.py uninstall
```

## Data and logs

- Database: `~/Library/Application Support/Speed Tracker/network_health.sqlite`
- Logs: `~/Library/Logs/SpeedTracker/`
- LaunchAgents: `~/Library/LaunchAgents/com.local.speed-tracker.*.plist`

The application does not collect public IP addresses or Wi-Fi SSIDs. The
local dashboard accepts connections only from this Mac. The public snapshot
also excludes interface names, gateway and resolver IPs, route signatures, raw
traceroute IPs, proxy/VPN hints, and raw diagnostic errors. GitHub Pages itself
is public even though the source repository is private.

## Validation

```sh
python3 -m unittest discover -v
```
