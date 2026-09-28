[CmdletBinding()]
param(
    [string]$Models,
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
        throw "$DisplayName is missing and Windows Package Manager (winget) is unavailable. Install App Installer and $DisplayName manually, then rerun this script."
    }

    Write-Host "Installing $DisplayName..."
    & winget install --id $PackageId -e --source winget --accept-package-agreements --accept-source-agreements | Out-Host
    $installExitCode = $LASTEXITCODE
    Update-ProcessPath
    if (Get-Command $Command -ErrorAction SilentlyContinue) { return }
    if ($installExitCode -ne 0) {
        throw "winget could not install $DisplayName. Check the network and winget output above, install it manually if needed, then rerun this script."
    }
    throw "$DisplayName was installed, but '$Command' is not available yet. Open a new PowerShell window and rerun this script."
}

function Get-LocalOllamaBaseUrl {
    <# Validate the configured loopback Ollama endpoint for local setup. #>
    param([hashtable]$DotEnv)
    $configuredValue = if ($DotEnv.ContainsKey("HARNESS_OLLAMA_HOST")) { $DotEnv["HARNESS_OLLAMA_HOST"] } else { "" }
    if ([string]::IsNullOrWhiteSpace($configuredValue)) { return "http://127.0.0.1:11434" }
    $candidate = $configuredValue.Trim().TrimEnd("/")
    try { $uri = [System.Uri]$candidate } catch { throw "HARNESS_OLLAMA_HOST is not a valid URL." }
    if (-not $uri.IsAbsoluteUri) { throw "HARNESS_OLLAMA_HOST is not a valid URL." }
    if ($uri.Scheme -ne "http" -or
        $uri.Host -notin @("127.0.0.1", "localhost", "::1") -or
        $uri.UserInfo -or $uri.Query -or $uri.Fragment -or $uri.AbsolutePath -ne "/") {
        throw "Local setup requires HARNESS_OLLAMA_HOST to be a loopback HTTP base URL. Use setup-remote.ps1 for a remote Ollama host."
    }
    return $candidate
}

function Wait-ForOllama {
    <# Start Ollama when needed and wait for its loopback API to become ready. #>
    param([string]$BaseUrl)
    try {
        return Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 2
    } catch {
        Write-Host "Ollama is not running; starting its local API server..."
    }

    $ollamaCommand = (Get-Command ollama -ErrorAction Stop).Source
    $serverProcess = Start-Process -FilePath $ollamaCommand -ArgumentList "serve" -WindowStyle Hidden -PassThru
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $tags = Invoke-RestMethod -Uri "$BaseUrl/api/tags" -Method Get -TimeoutSec 2
            Write-Host "Ollama API is ready."
            return $tags
        } catch {
            # The local server may still be loading; retry after checking its process.
        }
        if ($serverProcess.HasExited) {
            throw "Ollama stopped before its API became ready (exit code $($serverProcess.ExitCode)). Start Ollama manually and rerun this script."
        }
    }
    throw "Ollama did not become ready at $BaseUrl. Start Ollama manually, check that port $(([System.Uri]$BaseUrl).Port) is available, and rerun this script."
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
$baseUrl = Get-LocalOllamaBaseUrl $dotEnv
$modelListValue = Get-ModelSetupValue $Models $PSBoundParameters.ContainsKey("Models") $dotEnv
$selectedModels = @(ConvertTo-ModelList $modelListValue)

Install-RequiredCommand "ollama" "Ollama.Ollama" "Ollama"
Install-RequiredCommand "uv" "astral-sh.uv" "uv"

Write-Host "Checking local Ollama API at $baseUrl..."
$env:OLLAMA_HOST = ([System.Uri]$baseUrl).Authority
$tags = Wait-ForOllama $baseUrl
$availableModels = @($tags.models | ForEach-Object { $_.name })
foreach ($requiredModel in $selectedModels) {
    if ($requiredModel -in $availableModels) {
        Write-Host "Model '$requiredModel' is already installed."
        continue
    }
    Write-Host "Installing configured model '$requiredModel'..."
    & ollama pull $requiredModel
    if ($LASTEXITCODE -ne 0) {
        throw "Ollama could not install model '$requiredModel'. Check the network and model identifier, then rerun this script."
    }
}
if ($selectedModels.Count -eq 0) {
    Write-Host "No models configured; skipping model downloads. Set HARNESS_MODELS in .env when you are ready to select one."
}

if (Test-Path -LiteralPath $envPath) {
    $envContent = [System.IO.File]::ReadAllText($envPath)
    Write-Host "Preserving existing .env settings..."
} else {
    $envContent = [System.IO.File]::ReadAllText($envExamplePath)
    Write-Host "Creating .env from .env.example..."
}
$envContent = Set-DotEnvValue $envContent "HARNESS_OLLAMA_HOST" $baseUrl
$envContent = Set-DotEnvValue $envContent "HARNESS_MODELS" ($selectedModels -join ",")
$utf8WithoutBom = New-Object System.Text.UTF8Encoding -ArgumentList $false
[System.IO.File]::WriteAllText($envPath, $envContent, $utf8WithoutBom)

Write-Host "Installing Python 3.12 and locked project dependencies..."
Push-Location $projectRoot
try {
    & uv python install 3.12
    if ($LASTEXITCODE -ne 0) { throw "uv could not install Python 3.12. Check the network and uv output above, then rerun this script." }
    & uv sync --locked
    if ($LASTEXITCODE -ne 0) { throw "uv could not create the project environment from uv.lock. Review the output above, then rerun this script." }
    Write-Host "Setup complete. Start FLUX with: uv run flux"
    if ($Launch) {
        & uv run flux
        if ($LASTEXITCODE -ne 0) { throw "FLUX exited with code $LASTEXITCODE." }
    }
} finally {
    Pop-Location
}
