"""Constrained test-runner checks using simulated subprocesses."""

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from harness_prototype.execution import run_tests


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


if __name__ == "__main__":
    unittest.main()
