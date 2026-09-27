"""Checks for bounded structured-document and image inspection."""

import json
import tempfile
import unittest
from pathlib import Path

from docx import Document
from PIL import Image
from pypdf import PdfWriter

from harness_prototype.files import (
    MAX_STRUCTURED_FILE_BYTES,
    inspect_file,
)


class FileInspectorTests(unittest.TestCase):
    """Cover supported formats, diagnostics, paging, and resource limits."""

    def setUp(self) -> None:
        """Create an isolated directory for inspection samples."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_inspects_json_toml_and_yaml(self) -> None:
        """Parse every structured format into normalized JSON output."""
        samples = {
            "sample.json": ('{"name": "json", "items": [1, 2]}', "json"),
            "sample.toml": ('name = "toml"\nitems = [1, 2]\n', "toml"),
            "sample.yaml": ('name: yaml\nitems:\n  - 1\n  - 2\n', "yaml"),
            "alternate.yml": ('name: yml\n', "yml"),
        }
        for filename, (content, expected_name) in samples.items():
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_text(content, encoding="utf-8")
                result = inspect_file(str(path))
                self.assertTrue(result["ok"], result)
                self.assertEqual(json.loads(result["content"])["name"], expected_name)
                self.assertIn(result["format"], {"json", "toml", "yaml"})
                self.assertTrue(result["revision"].startswith("sha256:"))

    def test_pages_normalized_structured_output(self) -> None:
        """Provide a stable next-line continuation for rendered data."""
        path = self.root / "page.json"
        path.write_text(json.dumps({"items": list(range(20))}), encoding="utf-8")
        first = inspect_file(str(path), max_lines=4)
        second = inspect_file(str(path), start_line=first["next_line"], max_lines=4)
        self.assertTrue(first["truncated"])
        self.assertEqual(second["start_line"], first["next_line"])
        self.assertNotEqual(first["content"], second["content"])

    def test_reports_location_aware_parse_errors(self) -> None:
        """Return line and column details for malformed structured files."""
        samples = {
            "bad.json": '{\n  "missing": ]\n}',
            "bad.toml": 'value = [1,, 2]\n',
            "bad.yaml": 'value: [1,, 2]\n',
        }
        for filename, content in samples.items():
            with self.subTest(filename=filename):
                path = self.root / filename
                path.write_text(content, encoding="utf-8")
                result = inspect_file(str(path))
                self.assertFalse(result["ok"])
                self.assertIn("line", result)
                self.assertIn("column", result)

    def test_rejects_aliases_depth_and_file_size(self) -> None:
        """Enforce amplification, nesting, and input-size bounds explicitly."""
        alias = self.root / "alias.yaml"
        alias.write_text("base: &base [1, 2]\ncopy: *base\n", encoding="utf-8")
        self.assertIn("aliases", inspect_file(str(alias))["error"])

        deep = self.root / "deep.json"
        deep.write_text("[" * 34 + "0" + "]" * 34, encoding="utf-8")
        self.assertIn("depth", inspect_file(str(deep))["error"])

        large = self.root / "large.json"
        large.write_bytes(b" " * (MAX_STRUCTURED_FILE_BYTES + 1))
        self.assertIn("2 MiB", inspect_file(str(large))["error"])

    def test_extracts_pdf_and_docx_text(self) -> None:
        """Inspect bounded PDF pages and Word paragraphs and tables."""
        pdf_path = self.root / "sample.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        with pdf_path.open("wb") as destination:
            writer.write(destination)
        pdf = inspect_file(str(pdf_path))
        self.assertTrue(pdf["ok"], pdf)
        self.assertEqual(pdf["page_count"], 1)
        self.assertIn("Page 1", pdf["content"])

        docx_path = self.root / "sample.docx"
        document = Document()
        document.add_paragraph("First paragraph")
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "left"
        table.cell(0, 1).text = "right"
        document.save(docx_path)
        docx = inspect_file(str(docx_path))
        self.assertTrue(docx["ok"], docx)
        self.assertIn("First paragraph", docx["content"])
        self.assertIn("left | right", docx["content"])

    def test_validates_and_exposes_raster_images(self) -> None:
        """Return bounded metadata and a multimodal-forwarding marker for images."""
        path = self.root / "sample.png"
        Image.new("RGB", (3, 2), "red").save(path)
        result = inspect_file(str(path))
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["width"], result["height"]), (3, 2))
        self.assertEqual(result["image_format"], "PNG")
        self.assertTrue(result["image_for_model"])

    def test_rejects_unsupported_and_invalid_requests(self) -> None:
        """Reject unknown formats, bad images, and invalid paging arguments."""
        unknown = self.root / "sample.txt"
        unknown.write_text("hello", encoding="utf-8")
        broken = self.root / "broken.png"
        broken.write_bytes(b"not an image")
        self.assertFalse(inspect_file(str(unknown))["ok"])
        self.assertFalse(inspect_file(str(broken))["ok"])
        self.assertFalse(inspect_file(str(unknown), start_line=True)["ok"])


if __name__ == "__main__":
    unittest.main()
