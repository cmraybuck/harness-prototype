[CmdletBinding()]
param(
    [string]$OllamaHost,
    [string]$Model,
    [string]$Models,
    [string]$TailscaleProfile,
    [switch]$Launch
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
    <# Parse a comma-separated model list, preserving order and removing duplicates. #>
    param([string]$Value)
    $result = @($Value.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)
    if ($result.Count -eq 0) { throw "HARNESS_MODELS must contain at least one model identifier." }
    return $result
}

function Get-OllamaBaseUrl {
    <# Normalize and validate an Ollama HTTP base URL. #>
    param([string]$Value)
    $candidate = $Value.Trim().TrimEnd("/")
    try { $uri = [System.Uri]$candidate } catch { throw "HARNESS_OLLAMA_HOST is not a valid URL." }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -notin @("http", "https") -or
        -not $uri.Host -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne "/") {
        throw "HARNESS_OLLAMA_HOST must be an HTTP(S) base URL without credentials or an API path."
    }
    return $candidate
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

function Set-DotEnvValue {
    <# Replace or append one setting while preserving other local settings. #>
    param([string]$Content, [string]$Name, [string]$Value)
    $line = "${Name}=${Value}"
    $pattern = "(?m)^" + [regex]::Escape($Name) + "=.*$"
    if ($Content -match $pattern) { return [regex]::Replace($Content, $pattern, $line) }
    return $Content.TrimEnd() + [Environment]::NewLine + $line + [Environment]::NewLine
}

$projectRoot = $PSScriptRoot
$envExamplePath = Join-Path $projectRoot ".env.example"
$envPath = Join-Path $projectRoot ".env"
$settingsPath = if (Test-Path -LiteralPath $envPath) { $envPath } else { $envExamplePath }
$dotEnv = Import-DotEnv $settingsPath
$baseUrl = Get-OllamaBaseUrl (Get-SetupValue $OllamaHost "HARNESS_OLLAMA_HOST" $dotEnv "http://127.0.0.1:11434")
$selectedModel = Get-SetupValue $Model "HARNESS_MODEL" $dotEnv "devstral-small-2:latest"
$modelListValue = Get-SetupValue $Models "HARNESS_MODELS" $dotEnv "devstral-small-2:latest,qwen3:8b"
$selectedModels = @(ConvertTo-ModelList $modelListValue)
if ($selectedModel -notin $selectedModels) { $selectedModels = @($selectedModel) + $selectedModels }
$selectedProfile = Get-SetupValue $TailscaleProfile "HARNESS_TAILSCALE_PROFILE" $dotEnv ""
$hostName = ([System.Uri]$baseUrl).Host

Install-RequiredCommand "tailscale" "Tailscale.Tailscale" "Tailscale"
Install-RequiredCommand "uv" "astral-sh.uv" "uv"
Write-Host "Checking Tailscale connection..."
Connect-Tailscale
Select-TailscaleProfile $selectedProfile
Connect-Tailscale

if ($hostName -notin @("127.0.0.1", "localhost", "::1")) {
    & tailscale ping --timeout=10s $hostName | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Tailscale could not reach '$hostName'. Check the selected tailnet and host access policy." }
}

Write-Host "Checking Ollama API at $baseUrl..."
try { $tags = Invoke-RestMethod -Uri "$baseUrl/api/tags" -Method Get -TimeoutSec 15 }
catch { throw "The Ollama API is not reachable at $baseUrl. $($_.Exception.Message)" }
$availableModels = @($tags.models | ForEach-Object { $_.name })
foreach ($requiredModel in $selectedModels) {
    if ($requiredModel -notin $availableModels) {
        $displayModels = if ($availableModels.Count -gt 0) { $availableModels -join ", " } else { "none" }
        throw "Model '$requiredModel' is not installed on the Ollama host. Available models: $displayModels"
    }
}

if (Test-Path -LiteralPath $envPath) {
    $envContent = [System.IO.File]::ReadAllText($envPath)
    Write-Host "Updating setup values in existing .env..."
} else {
    $envContent = [System.IO.File]::ReadAllText($envExamplePath)
    Write-Host "Creating .env from .env.example..."
}
$envContent = Set-DotEnvValue $envContent "HARNESS_OLLAMA_HOST" $baseUrl
$envContent = Set-DotEnvValue $envContent "HARNESS_MODEL" $selectedModel
$envContent = Set-DotEnvValue $envContent "HARNESS_MODELS" ($selectedModels -join ",")
if ($selectedProfile) { $envContent = Set-DotEnvValue $envContent "HARNESS_TAILSCALE_PROFILE" $selectedProfile }
$utf8WithoutBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllText($envPath, $envContent, $utf8WithoutBom)

Write-Host "Installing locked project dependencies..."
Push-Location $projectRoot
try {
    & uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Python 3.12." }
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv could not create the project environment. Review the output above." }
    Write-Host "Setup complete. Start the harness with: uv run harness-prototype"
    if ($Launch) {
        & uv run harness-prototype
        if ($LASTEXITCODE -ne 0) { throw "The harness exited with code $LASTEXITCODE." }
    }
} finally {
    Pop-Location
}
