"""Configuration checks isolated from the developer's personal environment."""

import os
import tempfile
import unittest
from pathlib import Path

from harness_prototype.approval import ApprovalPolicy, ToolCategory
from harness_prototype.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_defaults_without_file(self):
        """Run with portable defaults when no dotenv file or overrides are supplied."""
        settings = load_settings(None, {})
        self.assertEqual(settings.host, "http://127.0.0.1:11434")
        self.assertEqual(settings.model, "devstral-small-2:latest")
        self.assertEqual(
            settings.models,
            ("devstral-small-2:latest", "qwen3:8b"),
        )
        self.assertEqual((settings.temperature, settings.context_length, settings.max_tool_rounds), (0.15, 4096, 8))
        self.assertEqual(settings.log_directory, (Path(__file__).parents[1] / "work/logs").resolve())
        self.assertEqual(settings.log_redact_values, ())
        self.assertEqual(settings.approval_policies[ToolCategory.READ_ONLY], ApprovalPolicy.ALWAYS_ALLOW)
        for category in set(ToolCategory) - {ToolCategory.READ_ONLY}:
            self.assertEqual(settings.approval_policies[category], ApprovalPolicy.ASK_FIRST)

    def test_dotenv_and_environment_precedence(self):
        """Load quoted dotenv values and let explicit environment variables win."""
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / ".env"
            file.write_text(
                '# Local setup\nHARNESS_MODEL="another:latest"\n'
                'HARNESS_MODELS="another:latest,second:latest"\n'
                'HARNESS_CONTEXT_LENGTH=8192\nHARNESS_MAX_TOOL_ROUNDS=12\n'
                'HARNESS_TEMPERATURE=0.3\nHARNESS_OLLAMA_HOST=http://localhost:1234\n',
                encoding="utf-8",
            )
            settings = load_settings(file, {"HARNESS_MODEL": "override:latest"})
        self.assertEqual(settings.model, "override:latest")
        self.assertEqual(settings.models, ("override:latest", "another:latest", "second:latest"))
        self.assertEqual(settings.context_length, 8192)
        self.assertEqual(settings.max_tool_rounds, 12)
        self.assertEqual(settings.temperature, 0.3)
        self.assertEqual(settings.host, "http://localhost:1234")

    def test_missing_file_uses_defaults(self):
        """Treat an absent personal file as optional configuration."""
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(load_settings(Path(temp) / "missing", {}), load_settings(None, {}))

    def test_invalid_values(self):
        """Reject invalid numeric settings, blank models, and malformed server URLs."""
        cases = {
            "HARNESS_CONTEXT_LENGTH": ["0", "-1", "1.5", "true"],
            "HARNESS_MAX_TOOL_ROUNDS": ["0", "abc"],
            "HARNESS_TEMPERATURE": ["nan", "inf", "-0.1", "3"],
            "HARNESS_MODEL": ["", " "],
            "HARNESS_MODELS": ["", " ", ", ,"],
            "HARNESS_OLLAMA_HOST": ["localhost", "http://localhost/api/chat", "http://localhost:99999", "http://user:secret@localhost"],
        }
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    load_settings(None, {key: value})

    def test_file_loading_does_not_mutate_environment(self):
        """Keep dotenv values local to the settings loader."""
        before = dict(os.environ)
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / ".env"
            file.write_text("HARNESS_MODEL=test-model\n", encoding="utf-8")
            load_settings(file, {})
        self.assertEqual(dict(os.environ), before)

    def test_logging_configuration_resolves_path_and_redactions(self) -> None:
        """Resolve relative storage and load only explicitly named secret values."""
        settings = load_settings(None, {
            "HARNESS_LOG_DIRECTORY": "private/logs",
            "HARNESS_LOG_REDACT_VARIABLES": "API_TOKEN, PASSWORD",
            "API_TOKEN": "token-value",
            "PASSWORD": "password-value",
        })
        self.assertEqual(settings.log_directory, (Path(__file__).parents[1] / "private/logs").resolve())
        self.assertEqual(settings.log_redact_values, ("token-value", "password-value"))

    def test_logging_rejects_missing_redaction_variable(self) -> None:
        """Prevent a misspelled secret name from silently producing unsafe logs."""
        with self.assertRaisesRegex(ValueError, "MISSING_SECRET"):
            load_settings(None, {"HARNESS_LOG_REDACT_VARIABLES": "MISSING_SECRET"})

    def test_approval_policies_are_configurable_by_category(self) -> None:
        """Load independent policy overrides for read-only and future risk categories."""
        settings = load_settings(None, {
            "HARNESS_READ_ONLY_POLICY": "deny",
            "HARNESS_WRITE_POLICY": "always_allow",
        })
        self.assertEqual(settings.approval_policies[ToolCategory.READ_ONLY], ApprovalPolicy.DENY)
        self.assertEqual(settings.approval_policies[ToolCategory.WRITE], ApprovalPolicy.ALWAYS_ALLOW)

    def test_invalid_approval_policy_is_rejected(self) -> None:
        """Reject policy typos rather than silently selecting a permissive behavior."""
        with self.assertRaisesRegex(ValueError, "HARNESS_NETWORK_POLICY"):
            load_settings(None, {"HARNESS_NETWORK_POLICY": "sometimes"})


if __name__ == "__main__":
    unittest.main()
