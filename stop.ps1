[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-OllamaModelWorkers {
    <# Find Ollama's model workers, including workers whose parent already exited. #>
    Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" |
        Where-Object {
            # CIM can briefly retain a worker after termination; confirm it is alive.
            $currentProcess = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
            if ($null -eq $currentProcess -or $currentProcess.HasExited -or
                $currentProcess.ProcessName -ne "llama-server") {
                return $false
            }
            if ([string]::IsNullOrWhiteSpace($_.ExecutablePath)) {
                throw "Cannot identify a model worker's executable path. Check permissions and retry."
            }
            $_.ExecutablePath -match '[\\/]ollama[\\/]'
        }
}

function Stop-OllamaProcess {
    <# Stop one discovered process and wait for exit, tolerating an exit race. #>
    param([int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return }
    try {
        $process | Stop-Process -Force
        Wait-Process -InputObject $process -Timeout 5 -ErrorAction Stop
    } catch {
        # A server can finish shutting down a worker before we stop or wait for it.
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) { throw }
    }
}

$ollamaProcesses = @(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue)
if ($ollamaProcesses.Count -gt 0) {
    Write-Host "Stopping local Ollama processes..."
    foreach ($process in $ollamaProcesses) {
        Stop-OllamaProcess -ProcessId $process.Id
    }
}

# Stop launchers first so they cannot start another worker during cleanup.
$modelWorkers = @(Get-OllamaModelWorkers)
if ($modelWorkers.Count -gt 0) {
    Write-Host "Stopping Ollama model workers..."
    foreach ($worker in $modelWorkers) {
        Stop-OllamaProcess -ProcessId $worker.ProcessId
    }
}

$remainingProcesses = @(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue |
    Where-Object { -not $_.HasExited })
$remainingWorkers = @(Get-OllamaModelWorkers)
if ($remainingProcesses.Count -gt 0 -or $remainingWorkers.Count -gt 0) {
    $remainingIds = @($remainingProcesses | ForEach-Object { $_.Id }) +
        @($remainingWorkers | ForEach-Object { $_.ProcessId })
    throw "Ollama processes or model workers are still running (PIDs: $($remainingIds -join ', ')). Retry after they finish shutting down."
}

if ($ollamaProcesses.Count -eq 0 -and $modelWorkers.Count -eq 0) {
    Write-Host "Ollama and its model workers are not running."
} else {
    Write-Host "Local Ollama and its model workers are stopped."
}
