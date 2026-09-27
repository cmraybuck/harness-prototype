"""Check focused, portable location discovery."""

import tempfile
import unittest
from inspect import signature
from pathlib import Path
from unittest.mock import patch

from harness_prototype.locations import application_data_directory, get_location
from harness_prototype.tools import execute_tool


class LocationTests(unittest.TestCase):
    """Cover location resolution and structured failures."""

    def test_core_locations_and_normalization(self):
        """Resolve cwd and home and normalize names."""
        self.assertEqual(get_location(" working_DIRECTORY ")["path"], str(Path.cwd().resolve()))
        self.assertEqual(get_location("HOME")["path"], str(Path.home().resolve()))

    def test_schema_avoids_ollama_name_parameter_collision(self):
        """Keep the model-facing argument clear of Ollama's Mistral parser collision."""
        self.assertEqual(list(signature(get_location).parameters), ["location"])

    def test_invalid_and_unsupported_names(self):
        """Reject invalid names with supported values."""
        for value in ("", "nope", None, 4):
            result = get_location(value)
            self.assertFalse(result["ok"])
            self.assertIn("supported", result)

    def test_windows_redirected_folder(self):
        """Resolve a redirected Windows folder without other lookups."""
        with tempfile.TemporaryDirectory() as temporary:
            redirected = Path(temporary)
            with patch("harness_prototype.locations._standard_folder_path", return_value=redirected):
                result = execute_tool("get_location", {"location": "desktop"})
        self.assertEqual(result["path"], str(redirected.resolve()))

    def test_one_failure_does_not_resolve_other_folders(self):
        """Resolve only the requested standard folder."""
        with patch("harness_prototype.locations._standard_folder_path", side_effect=OSError("unavailable")) as resolver:
            result = get_location("desktop")
        self.assertFalse(result["ok"])
        resolver.assert_called_once()

    def test_xdg_configured_folder(self):
        """Expand HOME in an XDG configured folder."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            target = home / "Cloud" / "Desktop"
            target.mkdir(parents=True)
            config = home / ".config"
            config.mkdir()
            (config / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/Cloud/Desktop"\n', encoding="utf-8")
            with patch("harness_prototype.locations.Path.home", return_value=home):
                result = get_location("desktop")
        self.assertEqual(result["path"], str(target.resolve()))

    def test_conventional_fallback_and_missing_directory(self):
        """Use existing conventional folders and reject missing ones."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "Music").mkdir()
            with patch("harness_prototype.locations.Path.home", return_value=home):
                self.assertTrue(get_location("music")["ok"])
                self.assertFalse(get_location("videos")["ok"])

    def test_application_data_directory_is_platform_specific(self) -> None:
        """Keep application-data discovery in the platform location adapter."""
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            local = home / "LocalData"
            with (
                patch("harness_prototype.locations.Path.home", return_value=home),
                patch("harness_prototype.locations.os.name", "nt"),
                patch.dict("harness_prototype.locations.os.environ", {"LOCALAPPDATA": str(local)}),
            ):
                self.assertEqual(application_data_directory(), local / "FLUX")


if __name__ == "__main__":
    unittest.main()
