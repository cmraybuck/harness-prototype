"""Deterministic checks for local skill discovery and activation."""

import json
from pathlib import Path
import tempfile
import unittest

from harness_prototype.skills import MAX_RESOURCE_BYTES, SkillState, discover_skills


class SkillTests(unittest.TestCase):
    """Cover valid skills, trust boundaries, duplicates, and requirements."""

    def setUp(self) -> None:
        """Create isolated application and workspace roots."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.application = self.root / "application"
        self.bundled = self.root / "bundled"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        """Remove isolated skill fixtures."""
        self.temporary.cleanup()

    def write_skill(
        self,
        root: Path,
        identifier: str = "review.focused",
        **updates: object,
    ) -> Path:
        """Write one valid version-one skill fixture with optional overrides."""
        directory = root / identifier
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "instructions.md").write_text("Review focused changes.", encoding="utf-8")
        manifest = {
            "version": 1,
            "id": identifier,
            "name": "Focused review",
            "description": "Review only affected code.",
            "instructions": "instructions.md",
            "resources": [],
            "requires": {"capabilities": ["workspace_inspection"], "tools": ["read_file"]},
        }
        manifest.update(updates)
        path = directory / "skill.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    def test_discovers_and_activates_valid_skill_with_resource(self) -> None:
        """Load bounded context and expose stable metadata without terminal objects."""
        path = self.write_skill(self.application, resources=["guide.txt"])
        (path.parent / "guide.txt").write_text("Project conventions.", encoding="utf-8")
        catalog = discover_skills(self.workspace, self.application, self.bundled)
        state = SkillState(catalog)

        result = state.activate(
            ["review.focused"], {"workspace_inspection"}, {"read_file"}
        )

        self.assertTrue(result["ok"])
        self.assertIn("Review focused changes.", state.context())
        self.assertIn("Resource guide.txt:\nProject conventions.", state.context())
        self.assertEqual(catalog.metadata()[0]["source"], "application")

    def test_provenance_is_bounded_metadata_and_detects_content_changes(self) -> None:
        """Persist source identity and a fingerprint without copying loaded instructions."""
        path = self.write_skill(self.application)
        catalog = discover_skills(self.workspace, self.application, self.bundled)
        state = SkillState(catalog)
        state.activate(["review.focused"], {"workspace_inspection"}, {"read_file"})
        original = state.provenance()
        self.assertEqual(original[0]["manifest_path"], str(path.resolve()))
        self.assertEqual(original[0]["format_version"], 1)
        self.assertEqual(len(original[0]["sha256"]), 64)
        self.assertNotIn("Review focused changes.", json.dumps(original))
        (path.parent / "instructions.md").write_text("Changed instructions.", encoding="utf-8")
        reloaded = SkillState(discover_skills(self.workspace, self.application, self.bundled))
        reloaded.activate(["review.focused"], {"workspace_inspection"}, {"read_file"})
        self.assertNotEqual(original[0]["sha256"], reloaded.provenance()[0]["sha256"])

    def test_rejects_traversal_and_unsupported_version(self) -> None:
        """Report unsafe paths and unsupported formats instead of loading them."""
        self.write_skill(self.application, instructions="../outside.md")
        self.write_skill(
            self.workspace / ".flux" / "skills", "future.skill", version=2
        )

        catalog = discover_skills(self.workspace, self.application, self.bundled)

        self.assertEqual(catalog.skills, {})
        errors = " ".join(item.error for item in catalog.diagnostics)
        self.assertIn("cannot leave", errors)
        self.assertIn("Unsupported skill format version", errors)

    def test_duplicate_identifier_rejects_both_definitions(self) -> None:
        """Never choose silently between application and workspace duplicates."""
        self.write_skill(self.application)
        self.write_skill(self.workspace / ".flux" / "skills")

        catalog = discover_skills(self.workspace, self.application, self.bundled)

        self.assertNotIn("review.focused", catalog.skills)
        self.assertEqual(len(catalog.diagnostics), 2)

    def test_rejects_oversized_resource(self) -> None:
        """Reject a skill instead of partially loading an oversized resource."""
        path = self.write_skill(self.application, resources=["large.txt"])
        (path.parent / "large.txt").write_text(
            "x" * (MAX_RESOURCE_BYTES + 1), encoding="utf-8"
        )

        catalog = discover_skills(self.workspace, self.application, self.bundled)

        self.assertEqual(catalog.skills, {})
        self.assertIn("exceeds", catalog.diagnostics[0].error)

    def test_activation_is_atomic_when_requirements_are_missing(self) -> None:
        """Keep the prior selection when a requested set cannot be activated."""
        self.write_skill(self.application)
        catalog = discover_skills(self.workspace, self.application, self.bundled)
        state = SkillState(catalog)
        state.active_ids = ("existing",)

        result = state.activate(["review.focused"], set(), {"read_file"})

        self.assertFalse(result["ok"])
        self.assertIn("workspace_inspection", result["error"])
        self.assertEqual(state.active_ids, ("existing",))

    def test_conversation_states_do_not_share_activation(self) -> None:
        """Keep independent selections when conversations share one catalog."""
        self.write_skill(self.application)
        catalog = discover_skills(self.workspace, self.application, self.bundled)
        first = SkillState(catalog)
        second = SkillState(catalog)

        first.activate(
            ["review.focused"], {"workspace_inspection"}, {"read_file"}
        )

        self.assertEqual(first.active_ids, ("review.focused",))
        self.assertEqual(second.active_ids, ())

    def test_packaged_pirate_skill_is_discoverable_and_activatable(self) -> None:
        """Ship one harmless skill that exercises the installed discovery path."""
        catalog = discover_skills(self.workspace, self.application)
        state = SkillState(catalog)

        result = state.activate(["pirate"], set(), set())

        self.assertTrue(result["ok"])
        self.assertEqual(catalog.skills["pirate"].source, "bundled")
        self.assertIn("playful pirate voice", state.context())

    def test_bundled_defaults_load_and_activate_without_requirements(self) -> None:
        """Load all shipped workflows and keep activation explicit and isolated."""
        catalog = discover_skills(self.workspace, self.application)
        identifiers = {
            "code-review", "debugging", "documentation", "implementation-plan", "pirate"
        }
        self.assertEqual(set(catalog.skills), identifiers)
        self.assertEqual(catalog.diagnostics, ())
        state = SkillState(catalog)
        other = SkillState(catalog)
        self.assertEqual(state.context(), "")

        result = state.activate(sorted(identifiers), set(), set())

        self.assertTrue(result["ok"])
        self.assertEqual(state.active_ids, tuple(sorted(identifiers)))
        for identifier in sorted(identifiers):
            with self.subTest(identifier=identifier):
                skill = catalog.skills[identifier]
                self.assertEqual(skill.source, "bundled")
                self.assertIn(skill.instructions, state.context())
        self.assertEqual(other.context(), "")
        self.assertTrue(state.activate([], set(), set())["ok"])
        self.assertEqual(state.context(), "")


if __name__ == "__main__":
    unittest.main()
