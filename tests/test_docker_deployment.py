"""Focused checks for the optional Docker Compose deployment."""

import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import yaml

from harness_prototype.application_service import service_main, verify_ollama_connection
from harness_prototype.application_state import UnsupportedSchemaError


PROJECT_ROOT = Path(__file__).parents[1]


class DockerDeploymentTests(unittest.TestCase):
    """Validate deployment boundaries without requiring a Docker daemon."""

    def test_compose_persists_data_and_limits_host_access(self) -> None:
        """Keep state durable, publish to loopback, and default to the checkout workspace."""
        compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
        service = compose["services"]["flux"]
        self.assertEqual(service["ports"], ["127.0.0.1:${FLUX_PORT:-8765}:8765"])
        self.assertEqual(
            service["environment"]["HARNESS_STATE_DATABASE_PATH"],
            "/var/lib/flux/state.sqlite3",
        )
        self.assertEqual(service["environment"]["HARNESS_LOG_DIRECTORY"], "/var/lib/flux/logs")
        self.assertTrue(service["env_file"][0]["required"])
        self.assertEqual(service["volumes"][0]["source"], "flux-data")
        workspace = service["volumes"][1]
        self.assertEqual(workspace["source"], "${FLUX_WORKSPACE_PATH:-.}")
        self.assertFalse(workspace["bind"]["create_host_path"])
        self.assertEqual(set(compose["volumes"]), {"flux-data"})

    def test_image_runs_as_non_root_foreground_service(self) -> None:
        """Build a minimal image whose default process is the application service."""
        dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("USER flux", dockerfile)
        self.assertIn('"harness_prototype.application_service"', dockerfile)
        self.assertIn('"--workspace", "/workspace"', dockerfile)

    def test_unavailable_ollama_has_actionable_diagnostic(self) -> None:
        """Name the endpoint setting when the startup connectivity check fails."""
        client = Mock()
        client.list.side_effect = OSError("offline")
        with self.assertRaisesRegex(RuntimeError, "HARNESS_OLLAMA_HOST"):
            verify_ollama_connection(client)

    def test_incompatible_state_fails_service_startup(self) -> None:
        """Report an incompatible persisted schema without entering the serve loop."""
        error = UnsupportedSchemaError("State database schema version 999 is newer than 4.")
        stderr = io.StringIO()
        with (
            patch("harness_prototype.application_service.verify_ollama_connection"),
            patch("harness_prototype.application_service.DesktopServiceHost", side_effect=error),
            patch("sys.stderr", stderr),
        ):
            result = service_main(["--workspace", str(PROJECT_ROOT)])
        self.assertEqual(result, 1)
        self.assertIn("schema version 999", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
