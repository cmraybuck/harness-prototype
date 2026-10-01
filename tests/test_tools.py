"""Registration and dispatch checks for model-facing tools."""

import os
import subprocess
import tempfile
import unittest
from inspect import Parameter, signature
from pathlib import Path
from unittest.mock import patch

from harness_prototype.approval import ToolCategory
from harness_prototype.tools import (
    ACTIVATE_CAPABILITIES_SCHEMA,
    AVAILABLE_TOOLS,
    CAPABILITY_GROUPS,
    MODEL_TOOLS,
    TOOL_CATEGORIES,
    TOOL_SPECS,
    TOOL_TARGET_RESOLVERS,
    describe_tool_activity,
    execute_tool,
    model_tools_for,
)


class ToolDispatchTests(unittest.TestCase):
    """Cover registry membership and dispatch-boundary validation."""

    def test_expected_tools_are_registered(self) -> None:
        """Expose exactly the supported model-facing tool set."""
        self.assertEqual(
            set(AVAILABLE_TOOLS),
            {
                "get_location", "list_files", "find_files", "search_project",
                "read_file", "inspect_file", "git_status", "git_diff", "apply_file_changes",
                "run_tests", "run_checks", "run_command",
                "install_dependencies", "start_service", "stop_service",
                "detect_project_technologies",
                "fetch_documentation",
                "create_directory",
            },
        )

    def test_all_current_tools_have_explicit_risk_categories(self) -> None:
        """Assign every capability to its separately configurable risk policy."""
        self.assertEqual(set(TOOL_CATEGORIES), set(AVAILABLE_TOOLS))
        self.assertEqual(TOOL_CATEGORIES["apply_file_changes"], ToolCategory.WRITE)
        self.assertEqual(TOOL_CATEGORIES["create_directory"], ToolCategory.WRITE)
        self.assertEqual(TOOL_CATEGORIES["run_tests"], ToolCategory.EXECUTION)
        self.assertEqual(TOOL_CATEGORIES["run_checks"], ToolCategory.EXECUTION)
        self.assertEqual(TOOL_CATEGORIES["run_command"], ToolCategory.EXECUTION)
        self.assertEqual(
            TOOL_CATEGORIES["install_dependencies"], ToolCategory.EXECUTION
        )
        self.assertEqual(TOOL_CATEGORIES["start_service"], ToolCategory.EXECUTION)
        self.assertEqual(TOOL_CATEGORIES["stop_service"], ToolCategory.EXECUTION)
        self.assertEqual(TOOL_CATEGORIES["fetch_documentation"], ToolCategory.NETWORK)
        read_only_tools = set(AVAILABLE_TOOLS) - {
            "create_directory", "apply_file_changes", "run_tests", "run_checks", "run_command",
            "install_dependencies", "start_service", "stop_service",
            "fetch_documentation",
        }
        self.assertEqual(
            {TOOL_CATEGORIES[name] for name in read_only_tools},
            {ToolCategory.READ_ONLY},
        )
        self.assertEqual(set(TOOL_SPECS), set(AVAILABLE_TOOLS))
        self.assertTrue(all(spec.activity_formatter for spec in TOOL_SPECS.values()))
        self.assertEqual(set(TOOL_TARGET_RESOLVERS), {"apply_file_changes", "create_directory"})
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
        run_command_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "run_command"
        )
        self.assertEqual(
            run_command_schema["function"]["parameters"]["required"], ["command"]
        )
        search_schema = next(
            tool for tool in MODEL_TOOLS
            if isinstance(tool, dict) and tool["function"]["name"] == "search_project"
        )
        search_parameters = search_schema["function"]["parameters"]
        self.assertEqual(search_parameters["required"], ["text"])
        self.assertNotIn("max_results", search_parameters["properties"])

    def test_tool_activity_describes_work_without_sensitive_payloads(self) -> None:
        """Give every tool useful dialogue while omitting write and command payloads."""
        self.assertEqual(
            describe_tool_activity("read_file", {"path": "AGENTS.md"}),
            "Reading AGENTS.md",
        )
        self.assertEqual(
            describe_tool_activity("find_files", {"pattern": "README*", "root": "src"}),
            "Looking for README* under src",
        )
        changes = describe_tool_activity("apply_file_changes", {
            "changes": [{"action": "replace", "path": "main.py", "content": "secret"}],
        })
        self.assertIn("replace main.py", changes)
        self.assertNotIn("secret", changes)
        command = describe_tool_activity(
            "run_command", {"command": ["git", "show", "secret-revision"]},
        )
        self.assertEqual(command, "Running git with 2 arguments")
        self.assertNotIn("secret-revision", command)

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
                self.assertTrue(spec.capability_groups)
                self.assertLessEqual(set(spec.capability_groups), set(CAPABILITY_GROUPS))
                model_tool = model_tools[name]
                description = model_tool["function"]["description"]
                parameters = model_tool["function"]["parameters"]
                required_names = list(parameters.get("required", ()))
                self.assertTrue(description)
                self.assertNotIn("Required parameters", description)
                self.assertNotIn("Safety category", description)
                self.assertNotIn("Material limits", description)
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

    def test_active_model_tools_are_derived_from_registry_groups(self) -> None:
        """Expose the activator always and only schemas in selected groups."""
        bootstrap = model_tools_for(set())
        inspection = model_tools_for({"workspace_inspection"})
        self.assertEqual(bootstrap, [ACTIVATE_CAPABILITIES_SCHEMA])
        names = {tool["function"]["name"] for tool in inspection}
        expected = {
            name for name, spec in TOOL_SPECS.items()
            if "workspace_inspection" in spec.capability_groups
        }
        self.assertEqual(names, {"activate_capabilities", *expected})
        combined = model_tools_for({"filesystem_navigation", "workspace_inspection"})
        combined_names = [tool["function"]["name"] for tool in combined]
        self.assertEqual(len(combined_names), len(set(combined_names)))

    def test_composed_tool_schemas_do_not_mutate_the_precomputed_cache(self) -> None:
        """Reuse generated schemas safely without rebuilding them each round."""
        first = model_tools_for({"documentation"})
        first[-1]["function"]["description"] = "changed"
        second = model_tools_for({"documentation"})
        self.assertNotEqual(second[-1]["function"]["description"], "changed")

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
            structured_sample = root / "sample.json"
            structured_sample.write_text('{"needle": true}\n', encoding="utf-8")
            subprocess.run(
                ["git", "init"], cwd=root, capture_output=True, check=True,
            )
            original_directory = Path.cwd()
            try:
                os.chdir(root)
                successful_arguments = {
                    "create_directory": {"path": "new-folder"},
                    "get_location": {"location": "working_directory"},
                    "list_files": {"path": temporary},
                    "find_files": {"pattern": "sample.txt", "root": temporary},
                    "search_project": {"text": "needle"},
                    "read_file": {"path": str(sample)},
                    "inspect_file": {"path": str(structured_sample)},
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
                successful_arguments["run_command"] = {"command": ["git", "status"]}
                successful_arguments["install_dependencies"] = {}
                successful_arguments["start_service"] = {"name": "web"}
                successful_arguments["stop_service"] = {"handle": "service-handle"}
                successful_arguments["fetch_documentation"] = {"url": "https://docs.example.com"}
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
                def fake_run_command(command):
                    """Return a deterministic successful ad hoc command result."""
                    return {
                        "ok": True, "error": None, "complete": True,
                        "truncated": False,
                    }
                def fake_install_dependencies(path="."):
                    """Return a deterministic successful dependency result."""
                    return {
                        "ok": True, "error": None, "complete": True,
                        "truncated": False,
                    }
                def fake_start_service(name):
                    """Return a deterministic successful service-start result."""
                    return {
                        "ok": True, "error": None, "complete": True,
                        "truncated": False,
                    }
                def fake_stop_service(handle):
                    """Return a deterministic successful service-stop result."""
                    return {
                        "ok": True, "error": None, "complete": True,
                        "truncated": False,
                    }
                def fake_fetch_documentation(url):
                    """Return a deterministic successful documentation result."""
                    return {
                        "ok": True, "error": None, "complete": True,
                        "truncated": False,
                    }
                with patch.dict(AVAILABLE_TOOLS, {
                    "run_tests": fake_run_tests,
                    "run_checks": fake_run_checks,
                    "run_command": fake_run_command,
                    "install_dependencies": fake_install_dependencies,
                    "start_service": fake_start_service,
                    "stop_service": fake_stop_service,
                    "fetch_documentation": fake_fetch_documentation,
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
