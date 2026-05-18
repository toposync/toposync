#Requires -Version 5.1
<#
.SYNOPSIS
Installs and runs TopoSync Processing Server as a Windows service.

.DESCRIPTION
This script is intended for an elevated PowerShell session on Windows.
It installs uv and Python 3.12 if needed, creates a dedicated virtual
environment under ProgramData, installs the requested TopoSync bundle,
opens the Windows Firewall port, creates/updates a Windows service, starts
it, and prints the registration payload for the origin TopoSync server.

Default port is 49321 instead of 9001. Port 9001 is registered by IANA for
etlservicemgr, while 49321 is in the dynamic/private range. If the requested
port is already busy, the script automatically picks the next free port unless
-AutoSelectPort:$false is provided.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\install_windows_processing_server.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\install_windows_processing_server.ps1 -Bundle cuda -AdvertiseHost 192.168.1.50

.EXAMPLE
irm https://example.com/install_windows_processing_server.ps1 -OutFile $env:TEMP\install-toposync-processing.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\install-toposync-processing.ps1 -Bundle auto
#>

[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "directml", "cuda")]
    [string]$Bundle = "auto",

    [string]$Version = "0.4.16",

    [string]$InstallRoot = "$env:ProgramData\TopoSync\ProcessingServer",

    [string]$DataDir = "",

    [string]$ServiceName = "TopoSyncProcessingServer",

    [string]$ServiceDisplayName = "TopoSync Processing Server",

    [string]$HostAddress = "0.0.0.0",

    [int]$Port = 49321,

    [bool]$AutoSelectPort = $true,

    [string]$ServerId = "",

    [string]$AdvertiseHost = "",

    [ValidateSet("Domain", "Private", "Public")]
    [string[]]$FirewallProfile = @("Domain", "Private"),

    [string]$ProcessingUsername = "toposync",

    [string]$ProcessingPassword = "",

    [switch]$NoAuth,

    [switch]$RecreateVenv,

    [switch]$ForceReinstallService,

    [bool]$StartService = $true
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
} catch {
    # Older hosts may not expose this enum; uv install will fail naturally if TLS is unusable.
}

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-Arg {
    param([Parameter(Mandatory = $true)][object]$Value)
    $text = [string]$Value
    if ($text -match '^[A-Za-z0-9_./:=\\-]+$') {
        return $text
    }
    return '"' + ($text -replace '"', '\"') + '"'
}

function Convert-BoundParametersToArgs {
    $outArgs = @()
    foreach ($key in $PSBoundParameters.Keys) {
        $value = $PSBoundParameters[$key]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) {
                $outArgs += "-$key"
            }
            continue
        }
        if ($value -is [array]) {
            foreach ($item in $value) {
                $outArgs += "-$key"
                $outArgs += (Quote-Arg $item)
            }
            continue
        }
        $outArgs += "-$key"
        $outArgs += (Quote-Arg $value)
    }
    return $outArgs
}

if (-not (Test-IsAdministrator)) {
    if ($PSCommandPath) {
        Write-Host "Reopening this installer as Administrator..."
        $argList = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", (Quote-Arg $PSCommandPath)
        ) + (Convert-BoundParametersToArgs)
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($argList -join " ")
        exit 0
    }
    throw "Run this script in an elevated PowerShell session. For link installs, download it to a .ps1 file first, then run it as Administrator."
}

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode {
    param([Parameter(Mandatory = $true)][string]$Action)
    if ($LASTEXITCODE -ne 0) {
        throw "$Action failed with exit code $LASTEXITCODE."
    }
}

function New-RandomPassword {
    $alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $bytes = New-Object byte[] 24
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    $chars = New-Object char[] $bytes.Length
    for ($i = 0; $i -lt $bytes.Length; $i++) {
        $index = ([int]$bytes[$i]) % $alphabet.Length
        $chars[$i] = $alphabet[$index]
    }
    return -join $chars
}

function Get-UvCommand {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }

    $candidates = @(
        "$env:USERPROFILE\.local\bin\uv.exe",
        "$env:USERPROFILE\.cargo\bin\uv.exe",
        "$env:ProgramFiles\uv\uv.exe",
        "$env:ProgramFiles\uv\bin\uv.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return ""
}

function Install-UvIfNeeded {
    $uv = Get-UvCommand
    if ($uv) {
        return $uv
    }

    Write-Step "Installing uv"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    Assert-LastExitCode "uv installer"

    $extraPaths = @(
        "$env:USERPROFILE\.local\bin",
        "$env:USERPROFILE\.cargo\bin"
    )
    foreach ($path in $extraPaths) {
        if ((Test-Path $path) -and ($env:Path -notlike "*$path*")) {
            $env:Path = "$path;$env:Path"
        }
    }

    $uv = Get-UvCommand
    if (-not $uv) {
        throw "uv installation completed but uv.exe was not found in PATH or the usual install directories."
    }
    return $uv
}

function Test-TcpPortAvailable {
    param([Parameter(Mandatory = $true)][int]$CandidatePort)
    $listener = $null
    try {
        $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $CandidatePort)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) {
            $listener.Stop()
        }
    }
}

function Resolve-ProcessingPort {
    param(
        [Parameter(Mandatory = $true)][int]$RequestedPort,
        [Parameter(Mandatory = $true)][bool]$MayAutoSelect
    )
    if ($RequestedPort -lt 1024 -or $RequestedPort -gt 65535) {
        throw "Port must be between 1024 and 65535."
    }
    if (Test-TcpPortAvailable -CandidatePort $RequestedPort) {
        return $RequestedPort
    }
    if (-not $MayAutoSelect) {
        throw "TCP port $RequestedPort is already in use. Choose another port or enable -AutoSelectPort."
    }
    for ($candidate = $RequestedPort + 1; $candidate -le 65535; $candidate++) {
        if (Test-TcpPortAvailable -CandidatePort $candidate) {
            Write-Warning "TCP port $RequestedPort is busy. Using $candidate instead."
            return $candidate
        }
    }
    throw "No free TCP port found from $RequestedPort to 65535."
}

function Normalize-ServerId {
    param([string]$Raw)
    $value = [string]$Raw
    $value = $value.Trim().ToLowerInvariant()
    if (-not $value) {
        $computerName = [string]$env:COMPUTERNAME
        if (-not $computerName) {
            $computerName = "windows"
        }
        $hostPart = ($computerName.Trim().ToLowerInvariant() -replace '[^a-z0-9_-]+', '-').Trim("-_")
        $value = "win-$hostPart"
    }
    $value = ($value -replace '[^a-z0-9_-]+', '-').Trim("-_")
    if (-not $value -or $value[0] -notmatch '[a-z]') {
        $value = "win-$value"
    }
    if ($value.Length -gt 64) {
        $value = $value.Substring(0, 64).Trim("-_")
    }
    if ($value -notmatch '^[a-z][a-z0-9_-]{0,63}$') {
        throw "Invalid ServerId after normalization: $value"
    }
    return $value
}

function Resolve-Bundle {
    param([ValidateSet("auto", "cpu", "directml", "cuda")][string]$RequestedBundle)
    if ($RequestedBundle -ne "auto") {
        return $RequestedBundle
    }
    $nvidiaSmi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($nvidiaSmi) {
        return "cuda"
    }
    return "directml"
}

function Get-PackageNameForBundle {
    param([ValidateSet("cpu", "directml", "cuda")][string]$SelectedBundle)
    switch ($SelectedBundle) {
        "cpu" { return "toposync" }
        "directml" { return "toposync-vision-directml" }
        "cuda" { return "toposync-vision-cuda" }
    }
}

function Get-PackageSpec {
    param(
        [Parameter(Mandatory = $true)][string]$PackageName,
        [Parameter(Mandatory = $true)][string]$PackageVersion
    )
    if ($PackageVersion.Trim().ToLowerInvariant() -in @("", "latest")) {
        return $PackageName
    }
    return "$PackageName==$PackageVersion"
}

function ConvertTo-PSLiteral {
    param([AllowNull()][string]$Value)
    $text = [string]$Value
    return "'" + ($text -replace "'", "''") + "'"
}

function Write-RunnerScript {
    param(
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][string]$ToposyncExe,
        [Parameter(Mandatory = $true)][string]$ServiceDataDir,
        [Parameter(Mandatory = $true)][string]$LogDir,
        [Parameter(Mandatory = $true)][string]$BindHost,
        [Parameter(Mandatory = $true)][int]$BindPort,
        [Parameter(Mandatory = $true)][string]$ResolvedServerId,
        [Parameter(Mandatory = $true)][bool]$AuthDisabled,
        [Parameter(Mandatory = $true)][string]$Username,
        [Parameter(Mandatory = $true)][string]$Password
    )

    $usernameLine = if ($AuthDisabled) {
        "Remove-Item Env:\TOPOSYNC_PROCESSING_USERNAME -ErrorAction SilentlyContinue"
    } else {
        "`$env:TOPOSYNC_PROCESSING_USERNAME = $(ConvertTo-PSLiteral $Username)"
    }
    $passwordLine = if ($AuthDisabled) {
        "Remove-Item Env:\TOPOSYNC_PROCESSING_PASSWORD -ErrorAction SilentlyContinue"
    } else {
        "`$env:TOPOSYNC_PROCESSING_PASSWORD = $(ConvertTo-PSLiteral $Password)"
    }

    $content = @"
`$ErrorActionPreference = "Stop"
`$env:PYTHONUTF8 = "1"
`$env:TOPOSYNC_ROLE = "processing"
`$env:TOPOSYNC_PROCESSING_SERVER_ID = $(ConvertTo-PSLiteral $ResolvedServerId)
`$env:TOPOSYNC_DATA_DIR = $(ConvertTo-PSLiteral $ServiceDataDir)
$usernameLine
$passwordLine

New-Item -ItemType Directory -Force -Path $(ConvertTo-PSLiteral $LogDir) | Out-Null
`$logPath = Join-Path $(ConvertTo-PSLiteral $LogDir) ("processing-server-" + (Get-Date -Format "yyyyMMdd") + ".log")
Start-Transcript -Path `$logPath -Append | Out-Null
try {
    & $(ConvertTo-PSLiteral $ToposyncExe) processing-serve --host $(ConvertTo-PSLiteral $BindHost) --port $BindPort --data-dir $(ConvertTo-PSLiteral $ServiceDataDir)
    `$exitCode = `$LASTEXITCODE
    if (`$null -eq `$exitCode) { `$exitCode = 0 }
    exit `$exitCode
} finally {
    try { Stop-Transcript | Out-Null } catch {}
}
"@
    Set-Content -Path $RunnerPath -Value $content -Encoding UTF8
}

function Set-InstallAcl {
    param([Parameter(Mandatory = $true)][string]$Path)
    try {
        & icacls.exe $Path /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)(F)" "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
    } catch {
        Write-Warning "Could not restrict ACLs on ${Path}: $($_.Exception.Message)"
    }
}

function Configure-FirewallRule {
    param(
        [Parameter(Mandatory = $true)][string]$RuleName,
        [Parameter(Mandatory = $true)][int]$RulePort,
        [Parameter(Mandatory = $true)][string[]]$Profiles
    )
    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($existing) {
        $existing | Remove-NetFirewallRule
    }
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $RulePort `
        -Profile $Profiles `
        -Description "TopoSync Processing Server inbound API/SSE port." | Out-Null
}

function Configure-WindowsService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$DisplayName,
        [Parameter(Mandatory = $true)][string]$RunnerPath,
        [Parameter(Mandatory = $true)][bool]$ForceRecreate
    )
    $powerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    $binaryPath = '"' + $powerShellExe + '" -NoProfile -ExecutionPolicy Bypass -File "' + $RunnerPath + '"'
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue

    if ($existing -and $ForceRecreate) {
        if ($existing.Status -ne "Stopped") {
            Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
            $existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
        }
        & sc.exe delete $Name | Out-Null
        Assert-LastExitCode "Deleting existing service $Name"
        Start-Sleep -Seconds 2
        $existing = $null
    }

    if ($existing) {
        if ($existing.Status -ne "Stopped") {
            Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
            $existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
        }
        & sc.exe config $Name binPath= $binaryPath start= auto DisplayName= $DisplayName | Out-Null
        Assert-LastExitCode "Updating service $Name"
    } else {
        New-Service -Name $Name -DisplayName $DisplayName -BinaryPathName $binaryPath -StartupType Automatic | Out-Null
    }

    & sc.exe description $Name "Runs TopoSync Processing Server for distributed pipelines." | Out-Null
    Assert-LastExitCode "Setting service description for $Name"
    & sc.exe failure $Name reset= 86400 actions= restart/5000/restart/15000/restart/30000 | Out-Null
    Assert-LastExitCode "Setting service restart policy for $Name"
    & sc.exe failureflag $Name 1 | Out-Null
    Assert-LastExitCode "Enabling service failure actions for $Name"
}

function Invoke-ProcessingStatus {
    param(
        [Parameter(Mandatory = $true)][string]$BaseUrl,
        [Parameter(Mandatory = $true)][bool]$AuthDisabled,
        [string]$Username = "",
        [string]$Password = ""
    )
    $headers = @{}
    if (-not $AuthDisabled) {
        $tokenBytes = [Text.Encoding]::ASCII.GetBytes(("{0}:{1}" -f $Username, $Password))
        $headers["Authorization"] = "Basic " + [Convert]::ToBase64String($tokenBytes)
    }
    return Invoke-RestMethod -Uri "$BaseUrl/api/processing/status" -Headers $headers -TimeoutSec 10
}

$existingServiceBeforeInstall = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existingServiceBeforeInstall -and $existingServiceBeforeInstall.Status -ne "Stopped") {
    Write-Step "Stopping existing service before install"
    Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue
    $existingServiceBeforeInstall.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
}

$selectedPort = Resolve-ProcessingPort -RequestedPort $Port -MayAutoSelect $AutoSelectPort
$resolvedServerId = Normalize-ServerId -Raw $ServerId
$selectedBundle = Resolve-Bundle -RequestedBundle $Bundle
$packageName = Get-PackageNameForBundle -SelectedBundle $selectedBundle
$packageSpec = Get-PackageSpec -PackageName $packageName -PackageVersion $Version

if (-not $DataDir) {
    $DataDir = Join-Path $InstallRoot "data"
}
if (-not $AdvertiseHost) {
    $AdvertiseHost = [Net.Dns]::GetHostName()
}
$baseUrl = "http://${AdvertiseHost}:$selectedPort"
$localBaseUrl = "http://127.0.0.1:$selectedPort"

if (-not $NoAuth -and -not $ProcessingPassword) {
    $ProcessingPassword = New-RandomPassword
}
if ($NoAuth) {
    $ProcessingUsername = ""
    $ProcessingPassword = ""
}

$venvDir = Join-Path $InstallRoot ".venv"
$pythonInstallDir = Join-Path $InstallRoot "uv-python"
$logDir = Join-Path $InstallRoot "logs"
$runnerPath = Join-Path $InstallRoot "run-processing-server.ps1"
$manifestPath = Join-Path $InstallRoot "processing-server-registration.json"
$firewallRuleName = "TopoSync Processing Server ($ServiceName)"

Write-Step "Preparing directories"
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataDir, $pythonInstallDir, $logDir | Out-Null
Set-InstallAcl -Path $InstallRoot

$uv = Install-UvIfNeeded
Write-Step "Installing Python 3.12 with uv"
$env:UV_PYTHON_INSTALL_DIR = $pythonInstallDir
& $uv python install 3.12 --install-dir $pythonInstallDir
Assert-LastExitCode "Installing Python 3.12"

if ($RecreateVenv -and (Test-Path $venvDir)) {
    Write-Step "Removing existing virtual environment"
    Remove-Item -Recurse -Force $venvDir
}

if (-not (Test-Path $venvDir)) {
    Write-Step "Creating virtual environment"
    & $uv venv $venvDir --python 3.12
    Assert-LastExitCode "Creating virtual environment"
}

$venvPython = Join-Path $venvDir "Scripts\python.exe"
$toposyncExe = Join-Path $venvDir "Scripts\toposync.exe"
if (-not (Test-Path $venvPython)) {
    throw "Virtual environment Python was not found at $venvPython"
}

Write-Step "Installing TopoSync bundle: $packageSpec"
& $uv pip install --python $venvPython --upgrade --refresh $packageSpec
Assert-LastExitCode "Installing TopoSync bundle"

if (-not (Test-Path $toposyncExe)) {
    throw "toposync.exe was not installed at $toposyncExe"
}

Write-Step "Writing service runner"
Write-RunnerScript `
    -RunnerPath $runnerPath `
    -ToposyncExe $toposyncExe `
    -ServiceDataDir $DataDir `
    -LogDir $logDir `
    -BindHost $HostAddress `
    -BindPort $selectedPort `
    -ResolvedServerId $resolvedServerId `
    -AuthDisabled ([bool]$NoAuth) `
    -Username $ProcessingUsername `
    -Password $ProcessingPassword

Write-Step "Configuring Windows Firewall"
Configure-FirewallRule -RuleName $firewallRuleName -RulePort $selectedPort -Profiles $FirewallProfile

Write-Step "Configuring Windows service"
Configure-WindowsService `
    -Name $ServiceName `
    -DisplayName $ServiceDisplayName `
    -RunnerPath $runnerPath `
    -ForceRecreate ([bool]$ForceReinstallService)

if ($StartService) {
    Write-Step "Starting service"
    Start-Service -Name $ServiceName
    Start-Sleep -Seconds 4
}

$statusOk = $false
if ($StartService) {
    Write-Step "Checking local processing status"
    try {
        $status = Invoke-ProcessingStatus `
            -BaseUrl $localBaseUrl `
            -AuthDisabled ([bool]$NoAuth) `
            -Username $ProcessingUsername `
            -Password $ProcessingPassword
        $statusOk = $true
        $providers = @()
        try {
            $providers = @($status.vision.execution_providers)
        } catch {
            $providers = @()
        }
        Write-Host "Processing server responded at $localBaseUrl"
        if ($providers.Count -gt 0) {
            Write-Host "ONNX Runtime providers: $($providers -join ', ')"
        }
    } catch {
        Write-Warning "The service was created, but status check failed: $($_.Exception.Message)"
        Write-Warning "Check logs under $logDir and Windows Services for $ServiceName."
    }
}

$registration = [ordered]@{
    id = $resolvedServerId
    name = $ServiceDisplayName
    kind = "http"
    url = $baseUrl
    username = $ProcessingUsername
    password = $ProcessingPassword
}
$manifest = [ordered]@{
    service_name = $ServiceName
    bundle = $selectedBundle
    package = $packageSpec
    install_root = $InstallRoot
    python_install_dir = $pythonInstallDir
    data_dir = $DataDir
    log_dir = $logDir
    host_address = $HostAddress
    port = $selectedPort
    firewall_rule = $firewallRuleName
    firewall_profile = $FirewallProfile
    local_status_url = "$localBaseUrl/api/processing/status"
    registration = $registration
    status_check_ok = $statusOk
}

$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path $manifestPath -Encoding UTF8

Write-Step "Done"
Write-Host "Service: $ServiceName"
Write-Host "Bundle: $selectedBundle ($packageSpec)"
Write-Host "Local status URL: $localBaseUrl/api/processing/status"
Write-Host "Origin URL to register: $baseUrl"
Write-Host "Registration JSON saved at: $manifestPath"
Write-Host ""
Write-Host "Registration payload for the TopoSync origin:"
($registration | ConvertTo-Json -Depth 5) | Write-Host
Write-Host ""
Write-Host "PowerShell registration example on the origin machine:"
$registrationJsonOneLine = (($registration | ConvertTo-Json -Depth 5 -Compress) -replace "'", "''")
Write-Host ('$body = ''' + $registrationJsonOneLine + '''')
Write-Host ('Invoke-RestMethod -Method Put -Uri "http://ORIGIN_HOST:8000/api/processing-servers/' + $resolvedServerId + '" -ContentType "application/json" -Body $body')
