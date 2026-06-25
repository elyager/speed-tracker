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
        self.assertEqual(parsed["loaded_latency_ms"], 200)
        self.assertEqual(parsed["interface_name"], "en0")
        self.assertIsNone(tracker.clean_interface("<script>"))

    def test_parse_ping(self):
        output = (
            "5 packets transmitted, 5 packets received, 0.0% packet loss\n"
            "round-trip min/avg/max/stddev = 10.000/20.500/30.000/2.000 ms\n"
        )
        self.assertEqual(tracker.parse_ping(output), {
            "packet_loss_percent": 0.0,
            "ping_latency_ms": 20.5,
        })

    def test_classify_thresholds(self):
        healthy = {
            "download_mbps": 100, "packet_loss_percent": 0,
            "idle_latency_ms": 50, "loaded_latency_ms": 200,
            "dns_ok": True, "https_ok": True,
        }
        self.assertEqual(tracker.classify(healthy), ("healthy", []))
        degraded = dict(healthy, packet_loss_percent=2, idle_latency_ms=101)
        status, reasons = tracker.classify(degraded)
        self.assertEqual(status, "degraded")
        self.assertEqual(len(reasons), 2)
        failed = dict(healthy, download_mbps=None, upload_mbps=None)
        self.assertEqual(tracker.classify(failed)[0], "failed")

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

    def test_collection_saves_partial_probe_failures(self):
        network_json = json.dumps({
            "dl_throughput": 80_000_000, "ul_throughput": 20_000_000,
            "base_rtt": 30, "responsiveness": 400, "interface_name": "en0",
        })

        def runner(command, timeout):
            executable = command[0]
            if executable.endswith("networkQuality"):
                return Completed(network_json)
            if executable.endswith("ping"):
                return Completed(
                    "5 packets transmitted, 5 received, 0.0% packet loss\n"
                    "round-trip min/avg/max/stddev = 8/10/12/1 ms\n"
                )
            if executable.endswith("dig"):
                return Completed(";; Query time: 22 msec\n")
            if executable.endswith("curl"):
                return Completed("", "offline", 7)
            raise AssertionError(command)

        result = tracker.collect_measurements(runner)
        self.assertEqual(result["status"], "degraded")
        self.assertFalse(result["https_ok"])
        self.assertEqual(result["download_mbps"], 80)
        self.assertTrue(result["errors"])

    def test_nonblocking_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock"
            with tracker.collection_lock(path):
                with self.assertRaises(RuntimeError):
                    with tracker.collection_lock(path):
                        pass

    def test_plists_are_structurally_valid_and_local(self):
        plists = tracker.agent_plists("/tmp/speed_tracker.py", "/usr/bin/python3")
        collect = plists[tracker.COLLECT_LABEL]
        server = plists[tracker.SERVER_LABEL]
        self.assertEqual(collect["StartCalendarInterval"], {"Minute": 0})
        self.assertTrue(collect["RunAtLoad"])
        self.assertEqual(collect["ProgramArguments"][-1], "--publish")
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
            result["errors"] = ["private diagnostic"]
            tracker.insert_run(connection, result)
            output = Path(directory) / "site"
            payload = tracker.build_static_site(connection, output)
            published = payload["runs"][0]
            self.assertNotIn("interface_name", published)
            self.assertNotIn("errors", published)
            self.assertEqual((output / "CNAME").read_text(), "net.noventayocho.work\n")
            self.assertTrue((output / ".nojekyll").exists())


def sample_result():
    return {
        "started_at": "2026-06-25T12:00:00+00:00",
        "completed_at": "2026-06-25T12:00:10+00:00",
        "duration_seconds": 10,
        "status": "healthy",
        "download_mbps": 100,
        "upload_mbps": 25,
        "idle_latency_ms": 30,
        "loaded_latency_ms": 150,
        "responsiveness_rpm": 400,
        "packet_loss_percent": 0,
        "ping_latency_ms": 20,
        "dns_time_ms": 15,
        "dns_ok": True,
        "https_time_ms": 100,
        "https_ok": True,
        "interface_name": "en0",
        "degraded_reasons": [],
        "errors": [],
    }


if __name__ == "__main__":
    unittest.main()
