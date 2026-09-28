"""Focused checks for model configuration in Windows setup scripts."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
PROJECT_ROOT = Path(__file__).parents[1]


@unittest.skipUnless(POWERSHELL, "PowerShell is required for setup script checks.")
class SetupScriptTests(unittest.TestCase):
    """Exercise setup helpers without installing software or downloading models."""

    def run_helpers(self, script_name: str, expressions: list[str]) -> list[list[object]]:
        """Load only function definitions and return JSON-encoded expression results."""
        script_path = PROJECT_ROOT / script_name
        command = """
$ScriptPath = $env:FLUX_TEST_SCRIPT_PATH
$Expression = $env:FLUX_TEST_EXPRESSION
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $ScriptPath, [ref]$tokens, [ref]$errors
)
if ($errors.Count) { throw $errors[0].Message }
$ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$result = [object[]]@(Invoke-Expression $Expression)
ConvertTo-Json -Compress -InputObject ([pscustomobject]@{ value = $result })
"""
        environment = dict(os.environ)
        environment["FLUX_TEST_SCRIPT_PATH"] = str(script_path)
        results = []
        for expression in expressions:
            environment["FLUX_TEST_EXPRESSION"] = expression
            completed = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
            )
            results.append(json.loads(completed.stdout)["value"])
        return results

    def test_model_lists_are_optional_ordered_and_validated(self) -> None:
        """Cover unconfigured, configured, and failed-validation model paths."""
        for script_name in ("setup.ps1", "setup-remote.ps1", "setup-host.ps1"):
            with self.subTest(script=script_name):
                results = self.run_helpers(
                    script_name,
                    [
                        "ConvertTo-ModelList ''",
                        "ConvertTo-ModelList 'second,first,second'",
                        "try { ConvertTo-ModelList ', ,' | Out-Null; 'accepted' } catch { 'rejected' }",
                    ],
                )
                self.assertEqual(results[0], [])
                self.assertEqual(results[1], ["second", "first"])
                self.assertEqual(results[2], ["rejected"])

    def test_rerun_updates_models_without_rewriting_other_settings(self) -> None:
        """Keep unrelated dotenv content while replacing an explicit model list."""
        results = self.run_helpers(
            "setup.ps1",
            [
                "Set-DotEnvValue \"HARNESS_OLLAMA_HOST=http://host`nHARNESS_MODELS=old\" 'HARNESS_MODELS' 'new,second'"
            ],
        )
        self.assertIn("HARNESS_OLLAMA_HOST=http://host", results[0][0])
        self.assertEqual(results[0][0].count("HARNESS_MODELS="), 1)
        self.assertIn("HARNESS_MODELS=new,second", results[0][0])

        host_results = self.run_helpers(
            "setup-host.ps1",
            ["Get-ModelSetupValue '' $false @{ HARNESS_MODELS = 'old,second' }"],
        )
        self.assertEqual(host_results[0], ["old,second"])

    def test_explicit_blank_overrides_existing_model_configuration(self) -> None:
        """Allow setup callers to deliberately return to the unconfigured state."""
        for script_name in ("setup.ps1", "setup-remote.ps1", "setup-host.ps1"):
            with self.subTest(script=script_name):
                results = self.run_helpers(
                    script_name,
                    ["Get-ModelSetupValue '' $true @{ HARNESS_MODELS = 'old' }"],
                )
                self.assertEqual(results[0], [""])

    def test_local_setup_accepts_only_loopback_ollama_urls(self) -> None:
        """Keep local setup on loopback and route remote users to its own script."""
        results = self.run_helpers(
            "setup.ps1",
            [
                "Get-LocalOllamaBaseUrl @{}",
                "Get-LocalOllamaBaseUrl @{ HARNESS_OLLAMA_HOST = 'http://localhost:11434/' }",
                "try { Get-LocalOllamaBaseUrl @{ HARNESS_OLLAMA_HOST = 'http://10.0.0.2:11434' } } catch { $_.Exception.Message }",
                "try { Get-LocalOllamaBaseUrl @{ HARNESS_OLLAMA_HOST = 'not-a-url' } } catch { $_.Exception.Message }",
            ],
        )
        self.assertEqual(results[0], ["http://127.0.0.1:11434"])
        self.assertEqual(results[1], ["http://localhost:11434"])
        self.assertIn("setup-remote.ps1", results[2][0])
        self.assertIn("not a valid URL", results[3][0])

    def test_local_setup_preserves_unrelated_dotenv_settings(self) -> None:
        """Update local setup values without replacing other user configuration."""
        results = self.run_helpers(
            "setup.ps1",
            [
                "Set-DotEnvValue \"HARNESS_TEMPERATURE=0.2`nHARNESS_OLLAMA_HOST=http://localhost:9999\" 'HARNESS_OLLAMA_HOST' 'http://127.0.0.1:11434'"
            ],
        )
        self.assertIn("HARNESS_TEMPERATURE=0.2", results[0][0])
        self.assertEqual(results[0][0].count("HARNESS_OLLAMA_HOST="), 1)
        self.assertIn("HARNESS_OLLAMA_HOST=http://127.0.0.1:11434", results[0][0])


if __name__ == "__main__":
    unittest.main()
