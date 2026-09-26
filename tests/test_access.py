"""Session workspace authorization checks."""

import tempfile
import unittest
from pathlib import Path

from harness_prototype.access import AccessChoice, WorkspaceAccess


class WorkspaceAccessTests(unittest.TestCase):
    """Cover default, granted, denied, and full-access decisions."""

    def setUp(self) -> None:
        """Create separate workspace and outside trees for each test."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / "workspace"
        self.outside = self.root / "outside"
        self.workspace.mkdir()
        self.outside.mkdir()
        self.access = WorkspaceAccess(self.workspace)

    def test_workspace_and_descendants_need_no_prompt(self) -> None:
        """Allow the startup directory subtree automatically."""
        prompted = []
        result = self.access.authorize_tool(
            "list_files",
            {"path": str(self.workspace / "nested")},
            lambda path: prompted.append(path) or AccessChoice.DENY,
        )
        self.assertIsNone(result)
        self.assertEqual(prompted, [])

    def test_directory_grant_covers_descendants(self) -> None:
        """Reuse one outside-directory grant without another prompt."""
        prompted = []

        def grant(path: Path) -> AccessChoice:
            """Record and grant the requested directory."""
            prompted.append(path)
            return AccessChoice.GRANT_DIRECTORY

        self.assertIsNone(self.access.authorize_tool("find_files", {"root": str(self.outside)}, grant))
        self.assertIsNone(self.access.authorize_tool(
            "read_file",
            {"path": str(self.outside / "nested" / "file.txt")},
            grant,
        ))
        self.assertEqual(prompted, [self.outside])

    def test_denial_is_remembered_and_cannot_be_bypassed_by_parent(self) -> None:
        """Reject later child or parent requests without prompting again."""
        first = self.access.authorize_tool(
            "list_files",
            {"path": str(self.outside)},
            lambda path: AccessChoice.DENY,
        )
        prompt_count = 0

        def unexpected_prompt(path: Path) -> AccessChoice:
            """Count any prompt that should have been suppressed."""
            nonlocal prompt_count
            prompt_count += 1
            return AccessChoice.GRANT_DIRECTORY

        child = self.access.authorize_tool(
            "list_files",
            {"path": str(self.outside / "nested")},
            unexpected_prompt,
        )
        parent = self.access.authorize_tool(
            "find_files",
            {"root": str(self.root)},
            unexpected_prompt,
        )
        self.assertEqual(first["status"], "access_denied")
        self.assertEqual(child["status"], "access_denied")
        self.assertEqual(parent["status"], "access_denied")
        self.assertEqual(prompt_count, 0)

    def test_full_access_suppresses_later_prompts(self) -> None:
        """Allow every later target after an explicit full-session grant."""
        self.assertIsNone(self.access.authorize_tool(
            "list_files",
            {"path": str(self.outside)},
            lambda path: AccessChoice.GRANT_ALL,
        ))
        self.assertIsNone(self.access.authorize_tool(
            "list_files",
            {"path": str(self.root / "another")},
            lambda path: self.fail("full access should suppress prompts"),
        ))

    def test_location_discovery_does_not_request_access(self) -> None:
        """Permit path discovery while leaving later file use guarded."""
        result = self.access.authorize_tool(
            "get_location",
            {"location": "desktop"},
            lambda path: self.fail("location discovery should not prompt"),
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
