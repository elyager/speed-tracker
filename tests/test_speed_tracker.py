import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import speed_tracker as tracker


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class SpeedTrackerTests(unittest.TestCase):
    def test_parse_network_quality(self):
        parsed = tracker.parse_network_quality(json.dumps({
            "dl_throughput": 125_000_000,
            "ul_throughput": 25_000_000,
            "base_rtt": 42,
            "responsiveness": 300,
            "interface_name": "en0",
        }))
        self.assertEqual(parsed["download_mbps"], 125)
        self.assertEqual(parsed["upload_mbps"], 25)
        self.assertEqual(parsed["networkquality_download_mbps"], 125)
        self.assertEqual(parsed["networkquality_upload_mbps"], 25)
        self.assertEqual(parsed["loaded_latency_ms"], 200)
        self.assertEqual(parsed["interface_name"], "en0")
        self.assertIsNone(tracker.clean_interface("<script>"))

    def test_parse_ping_with_latency_samples(self):
        output = (
            "64 bytes from 1.1.1.1: icmp_seq=0 ttl=57 time=10.000 ms\n"
            "64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=30.000 ms\n"
            "2 packets transmitted, 2 packets received, 0.0% packet loss\n"
            "round-trip min/avg/max/stddev = 10.000/20.000/30.000/2.000 ms\n"
        )
        parsed = tracker.parse_ping(output)
        self.assertEqual(parsed["packet_loss_percent"], 0.0)
        self.assertEqual(parsed["ping_latency_ms"], 20)
        self.assertEqual(parsed["latencies_ms"], [10, 30])

    def test_parse_curl_timings(self):
        parsed = tracker.parse_curl_timings("0.010 0.030 0.080 0.120 0.200 200")
        self.assertTrue(parsed["https_ok"])
        self.assertEqual(parsed["dns_time_ms"], 10)
        self.assertEqual(parsed["tcp_connect_ms"], 20)
        self.assertEqual(parsed["tls_handshake_ms"], 50)
        self.assertEqual(parsed["first_byte_ms"], 40)
        self.assertEqual(parsed["https_time_ms"], 200)

    def test_route_dns_proxy_and_traceroute_parsers(self):
        route = tracker.parse_default_route(
            "gateway: 192.168.1.1\ninterface: en0\n"
        )
        self.assertTrue(route["route_ok"])
        self.assertEqual(route["default_interface"], "en0")
        self.assertEqual(route["route_signature"], "en0:192.168.1.1")
        self.assertEqual(
            tracker.parse_scutil_dns("nameserver[0] : 1.1.1.1\nnameserver[1] : 8.8.8.8"),
            2,
        )
        self.assertEqual(
            tracker.parse_proxy_vpn_hints("HTTPEnable : 1", "Network interfaces: utun4"),
            ["proxy_enabled", "vpn_interface_present"],
        )
        self.assertEqual(tracker.parse_traceroute(
            " 1  192.168.1.1  2.1 ms\n 2  *\n"
        ), [
            {"hop": 1, "status": "reply", "latency_ms": 2.1},
            {"hop": 2, "status": "timeout", "latency_ms": None},
        ])

    def test_health_aggregation_percentiles_jitter_and_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = tracker.connect_db(Path(directory) / "test.sqlite")
            base = "2026-06-25T12:00:00+00:00"
            for index, latency in enumerate([10, 20, 30, 40, 50]):
                tracker.insert_monitor_sample(connection, {
                    "sampled_at": f"2026-06-25T12:0{index}:00+00:00",
                    "kind": "internet_ping",
                    "ok": latency != 30,
                    "latency_ms": latency if latency != 30 else None,
                })
            tracker.insert_monitor_sample(connection, {
                "sampled_at": base,
                "kind": "router_ping",
                "ok": True,
                "latency_ms": 2,
            })
            tracker.insert_monitor_sample(connection, {
                "sampled_at": base,
                "kind": "dns",
                "ok": True,
                "dns_time_ms": 15,
            })
            tracker.insert_monitor_sample(connection, {
                "sampled_at": base,
                "kind": "https",
                "ok": True,
                "https_time_ms": 100,
                "tcp_connect_ms": 20,
            })
            tracker.insert_monitor_sample(connection, {
                "sampled_at": base,
                "kind": "route",
                "ok": True,
                "route_signature": "en0:192.168.1.1",
                "default_interface": "en0",
                "ipv4_route_ok": True,
                "ipv6_route_ok": False,
            })
            aggregate = tracker.aggregate_health(
                connection, now="2026-06-25T12:04:30+00:00"
            )
            self.assertTrue(aggregate["router_ping_ok"])
            self.assertEqual(aggregate["packet_loss_percent"], 20)
            self.assertEqual(aggregate["latency_p50"], 20)
            self.assertEqual(aggregate["latency_p95"], 50)
            self.assertEqual(aggregate["latency_p99"], 50)
            self.assertAlmostEqual(aggregate["jitter_ms"], 13.333333333333334)
            self.assertEqual(aggregate["dns_time_ms"], 15)
            self.assertFalse(aggregate["route_changed"])

    def test_summary_current_health_does_not_require_hourly_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = tracker.connect_db(Path(directory) / "test.sqlite")
            for kind in ("router_ping", "internet_ping", "dns", "https", "route"):
                sample = {
                    "sampled_at": tracker.utc_now(),
                    "kind": kind,
                    "ok": True,
                }
                if kind == "internet_ping":
                    sample["latency_ms"] = 20
                if kind == "dns":
                    sample["dns_time_ms"] = 15
                if kind == "https":
                    sample["https_time_ms"] = 100
                if kind == "route":
                    sample["ipv4_route_ok"] = True
                    sample["ipv6_route_ok"] = False
                tracker.insert_monitor_sample(connection, sample)
            result = tracker.summary(connection)
            self.assertEqual(result["current_health"]["status"], "healthy")

    def test_classify_thresholds(self):
        healthy = {
            "download_mbps": 100,
            "networkquality_download_mbps": 100,
            "packet_loss_percent": 0,
            "latency_p95": 50,
            "latency_p99": 100,
            "idle_latency_ms": 50,
            "loaded_latency_ms": 200,
            "dns_ok": True,
            "https_ok": True,
            "internet_ping_ok": True,
        }
        self.assertEqual(tracker.classify(healthy), ("healthy", []))
        degraded = dict(healthy, packet_loss_percent=2, latency_p95=151)
        status, reasons = tracker.classify(degraded)
        self.assertEqual(status, "degraded")
        self.assertIn("Packet loss is 2.0%", reasons)
        self.assertIn("p95 latency is 151 ms", reasons)
        failed = dict(healthy, download_mbps=None, networkquality_download_mbps=None)
        self.assertEqual(tracker.classify(failed)[0], "failed")

    def test_database_migration_copies_legacy_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            legacy = sqlite3.connect(path)
            legacy.executescript("""
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                status TEXT NOT NULL,
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
            INSERT INTO runs (
                started_at, completed_at, duration_seconds, status,
                download_mbps, upload_mbps, dns_ok, https_ok,
                degraded_reasons, errors
            ) VALUES (
                '2026-06-25T12:00:00+00:00',
                '2026-06-25T12:00:10+00:00',
                10, 'healthy', 100, 25, 1, 1, '[]', '[]'
            );
            """)
            legacy.close()
            connection = tracker.connect_db(path)
            latest = tracker.latest_run(connection)
            self.assertEqual(latest["networkquality_download_mbps"], 100)
            self.assertIn("monitor_samples", {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            })

    def test_database_and_history_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = tracker.connect_db(Path(directory) / "test.sqlite")
            result = sample_result()
            tracker.insert_run(connection, result)
            latest = tracker.latest_run(connection)
            self.assertEqual(latest["status"], "healthy")
            self.assertTrue(latest["dns_ok"])
            self.assertEqual(len(tracker.history(connection, "all")), 1)
            with self.assertRaises(ValueError):
                tracker.history(connection, "invalid")

    def test_collection_records_dual_speed_and_partial_probe_failures(self):
        network_json = json.dumps({
            "dl_throughput": 80_000_000,
            "ul_throughput": 20_000_000,
            "base_rtt": 30,
            "responsiveness": 400,
            "interface_name": "en0",
        })
        network_quality_commands = []

        def runner(command, timeout):
            executable = command[0]
            if executable.endswith("traceroute"):
                return Completed(" 1  192.168.1.1  2.1 ms\n")
            if executable.endswith("route"):
                if "-inet6" in command:
                    return Completed("", "route: not in table", 1)
                return Completed("gateway: 192.168.1.1\ninterface: en0\n")
            if executable.endswith("scutil"):
                if command[-1] == "--dns":
                    return Completed("nameserver[0] : 1.1.1.1\n")
                return Completed("")
            if executable.endswith("ping"):
                return Completed(
                    "20 packets transmitted, 20 packets received, 0.0% packet loss\n"
                    "round-trip min/avg/max/stddev = 8/10/12/1 ms\n"
                )
            if executable.endswith("dig"):
                return Completed(";; Query time: 22 msec\n")
            if executable.endswith("curl"):
                return Completed(
                    "0.010 0.030 0.080 0.120 0.200 000",
                    "offline",
                    7,
                )
            if executable.endswith("networkQuality"):
                network_quality_commands.append(command)
                return Completed(network_json)
            raise AssertionError(command)

        result = tracker.collect_measurements(runner=runner)
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["https_ok"])
        self.assertEqual(result["download_mbps"], 80)
        self.assertEqual(result["upload_mbps"], 20)
        self.assertEqual(result["networkquality_download_mbps"], 80)
        self.assertEqual(result["networkquality_upload_mbps"], 20)
        self.assertEqual(
            network_quality_commands,
            [["/usr/bin/networkQuality", "-s", "-c", "-M", "120"]],
        )
        self.assertEqual(result["route_hop_count"], 1)
        self.assertTrue(result["errors"])

    def test_nonblocking_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with tracker.collection_lock(path):
                with self.assertRaises(RuntimeError):
                    with tracker.collection_lock(path):
                        pass

    def test_plists_are_structurally_valid_and_local(self):
        plists = tracker.agent_plists(
            "/tmp/speed_tracker.py", "/usr/bin/python3", 90
        )
        collect = plists[tracker.COLLECT_LABEL]
        monitor = plists[tracker.MONITOR_LABEL]
        server = plists[tracker.SERVER_LABEL]
        self.assertEqual(collect["StartInterval"], 90 * 60)
        self.assertTrue(collect["RunAtLoad"])
        self.assertEqual(collect["ProgramArguments"][-1], "--publish")
        self.assertEqual(monitor["ProgramArguments"][-1], "monitor")
        self.assertTrue(monitor["KeepAlive"])
        self.assertEqual(server["ProgramArguments"][-1], "serve")
        self.assertNotIn("0.0.0.0", json.dumps(plists))

    def test_schema_has_no_public_ip_or_ssid_fields(self):
        lowered = tracker.SCHEMA.lower()
        self.assertNotIn("public_ip", lowered)
        self.assertNotIn("ssid", lowered)

    def test_static_export_excludes_private_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            connection = tracker.connect_db(Path(directory) / "test.sqlite")
            result = sample_result()
            result["interface_name"] = "en0"
            result["default_interface"] = "en0"
            result["route_signature"] = "en0:192.168.1.1"
            result["proxy_vpn_hints"] = ["vpn_interface_present"]
            result["traceroute_summary"] = [{"hop": 1, "latency_ms": 2.1}]
            result["errors"] = ["private diagnostic"]
            tracker.insert_run(connection, result)
            output = Path(directory) / "site"
            payload = tracker.build_static_site(connection, output)
            published = payload["runs"][0]
            self.assertNotIn("interface_name", published)
            self.assertNotIn("default_interface", published)
            self.assertNotIn("route_signature", published)
            self.assertNotIn("proxy_vpn_hints", published)
            self.assertNotIn("traceroute_summary", published)
            self.assertNotIn("errors", published)
            self.assertEqual((output / "CNAME").read_text(), "net.noventayocho.work\n")
            self.assertTrue((output / ".nojekyll").exists())
            dashboard = (output / "index.html").read_text()
            self.assertIn('id="apple-download" class="value"', dashboard)
            self.assertIn('id="apple-upload" class="value"', dashboard)
            self.assertIn("Speed over time", dashboard)
            self.assertIn("Latency over time", dashboard)
            self.assertIn("Packet loss over time", dashboard)
            self.assertIn("HTTP request breakdown", dashboard)
            self.assertIn("Latency p95", dashboard)
            self.assertIn("HTTP total", dashboard)
            self.assertIn("<th>TTFB</th><th>Result</th>", dashboard)
            self.assertIn('id="collection-interval"', dashboard)
            self.assertIn("networkquality_download_mbps',name:'Download',color:'#42d392'", dashboard)
            self.assertIn("networkquality_upload_mbps',name:'Upload',color:'#58a6ff'", dashboard)
            self.assertNotIn("data-speed-metric", dashboard)
            self.assertNotIn("speedMetric", dashboard)

    def test_collection_interval_config_validation_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            self.assertEqual(tracker.load_collection_interval(path), 60)
            self.assertEqual(tracker.save_collection_interval(90, path), 90)
            self.assertEqual(tracker.load_collection_interval(path), 90)
            for invalid in (14, 1441, 60.5, "hourly", True):
                with self.assertRaises(ValueError):
                    tracker.validate_collection_interval(invalid)


def sample_result():
    return {
        "started_at": "2026-06-25T12:00:00+00:00",
        "completed_at": "2026-06-25T12:00:10+00:00",
        "duration_seconds": 10,
        "status": "healthy",
        "download_mbps": 100,
        "upload_mbps": 25,
        "networkquality_download_mbps": 95,
        "networkquality_upload_mbps": 24,
        "idle_latency_ms": 30,
        "loaded_latency_ms": 150,
        "responsiveness_rpm": 400,
        "packet_loss_percent": 0,
        "gateway_packet_loss_percent": 0,
        "ping_latency_ms": 20,
        "latency_p50": 20,
        "latency_p95": 30,
        "latency_p99": 40,
        "jitter_ms": 3,
        "dns_time_ms": 15,
        "dns_ok": True,
        "https_time_ms": 100,
        "https_ok": True,
        "tcp_connect_ms": 20,
        "tls_handshake_ms": 30,
        "first_byte_ms": 40,
        "router_ping_ok": True,
        "internet_ping_ok": True,
        "ipv6_ok": None,
        "ipv4_route_ok": True,
        "ipv6_route_ok": False,
        "route_hop_count": 4,
        "route_changed": False,
        "default_interface": "en0",
        "route_signature": "en0:192.168.1.1",
        "dns_fallback_time_ms": None,
        "dns_fallback_ok": None,
        "dns_resolver_count": 1,
        "proxy_vpn_hints": [],
        "traceroute_summary": [],
        "interface_name": "en0",
        "degraded_reasons": [],
        "errors": [],
    }


if __name__ == "__main__":
    unittest.main()
