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

    def test_local_stop_script_does_not_change_tailscale_state(self) -> None:
        """Keep local shutdown limited to Ollama processes."""
        script_path = PROJECT_ROOT / "stop.ps1"
        content = script_path.read_text(encoding="utf-8")
        self.assertIn('Get-Process -Name "ollama*"', content)
        self.assertIn("Stop-Process -Force", content)
        self.assertNotIn("tailscale", content.casefold())
        environment = dict(os.environ)
        environment["FLUX_TEST_SCRIPT_PATH"] = str(script_path)
        completed = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$errors=$null; [System.Management.Automation.Language.Parser]::ParseFile($env:FLUX_TEST_SCRIPT_PATH,[ref]$null,[ref]$errors) | Out-Null; if ($errors.Count) { throw $errors[0].Message }",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_local_stop_script_cleans_up_only_ollama_workers(self) -> None:
        """Exercise shutdown with mocked cmdlets so no real processes are stopped."""
        launcher = {"Id": 10, "ProcessId": 10, "ProcessName": "ollama", "ExecutablePath": "C:/Apps/Ollama/ollama.exe", "HasExited": False}
        worker = {"Id": 20, "ProcessId": 20, "ProcessName": "llama-server", "ExecutablePath": r"C:\Apps\Ollama\lib\ollama\llama-server.exe", "HasExited": False}
        unrelated = {"Id": 30, "ProcessId": 30, "ProcessName": "llama-server", "ExecutablePath": "C:/Apps/llama.cpp/llama-server.exe", "HasExited": False}
        cases = [
            {"processes": [worker], "fail_id": 0, "exit_race": False},
            {"processes": [launcher, worker, unrelated], "fail_id": 0, "exit_race": False},
            {"processes": [], "fail_id": 0, "exit_race": False},
            {"processes": [unrelated], "fail_id": 0, "exit_race": False},
            {"processes": [worker], "fail_id": 20, "exit_race": False},
            {"processes": [worker], "fail_id": 0, "exit_race": True},
            {"processes": [{**worker, "ExecutablePath": None}], "fail_id": 0, "exit_race": False},
            {"processes": [worker], "fail_id": 0, "exit_race": False, "stale_cim": True},
        ]
        command = r"""
# Shadow every process cmdlet before invoking the real shutdown script.
function Get-Process {
    <# Return only the synthetic process table. #>
    param([string]$Name, [int]$Id, $ErrorAction)
    if ($Name) {
        $global:FluxStopFixtureProcesses | Where-Object { $_.ProcessName -like $Name }
    } else {
        $global:FluxStopFixtureProcesses | Where-Object { $_.Id -eq $Id }
    }
}
function Get-CimInstance {
    <# Return synthetic workers and optionally simulate a concurrent exit. #>
    param($ClassName, $Filter)
    $workers = @($global:FluxStopFixtureProcesses | Where-Object { $_.ProcessName -eq 'llama-server' })
    if ($global:FluxStopFixtureStaleCim) { $workers = $global:FluxStopFixtureOriginalWorkers }
    if ($global:FluxStopFixtureExitRace) {
        $global:FluxStopFixtureProcesses = @($global:FluxStopFixtureProcesses | Where-Object { $_.ProcessName -ne 'llama-server' })
        $global:FluxStopFixtureExitRace = $false
    }
    $workers
}
function Stop-Process {
    <# Record fake terminations instead of touching the operating system. #>
    [CmdletBinding()]
    param([Parameter(ValueFromPipeline=$true)]$InputObject, [switch]$Force)
    process {
        if ($InputObject.Id -eq $global:FluxStopFixtureFailId) { throw 'Access denied in fixture.' }
        $global:FluxStopFixtureStopped += $InputObject.Id
        $global:FluxStopFixtureProcesses = @($global:FluxStopFixtureProcesses | Where-Object { $_.Id -ne $InputObject.Id })
    }
}
function Wait-Process {
    <# Fake workers exit immediately; never wait on an actual operating-system PID. #>
    [CmdletBinding()]
    param($InputObject, [int]$Timeout)
}
if ((Get-Command Stop-Process).CommandType -ne 'Function') { throw 'Mock shutdown is required.' }
$results = @(foreach ($case in (ConvertFrom-Json $env:FLUX_STOP_FIXTURES)) {
    $global:FluxStopFixtureProcesses = @($case.processes)
    $global:FluxStopFixtureFailId = $case.fail_id
    $global:FluxStopFixtureExitRace = $case.exit_race
    $global:FluxStopFixtureStopped = @()
    $global:FluxStopFixtureStaleCim = $case.PSObject.Properties.Name -contains 'stale_cim'
    $global:FluxStopFixtureOriginalWorkers = @($case.processes | Where-Object { $_.ProcessName -eq 'llama-server' })
    $failure = $null
    try { & $env:FLUX_TEST_SCRIPT_PATH 6>$null | Out-Null } catch { $failure = $_.Exception.Message }
    [pscustomobject]@{ stopped = @($global:FluxStopFixtureStopped); error = $failure }
})
ConvertTo-Json -Compress -Depth 5 -InputObject $results
"""
        environment = dict(os.environ)
        environment["FLUX_TEST_SCRIPT_PATH"] = str(PROJECT_ROOT / "stop.ps1")
        environment["FLUX_STOP_FIXTURES"] = json.dumps(cases)
        completed = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            check=True, capture_output=True, text=True, encoding="utf-8", env=environment,
        )
        results = json.loads(completed.stdout)
        self.assertEqual([result["stopped"] for result in results], [[20], [10, 20], [], [], [], [], [], [20]])
        self.assertIn("Access denied", results[4]["error"])
        self.assertIn("executable path", results[6]["error"])
        for index in (0, 1, 2, 3, 5, 7):
            self.assertIsNone(results[index]["error"])


if __name__ == "__main__":
    unittest.main()
