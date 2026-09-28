[CmdletBinding()]
param(
    [string]$TailscaleProfile,
    [string]$Models,
    [string]$LocalOllamaHost,
    [Nullable[int]]$ServePort
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Import-DotEnv {
    <# Read simple NAME=value settings from a dotenv file. #>
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) { continue }
        $name, $value = $trimmed.Split("=", 2)
        $values[$name.Trim()] = $value.Trim().Trim('"').Trim("'")
    }
    return $values
}

function Get-SetupValue {
    <# Resolve a CLI override, process variable, dotenv value, then default. #>
    param([AllowEmptyString()][string]$Override, [string]$Name, [hashtable]$DotEnv, [AllowEmptyString()][string]$Default)
    if ($Override) { return $Override.Trim() }
    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if ($processValue) { return $processValue.Trim() }
    if ($DotEnv.ContainsKey($Name) -and $DotEnv[$Name]) { return $DotEnv[$Name].Trim() }
    return $Default
}

function ConvertTo-ModelList {
    <# Parse an optional model list, preserving order and removing duplicates. #>
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return @() }
    $result = @($Value.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
    if ($result.Count -eq 0) { throw "HARNESS_MODELS must contain at least one model identifier." }
    return $result
}

function Get-ModelSetupValue {
    <# Resolve an optional model setting while preserving an explicit blank value. #>
    param([AllowEmptyString()][string]$Override, [bool]$OverrideProvided, [hashtable]$DotEnv)
    if ($OverrideProvided) { return $Override.Trim() }
    $processValue = [Environment]::GetEnvironmentVariable("HARNESS_MODELS")
    if ($null -ne $processValue) { return $processValue.Trim() }
    if ($DotEnv.ContainsKey("HARNESS_MODELS")) { return $DotEnv["HARNESS_MODELS"].Trim() }
    return ""
}

function Update-ProcessPath {
    <# Add persisted PATH entries without discarding this session's entries. #>
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $pathEntries = @($env:Path, $machinePath, $userPath) |
        Where-Object { $_ } |
        ForEach-Object { $_ -split ";" } |
        Where-Object { $_ } |
        Select-Object -Unique
    $env:Path = $pathEntries -join ";"
}

function Install-RequiredCommand {
    <# Install a missing Windows command from the public winget repository. #>
    param(
        [string]$Command,
        [string]$PackageId,
        [string]$DisplayName
    )

    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    Update-ProcessPath
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$DisplayName is missing and Windows Package Manager (winget) is unavailable. Install $DisplayName manually and rerun this script."
    }

    Write-Host "Installing $DisplayName..."
    & winget install --id $PackageId -e --source winget --accept-package-agreements --accept-source-agreements | Out-Host
    $installExitCode = $LASTEXITCODE
    Update-ProcessPath
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    if ($installExitCode -ne 0) { throw "winget could not install $DisplayName, and '$Command' is not available." }
    throw "$DisplayName was installed, but '$Command' is not available yet. Open a new PowerShell window and rerun this script."
}

function Select-TailscaleProfile {
    <# Switch this computer to an optional previously authenticated Tailscale account. #>
    param([string]$Account)
    if (-not $Account) { return }
    Write-Host "Selecting this computer's Tailscale account $Account..."
    & tailscale switch $Account | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale could not switch to '$Account'. Sign in to that account, then rerun this script."
    }
}

function Connect-Tailscale {
    <# Connect interactively when the selected Tailscale profile is offline. #>
    & tailscale status | Out-Null
    if ($LASTEXITCODE -eq 0) { return }

    Write-Host "Tailscale is not connected; starting connection and sign-in if needed..."
    & tailscale up | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale could not connect. Complete any browser sign-in, then rerun this script."
    }

    & tailscale status | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Tailscale still is not connected after 'tailscale up'. Check the Windows Tailscale client and rerun this script."
    }
}

function Connect-Ollama {
    <# Start a hidden Ollama server when the configured loopback API is offline. #>
    param([string]$BaseUrl)

    try {
        Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 2 | Out-Null
        return
    } catch {
        Write-Host "Ollama is not running; starting its local API server..."
    }

    $ollamaCommand = (Get-Command ollama -ErrorAction Stop).Source
    $serverProcess = Start-Process -FilePath $ollamaCommand -ArgumentList "serve" -WindowStyle Hidden -PassThru
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 2 | Out-Null
            Write-Host "Ollama API is ready."
            return
        } catch {
            # The server may still be loading; retry after checking its process.
        }
        if ($serverProcess.HasExited) {
            throw "Ollama stopped before its API became ready (exit code $($serverProcess.ExitCode))."
        }
    }
    throw "Ollama did not become ready at $BaseUrl after repeated checks."
}

function Assert-BaseUrl {
    <# Validate the loopback Ollama base URL used as the private Serve target. #>
    param([string]$Value)
    $candidate = $Value.Trim().TrimEnd("/")
    try { $uri = [System.Uri]$candidate } catch { throw "HARNESS_OLLAMA_LOCAL_HOST is not a valid URL." }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne "http" -or
        $uri.Host -notin @("127.0.0.1", "localhost", "::1") -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne "/") {
        throw "HARNESS_OLLAMA_LOCAL_HOST must be a loopback HTTP base URL without credentials or an API path."
    }
    return $candidate
}

$projectRoot = $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"
$envExamplePath = Join-Path $projectRoot ".env.example"
$settingsPath = if (Test-Path -LiteralPath $envPath) { $envPath } else { $envExamplePath }
$dotEnv = Import-DotEnv $settingsPath
$selectedProfile = Get-SetupValue $TailscaleProfile "HARNESS_TAILSCALE_PROFILE" $dotEnv ""
$modelListValue = Get-ModelSetupValue $Models $PSBoundParameters.ContainsKey("Models") $dotEnv
$selectedModels = @(ConvertTo-ModelList $modelListValue)
$localBaseUrl = Assert-BaseUrl (Get-SetupValue $LocalOllamaHost "HARNESS_OLLAMA_LOCAL_HOST" $dotEnv "http://127.0.0.1:11434")
$portValue = if ($null -ne $ServePort) { [string]$ServePort } else { Get-SetupValue "" "HARNESS_TAILSCALE_SERVE_PORT" $dotEnv "11434" }
try { $configuredPort = [int]$portValue }
catch { throw "HARNESS_TAILSCALE_SERVE_PORT must be a whole number between 1 and 65535." }
if ($configuredPort -lt 1 -or $configuredPort -gt 65535) { throw "HARNESS_TAILSCALE_SERVE_PORT must be between 1 and 65535." }

Install-RequiredCommand "ollama" "Ollama.Ollama" "Ollama"
Install-RequiredCommand "tailscale" "Tailscale.Tailscale" "Tailscale"
Select-TailscaleProfile $selectedProfile
Write-Host "Checking Tailscale connection..."
Connect-Tailscale

Write-Host "Checking local Ollama API at $localBaseUrl..."
$env:OLLAMA_HOST = ([System.Uri]$localBaseUrl).Authority
Connect-Ollama $localBaseUrl

foreach ($requiredModel in $selectedModels) {
    Write-Host "Ensuring model '$requiredModel' is installed..."
    & ollama pull $requiredModel
    if ($LASTEXITCODE -ne 0) { throw "Ollama could not install model '$requiredModel'." }
}
if ($selectedModels.Count -eq 0) {
    Write-Host "No models configured; skipping model downloads. Set HARNESS_MODELS after selecting compatible Ollama models."
}

Write-Host "Forwarding private Tailscale port $configuredPort to $localBaseUrl..."
& tailscale serve --bg "--tcp=$configuredPort" "tcp://$(([System.Uri]$localBaseUrl).Authority)" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Tailscale could not create the Serve rule." }
& tailscale serve status | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Tailscale could not confirm the Serve rule." }

$status = & tailscale status --json | ConvertFrom-Json
$tailscaleIp = @($status.TailscaleIPs | Where-Object { $_ -match "\." }) | Select-Object -First 1
if (-not $tailscaleIp) { throw "Tailscale did not report an IPv4 address for this host." }
Write-Host "Host setup complete. Configure teammates with:"
Write-Host "  HARNESS_OLLAMA_HOST=http://${tailscaleIp}:$configuredPort"
Write-Host "Review the tailnet Grant separately so only approved users can reach TCP port $configuredPort."
