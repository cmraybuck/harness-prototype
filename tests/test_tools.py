"""Registration and dispatch checks for model-facing tools."""

import json
import os
import subprocess
import tempfile
import unittest
from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import patch

from harness_prototype.approval import ToolCategory
from harness_prototype.tools import (
    AVAILABLE_TOOLS,
    MODEL_TOOLS,
    TOOL_CATEGORIES,
    TOOL_SPECS,
    TOOL_TARGET_RESOLVERS,
    execute_tool,
)


class ToolDispatchTests(unittest.TestCase):
    """Cover registry membership and dispatch-boundary validation."""

    def test_expected_tools_are_registered(self) -> None:
        """Expose exactly the supported model-facing tool set."""
        self.assertEqual(
            set(AVAILABLE_TOOLS),
            {
                "get_location", "list_files", "find_files", "search_project",
                "read_file", "git_status", "git_diff", "apply_file_changes",
                "run_tests", "run_checks",
                "detect_project_technologies",
            },
        )

    def test_all_current_tools_have_explicit_risk_categories(self) -> None:
        """Assign every capability to its separately configurable risk policy."""
        self.assertEqual(set(TOOL_CATEGORIES), set(AVAILABLE_TOOLS))
        self.assertEqual(TOOL_CATEGORIES["apply_file_changes"], ToolCategory.WRITE)
        self.assertEqual(TOOL_CATEGORIES["run_tests"], ToolCategory.EXECUTION)
        self.assertEqual(TOOL_CATEGORIES["run_checks"], ToolCategory.EXECUTION)
        read_only_tools = set(AVAILABLE_TOOLS) - {
            "apply_file_changes", "run_tests", "run_checks",
        }
        self.assertEqual(
            {TOOL_CATEGORIES[name] for name in read_only_tools},
            {ToolCategory.READ_ONLY},
        )
        self.assertEqual(set(TOOL_SPECS), set(AVAILABLE_TOOLS))
        self.assertEqual(set(TOOL_TARGET_RESOLVERS), {"apply_file_changes"})
        file_change_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "apply_file_changes"
        )
        change_items = file_change_schema["function"]["parameters"]["properties"]["changes"]["items"]
        self.assertEqual(
            change_items["properties"]["action"]["enum"],
            ["create", "overwrite", "append", "replace"],
        )
        run_tests_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "run_tests"
        )
        self.assertEqual(
            run_tests_schema["function"]["parameters"]["properties"]["targets"]["maxItems"],
            50,
        )
        run_checks_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "run_checks"
        )
        self.assertEqual(
            run_checks_schema["function"]["parameters"]["required"],
            ["name"],
        )
        search_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "search_project"
        )
        search_parameters = search_schema["function"]["parameters"]
        self.assertEqual(search_parameters["required"], ["text"])
        self.assertNotIn("max_results", search_parameters["properties"])

    def test_model_tools_track_registry_and_capability_metadata(self) -> None:
        """Generate model definitions from their executable specifications."""
        model_tools = {
            tool["function"]["name"]: tool
            for tool in MODEL_TOOLS
        }
        self.assertEqual(set(model_tools), set(TOOL_SPECS))
        for name, spec in TOOL_SPECS.items():
            with self.subTest(tool=name):
                self.assertTrue(spec.material_limits)
                model_tool = model_tools[name]
                description = model_tool["function"]["description"]
                parameters = model_tool["function"]["parameters"]
                required_names = list(parameters.get("required", ()))
                required_text = ", ".join(required_names) or "none"
                self.assertIn(
                    f"Required parameters (report exactly): {required_text}.",
                    description,
                )
                optional_parameters = []
                for parameter_name, property_schema in parameters[
                    "properties"
                ].items():
                    if parameter_name in required_names:
                        continue
                    default = json.dumps(
                        property_schema.get("default"), ensure_ascii=False,
                    )
                    optional_parameters.append(
                        f"{parameter_name} (default {default})"
                        if "default" in property_schema
                        else f"{parameter_name} (no default declared)"
                    )
                optional_text = ", ".join(optional_parameters) or "none"
                self.assertIn(
                    f"Optional parameters (report exactly): {optional_text}.",
                    description,
                )
                if spec.model_schema is not None:
                    self.assertEqual(
                        parameters,
                        spec.model_schema["function"]["parameters"],
                    )
                else:
                    function_signature = signature(spec.function)
                    self.assertEqual(
                        set(parameters["properties"]),
                        set(function_signature.parameters),
                    )
                    expected_required = [
                        parameter_name
                        for parameter_name, parameter
                        in function_signature.parameters.items()
                        if parameter.default is Parameter.empty
                    ]
                    self.assertEqual(
                        parameters.get("required", []), expected_required,
                    )
                    for parameter_name, parameter in (
                        function_signature.parameters.items()
                    ):
                        if parameter.default is not Parameter.empty:
                            self.assertEqual(
                                parameters["properties"][parameter_name]["default"],
                                parameter.default,
                            )
                self.assertIn(
                    f"Safety category: {spec.category.value}.", description,
                )
                self.assertIn("Material limits (report all verbatim):", description)
                for limit in spec.material_limits:
                    self.assertIn(limit, description)

    def test_rejects_unknown_tool_and_invalid_argument_shape(self) -> None:
        """Return structured errors for invalid dispatch requests."""
        self.assertFalse(execute_tool("unknown", {})["ok"])
        self.assertFalse(execute_tool("list_files", [])["ok"])

    def test_rejects_signature_mismatch(self) -> None:
        """Reject unexpected arguments before invoking a tool."""
        self.assertFalse(execute_tool("list_files", {"extra": True})["ok"])
        self.assertFalse(execute_tool(
            "apply_file_changes",
            {"changes": []},
        )["ok"])

    def assert_common_contract(self, result: dict, expected_ok: bool) -> None:
        """Check the status fields shared by every model-facing result."""
        self.assertEqual(result["ok"], expected_ok)
        self.assertIsInstance(result["complete"], bool)
        self.assertIsInstance(result["truncated"], bool)
        if expected_ok:
            self.assertIsNone(result["error"])
        else:
            self.assertIsInstance(result["error"], str)
            self.assertTrue(result["error"])
            self.assertFalse(result["complete"])
            self.assertFalse(result["truncated"])

    def test_all_registered_tools_follow_common_result_contract(self) -> None:
        """Require common status fields on successful and failed tool calls."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sample = root / "sample.txt"
            sample.write_text("needle\n", encoding="utf-8")
            subprocess.run(
                ["git", "init"], cwd=root, capture_output=True, check=True,
            )
            original_directory = Path.cwd()
            try:
                os.chdir(root)
                successful_arguments = {
                    "get_location": {"location": "working_directory"},
                    "list_files": {"path": temporary},
                    "find_files": {"pattern": "sample.txt", "root": temporary},
                    "search_project": {"text": "needle"},
                    "read_file": {"path": str(sample)},
                    "git_status": {},
                    "git_diff": {},
                    "detect_project_technologies": {"path": temporary},
                    "apply_file_changes": {
                        "base_path": ".",
                        "changes": [
                            {"path": "created.txt", "action": "create", "content": "created"},
                        ],
                    },
                }
                successful_arguments["run_tests"] = {}
                successful_arguments["run_checks"] = {"name": "lint"}
                def fake_run_tests(targets=None):
                    """Return a deterministic successful execution result."""
                    return {
                        "ok": True,
                        "error": None,
                        "complete": True,
                        "truncated": False,
                    }
                def fake_run_checks(name, target=None):
                    """Return a deterministic successful configured-check result."""
                    return {
                        "ok": True,
                        "error": None,
                        "complete": True,
                        "truncated": False,
                    }
                with patch.dict(AVAILABLE_TOOLS, {
                    "run_tests": fake_run_tests,
                    "run_checks": fake_run_checks,
                }):
                    for name in AVAILABLE_TOOLS:
                        with self.subTest(name=name, outcome="success"):
                            self.assert_common_contract(
                                execute_tool(name, successful_arguments[name]),
                                expected_ok=True,
                            )
                        with self.subTest(name=name, outcome="failure"):
                            self.assert_common_contract(
                                execute_tool(name, {"unexpected": True}),
                                expected_ok=False,
                            )
            finally:
                os.chdir(original_directory)

    def test_partial_result_contract_distinguishes_truncation(self) -> None:
        """Mark a bounded page successful, incomplete, and truncated."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first.txt").write_text("first", encoding="utf-8")
            (root / "second.txt").write_text("second", encoding="utf-8")
            result = execute_tool(
                "list_files",
                {"path": temporary, "max_entries": 1},
            )
        self.assert_common_contract(result, expected_ok=True)
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["next_offset"], 1)


if __name__ == "__main__":
    unittest.main()
