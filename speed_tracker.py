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
import shlex
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
MONITOR_LABEL = "com.local.speed-tracker.monitor"
SERVER_LABEL = "com.local.speed-tracker.serve"
HOST = "127.0.0.1"
PORT = 8765
SITE_DIR = Path(__file__).resolve().parent / "site"
PUBLIC_DOMAIN = "net.noventayocho.work"

INTERNET_PING_HOST = "1.1.1.1"
IPV6_PING_HOST = "2606:4700:4700::1111"
DNS_HOST = "example.com"
HTTPS_URL = "https://example.com/"
FALLBACK_RESOLVER = "1.1.1.1"
MONITOR_INTERVAL_SECONDS = 15
MONITOR_WINDOW_SECONDS = 5 * 60

RUN_COLUMNS = {
    "router_ping_ok": "INTEGER",
    "internet_ping_ok": "INTEGER",
    "ipv6_ok": "INTEGER",
    "ipv4_route_ok": "INTEGER",
    "ipv6_route_ok": "INTEGER",
    "gateway_packet_loss_percent": "REAL",
    "latency_p50": "REAL",
    "latency_p95": "REAL",
    "latency_p99": "REAL",
    "jitter_ms": "REAL",
    "tcp_connect_ms": "REAL",
    "tls_handshake_ms": "REAL",
    "first_byte_ms": "REAL",
    "testmy_download_mbps": "REAL",
    "testmy_upload_mbps": "REAL",
    "networkquality_download_mbps": "REAL",
    "networkquality_upload_mbps": "REAL",
    "route_hop_count": "INTEGER",
    "route_changed": "INTEGER",
    "default_interface": "TEXT",
    "route_signature": "TEXT",
    "dns_fallback_time_ms": "REAL",
    "dns_fallback_ok": "INTEGER",
    "dns_resolver_count": "INTEGER",
    "proxy_vpn_hints": "TEXT NOT NULL DEFAULT '[]'",
    "traceroute_summary": "TEXT NOT NULL DEFAULT '[]'",
}

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
    router_ping_ok INTEGER,
    internet_ping_ok INTEGER,
    ipv6_ok INTEGER,
    ipv4_route_ok INTEGER,
    ipv6_route_ok INTEGER,
    gateway_packet_loss_percent REAL,
    latency_p50 REAL,
    latency_p95 REAL,
    latency_p99 REAL,
    jitter_ms REAL,
    tcp_connect_ms REAL,
    tls_handshake_ms REAL,
    first_byte_ms REAL,
    testmy_download_mbps REAL,
    testmy_upload_mbps REAL,
    networkquality_download_mbps REAL,
    networkquality_upload_mbps REAL,
    route_hop_count INTEGER,
    route_changed INTEGER,
    default_interface TEXT,
    route_signature TEXT,
    dns_fallback_time_ms REAL,
    dns_fallback_ok INTEGER,
    dns_resolver_count INTEGER,
    proxy_vpn_hints TEXT NOT NULL DEFAULT '[]',
    traceroute_summary TEXT NOT NULL DEFAULT '[]',
    degraded_reasons TEXT NOT NULL,
    errors TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at);
CREATE TABLE IF NOT EXISTS monitor_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sampled_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'router_ping', 'internet_ping', 'ipv6_ping', 'dns', 'https', 'route'
    )),
    ok INTEGER NOT NULL,
    latency_ms REAL,
    packet_loss_percent REAL,
    dns_time_ms REAL,
    tcp_connect_ms REAL,
    tls_handshake_ms REAL,
    first_byte_ms REAL,
    https_time_ms REAL,
    default_interface TEXT,
    route_signature TEXT,
    ipv4_route_ok INTEGER,
    ipv6_route_ok INTEGER,
    details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_monitor_samples_time
    ON monitor_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_monitor_samples_kind_time
    ON monitor_samples(kind, sampled_at);
"""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def connect_db(path=DB_PATH):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    migrate_database(connection)
    return connection


def migrate_database(connection):
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(runs)").fetchall()
    }
    for name, definition in RUN_COLUMNS.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {definition}")
    connection.execute(
        """
        UPDATE runs
        SET networkquality_download_mbps = download_mbps
        WHERE networkquality_download_mbps IS NULL AND download_mbps IS NOT NULL
        """
    )
    connection.execute(
        """
        UPDATE runs
        SET networkquality_upload_mbps = upload_mbps
        WHERE networkquality_upload_mbps IS NULL AND upload_mbps IS NOT NULL
        """
    )
    connection.commit()


def run_command(args, timeout):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def number(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def bits_to_mbps(value):
    value = number(value)
    return value / 1_000_000 if value is not None else None


def clean_interface(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,32}", value):
        return value
    return None


def clean_route_value(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:.%_-]{1,80}", value):
        return value
    return None


def safe_json_list(value):
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def safe_json_dict(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def bool_or_none(value):
    return None if value is None else bool(value)


def parse_network_quality(output):
    data = json.loads(output)
    rpm = number(data.get("responsiveness"))
    download = bits_to_mbps(data.get("dl_throughput"))
    upload = bits_to_mbps(data.get("ul_throughput"))
    return {
        "download_mbps": download,
        "upload_mbps": upload,
        "networkquality_download_mbps": download,
        "networkquality_upload_mbps": upload,
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
    latencies = [
        float(match)
        for match in re.findall(r"time[=<]([\d.]+)\s*ms", output)
    ]
    summary = re.search(
        r"(?:round-trip|rtt) min/avg/max/(?:stddev|mdev) = "
        r"[\d.]+/([\d.]+)/",
        output,
    )
    average = float(summary.group(1)) if summary else None
    if average is None and latencies:
        average = sum(latencies) / len(latencies)
    return {
        "packet_loss_percent": float(loss.group(1)) if loss else None,
        "ping_latency_ms": average,
        "latency_ms": average,
        "latencies_ms": latencies,
    }


def parse_dns(output):
    match = re.search(r"Query time:\s*(\d+)\s*msec", output)
    return float(match.group(1)) if match else None


def parse_curl(output):
    parsed = parse_curl_timings(output)
    return parsed["https_time_ms"], parsed["https_ok"]


def parse_curl_timings(output):
    parts = output.strip().split()
    if len(parts) == 2:
        try:
            elapsed = float(parts[0]) * 1000
            status_code = int(parts[1])
        except ValueError:
            return empty_curl_timings()
        return {
            **empty_curl_timings(),
            "https_time_ms": elapsed,
            "https_ok": 200 <= status_code < 400,
        }
    if len(parts) != 6:
        return empty_curl_timings()
    try:
        lookup, connect, appconnect, starttransfer, total = [
            float(value) * 1000 for value in parts[:5]
        ]
        status_code = int(parts[5])
    except ValueError:
        return empty_curl_timings()
    tcp_connect = max(0.0, connect - lookup)
    tls_handshake = max(0.0, appconnect - connect) if appconnect > 0 else None
    first_byte_start = appconnect if appconnect > 0 else connect
    first_byte = max(0.0, starttransfer - first_byte_start)
    return {
        "dns_time_ms": lookup,
        "tcp_connect_ms": tcp_connect,
        "tls_handshake_ms": tls_handshake,
        "first_byte_ms": first_byte,
        "https_time_ms": total,
        "https_ok": 200 <= status_code < 400,
    }


def empty_curl_timings():
    return {
        "dns_time_ms": None,
        "tcp_connect_ms": None,
        "tls_handshake_ms": None,
        "first_byte_ms": None,
        "https_time_ms": None,
        "https_ok": False,
    }


def parse_testmy_json(output):
    data = json.loads(output)
    if not isinstance(data, dict):
        raise ValueError("TestMy output must be a JSON object")
    download = number(data.get("download_mbps"))
    upload = number(data.get("upload_mbps"))
    if download is None and isinstance(data.get("download"), dict):
        download = number(data["download"].get("mbps"))
    if upload is None and isinstance(data.get("upload"), dict):
        upload = number(data["upload"].get("mbps"))
    if download is None and upload is None:
        raise ValueError("TestMy output needs download_mbps or upload_mbps")
    return {
        "testmy_download_mbps": download,
        "testmy_upload_mbps": upload,
    }


def testmy_command_from_env():
    command = os.environ.get("SPEED_TRACKER_TESTMY_COMMAND", "").strip()
    return shlex.split(command) if command else None


def run_testmy_speed(runner=run_command, command=None):
    command = command if command is not None else testmy_command_from_env()
    if not command:
        return {}, None
    completed = runner(command, 180)
    if completed.returncode:
        message = completed.stderr.strip() or f"TestMy command exited {completed.returncode}"
        return {}, message
    try:
        return parse_testmy_json(completed.stdout), None
    except (json.JSONDecodeError, ValueError) as exc:
        return {}, f"TestMy parse failed: {exc}"


def parse_default_route(output):
    gateway_match = re.search(r"^\s*gateway:\s*(\S+)", output, re.MULTILINE)
    interface_match = re.search(r"^\s*interface:\s*(\S+)", output, re.MULTILINE)
    gateway = clean_route_value(gateway_match.group(1)) if gateway_match else None
    interface = clean_interface(interface_match.group(1)) if interface_match else None
    return {
        "gateway": gateway,
        "default_interface": interface,
        "route_ok": bool(gateway or interface),
        "route_signature": ":".join(value for value in (interface, gateway) if value),
    }


def default_route(runner=run_command, ipv6=False):
    args = ["/sbin/route", "-n", "get"]
    if ipv6:
        args.append("-inet6")
    args.append("default")
    completed = runner(args, 5)
    if completed.returncode:
        return {
            "gateway": None,
            "default_interface": None,
            "route_ok": False,
            "route_signature": "",
        }
    return parse_default_route(completed.stdout + completed.stderr)


def parse_scutil_dns(output):
    resolvers = set(re.findall(r"nameserver\[\d+\]\s*:\s*([^\s]+)", output))
    return len(resolvers) if resolvers else None


def parse_proxy_vpn_hints(proxy_output="", nwi_output=""):
    hints = []
    if re.search(r"(HTTPEnable|HTTPSEnable|SOCKSEnable)\s*:\s*1", proxy_output):
        hints.append("proxy_enabled")
    if re.search(r"\butun\d+\b|\bppp\d+\b|\bipsec\d+\b", nwi_output):
        hints.append("vpn_interface_present")
    return hints


def system_context(runner=run_command):
    result = {"dns_resolver_count": None, "proxy_vpn_hints": []}
    try:
        completed = runner(["/usr/sbin/scutil", "--dns"], 5)
        if completed.returncode == 0:
            result["dns_resolver_count"] = parse_scutil_dns(completed.stdout)
    except (OSError, subprocess.TimeoutExpired):
        pass
    proxy_output = ""
    nwi_output = ""
    try:
        completed = runner(["/usr/sbin/scutil", "--proxy"], 5)
        if completed.returncode == 0:
            proxy_output = completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        completed = runner(["/usr/sbin/scutil", "--nwi"], 5)
        if completed.returncode == 0:
            nwi_output = completed.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    result["proxy_vpn_hints"] = parse_proxy_vpn_hints(proxy_output, nwi_output)
    return result


def parse_traceroute(output):
    summary = []
    for line in output.splitlines():
        match = re.match(r"\s*(\d+)\s+(.+)", line)
        if not match:
            continue
        hop = int(match.group(1))
        rest = match.group(2)
        latency = re.search(r"([\d.]+)\s*ms", rest)
        summary.append({
            "hop": hop,
            "status": "reply" if latency else "timeout",
            "latency_ms": float(latency.group(1)) if latency else None,
        })
    return summary


def run_traceroute(runner=run_command):
    completed = runner(
        ["/usr/sbin/traceroute", "-n", "-m", "12", "-q", "1", "-w", "1", INTERNET_PING_HOST],
        20,
    )
    if completed.returncode not in (0, 1):
        return [], f"traceroute exited {completed.returncode}"
    summary = parse_traceroute(completed.stdout + completed.stderr)
    return summary, None


def run_ping(host, count=1, wait_ms=1000, runner=run_command, ipv6=False):
    if not host:
        return {
            "ok": False,
            "packet_loss_percent": None,
            "ping_latency_ms": None,
            "latency_ms": None,
            "latencies_ms": [],
        }
    if ipv6:
        args = ["/sbin/ping6", "-c", str(count), host]
    else:
        args = ["/sbin/ping", "-c", str(count), "-W", str(wait_ms), host]
    timeout = max(6, count + 5)
    completed = runner(args, timeout)
    parsed = parse_ping(completed.stdout + completed.stderr)
    parsed["ok"] = completed.returncode == 0 and parsed["packet_loss_percent"] != 100
    return parsed


def run_dns_probe(runner=run_command, resolver=None):
    args = ["/usr/bin/dig", "+stats", "+tries=1", "+time=5"]
    if resolver:
        args.append(f"@{resolver}")
    args.append(DNS_HOST)
    completed = runner(args, 8)
    elapsed = parse_dns(completed.stdout)
    ok = completed.returncode == 0 and elapsed is not None
    return {
        "dns_time_ms": elapsed,
        "dns_ok": ok,
        "error": None if ok else f"dig exited {completed.returncode}",
    }


def run_https_probe(runner=run_command):
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
            "%{time_namelookup} %{time_connect} %{time_appconnect} "
            "%{time_starttransfer} %{time_total} %{http_code}",
            HTTPS_URL,
        ],
        12,
    )
    parsed = parse_curl_timings(completed.stdout)
    parsed["error"] = None
    if completed.returncode or not parsed["https_ok"]:
        parsed["error"] = completed.stderr.strip() or f"curl exited {completed.returncode}"
    return parsed


def percentile(values, percent):
    values = sorted(value for value in values if value is not None)
    if not values:
        return None
    index = max(0, min(len(values) - 1, int((percent / 100) * len(values) + 0.999999) - 1))
    return values[index]


def jitter(values):
    values = [value for value in values if value is not None]
    if len(values) < 2:
        return None
    deltas = [abs(values[index] - values[index - 1]) for index in range(1, len(values))]
    return sum(deltas) / len(deltas)


def insert_monitor_sample(connection, sample):
    columns = [
        "sampled_at", "kind", "ok", "latency_ms", "packet_loss_percent",
        "dns_time_ms", "tcp_connect_ms", "tls_handshake_ms", "first_byte_ms",
        "https_time_ms", "default_interface", "route_signature",
        "ipv4_route_ok", "ipv6_route_ok", "details",
    ]
    values = []
    for column in columns:
        value = sample.get(column)
        if column == "details":
            value = json.dumps(value or {}, separators=(",", ":"))
        if column in ("ok", "ipv4_route_ok", "ipv6_route_ok"):
            value = int(bool(value))
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO monitor_samples ({','.join(columns)}) VALUES ({placeholders})",
        values,
    )
    connection.commit()
    return cursor.lastrowid


def monitor_sample_to_dict(row):
    data = dict(row)
    for field in ("ok", "ipv4_route_ok", "ipv6_route_ok"):
        data[field] = bool(data[field])
    data["details"] = safe_json_dict(data.get("details"))
    return data


def monitor_rows(connection, window_seconds=MONITOR_WINDOW_SECONDS, now=None):
    now = parse_datetime(now or utc_now())
    cutoff = now - dt.timedelta(seconds=window_seconds)
    rows = connection.execute(
        """
        SELECT * FROM monitor_samples
        WHERE datetime(sampled_at) >= datetime(?)
        ORDER BY sampled_at ASC
        """,
        [cutoff.isoformat()],
    ).fetchall()
    return [monitor_sample_to_dict(row) for row in rows]


def parse_datetime(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def latest_value(rows, key):
    for row in reversed(rows):
        value = row.get(key)
        if value is not None:
            return value
    return None


def aggregate_health(connection, window_seconds=MONITOR_WINDOW_SECONDS, now=None):
    rows = monitor_rows(connection, window_seconds, now)
    by_kind = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)

    internet = by_kind.get("internet_ping", [])
    router = by_kind.get("router_ping", [])
    ipv6 = by_kind.get("ipv6_ping", [])
    dns = by_kind.get("dns", [])
    https = by_kind.get("https", [])
    route = by_kind.get("route", [])
    latencies = [row["latency_ms"] for row in internet if row.get("latency_ms") is not None]

    route_signatures = {
        row["route_signature"] for row in route if row.get("route_signature")
    }
    return {
        "router_ping_ok": latest_value(router, "ok"),
        "internet_ping_ok": latest_value(internet, "ok"),
        "ipv6_ok": latest_value(ipv6, "ok"),
        "dns_ok": latest_value(dns, "ok"),
        "https_ok": latest_value(https, "ok"),
        "packet_loss_percent": sample_loss(internet),
        "gateway_packet_loss_percent": sample_loss(router),
        "ping_latency_ms": latencies[-1] if latencies else None,
        "latency_p50": percentile(latencies, 50),
        "latency_p95": percentile(latencies, 95),
        "latency_p99": percentile(latencies, 99),
        "jitter_ms": jitter(latencies),
        "dns_time_ms": latest_value(dns, "dns_time_ms"),
        "tcp_connect_ms": latest_value(https, "tcp_connect_ms"),
        "tls_handshake_ms": latest_value(https, "tls_handshake_ms"),
        "first_byte_ms": latest_value(https, "first_byte_ms"),
        "https_time_ms": latest_value(https, "https_time_ms"),
        "route_changed": len(route_signatures) > 1 if route else None,
        "route_signature": latest_value(route, "route_signature"),
        "default_interface": latest_value(route, "default_interface"),
        "ipv4_route_ok": latest_value(route, "ipv4_route_ok"),
        "ipv6_route_ok": latest_value(route, "ipv6_route_ok"),
    }


def sample_loss(rows):
    if not rows:
        return None
    failed = sum(1 for row in rows if not row.get("ok"))
    return failed * 100.0 / len(rows)


def classify(result, testmy_configured=False, require_speed=True):
    reasons = []
    if result.get("internet_ping_ok") is False and result.get("https_ok") is False:
        reasons.append("No usable internet path")
    if result.get("router_ping_ok") is False:
        reasons.append("Router ping failed")
    if result.get("internet_ping_ok") is False:
        reasons.append("Internet ping failed")
    if result.get("ipv6_route_ok") and result.get("ipv6_ok") is False:
        reasons.append("IPv6 ping failed")
    if result.get("packet_loss_percent") is None:
        reasons.append("Packet-loss probe failed")
    elif result["packet_loss_percent"] > 1:
        reasons.append(f"Packet loss is {result['packet_loss_percent']:.1f}%")
    if (
        result.get("gateway_packet_loss_percent") is not None
        and result["gateway_packet_loss_percent"] > 1
    ):
        reasons.append(
            f"Gateway packet loss is {result['gateway_packet_loss_percent']:.1f}%"
        )
    if result.get("idle_latency_ms") is not None and result["idle_latency_ms"] > 100:
        reasons.append(f"Idle latency is {result['idle_latency_ms']:.0f} ms")
    if result.get("latency_p95") is not None and result["latency_p95"] > 150:
        reasons.append(f"p95 latency is {result['latency_p95']:.0f} ms")
    if result.get("latency_p99") is not None and result["latency_p99"] > 300:
        reasons.append(f"p99 latency is {result['latency_p99']:.0f} ms")
    if (
        result.get("loaded_latency_ms") is not None
        and result["loaded_latency_ms"] > 250
    ):
        reasons.append(f"Loaded latency is {result['loaded_latency_ms']:.0f} ms")
    if result.get("dns_ok") is False:
        reasons.append("DNS lookup failed")
    if result.get("https_ok") is False:
        reasons.append("HTTPS request failed")
    if result.get("route_changed"):
        reasons.append("Default route changed")
    if testmy_configured and result.get("testmy_download_mbps") is None:
        reasons.append("TestMy speed test failed")

    has_networkquality = (
        result.get("networkquality_download_mbps") is not None
        or result.get("networkquality_upload_mbps") is not None
    )
    has_testmy = (
        result.get("testmy_download_mbps") is not None
        or result.get("testmy_upload_mbps") is not None
    )
    has_legacy_speed = (
        result.get("download_mbps") is not None
        or result.get("upload_mbps") is not None
    )
    if require_speed and not (has_networkquality or has_testmy or has_legacy_speed):
        reasons.insert(0, "Network speed tests failed")
    if "No usable internet path" in reasons or "Network speed tests failed" in reasons:
        return "failed", dedupe(reasons)
    return ("degraded" if reasons else "healthy"), dedupe(reasons)


def dedupe(values):
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def collect_measurements(connection=None, runner=run_command, testmy_command=None):
    if callable(connection) and runner is run_command:
        runner = connection
        connection = None
    started_at = utc_now()
    started_monotonic = time.monotonic()
    result = default_result()
    errors = []

    if connection is not None:
        result.update({k: v for k, v in aggregate_health(connection).items() if v is not None})

    ipv4_route = probe_route(runner, ipv6=False)
    ipv6_route = probe_route(runner, ipv6=True)
    result.update({
        "default_interface": ipv4_route.get("default_interface"),
        "route_signature": ipv4_route.get("route_signature"),
        "ipv4_route_ok": ipv4_route.get("route_ok"),
        "ipv6_route_ok": ipv6_route.get("route_ok"),
    })
    if connection is not None and result.get("route_signature"):
        result["route_changed"] = bool(
            result.get("route_changed")
            or route_changed_since_last_run(connection, result["route_signature"])
        )

    context = system_context(runner)
    result.update(context)

    gateway = ipv4_route.get("gateway")
    if gateway:
        try:
            gateway_ping = run_ping(gateway, count=20, runner=runner)
            result["router_ping_ok"] = gateway_ping["ok"]
            result["gateway_packet_loss_percent"] = gateway_ping["packet_loss_percent"]
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            errors.append(f"gateway ping: {exc}")

    try:
        internet_ping = run_ping(INTERNET_PING_HOST, count=20, runner=runner)
        result["internet_ping_ok"] = internet_ping["ok"]
        result["packet_loss_percent"] = internet_ping["packet_loss_percent"]
        result["ping_latency_ms"] = internet_ping["ping_latency_ms"]
        latencies = internet_ping.get("latencies_ms") or []
        result["latency_p50"] = percentile(latencies, 50) or result.get("latency_p50")
        result["latency_p95"] = percentile(latencies, 95) or result.get("latency_p95")
        result["latency_p99"] = percentile(latencies, 99) or result.get("latency_p99")
        result["jitter_ms"] = jitter(latencies) or result.get("jitter_ms")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"internet ping: {exc}")

    if ipv6_route.get("route_ok"):
        try:
            ipv6_ping = run_ping(IPV6_PING_HOST, count=3, runner=runner, ipv6=True)
            result["ipv6_ok"] = ipv6_ping["ok"]
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            errors.append(f"IPv6 ping: {exc}")

    try:
        dns = run_dns_probe(runner)
        result["dns_time_ms"] = dns["dns_time_ms"]
        result["dns_ok"] = dns["dns_ok"]
        if not dns["dns_ok"]:
            errors.append(dns["error"])
            fallback = run_dns_probe(runner, FALLBACK_RESOLVER)
            result["dns_fallback_time_ms"] = fallback["dns_time_ms"]
            result["dns_fallback_ok"] = fallback["dns_ok"]
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"dig: {exc}")

    try:
        https = run_https_probe(runner)
        result.update({
            "tcp_connect_ms": https["tcp_connect_ms"],
            "tls_handshake_ms": https["tls_handshake_ms"],
            "first_byte_ms": https["first_byte_ms"],
            "https_time_ms": https["https_time_ms"],
            "https_ok": https["https_ok"],
        })
        if https["dns_time_ms"] is not None:
            result["dns_time_ms"] = result["dns_time_ms"] or https["dns_time_ms"]
        if https["error"]:
            errors.append(https["error"])
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"curl: {exc}")

    try:
        testmy, testmy_error = run_testmy_speed(runner, testmy_command)
        result.update(testmy)
        if testmy_error:
            errors.append(testmy_error)
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append(f"TestMy: {exc}")

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

    if result.get("testmy_download_mbps") is not None:
        result["download_mbps"] = result["testmy_download_mbps"]
    elif result.get("networkquality_download_mbps") is not None:
        result["download_mbps"] = result["networkquality_download_mbps"]
    if result.get("testmy_upload_mbps") is not None:
        result["upload_mbps"] = result["testmy_upload_mbps"]
    elif result.get("networkquality_upload_mbps") is not None:
        result["upload_mbps"] = result["networkquality_upload_mbps"]

    try:
        traceroute_summary, traceroute_error = run_traceroute(runner)
        result["traceroute_summary"] = traceroute_summary
        result["route_hop_count"] = len(traceroute_summary) or None
        if traceroute_error:
            errors.append(traceroute_error)
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        errors.append(f"traceroute: {exc}")

    result["started_at"] = started_at
    result["completed_at"] = utc_now()
    result["duration_seconds"] = round(time.monotonic() - started_monotonic, 3)
    result["status"], reasons = classify(
        result, testmy_configured=bool(testmy_command or testmy_command_from_env())
    )
    result["degraded_reasons"] = reasons
    result["errors"] = [error for error in errors if error]
    return result


def default_result():
    return {
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
        "router_ping_ok": None,
        "internet_ping_ok": None,
        "ipv6_ok": None,
        "ipv4_route_ok": None,
        "ipv6_route_ok": None,
        "gateway_packet_loss_percent": None,
        "latency_p50": None,
        "latency_p95": None,
        "latency_p99": None,
        "jitter_ms": None,
        "tcp_connect_ms": None,
        "tls_handshake_ms": None,
        "first_byte_ms": None,
        "testmy_download_mbps": None,
        "testmy_upload_mbps": None,
        "networkquality_download_mbps": None,
        "networkquality_upload_mbps": None,
        "route_hop_count": None,
        "route_changed": False,
        "default_interface": None,
        "route_signature": None,
        "dns_fallback_time_ms": None,
        "dns_fallback_ok": None,
        "dns_resolver_count": None,
        "proxy_vpn_hints": [],
        "traceroute_summary": [],
    }


def probe_route(runner=run_command, ipv6=False):
    try:
        return default_route(runner, ipv6)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {
            "gateway": None,
            "default_interface": None,
            "route_ok": False,
            "route_signature": "",
        }


def route_changed_since_last_run(connection, route_signature):
    row = connection.execute(
        """
        SELECT route_signature FROM runs
        WHERE route_signature IS NOT NULL AND route_signature != ''
        ORDER BY started_at DESC LIMIT 1
        """
    ).fetchone()
    return bool(row and row["route_signature"] != route_signature)


def insert_run(connection, result):
    columns = [
        "started_at", "completed_at", "duration_seconds", "status",
        "download_mbps", "upload_mbps", "idle_latency_ms", "loaded_latency_ms",
        "responsiveness_rpm", "packet_loss_percent", "ping_latency_ms",
        "dns_time_ms", "dns_ok", "https_time_ms", "https_ok", "interface_name",
        "router_ping_ok", "internet_ping_ok", "ipv6_ok", "ipv4_route_ok",
        "ipv6_route_ok", "gateway_packet_loss_percent", "latency_p50",
        "latency_p95", "latency_p99", "jitter_ms", "tcp_connect_ms",
        "tls_handshake_ms", "first_byte_ms", "testmy_download_mbps",
        "testmy_upload_mbps", "networkquality_download_mbps",
        "networkquality_upload_mbps", "route_hop_count", "route_changed",
        "default_interface", "route_signature", "dns_fallback_time_ms",
        "dns_fallback_ok", "dns_resolver_count", "proxy_vpn_hints",
        "traceroute_summary", "degraded_reasons", "errors",
    ]
    values = []
    for column in columns:
        value = result.get(column)
        if column in ("degraded_reasons", "errors", "proxy_vpn_hints", "traceroute_summary"):
            value = json.dumps(value or [], separators=(",", ":"))
        if column in (
            "dns_ok", "https_ok", "router_ping_ok", "internet_ping_ok", "ipv6_ok",
            "ipv4_route_ok", "ipv6_route_ok", "route_changed", "dns_fallback_ok",
        ):
            value = None if value is None else int(bool(value))
        values.append(value)
    placeholders = ",".join("?" for _ in columns)
    cursor = connection.execute(
        f"INSERT INTO runs ({','.join(columns)}) VALUES ({placeholders})", values
    )
    connection.commit()
    return cursor.lastrowid


def row_to_dict(row):
    data = dict(row)
    for field in (
        "dns_ok", "https_ok", "router_ping_ok", "internet_ping_ok", "ipv6_ok",
        "ipv4_route_ok", "ipv6_route_ok", "route_changed", "dns_fallback_ok",
    ):
        if field in data:
            data[field] = bool_or_none(data[field])
    data["degraded_reasons"] = safe_json_list(data.get("degraded_reasons"))
    data["errors"] = safe_json_list(data.get("errors"))
    if "proxy_vpn_hints" in data:
        data["proxy_vpn_hints"] = safe_json_list(data.get("proxy_vpn_hints"))
    if "traceroute_summary" in data:
        data["traceroute_summary"] = safe_json_list(data.get("traceroute_summary"))
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
    health = aggregate_health(connection)
    counts = connection.execute(
        """
        SELECT status, COUNT(*) AS count FROM runs
        WHERE datetime(started_at) >= datetime('now', '-24 hours')
        GROUP BY status
        """
    ).fetchall()
    status, reasons = classify({
        **default_result(),
        **(latest or {}),
        **{key: value for key, value in health.items() if value is not None},
    }, require_speed=False)
    return {
        "latest": latest,
        "current_health": {
            **health,
            "status": status,
            "degraded_reasons": reasons,
        },
        "last_24_hours": {row["status"]: row["count"] for row in counts},
    }


PUBLIC_FIELDS = (
    "started_at",
    "completed_at",
    "duration_seconds",
    "status",
    "download_mbps",
    "upload_mbps",
    "testmy_download_mbps",
    "testmy_upload_mbps",
    "networkquality_download_mbps",
    "networkquality_upload_mbps",
    "idle_latency_ms",
    "loaded_latency_ms",
    "responsiveness_rpm",
    "packet_loss_percent",
    "gateway_packet_loss_percent",
    "ping_latency_ms",
    "latency_p50",
    "latency_p95",
    "latency_p99",
    "jitter_ms",
    "dns_time_ms",
    "dns_ok",
    "https_time_ms",
    "https_ok",
    "tcp_connect_ms",
    "tls_handshake_ms",
    "first_byte_ms",
    "router_ping_ok",
    "internet_ping_ok",
    "ipv6_ok",
    "ipv4_route_ok",
    "ipv6_route_ok",
    "route_hop_count",
    "route_changed",
    "dns_fallback_time_ms",
    "dns_fallback_ok",
    "dns_resolver_count",
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
        "current_health": public_health(aggregate_health(connection)),
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


def public_health(health):
    public = dict(health)
    public.pop("default_interface", None)
    public.pop("route_signature", None)
    return public


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
:root{color-scheme:dark;--bg:#0b1220;--card:#121c2f;--muted:#93a4bd;--line:#273854;--good:#42d392;--warn:#f5b942;--bad:#ff6b6b;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#edf3fb;font:15px system-ui,-apple-system,sans-serif}
main{max-width:1220px;margin:auto;padding:28px}header{display:flex;justify-content:space-between;gap:16px;align-items:center}
h1{margin:0;font-size:26px}.range button{background:transparent;color:var(--muted);border:1px solid var(--line);padding:7px 11px}
.range button:first-child{border-radius:8px 0 0 8px}.range button:last-child{border-radius:0 8px 8px 0}.range .active{background:#253b60;color:white}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}.card,.panel{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px}
.label{color:var(--muted);font-size:12px;text-transform:uppercase}.value{font-size:24px;margin-top:5px}.status{font-weight:700}
.healthy{color:var(--good)}.degraded{color:var(--warn)}.failed{color:var(--bad)}
.layers{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:12px}.layer{border:1px solid var(--line);border-radius:8px;padding:10px;background:#0f1828}.layer strong{display:block;font-size:13px}.layer span{color:var(--muted);font-size:12px}
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px}.panel h2{font-size:15px;margin:0 0 10px}canvas{width:100%;height:220px}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted)}
#message{color:var(--muted);margin:12px 0}.wide{margin-top:12px;overflow:auto}.small{font-size:12px;color:var(--muted)}
@media(max-width:980px){.cards,.charts{grid-template-columns:1fr 1fr}.layers{grid-template-columns:repeat(3,1fr)}}@media(max-width:620px){main{padding:16px}.cards,.charts,.layers{grid-template-columns:1fr}header{align-items:flex-start;flex-direction:column}}
</style></head><body><main>
<header><div><h1>Connection health</h1><div id="message">Loading...</div></div>
<div class="range"><button data-range="24h" class="active">24h</button><button data-range="7d">7d</button><button data-range="30d">30d</button><button data-range="all">All</button></div></header>
<section class="cards">
<div class="card"><div class="label">Status</div><div id="status" class="value">-</div></div>
<div class="card"><div class="label">TestMy speed</div><div id="testmy" class="value">-</div></div>
<div class="card"><div class="label">Apple speed</div><div id="networkquality" class="value">-</div></div>
<div class="card"><div class="label">p95 / p99 latency</div><div id="latency" class="value">-</div></div>
</section>
<section class="layers">
<div class="layer"><strong id="layer-router">-</strong><span>Router</span></div>
<div class="layer"><strong id="layer-internet">-</strong><span>Internet</span></div>
<div class="layer"><strong id="layer-ipv6">-</strong><span>IPv6</span></div>
<div class="layer"><strong id="layer-dns">-</strong><span>DNS</span></div>
<div class="layer"><strong id="layer-https">-</strong><span>HTTP/TLS</span></div>
<div class="layer"><strong id="layer-route">-</strong><span>Route</span></div>
</section>
<section class="charts">
<div class="panel"><h2>TestMy / Apple download (Mbps)</h2><canvas id="speed"></canvas></div>
<div class="panel"><h2>Latency percentiles (ms)</h2><canvas id="latencies"></canvas></div>
<div class="panel"><h2>Packet loss (%)</h2><canvas id="loss"></canvas></div>
<div class="panel"><h2>HTTP/TLS timing (ms)</h2><canvas id="http"></canvas></div>
</section>
<section class="panel wide"><h2>Recent hourly tests</h2><table><thead><tr><th>Time</th><th>Status</th><th>TestMy</th><th>Apple</th><th>Loss</th><th>p95/p99</th><th>DNS/HTTPS</th><th>Details</th></tr></thead><tbody id="rows"></tbody></table></section>
</main><script>
let selected='24h'; const fmt=(v,d=1)=>v==null?'-':Number(v).toFixed(d);
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ok=v=>v==null?'-':(v?'OK':'Fail'); const pair=(a,b)=>fmt(a)+' / '+fmt(b);
function draw(id,runs,series){const c=document.getElementById(id),dpr=devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*dpr;c.height=h*dpr;const x=c.getContext('2d');x.scale(dpr,dpr);x.clearRect(0,0,w,h);x.strokeStyle='#273854';x.beginPath();for(let i=0;i<5;i++){let y=12+i*(h-34)/4;x.moveTo(35,y);x.lineTo(w-8,y)}x.stroke();let vals=series.flatMap(s=>runs.map(r=>r[s.key]).filter(v=>v!=null));let max=Math.max(...vals,1);series.forEach(s=>{x.strokeStyle=s.color;x.lineWidth=2;x.beginPath();let started=false;runs.forEach((r,i)=>{let v=r[s.key];if(v==null){started=false;return}let px=35+(w-45)*(runs.length<2?1:i/(runs.length-1)),py=12+(h-34)*(1-v/max);if(!started)x.moveTo(px,py);else x.lineTo(px,py);started=true});x.stroke()});x.fillStyle='#93a4bd';x.font='11px system-ui';x.fillText(max.toFixed(max<10?1:0),2,15);x.fillText('0',18,h-19);series.forEach((s,i)=>{x.fillStyle=s.color;x.fillText(s.name,42+i*98,h-5)})}
async function load(){let staticMode=!['127.0.0.1','localhost'].includes(location.hostname),runs,l,h={};
if(staticMode){let payload=await (await fetch('./data.json',{cache:'no-store'})).json(),all=payload.runs||[],hours={'24h':24,'7d':168,'30d':720}[selected],cutoff=hours?Date.now()-hours*3600000:0;runs=all.filter(r=>new Date(r.started_at).getTime()>=cutoff).reverse();l=all[0]||null;h=payload.current_health||{}}
else{let [sumRes,runsRes]=await Promise.all([fetch('/api/summary'),fetch('/api/runs?range='+selected+'&limit=10000')]),sum=await sumRes.json();runs=(await runsRes.json()).runs.reverse();l=sum.latest;h=sum.current_health||{}}
if(!l){document.getElementById('message').textContent='No hourly tests recorded yet. Run: python3 speed_tracker.py collect';return}
document.getElementById('message').textContent='Last hourly test '+new Date(l.completed_at).toLocaleString()+' · '+fmt(l.duration_seconds)+' seconds';
let status=document.getElementById('status');let currentStatus=h.status||l.status;status.textContent=currentStatus;status.className='value status '+currentStatus;
document.getElementById('testmy').textContent=pair(l.testmy_download_mbps,l.testmy_upload_mbps)+' Mbps';
document.getElementById('networkquality').textContent=pair(l.networkquality_download_mbps,l.networkquality_upload_mbps)+' Mbps';
document.getElementById('latency').textContent=pair(h.latency_p95??l.latency_p95,h.latency_p99??l.latency_p99)+' ms';
document.getElementById('layer-router').textContent=ok(h.router_ping_ok??l.router_ping_ok);
document.getElementById('layer-internet').textContent=ok(h.internet_ping_ok??l.internet_ping_ok);
document.getElementById('layer-ipv6').textContent=ok(h.ipv6_ok??l.ipv6_ok);
document.getElementById('layer-dns').textContent=ok(h.dns_ok??l.dns_ok);
document.getElementById('layer-https').textContent=ok(h.https_ok??l.https_ok);
document.getElementById('layer-route').textContent=(h.route_changed??l.route_changed)?'Changed':ok(h.ipv4_route_ok??l.ipv4_route_ok);
draw('speed',runs,[{key:'testmy_download_mbps',name:'TestMy',color:'#58a6ff'},{key:'networkquality_download_mbps',name:'Apple',color:'#42d392'}]);
draw('latencies',runs,[{key:'latency_p95',name:'p95',color:'#f5b942'},{key:'latency_p99',name:'p99',color:'#ff6b6b'}]);
draw('loss',runs,[{key:'packet_loss_percent',name:'Internet',color:'#ff6b6b'},{key:'gateway_packet_loss_percent',name:'Router',color:'#b392f0'}]);
draw('http',runs,[{key:'dns_time_ms',name:'DNS',color:'#58a6ff'},{key:'tcp_connect_ms',name:'TCP',color:'#42d392'},{key:'tls_handshake_ms',name:'TLS',color:'#f5b942'},{key:'first_byte_ms',name:'TTFB',color:'#b392f0'}]);
document.getElementById('rows').innerHTML=runs.slice().reverse().slice(0,100).map(r=>`<tr><td>${esc(new Date(r.started_at).toLocaleString())}</td><td class="${r.status}">${r.status}</td><td>${pair(r.testmy_download_mbps,r.testmy_upload_mbps)}</td><td>${pair(r.networkquality_download_mbps,r.networkquality_upload_mbps)}</td><td>${fmt(r.packet_loss_percent)}% / ${fmt(r.gateway_packet_loss_percent)}%</td><td>${pair(r.latency_p95,r.latency_p99)}</td><td>${fmt(r.dns_time_ms,0)} ms / ${fmt(r.https_time_ms,0)} ms</td><td>${esc((r.degraded_reasons||[]).join('; ')||'-')}</td></tr>`).join('');
}
document.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>{selected=b.dataset.range;document.querySelectorAll('[data-range]').forEach(x=>x.classList.toggle('active',x===b));load()});addEventListener('resize',()=>load());load().catch(e=>document.getElementById('message').textContent=e);
</script></body></html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    db_path = DB_PATH

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.respond(DASHBOARD.encode(), "text/html; charset=utf-8")
            return
        if parsed.path not in ("/api/summary", "/api/runs", "/api/health"):
            self.send_error(404)
            return
        try:
            with connect_db(self.db_path) as connection:
                if parsed.path == "/api/summary":
                    payload = summary(connection)
                elif parsed.path == "/api/health":
                    payload = aggregate_health(connection)
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


def monitor_once(connection, runner=run_command, state=None, now_monotonic=None):
    state = state if state is not None else {}
    now_monotonic = time.monotonic() if now_monotonic is None else now_monotonic
    sampled_at = utc_now()
    route_info = state.get("route_info")

    if route_info is None or now_monotonic >= state.get("next_route", 0):
        ipv4 = probe_route(runner, ipv6=False)
        ipv6 = probe_route(runner, ipv6=True)
        route_info = {**ipv4, "ipv6_route_ok": ipv6.get("route_ok")}
        state["route_info"] = route_info
        state["next_route"] = now_monotonic + 300
        insert_monitor_sample(connection, {
            "sampled_at": sampled_at,
            "kind": "route",
            "ok": ipv4.get("route_ok"),
            "default_interface": ipv4.get("default_interface"),
            "route_signature": ipv4.get("route_signature"),
            "ipv4_route_ok": ipv4.get("route_ok"),
            "ipv6_route_ok": ipv6.get("route_ok"),
        })

    gateway = route_info.get("gateway")
    router = run_ping(gateway, count=1, runner=runner) if gateway else {"ok": False}
    insert_monitor_sample(connection, {
        "sampled_at": sampled_at,
        "kind": "router_ping",
        "ok": router.get("ok"),
        "latency_ms": router.get("latency_ms"),
        "packet_loss_percent": router.get("packet_loss_percent"),
    })

    internet = run_ping(INTERNET_PING_HOST, count=1, runner=runner)
    insert_monitor_sample(connection, {
        "sampled_at": sampled_at,
        "kind": "internet_ping",
        "ok": internet.get("ok"),
        "latency_ms": internet.get("latency_ms"),
        "packet_loss_percent": internet.get("packet_loss_percent"),
    })

    if route_info.get("ipv6_route_ok") and now_monotonic >= state.get("next_ipv6", 0):
        ipv6 = run_ping(IPV6_PING_HOST, count=1, runner=runner, ipv6=True)
        state["next_ipv6"] = now_monotonic + 60
        insert_monitor_sample(connection, {
            "sampled_at": sampled_at,
            "kind": "ipv6_ping",
            "ok": ipv6.get("ok"),
            "latency_ms": ipv6.get("latency_ms"),
            "packet_loss_percent": ipv6.get("packet_loss_percent"),
        })

    if now_monotonic >= state.get("next_dns", 0):
        dns = run_dns_probe(runner)
        details = {}
        if not dns["dns_ok"]:
            fallback = run_dns_probe(runner, FALLBACK_RESOLVER)
            details = {
                "fallback_ok": fallback["dns_ok"],
                "fallback_time_ms": fallback["dns_time_ms"],
            }
        state["next_dns"] = now_monotonic + 300
        insert_monitor_sample(connection, {
            "sampled_at": sampled_at,
            "kind": "dns",
            "ok": dns["dns_ok"],
            "latency_ms": dns["dns_time_ms"],
            "dns_time_ms": dns["dns_time_ms"],
            "details": details,
        })

    if now_monotonic >= state.get("next_https", 0):
        https = run_https_probe(runner)
        state["next_https"] = now_monotonic + 300
        insert_monitor_sample(connection, {
            "sampled_at": sampled_at,
            "kind": "https",
            "ok": https["https_ok"],
            "dns_time_ms": https["dns_time_ms"],
            "tcp_connect_ms": https["tcp_connect_ms"],
            "tls_handshake_ms": https["tls_handshake_ms"],
            "first_byte_ms": https["first_byte_ms"],
            "https_time_ms": https["https_time_ms"],
        })
    return state


def monitor_forever(db_path=DB_PATH, runner=run_command):
    state = {}
    with connect_db(db_path) as connection:
        while True:
            started = time.monotonic()
            try:
                state = monitor_once(connection, runner, state, started)
            except Exception as exc:  # Keep the LaunchAgent alive after transient probe errors.
                print(f"monitor probe failed: {exc}", file=sys.stderr, flush=True)
            elapsed = time.monotonic() - started
            time.sleep(max(1, MONITOR_INTERVAL_SECONDS - elapsed))


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
    monitor = {
        **common,
        "Label": MONITOR_LABEL,
        "ProgramArguments": [python_path, script_path, "monitor"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(LOG_DIR / "monitor.log"),
        "StandardErrorPath": str(LOG_DIR / "monitor-error.log"),
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
    return {COLLECT_LABEL: collect, MONITOR_LABEL: monitor, SERVER_LABEL: server}


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
    for label in (COLLECT_LABEL, MONITOR_LABEL, SERVER_LABEL):
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
    collect_parser = subparsers.add_parser("collect", help="Run and save a complete network test")
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
    subparsers.add_parser("monitor", help="Run low-impact continuous health probes")
    serve_parser = subparsers.add_parser("serve", help="Run the local dashboard")
    serve_parser.add_argument("--host", default=HOST, choices=[HOST])
    serve_parser.add_argument("--port", default=PORT, type=int)
    subparsers.add_parser("install", help="Install and load macOS LaunchAgents")
    subparsers.add_parser("uninstall", help="Unload and remove LaunchAgents")
    args = parser.parse_args(argv)

    if args.command == "collect":
        try:
            with collection_lock():
                with connect_db(args.database) as connection:
                    result = collect_measurements(connection)
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
    if args.command == "monitor":
        try:
            monitor_forever(args.database)
        except KeyboardInterrupt:
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
