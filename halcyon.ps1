param(
  [Parameter(Position = 0)]
  [string]$Command = "help"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$ManifestPath = Join-Path $ScriptDir "halcyon-release.json"
$Manifest = Get-Content $ManifestPath -Raw | ConvertFrom-Json
$CurrentVersion = [string]$Manifest.version
$ManifestUrl = [string]$Manifest.manifest_url

function Require-Command([string]$Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "halcyon: missing required command '$Name'"
  }
}

function Get-LatestVersion {
  try {
    $remoteManifest = Invoke-RestMethod -Uri $ManifestUrl -Method Get -TimeoutSec 4
    return [string]$remoteManifest.version
  } catch {
    return $null
  }
}

function Convert-VersionParts([string]$Version) {
  if ([string]::IsNullOrWhiteSpace($Version)) {
    return @(0)
  }

  $parts = @()
  foreach ($chunk in ($Version -replace "-", ".").Split(".")) {
    if ([string]::IsNullOrWhiteSpace($chunk)) {
      continue
    }
    $digits = -join ($chunk.ToCharArray() | Where-Object { [char]::IsDigit($_) })
    if ([string]::IsNullOrWhiteSpace($digits)) {
      $parts += 0
    } else {
      $parts += [int]$digits
    }
  }

  if ($parts.Count -eq 0) {
    return @(0)
  }

  return $parts
}

function Test-RemoteNewer([string]$LocalVersion, [string]$RemoteVersion) {
  if ($LocalVersion -eq $RemoteVersion) {
    return $false
  }

  $left = Convert-VersionParts $LocalVersion
  $right = Convert-VersionParts $RemoteVersion
  $width = [Math]::Max($left.Count, $right.Count)
  while ($left.Count -lt $width) { $left += 0 }
  while ($right.Count -lt $width) { $right += 0 }

  for ($index = 0; $index -lt $width; $index += 1) {
    if ($left[$index] -lt $right[$index]) { return $true }
    if ($left[$index] -gt $right[$index]) { return $false }
  }

  return $false
}

function Show-Usage {
  @"
Usage: halcyon <start|stop|status|update>

  halcyon start    Start the Docker Compose stack
  halcyon stop     Stop the Docker Compose stack
  halcyon status   Show running containers and check for a newer release
  halcyon update   Pull the latest git version and rebuild the stack
"@
}

function Show-Status {
  Require-Command docker
  Write-Host "Current version: $CurrentVersion"
  $latestVersion = Get-LatestVersion
  if ($latestVersion) {
    Write-Host "Newest version:  $latestVersion"
    if (Test-RemoteNewer $CurrentVersion $latestVersion) {
      Write-Host "Status:          update available"
    } else {
      Write-Host "Status:          up to date"
    }
  } else {
    Write-Host "Newest version:  unavailable"
    Write-Host "Status:          unable to reach update server"
  }
  Write-Host ""
  docker compose ps
}

switch ($Command.ToLowerInvariant()) {
  "start" {
    Require-Command docker
    docker compose up -d
    break
  }
  "stop" {
    Require-Command docker
    docker compose stop
    break
  }
  "status" {
    Show-Status
    break
  }
  "update" {
    Require-Command docker
    Require-Command git
    if (-not (Test-Path (Join-Path $ScriptDir ".git"))) {
      throw "halcyon update requires a git clone install. Download the newest release package or clone the repository."
    }
    git fetch --tags origin main
    git pull --ff-only origin main
    docker compose up -d --build
    Write-Host ""
    Show-Status
    break
  }
  { $_ -in @("", "-h", "--help", "help") } {
    Show-Usage
    break
  }
  default {
    throw "halcyon: unknown command '$Command'"
  }
}
