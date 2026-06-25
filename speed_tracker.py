#!/usr/bin/env python3
"""Local network speed and health tracker for macOS."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


APP_DIR = Path.home() / "Library" / "Application Support" / "Speed Tracker"
DB_PATH = APP_DIR / "network_health.sqlite"
LOCK_PATH = APP_DIR / "collect.lock"
LOG_DIR = Path.home() / "Library" / "Logs" / "SpeedTracker"
AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
COLLECT_LABEL = "com.local.speed-tracker.collect"
SERVER_LABEL = "com.local.speed-tracker.serve"
HOST = "127.0.0.1"
PORT = 8765
SITE_DIR = Path(__file__).resolve().parent / "site"
PUBLIC_DOMAIN = "net.noventayocho.work"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('healthy', 'degraded', 'failed')),
    download_mbps REAL,
    upload_mbps REAL,
    idle_latency_ms REAL,
    loaded_latency_ms REAL,
    responsiveness_rpm REAL,
    packet_loss_percent REAL,
    ping_latency_ms REAL,
    dns_time_ms REAL,
    dns_ok INTEGER NOT NULL,
    https_time_ms REAL,
    https_ok INTEGER NOT NULL,
    interface_name TEXT,
    degraded_reasons TEXT NOT NULL,
    errors TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
"""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect_db(path=DB_PATH):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def run_command(args, timeout):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def parse_network_quality(output):
    data = json.loads(output)
    rpm = number(data.get("responsiveness"))
    return {
        "download_mbps": bits_to_mbps(data.get("dl_throughput")),
        "upload_mbps": bits_to_mbps(data.get("ul_throughput")),
        "idle_latency_ms": number(data.get("base_rtt")),
        "responsiveness_rpm": rpm,
        "loaded_latency_ms": (60000.0 / rpm) if rpm and rpm > 0 else None,
        "interface_name": clean_interface(data.get("interface_name")),
        "network_quality_error": (
            f"{data.get('error_domain')}: {data.get('error_code')}"
            if data.get("error_domain")
            else None
        ),
    }


def parse_ping(output):
    loss = re.search(r"([\d.]+)% packet loss", output)
    latency = re.search(
        r"(?:round-trip|rtt) min/avg/max/(?:stddev|mdev) = "
        r"[\d.]+/([\d.]+)/",
        output,
    )
    return {
        "packet_loss_percent": float(loss.group(1)) if loss else None,
        "ping_latency_ms": float(latency.group(1)) if latency else None,
    }


def parse_dns(output):
    match = re.search(r"Query time:\s*(\d+)\s*msec", output)
    return float(match.group(1)) if match else None


def parse_curl(output):
    parts = output.strip().split()
    if len(parts) != 2:
        return None, False
    try:
        elapsed = float(parts[0]) * 1000
        status_code = int(parts[1])
    except ValueError:
        return None, False
    return elapsed, 200 <= status_code < 400


def number(value):
    return float(value) if isinstance(value, (int, float)) else None


def bits_to_mbps(value):
    value = number(value)
    return value / 1_000_000 if value is not None else None


def clean_interface(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", value):
        return value
    return None


def classify(result):
    reasons = []
    if result.get("packet_loss_percent") is None:
        reasons.append("Packet-loss probe failed")
    elif result["packet_loss_percent"] > 1:
        reasons.append(f"Packet loss is {result['packet_loss_percent']:.1f}%")
    if result.get("idle_latency_ms") is not None and result["idle_latency_ms"] > 100:
        reasons.append(f"Idle latency is {result['idle_latency_ms']:.0f} ms")
    if (
        result.get("loaded_latency_ms") is not None
        and result["loaded_latency_ms"] > 250
    ):
        reasons.append(f"Loaded latency is {result['loaded_latency_ms']:.0f} ms")
    if not result.get("dns_ok"):
        reasons.append("DNS lookup failed")
    if not result.get("https_ok"):
        reasons.append("HTTPS request failed")

    has_speed_result = (
        result.get("download_mbps") is not None
        or result.get("upload_mbps") is not None
    )
    if not has_speed_result:
        reasons.insert(0, "Network speed test failed")
        return "failed", reasons
    return ("degraded" if reasons else "healthy"), reasons


def collect_measurements(runner=run_command):
    started_at = utc_now()
    started_monotonic = time.monotonic()
    result = {
        "download_mbps": None,
        "upload_mbps": None,
        "idle_latency_ms": None,
        "loaded_latency_ms": None,
        "responsiveness_rpm": None,
        "packet_loss_percent": None,
        "ping_latency_ms": None,
        "dns_time_ms": None,
        "dns_ok": False,
        "https_time_ms": None,
        "https_ok": False,
        "interface_name": None,
    }
    errors = []

    try:
        completed = runner(["/usr/bin/networkQuality", "-c", "-M", "120"], 130)
        if completed.stdout.strip():
            result.update(parse_network_quality(completed.stdout))
        network_quality_error = result.pop("network_quality_error", None)
        if completed.returncode or network_quality_error:
            errors.append(
                completed.stderr.strip()
                or network_quality_error
                or f"networkQuality exited {completed.returncode}"
            )
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"networkQuality: {exc}")

    try:
        completed = runner(["/sbin/ping", "-c", "5", "-W", "1000", "1.1.1.1"], 12)
        result.update(parse_ping(completed.stdout + completed.stderr))
        if completed.returncode:
            errors.append(f"ping exited {completed.returncode}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"ping: {exc}")

    try:
        completed = runner(
            ["/usr/bin/dig", "+stats", "+tries=1", "+time=5", "example.com"], 8
        )
        result["dns_time_ms"] = parse_dns(completed.stdout)
        result["dns_ok"] = completed.returncode == 0 and result["dns_time_ms"] is not None
        if not result["dns_ok"]:
            errors.append(f"dig exited {completed.returncode}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"dig: {exc}")

    try:
        completed = runner(
            [
                "/usr/bin/curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--max-time",
                "10",
                "--write-out",
                "%{time_total} %{http_code}",
                "https://example.com/",
            ],
            12,
        )
        result["https_time_ms"], result["https_ok"] = parse_curl(completed.stdout)
        if completed.returncode or not result["https_ok"]:
            errors.append(completed.stderr.strip() or f"curl exited {completed.returncode}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"curl: {exc}")

    result["started_at"] = started_at
    result["completed_at"] = utc_now()
    result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
    result["status"], reasons = classify(result)
    result["degraded_reasons"] = reasons
    result["errors"] = [error for error in errors if error]
    return result


def insert_run(connection, result):
    columns = [
        "started_at", "completed_at", "duration_seconds", "status",
        "download_mbps", "upload_mbps", "idle_latency_ms", "loaded_latency_ms",
        "responsiveness_rpm", "packet_loss_percent", "ping_latency_ms",
        "dns_time_ms", "dns_ok", "https_time_ms", "https_ok", "interface_name",
        "degraded_reasons", "errors",
    ]
    values = []
    for column in columns:
        value = result.get(column)
        if column in ("degraded_reasons", "errors"):
            value = json.dumps(value, separators=(",", ":"))
        if column in ("dns_ok", "https_ok"):
            value = int(bool(value))
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO runs ({','.join(columns)}) VALUES ({placeholders})", values
    )
    connection.commit()
    return cursor.lastrowid


def row_to_dict(row):
    data = dict(row)
    data["dns_ok"] = bool(data["dns_ok"])
    data["https_ok"] = bool(data["https_ok"])
    data["degraded_reasons"] = json.loads(data["degraded_reasons"])
    data["errors"] = json.loads(data["errors"])
    return data


def latest_run(connection):
    row = connection.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return row_to_dict(row) if row else None


def history(connection, range_name="24h", limit=1000):
    ranges = {"24h": "-24 hours", "7d": "-7 days", "30d": "-30 days"}
    parameters = []
    where = ""
    if range_name in ranges:
        where = "WHERE datetime(started_at) >= datetime('now', ?)"
        parameters.append(ranges[range_name])
    elif range_name != "all":
        raise ValueError("range must be 24h, 7d, 30d, or all")
    parameters.append(max(1, min(int(limit), 10000)))
    rows = connection.execute(
        f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", parameters
    ).fetchall()
    return [row_to_dict(row) for row in rows]


@contextlib.contextmanager
def collection_lock(path=LOCK_PATH):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("another collection is already running")
    try:
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


def summary(connection):
    latest = latest_run(connection)
    counts = connection.execute(
        """
        SELECT status, COUNT(*) AS count FROM runs
        WHERE datetime(started_at) >= datetime('now', '-24 hours')
        GROUP BY status
        """
    ).fetchall()
    return {
        "latest": latest,
        "last_24_hours": {row["status"]: row["count"] for row in counts},
    }


PUBLIC_FIELDS = (
    "started_at",
    "completed_at",
    "duration_seconds",
    "status",
    "download_mbps",
    "upload_mbps",
    "idle_latency_ms",
    "loaded_latency_ms",
    "responsiveness_rpm",
    "packet_loss_percent",
    "ping_latency_ms",
    "dns_time_ms",
    "dns_ok",
    "https_time_ms",
    "https_ok",
    "degraded_reasons",
)


def public_run(run):
    return {field: run.get(field) for field in PUBLIC_FIELDS}


def build_static_site(connection, output_dir=SITE_DIR):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = connection.execute(
        "SELECT * FROM runs ORDER BY started_at DESC"
    ).fetchall()
    payload = {
        "generated_at": utc_now(),
        "runs": [public_run(row_to_dict(row)) for row in rows],
    }
    files = {
        "index.html": DASHBOARD,
        "data.json": json.dumps(payload, separators=(",", ":")),
        "CNAME": PUBLIC_DOMAIN + "\n",
        ".nojekyll": "",
    }
    for name, content in files.items():
        target = output_dir / name
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
    return payload


def publish_static_site(repository_dir=None):
    repository_dir = Path(repository_dir or __file__).resolve().parent
    paths = ["site/index.html", "site/data.json", "site/CNAME", "site/.nojekyll"]
    add = subprocess.run(
        ["/usr/bin/git", "add", "--", *paths],
        cwd=repository_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode:
        raise RuntimeError(add.stderr.strip() or "git add failed")
    changed = subprocess.run(
        ["/usr/bin/git", "diff", "--cached", "--quiet"],
        cwd=repository_dir,
        check=False,
    )
    if changed.returncode == 0:
        return False
    if changed.returncode != 1:
        raise RuntimeError("could not inspect staged dashboard changes")
    message = "Update network measurements " + dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    commit = subprocess.run(
        ["/usr/bin/git", "commit", "-m", message],
        cwd=repository_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode:
        raise RuntimeError(commit.stderr.strip() or "git commit failed")
    push = subprocess.run(
        ["/usr/bin/git", "push", "origin", "HEAD"],
        cwd=repository_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if push.returncode:
        raise RuntimeError(push.stderr.strip() or "git push failed")
    return True


DASHBOARD = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Speed Tracker</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--card:#121c2f;--muted:#93a4bd;--line:#273854;--good:#42d392;--warn:#f5b942;--bad:#ff6b6b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf3fb;font:15px system-ui,-apple-system,sans-serif}
main{max-width:1180px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;gap:16px;align-items:center}
h1{margin:0;font-size:26px}.range button{background:transparent;color:var(--muted);border:1px solid var(--line);padding:7px 11px}
.range button:first-child{border-radius:8px 0 0 8px}.range button:last-child{border-radius:0 8px 8px 0}.range .active{background:#253b60;color:white}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}.card,.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.label{color:var(--muted);font-size:12px;text-transform:uppercase}.value{font-size:26px;margin-top:5px}.status{font-weight:700}
.healthy{color:var(--good)}.degraded{color:var(--warn)}.failed{color:var(--bad)}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}.panel h2{font-size:15px;margin:0 0 10px}canvas{width:100%;height:220px}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line)}th{color:var(--muted)}
#message{color:var(--muted);margin:12px 0}.wide{margin-top:12px;overflow:auto}
@media(max-width:800px){.cards,.charts{grid-template-columns:1fr 1fr}}@media(max-width:520px){main{padding:16px}.cards,.charts{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
</style></head><body><main>
<header><div><h1>Connection health</h1><div id="message">Loading…</div></div>
<div class="range"><button data-range="24h" class="active">24h</button><button data-range="7d">7d</button><button data-range="30d">30d</button><button data-range="all">All</button></div></header>
<section class="cards">
<div class="card"><div class="label">Status</div><div id="status" class="value">—</div></div>
<div class="card"><div class="label">Download</div><div id="download" class="value">—</div></div>
<div class="card"><div class="label">Upload</div><div id="upload" class="value">—</div></div>
<div class="card"><div class="label">Idle / loaded latency</div><div id="latency" class="value">—</div></div>
</section>
<section class="charts">
<div class="panel"><h2>Download / upload (Mbps)</h2><canvas id="speed"></canvas></div>
<div class="panel"><h2>Idle / loaded latency (ms)</h2><canvas id="latencies"></canvas></div>
<div class="panel"><h2>Responsiveness (RPM)</h2><canvas id="responsiveness"></canvas></div>
<div class="panel"><h2>Packet loss / DNS / HTTPS (ms)</h2><canvas id="health"></canvas></div>
</section>
<section class="panel wide"><h2>Recent runs</h2><table><thead><tr><th>Time</th><th>Status</th><th>Down</th><th>Up</th><th>Loss</th><th>DNS</th><th>HTTPS</th><th>Details</th></tr></thead><tbody id="rows"></tbody></table></section>
</main><script>
let selected='24h'; const fmt=(v,d=1)=>v==null?'—':Number(v).toFixed(d);
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function draw(id,runs,series){const c=document.getElementById(id),dpr=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;const x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,w,h);x.strokeStyle='#273854';x.beginPath();for(let i=0;i<5;i++){let y=12+i*(h-30)/4;x.moveTo(35,y);x.lineTo(w-8,y)}x.stroke();let vals=series.flatMap(s=>runs.map(r=>r[s.key]).filter(v=>v!=null));let max=Math.max(...vals,1);series.forEach(s=>{x.strokeStyle=s.color;x.lineWidth=2;x.beginPath();let started=false;runs.forEach((r,i)=>{let v=r[s.key];if(v==null){started=false;return}let px=35+(w-45)*(runs.length<2?1:i/(runs.length-1)),py=12+(h-30)*(1-v/max);if(!started)x.moveTo(px,py);else x.lineTo(px,py);started=true});x.stroke()});x.fillStyle='#93a4bd';x.font='11px system-ui';x.fillText(max.toFixed(max<10?1:0),2,15);x.fillText('0',18,h-15);series.forEach((s,i)=>{x.fillStyle=s.color;x.fillText(s.name,42+i*95,h-5)})}
async function load(){let staticMode=!['127.0.0.1','localhost'].includes(location.hostname),runs,l;
if(staticMode){let payload=await (await fetch('./data.json',{cache:'no-store'})).json(),all=payload.runs||[],hours={'24h':24,'7d':168,'30d':720}[selected],cutoff=hours?Date.now()-hours*3600000:0;runs=all.filter(r=>new Date(r.started_at).getTime()>=cutoff).reverse();l=all[0]||null}
else{let [sumRes,runsRes]=await Promise.all([fetch('/api/summary'),fetch('/api/runs?range='+selected+'&limit=10000')]),sum=await sumRes.json();runs=(await runsRes.json()).runs.reverse();l=sum.latest}
if(!l){document.getElementById('message').textContent='No tests recorded yet. Run: python3 speed_tracker.py collect';return}
document.getElementById('message').textContent='Last test '+new Date(l.completed_at).toLocaleString()+' · '+fmt(l.duration_seconds)+' seconds';
let status=document.getElementById('status');status.textContent=l.status;status.className='value status '+l.status;
document.getElementById('download').textContent=fmt(l.download_mbps)+' Mbps';document.getElementById('upload').textContent=fmt(l.upload_mbps)+' Mbps';document.getElementById('latency').textContent=fmt(l.idle_latency_ms,0)+' / '+fmt(l.loaded_latency_ms,0)+' ms';
draw('speed',runs,[{key:'download_mbps',name:'Download',color:'#58a6ff'},{key:'upload_mbps',name:'Upload',color:'#42d392'}]);
draw('latencies',runs,[{key:'idle_latency_ms',name:'Idle',color:'#42d392'},{key:'loaded_latency_ms',name:'Loaded',color:'#f5b942'}]);
draw('responsiveness',runs,[{key:'responsiveness_rpm',name:'RPM',color:'#b392f0'}]);
draw('health',runs,[{key:'packet_loss_percent',name:'Loss %',color:'#ff6b6b'},{key:'dns_time_ms',name:'DNS',color:'#58a6ff'},{key:'https_time_ms',name:'HTTPS',color:'#f5b942'}]);
document.getElementById('rows').innerHTML=runs.slice().reverse().slice(0,100).map(r=>`<tr><td>${esc(new Date(r.started_at).toLocaleString())}</td><td class="${r.status}">${r.status}</td><td>${fmt(r.download_mbps)}</td><td>${fmt(r.upload_mbps)}</td><td>${fmt(r.packet_loss_percent)}%</td><td>${fmt(r.dns_time_ms,0)} ms</td><td>${fmt(r.https_time_ms,0)} ms</td><td>${esc([...(r.degraded_reasons||[]),...(r.errors||[])].join('; ')||'—')}</td></tr>`).join('');
}
document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>{selected=b.dataset.range;document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('active',x===b));load()});addEventListener('resize',()=>load());load().catch(e=>document.getElementById('message').textContent=e);
</script></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond(DASHBOARD.encode(), "text/html; charset=utf-8")
            return
        if parsed.path not in ("/api/summary", "/api/runs"):
            self.send_error(404)
            return
        try:
            with connect_db(self.db_path) as connection:
                if parsed.path == "/api/summary":
                    payload = summary(connection)
                else:
                    query = parse_qs(parsed.query)
                    payload = {
                        "runs": history(
                            connection,
                            query.get("range", ["24h"])[0],
                            query.get("limit", ["1000"])[0],
                        )
                    }
            self.respond(json.dumps(payload).encode(), "application/json")
        except (ValueError, sqlite3.Error) as exc:
            self.respond(json.dumps({"error": str(exc)}).encode(), "application/json", 400)

    def respond(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def agent_plists(script_path=None, python_path=None):
    script_path = str(Path(script_path or __file__).resolve())
    python_path = python_path or sys.executable
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    common = {
        "ProcessType": "Background",
        "WorkingDirectory": str(Path(script_path).parent),
    }
    collect = {
        **common,
        "Label": COLLECT_LABEL,
        "ProgramArguments": [python_path, script_path, "collect", "--publish"],
        "RunAtLoad": True,
        "StartCalendarInterval": {"Minute": 0},
        "StandardOutPath": str(LOG_DIR / "collect.log"),
        "StandardErrorPath": str(LOG_DIR / "collect-error.log"),
    }
    server = {
        **common,
        "Label": SERVER_LABEL,
        "ProgramArguments": [python_path, script_path, "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG_DIR / "server.log"),
        "StandardErrorPath": str(LOG_DIR / "server-error.log"),
    }
    return {COLLECT_LABEL: collect, SERVER_LABEL: server}


def write_agents():
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    created = []
    for label, data in agent_plists().items():
        path = AGENT_DIR / f"{label}.plist"
        with path.open("wb") as handle:
            plistlib.dump(data, handle, sort_keys=False)
        created.append(path)
    return created


def launchctl(*args):
    return subprocess.run(
        ["/bin/launchctl", *args], capture_output=True, text=True, check=False
    )


def install_agents():
    paths = write_agents()
    domain = f"gui/{os.getuid()}"
    for path in paths:
        launchctl("bootout", domain, str(path))
        result = launchctl("bootstrap", domain, str(path))
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"could not load {path.name}")
    return paths


def uninstall_agents():
    domain = f"gui/{os.getuid()}"
    removed = []
    for label in (COLLECT_LABEL, SERVER_LABEL):
        path = AGENT_DIR / f"{label}.plist"
        launchctl("bootout", domain, str(path))
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def print_result(result):
    print(json.dumps(result, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DB_PATH, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect_parser = subparsers.add_parser("collect", help="Run and save a network test")
    collect_parser.add_argument(
        "--publish",
        action="store_true",
        help="Build and push the sanitized GitHub Pages snapshot",
    )
    export_parser = subparsers.add_parser(
        "export", help="Build a static dashboard from saved tests"
    )
    export_parser.add_argument("--output", type=Path, default=SITE_DIR)
    subparsers.add_parser("status", help="Print the latest saved test")
    serve_parser = subparsers.add_parser("serve", help="Run the local dashboard")
    serve_parser.add_argument("--host", default=HOST, choices=[HOST])
    serve_parser.add_argument("--port", default=PORT, type=int)
    subparsers.add_parser("install", help="Install and load macOS LaunchAgents")
    subparsers.add_parser("uninstall", help="Unload and remove LaunchAgents")
    args = parser.parse_args(argv)

    if args.command == "collect":
        try:
            with collection_lock():
                result = collect_measurements()
                with connect_db(args.database) as connection:
                    result["id"] = insert_run(connection, result)
                    if args.publish:
                        build_static_site(connection)
                if args.publish:
                    result["published"] = publish_static_site()
                print_result(result)
                return 0 if result["status"] != "failed" else 1
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 75
    if args.command == "export":
        with connect_db(args.database) as connection:
            payload = build_static_site(connection, args.output)
        print(f"Exported {len(payload['runs'])} runs to {args.output}")
        return 0
    if args.command == "status":
        with connect_db(args.database) as connection:
            result = latest_run(connection)
        if not result:
            print("No tests recorded.", file=sys.stderr)
            return 1
        print_result(result)
        return 0
    if args.command == "serve":
        DashboardHandler.db_path = args.database
        server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
        print(f"Speed Tracker dashboard: http://{args.host}:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.command == "install":
        for path in install_agents():
            print(f"Installed {path}")
        print(f"Dashboard: http://{HOST}:{PORT}")
        return 0
    if args.command == "uninstall":
        for path in uninstall_agents():
            print(f"Removed {path}")
        print(f"History retained at {args.database.expanduser()}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
