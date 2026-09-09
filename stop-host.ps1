[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ollamaProcesses = @(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue)
if ($ollamaProcesses.Count -gt 0) {
    Write-Host "Stopping Ollama..."
    $ollamaProcesses | Stop-Process -Force
} else {
    Write-Host "Ollama is not running."
}

if (Get-Command tailscale -ErrorAction SilentlyContinue) {
    Write-Host "Disconnecting Tailscale..."
    & tailscale down | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Tailscale could not disconnect." }
} else {
    Write-Warning "Tailscale was not found, so it could not be disconnected."
}

Write-Host "Ollama is stopped and Tailscale is disconnected."
