"""Session workspace authorization checks."""

import tempfile
import unittest
from pathlib import Path

from harness_prototype.access import AccessChoice, WorkspaceAccess
from harness_prototype.files import file_change_target_directories


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

    def test_file_changes_prompt_for_each_outside_target_directory(self) -> None:
        """Authorize every structured change target before writes can dispatch."""
        second_outside = self.root / "second-outside"
        second_outside.mkdir()
        prompted = []

        def grant(path: Path) -> AccessChoice:
            """Record and grant each requested target directory."""
            prompted.append(path)
            return AccessChoice.GRANT_DIRECTORY

        result = self.access.authorize_tool(
            "apply_file_changes",
            {
                "base_path": ".",
                "changes": [
                    {
                        "path": str(self.outside / "first.txt"),
                        "action": "create",
                        "content": "first",
                    },
                    {
                        "path": str(second_outside / "second.txt"),
                        "action": "create",
                        "content": "second",
                    },
                ],
            },
            grant,
            file_change_target_directories,
        )
        self.assertIsNone(result)
        self.assertEqual(prompted, [self.outside, second_outside])

    def test_file_changes_use_explicit_base_path_for_relative_target(self) -> None:
        """Prompt for base_path rather than silently resolving in the workspace."""
        prompted = []
        result = self.access.authorize_tool(
            "apply_file_changes",
            {
                "base_path": str(self.outside),
                "changes": [
                    {"path": "selected.txt", "action": "create", "content": "text"},
                ],
            },
            lambda path: prompted.append(path) or AccessChoice.GRANT_DIRECTORY,
            file_change_target_directories,
        )
        self.assertIsNone(result)
        self.assertEqual(prompted, [self.outside])

    def test_file_change_denial_prevents_outside_dispatch(self) -> None:
        """Return access_denied when an outside change target is not granted."""
        result = self.access.authorize_tool(
            "apply_file_changes",
            {
                "base_path": ".",
                "changes": [{
                    "path": str(self.outside / "denied.txt"),
                    "action": "create",
                    "content": "nope",
                }],
            },
            lambda path: AccessChoice.DENY,
            file_change_target_directories,
        )
        self.assertEqual(result["status"], "access_denied")

    def test_file_changes_resolve_linked_parent_before_authorizing(self) -> None:
        """Prompt for a linked target's canonical outside directory."""
        link = self.workspace / "linked"
        try:
            link.symlink_to(self.outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Directory symlinks are unavailable: {error}")
        prompted = []
        result = self.access.authorize_tool(
            "apply_file_changes",
            {
                "base_path": str(self.workspace),
                "changes": [
                    {"path": "linked/file.txt", "action": "create", "content": "text"},
                ],
            },
            lambda path: prompted.append(path) or AccessChoice.DENY,
            file_change_target_directories,
        )
        self.assertEqual(result["status"], "access_denied")
        self.assertEqual(prompted, [self.outside])


if __name__ == "__main__":
    unittest.main()
