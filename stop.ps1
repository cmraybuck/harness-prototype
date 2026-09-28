[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ollamaProcesses = @(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue)
if ($ollamaProcesses.Count -eq 0) {
    Write-Host "Ollama is not running."
    return
}

Write-Host "Stopping local Ollama processes..."
$ollamaProcesses | Stop-Process -Force
Write-Host "Local Ollama is stopped."
