"""Tests for bounded project technology detection."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.project_detection import detect_project_technologies


class ProjectDetectionTests(unittest.TestCase):
    """Cover supported, mixed, inconclusive, invalid, and bounded scans."""

    def test_detects_mixed_projects_with_evidence_and_scripts(self) -> None:
        """Report Python, Node, .NET, and Rust projects in one repository."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "python").mkdir()
            (root / "python" / "pyproject.toml").write_text(
                '[project]\nname = "sample"\ndependencies = ["pytest"]\n'
                '[project.scripts]\nserve = "sample:main"\n',
                encoding="utf-8",
            )
            (root / "python" / "uv.lock").write_text("", encoding="utf-8")
            (root / "web").mkdir()
            (root / "web" / "package.json").write_text(
                json.dumps({
                    "packageManager": "pnpm@10",
                    "devDependencies": {"vitest": "1", "typescript": "5"},
                    "scripts": {"test": "vitest"},
                }),
                encoding="utf-8",
            )
            (root / "dotnet").mkdir()
            (root / "dotnet" / "sample.csproj").write_text(
                '<Project><ItemGroup><PackageReference Include="xunit" />'
                '</ItemGroup><Target Name="Verify" /></Project>',
                encoding="utf-8",
            )
            (root / "rust").mkdir()
            (root / "rust" / "Cargo.toml").write_text(
                '[package]\nname = "sample"\nversion = "0.1.0"\n', encoding="utf-8",
            )

            result = detect_project_technologies(temporary)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "detected")
        self.assertEqual(result["manifest_count"], 4)
        languages = {
            item["name"]
            for detection in result["detections"]
            for item in detection["languages"]
        }
        self.assertEqual(
            languages, {"Python", "JavaScript", "TypeScript", "C#", "Rust"},
        )
        evidence_items = (
            item
            for detection in result["detections"]
            for field in ("languages", "package_managers", "test_frameworks", "scripts")
            for item in detection[field]
        )
        self.assertTrue(all(item["evidence"] for item in evidence_items))

    def test_returns_explicit_inconclusive_result(self) -> None:
        """Explain when a directory has no supported manifests."""
        with tempfile.TemporaryDirectory() as temporary:
            result = detect_project_technologies(temporary)

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "inconclusive")
        self.assertTrue(result["reason"])
        self.assertEqual(result["detections"], [])

    def test_reports_malformed_manifest_without_executing_it(self) -> None:
        """Keep parse failures structured and mark the scan incomplete."""
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary, "package.json").write_text("not json", encoding="utf-8")
            result = detect_project_technologies(temporary)

        self.assertTrue(result["ok"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(result["error_count"], 1)

    def test_bounds_manifest_results(self) -> None:
        """Stop explicitly when the manifest-result limit is reached."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(2):
                project = root / str(index)
                project.mkdir()
                (project / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
            with patch("harness_prototype.project_detection.MAX_MANIFESTS", 1):
                result = detect_project_technologies(temporary)

        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["stop_reason"], "manifest_limit")
        self.assertEqual(len(result["detections"]), 1)

    def test_rejects_invalid_paths(self) -> None:
        """Return common structured errors for invalid directory inputs."""
        self.assertFalse(detect_project_technologies("")["ok"])
        with tempfile.TemporaryDirectory() as temporary:
            file_path = Path(temporary, "file.txt")
            file_path.write_text("text", encoding="utf-8")
            self.assertFalse(detect_project_technologies(str(file_path))["ok"])


if __name__ == "__main__":
    unittest.main()
