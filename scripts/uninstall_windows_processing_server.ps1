#Requires -Version 5.1
<#
.SYNOPSIS
Uninstalls the Toposync Processing Server Windows service.

.DESCRIPTION
This script is intended for an elevated PowerShell session on Windows.
It stops and removes the Windows service, removes the Windows Firewall rule,
and removes the generated runtime files. By default it preserves data and logs
under ProgramData. Use -RemoveData to delete the full install root.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\uninstall_windows_processing_server.ps1

.EXAMPLE
powershell -ExecutionPolicy Bypass -File .\uninstall_windows_processing_server.ps1 -RemoveData
#>

[CmdletBinding()]
param(
    [string]$InstallRoot = "$env:ProgramData\Toposync\ProcessingServer",

    [string]$ServiceName = "ToposyncProcessingServer",

    [string]$FirewallRuleName = "",

    [bool]$RemoveFirewallRule = $true,

    [bool]$RemoveRuntimeFiles = $true,

    [switch]$RemoveData,

    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

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
        $outArgs += "-$key"
        $outArgs += (Quote-Arg $value)
    }
    return $outArgs
}

if (-not (Test-IsAdministrator)) {
    if ($PSCommandPath) {
        Write-Host "Reopening this uninstaller as Administrator..."
        $argList = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", (Quote-Arg $PSCommandPath)
        ) + (Convert-BoundParametersToArgs)
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($argList -join " ")
        exit 0
    }
    throw "Run this script in an elevated PowerShell session. For link uninstalls, download it to a .ps1 file first, then run it as Administrator."
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

function Wait-ServiceRemoved {
    param([Parameter(Mandatory = $true)][string]$Name)
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
        if (-not $existing) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
}

function Stop-AndRemoveService {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][bool]$MayForce
    )
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "Service not found: $Name"
        return
    }

    if ($existing.Status -ne "Stopped") {
        Write-Host "Stopping service: $Name"
        Stop-Service -Name $Name -Force:$MayForce -ErrorAction SilentlyContinue
        try {
            $existing.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
        } catch {
            if (-not $MayForce) {
                throw "Service $Name did not stop. Rerun with -Force to allow forced stop."
            }
        }
    }

    & sc.exe delete $Name | Out-Null
    Assert-LastExitCode "Deleting service $Name"
    Wait-ServiceRemoved -Name $Name
}

function Remove-FirewallRules {
    param([Parameter(Mandatory = $true)][string]$RuleName)
    if (-not (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
        Write-Warning "Windows Firewall PowerShell cmdlets are unavailable; skipping firewall cleanup."
        return
    }

    $rules = @(Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue)
    if ($rules.Count -eq 0) {
        Write-Host "Firewall rule not found: $RuleName"
        return
    }
    $rules | Remove-NetFirewallRule
    Write-Host "Removed firewall rule: $RuleName"
}

function Remove-PathIfExists {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][bool]$Recurse
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }
    if ($Recurse) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    } else {
        Remove-Item -LiteralPath $Path -Force
    }
}

if (-not $FirewallRuleName) {
    $FirewallRuleName = "Toposync Processing Server ($ServiceName)"
}

Write-Step "Removing Windows service"
Stop-AndRemoveService -Name $ServiceName -MayForce ([bool]$Force)

if ($RemoveFirewallRule) {
    Write-Step "Removing Windows Firewall rule"
    Remove-FirewallRules -RuleName $FirewallRuleName
}

if ($RemoveData) {
    Write-Step "Removing install root"
    Remove-PathIfExists -Path $InstallRoot -Recurse $true
} elseif ($RemoveRuntimeFiles) {
    Write-Step "Removing runtime files"
    $runtimePaths = @(
        (Join-Path $InstallRoot ".venv"),
        (Join-Path $InstallRoot "uv-python"),
        (Join-Path $InstallRoot "run-processing-server.ps1"),
        (Join-Path $InstallRoot "ToposyncProcessingService.exe"),
        (Join-Path $InstallRoot "ToposyncProcessingService.cs"),
        (Join-Path $InstallRoot "processing-server-registration.json")
    )
    foreach ($path in $runtimePaths) {
        Remove-PathIfExists -Path $path -Recurse $true
    }
}

Write-Step "Done"
Write-Host "Service removed: $ServiceName"
if ($RemoveFirewallRule) {
    Write-Host "Firewall rule removed: $FirewallRuleName"
}
if ($RemoveData) {
    Write-Host "Install root removed: $InstallRoot"
} else {
    Write-Host "Preserved data/logs under: $InstallRoot"
    Write-Host "Use -RemoveData to delete the full install root."
}
Write-Host ""
Write-Host "If this processing server was registered in an origin Toposync instance, remove it there as well."
