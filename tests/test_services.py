"""Managed background-service lifecycle checks using short-lived local processes."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from harness_prototype.config import ServiceConfig
from harness_prototype.services import start_service, stop_all_services, stop_service


class ServiceTests(unittest.TestCase):
    """Cover readiness, stable handles, duplicate starts, and safe cleanup."""

    def setUp(self) -> None:
        """Enter a disposable workspace and clear any prior managed processes."""
        stop_all_services()
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.original_directory = Path.cwd()
        os.chdir(self.workspace)

    def tearDown(self) -> None:
        """Stop test services and restore the original process directory."""
        stop_all_services()
        os.chdir(self.original_directory)
        self.temporary.cleanup()

    def test_start_reports_readiness_logs_and_duplicate_handle_then_stops(self) -> None:
        """Return one stable handle and bounded logs for a configured ready service."""
        config = ServiceConfig(
            (sys.executable, "-u", "-c", "import time; print('READY'); time.sleep(30)"),
            ".", "READY", 3,
        )
        with patch("harness_prototype.services.SERVICES", {"web": config}):
            started = start_service("web")
            duplicate = start_service("web")
            stopped = stop_service(started["handle"])
        self.assertTrue(started["ok"])
        self.assertEqual(started["status"], "ready")
        self.assertIn("READY", started["stdout"])
        self.assertEqual(duplicate["status"], "already_running")
        self.assertEqual(duplicate["handle"], started["handle"])
        self.assertEqual(stopped["status"], "stopped")

    def test_unknown_handle_and_service_cannot_affect_other_processes(self) -> None:
        """Reject unknown names and opaque handles without attempting termination."""
        self.assertEqual(start_service("missing")["status"], "unknown_service")
        self.assertEqual(stop_service("not-a-managed-handle")["status"], "unknown_handle")

    def test_early_exit_is_reported_as_startup_failure(self) -> None:
        """Return final logs when a configured process exits before readiness."""
        config = ServiceConfig(
            (sys.executable, "-u", "-c", "print('failed'); raise SystemExit(4)"),
            ".", "NEVER_READY", 3,
        )
        with patch("harness_prototype.services.SERVICES", {"broken": config}):
            result = start_service("broken")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "startup_failed")
        self.assertEqual(result["exit_code"], 4)
        self.assertIn("failed", result["stdout"])

    def test_cleanup_stops_all_started_services(self) -> None:
        """Invalidate every handle after session cleanup terminates its process."""
        config = ServiceConfig(
            (sys.executable, "-u", "-c", "import time; time.sleep(30)"),
            ".", None, 3,
        )
        with patch("harness_prototype.services.SERVICES", {"worker": config}):
            started = start_service("worker")
            stop_all_services()
        self.assertTrue(started["ok"])
        self.assertEqual(stop_service(started["handle"])["status"], "unknown_handle")


if __name__ == "__main__":
    unittest.main()
