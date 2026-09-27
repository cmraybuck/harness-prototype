"""Constrained test-runner checks using simulated subprocesses."""

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness_prototype.config import CheckConfig
from harness_prototype.execution import (
    install_dependencies,
    run_checks,
    run_command,
    run_tests,
)


class TestExecutionTests(unittest.TestCase):
    """Cover test selection, outcomes, limits, and workspace confinement."""

    def setUp(self) -> None:
        """Enter a disposable active workspace for every execution scenario."""
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        self.original_directory = Path.cwd()
        os.chdir(self.workspace)

    def tearDown(self) -> None:
        """Restore the process directory before deleting the disposable workspace."""
        os.chdir(self.original_directory)
        self.temporary.cleanup()

    @staticmethod
    def _process_factory(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        """Build a Popen replacement that writes captured output and exits."""
        process = Mock(pid=1234, returncode=returncode)
        process.wait.return_value = returncode

        def launch(command, **kwargs):
            """Return a simulated child with independent stdout and stderr pipes."""
            process.stdout = io.BytesIO(stdout)
            process.stderr = io.BytesIO(stderr)
            process.command = command
            process.options = kwargs
            return process

        return process, launch

    def test_full_and_targeted_commands_run_without_a_shell(self) -> None:
        """Use the configured templates and expand selectors as separate arguments."""
        process, launch = self._process_factory(0, stdout=b"passed\n")
        with patch("harness_prototype.execution.TEST_COMMAND", ("runner", "all")), patch(
            "harness_prototype.execution.TARGETED_TEST_COMMAND",
            ("runner", "before", "{targets}", "after"),
        ), patch("harness_prototype.execution.subprocess.Popen", side_effect=launch):
            full = run_tests()
            targeted = run_tests(["tests.test_one", "tests/test_two.py::case"])
        self.assertEqual(full["status"], "passed")
        self.assertEqual(targeted["status"], "passed")
        self.assertEqual(
            process.command,
            ["runner", "before", "tests.test_one", "tests/test_two.py::case", "after"],
        )
        self.assertEqual(process.options["cwd"], self.workspace.resolve())
        self.assertFalse(process.options["shell"])
        self.assertEqual(process.options["stdin"], subprocess.DEVNULL)
        self.assertEqual(process.options["stdout"], subprocess.PIPE)
        self.assertEqual(process.options["stderr"], subprocess.PIPE)

    def test_nonzero_exit_is_an_authoritative_test_failure(self) -> None:
        """Distinguish found test failures from failure to execute the tool."""
        _, launch = self._process_factory(2, stderr=b"assertion failed\n")
        with patch("harness_prototype.execution.subprocess.Popen", side_effect=launch):
            result = run_tests()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("assertion failed", result["stderr"])

    def test_timeout_terminates_process_and_preserves_output(self) -> None:
        """Return a timeout result after terminating the managed process group."""
        process, launch = self._process_factory(1, stdout=b"partial\n")
        process.wait.side_effect = subprocess.TimeoutExpired("runner", 1)

        def terminate(timed_out_process) -> None:
            """Simulate process-group termination and its resulting exit code."""
            self.assertIs(timed_out_process, process)
            process.returncode = -9

        with patch("harness_prototype.execution.TEST_TIMEOUT_SECONDS", 1), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ), patch("harness_prototype.execution._terminate_process", side_effect=terminate) as stop:
            result = run_tests()
        stop.assert_called_once_with(process)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["exit_code"], -9)
        self.assertIn("partial", result["stdout"])

    def test_launch_error_has_stable_empty_capture_fields(self) -> None:
        """Report a missing configured executable separately from a failed test."""
        with patch(
            "harness_prototype.execution.subprocess.Popen",
            side_effect=FileNotFoundError("missing runner"),
        ):
            result = run_tests()
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "launch_error")
        self.assertIsNone(result["exit_code"])
        self.assertEqual((result["stdout"], result["stderr"]), ("", ""))

    def test_large_output_keeps_bounded_head_and_tail(self) -> None:
        """Bound captured output while retaining explicit final status and useful tail text."""
        output = b"head-" + (b"x" * 100) + b"-tail"
        _, launch = self._process_factory(0, stdout=output)
        with patch("harness_prototype.execution.TEST_OUTPUT_LIMIT", 50), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ):
            result = run_tests()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["output_truncated"])
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 50)
        self.assertTrue(result["stdout"].endswith("-tail"))

    def test_invalid_targets_never_launch_a_process(self) -> None:
        """Reject options, malformed selectors, and paths outside the workspace."""
        invalid_targets = [[], ["-k"], ["../outside/test_sample.py"], [True]]
        with patch("harness_prototype.execution.subprocess.Popen") as launch:
            for targets in invalid_targets:
                with self.subTest(targets=targets):
                    result = run_tests(targets)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "invalid_target")
        launch.assert_not_called()

    def test_configured_check_and_target_run_without_a_shell(self) -> None:
        """Select only exact configured commands and reuse bounded execution."""
        process, launch = self._process_factory(0, stdout=b"clean\n")
        checks = {
            "lint": CheckConfig(
                ("runner", "lint", "."),
                {"source": ("runner", "lint", "src")},
            )
        }
        with patch("harness_prototype.execution.CHECKS", checks), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ):
            result = run_checks("lint", "source")
        self.assertEqual(result["status"], "passed")
        self.assertEqual(process.command, ["runner", "lint", "src"])
        self.assertFalse(process.options["shell"])
        self.assertEqual(process.options["cwd"], self.workspace.resolve())

    def test_unknown_check_and_target_never_launch_a_process(self) -> None:
        """Reject every name absent from the configured allowlist."""
        checks = {"lint": CheckConfig(("runner", "lint"), {})}
        with patch("harness_prototype.execution.CHECKS", checks), patch(
            "harness_prototype.execution.subprocess.Popen"
        ) as launch:
            unknown_check = run_checks("build")
            unknown_target = run_checks("lint", "source")
        self.assertEqual(unknown_check["status"], "unknown_check")
        self.assertEqual(unknown_target["status"], "invalid_target")
        launch.assert_not_called()

    def test_check_failure_and_timeout_are_recoverable(self) -> None:
        """Preserve check output and distinguish nonzero completion from timeout."""
        checks = {"lint": CheckConfig(("runner", "lint"), {})}
        process, launch = self._process_factory(2, stderr=b"lint failed\n")
        with patch("harness_prototype.execution.CHECKS", checks), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ):
            failed = run_checks("lint")
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("lint failed", failed["stderr"])

        process.wait.side_effect = subprocess.TimeoutExpired("runner", 1)
        with patch("harness_prototype.execution.CHECKS", checks), patch(
            "harness_prototype.execution.TEST_TIMEOUT_SECONDS", 1
        ), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ), patch("harness_prototype.execution._terminate_process"):
            timed_out = run_checks("lint")
        self.assertFalse(timed_out["ok"])
        self.assertEqual(timed_out["status"], "timed_out")

    def test_allowed_command_runs_in_workspace_without_a_shell(self) -> None:
        """Execute an allowed argument array with shared process bounds."""
        process, launch = self._process_factory(0, stdout=b"status output\n")
        with patch(
            "harness_prototype.execution.COMMAND_ALLOWED_EXECUTABLES", ("git",)
        ), patch("harness_prototype.execution.subprocess.Popen", side_effect=launch):
            result = run_command(["git", "status", "--short"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(process.command, ["git", "status", "--short"])
        self.assertEqual(process.options["cwd"], self.workspace.resolve())
        self.assertFalse(process.options["shell"])
        self.assertEqual(process.options["stdin"], subprocess.DEVNULL)

    def test_disallowed_commands_never_launch(self) -> None:
        """Reject paths, shells, unknown executables, and malformed arguments."""
        commands = [
            ["../runner"], ["powershell", "-Command", "Write-Output hi"],
            ["unknown-tool"], [], ["git", True],
        ]
        with patch(
            "harness_prototype.execution.COMMAND_ALLOWED_EXECUTABLES",
            ("git", "powershell"),
        ), patch("harness_prototype.execution.subprocess.Popen") as launch:
            for command in commands:
                with self.subTest(command=command):
                    result = run_command(command)
                    self.assertFalse(result["ok"])
                    self.assertIn(
                        result["status"], {"invalid_command", "disallowed_command"}
                    )
        launch.assert_not_called()

    def test_command_failure_timeout_and_truncation_are_explicit(self) -> None:
        """Reuse accurate failure, timeout, and bounded-output result fields."""
        process, launch = self._process_factory(3, stderr=b"failure\n")
        with patch(
            "harness_prototype.execution.COMMAND_ALLOWED_EXECUTABLES", ("git",)
        ), patch("harness_prototype.execution.subprocess.Popen", side_effect=launch):
            failed = run_command(["git", "status"])
        self.assertTrue(failed["ok"])
        self.assertEqual((failed["status"], failed["exit_code"]), ("failed", 3))

        process.wait.side_effect = subprocess.TimeoutExpired("git", 1)
        with patch(
            "harness_prototype.execution.COMMAND_ALLOWED_EXECUTABLES", ("git",)
        ), patch("harness_prototype.execution.TEST_TIMEOUT_SECONDS", 1), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=launch
        ), patch("harness_prototype.execution._terminate_process"):
            timed_out = run_command(["git", "status"])
        self.assertFalse(timed_out["ok"])
        self.assertEqual(timed_out["status"], "timed_out")

        _, verbose_launch = self._process_factory(0, stdout=b"x" * 100)
        with patch(
            "harness_prototype.execution.COMMAND_ALLOWED_EXECUTABLES", ("git",)
        ), patch("harness_prototype.execution.TEST_OUTPUT_LIMIT", 40), patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=verbose_launch
        ):
            truncated = run_command(["git", "status"])
        self.assertTrue(truncated["output_truncated"])
        self.assertFalse(truncated["complete"])

    def test_install_dependencies_prefers_locked_manifests(self) -> None:
        """Generate lock-preserving commands without invoking real package managers."""
        cases = [
            ({"package.json": "{}", "package-lock.json": "{}"}, ["npm", "ci"], "npm"),
            (
                {"pyproject.toml": "[project]\nname='sample'\n", "uv.lock": ""},
                ["uv", "sync", "--locked"],
                "uv",
            ),
            (
                {"Cargo.toml": "[package]\nname='sample'\n", "Cargo.lock": ""},
                ["cargo", "fetch", "--locked"],
                "cargo",
            ),
            (
                {"sample.csproj": "<Project />", "packages.lock.json": "{}"},
                ["dotnet", "restore", "sample.csproj", "--locked-mode"],
                "dotnet",
            ),
        ]
        for files, expected_command, expected_manager in cases:
            with self.subTest(manager=expected_manager):
                for path in self.workspace.iterdir():
                    path.unlink()
                for name, content in files.items():
                    (self.workspace / name).write_text(content, encoding="utf-8")
                process, launch = self._process_factory(0, stdout=b"installed\n")
                with patch("harness_prototype.execution.subprocess.Popen", side_effect=launch):
                    result = install_dependencies()
                self.assertEqual(process.command, expected_command)
                self.assertEqual(result["manager"], expected_manager)
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(process.options["cwd"], self.workspace.resolve())
                self.assertFalse(process.options["shell"])

    def test_install_dependencies_rejects_unsafe_or_ambiguous_projects(self) -> None:
        """Reject workspace escapes, manager conflicts, and system pip installs."""
        with patch("harness_prototype.execution.subprocess.Popen") as launch:
            outside = install_dependencies("..")
            (self.workspace / "package.json").write_text("{}", encoding="utf-8")
            (self.workspace / "package-lock.json").write_text("{}", encoding="utf-8")
            (self.workspace / "yarn.lock").write_text("", encoding="utf-8")
            ambiguous = install_dependencies()
            for path in self.workspace.iterdir():
                path.unlink()
            (self.workspace / "requirements.txt").write_text("example\n", encoding="utf-8")
            with patch("harness_prototype.execution.sys.prefix", "base"), patch(
                "harness_prototype.execution.sys.base_prefix", "base"
            ), patch.dict(os.environ, {}, clear=True):
                system_pip = install_dependencies()
        self.assertEqual(outside["status"], "invalid_project")
        self.assertEqual(ambiguous["status"], "ambiguous_project")
        self.assertEqual(system_pip["status"], "system_install_denied")
        launch.assert_not_called()

    def test_install_dependencies_reports_failure_timeout_and_modified_files(self) -> None:
        """Preserve structured process outcomes and report changed lockfiles."""
        (self.workspace / "package.json").write_text("{}", encoding="utf-8")
        lockfile = self.workspace / "package-lock.json"
        lockfile.write_text("before", encoding="utf-8")
        process, base_launch = self._process_factory(2, stderr=b"install failed\n")

        def changing_launch(command, **kwargs):
            """Simulate a package manager changing its lockfile before exiting."""
            lockfile.write_text("after content", encoding="utf-8")
            return base_launch(command, **kwargs)

        with patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=changing_launch
        ):
            failed = install_dependencies()
        self.assertTrue(failed["ok"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 2)
        self.assertEqual(failed["modified_files"], ["package-lock.json"])

        process.wait.side_effect = subprocess.TimeoutExpired("npm", 1)
        with patch(
            "harness_prototype.execution.subprocess.Popen", side_effect=base_launch
        ), patch("harness_prototype.execution.TEST_TIMEOUT_SECONDS", 1), patch(
            "harness_prototype.execution._terminate_process"
        ):
            timed_out = install_dependencies()
        self.assertFalse(timed_out["ok"])
        self.assertEqual(timed_out["status"], "timed_out")


if __name__ == "__main__":
    unittest.main()
