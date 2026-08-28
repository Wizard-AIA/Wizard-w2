# Wizard Windows Installer (PowerShell)
# Usage: irm https://wizardw2.vercel.app/install.ps1 | iex
#
# Supported Operating System: Windows 10/11 / Windows Server 2019+
# Supported Architecture: x86_64 (AMD64), ARM64

$ErrorActionPreference = 'Stop'

function Write-WizardLog($text, $color = "Magenta") {
    Write-Host "[wizard-install] " -NoNewline -ForegroundColor Magenta
    Write-Host $text -ForegroundColor $color
}

Write-Host ""
Write-Host "  Wizard - Autonomous AI Data Analyst Workspace" -ForegroundColor Magenta
Write-Host "  Local-First • AST Sandboxed • Zero Cloud Telemetry" -ForegroundColor DarkGray
Write-Host ""

# 1. Architecture Check
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq "AMD64" -or $arch -eq "x86_64") {
    $targetArch = "amd64"
} elseif ($arch -eq "ARM64") {
    $targetArch = "arm64"
} else {
    Write-WizardLog "Unsupported Windows architecture: $arch. Defaulting to amd64." "Yellow"
    $targetArch = "amd64"
}

Write-WizardLog "Detected platform: windows-$targetArch" "White"

# 2. Resolve Release Version
$repo = "Wizard-AIA/Wizard-w2"
$tag = $env:WIZARD_VERSION

if (-not $tag) {
    Write-WizardLog "Resolving latest release from GitHub..." "White"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $releaseJson = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" -UseBasicParsing -TimeoutSec 10
        if ($releaseJson.tag_name) {
            $tag = $releaseJson.tag_name
        }
    } catch {
        Write-WizardLog "Could not contact GitHub API, falling back to release baseline v1.0.2" "Yellow"
        $tag = "v1.0.2"
    }
}

if (-not $tag) { $tag = "v1.0.2" }
if (-not $tag.StartsWith("v")) { $tag = "v$tag" }

Write-WizardLog "Target release: $tag" "Green"

# 3. Destination Paths
$assetName = "Wizard-$tag-windows-$targetArch.zip"
$downloadUrl = "https://github.com/$repo/releases/download/$tag/$assetName"

$installDir = Join-Path $env:LOCALAPPDATA "Wizard"
$binDir = Join-Path $installDir "bin"
$tempZip = Join-Path $env:TEMP $assetName

# 4. Download Release
Write-WizardLog "Downloading $assetName..." "White"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $downloadUrl -OutFile $tempZip -UseBasicParsing
} catch {
    Write-WizardLog "Failed to download $downloadUrl : $_" "Red"
    exit 1
}

# 5. Extract Archive
Write-WizardLog "Extracting to $installDir..." "White"
if (-not (Test-Path $installDir)) {
    New-Item -ItemType Directory -Path $installDir -Force | Out-Null
}
if (-not (Test-Path $binDir)) {
    New-Item -ItemType Directory -Path $binDir -Force | Out-Null
}

try {
    Expand-Archive -Path $tempZip -DestinationPath $installDir -Force
} catch {
    Write-WizardLog "Expand-Archive failed: $_" "Red"
    exit 1
} finally {
    if (Test-Path $tempZip) {
        Remove-Item $tempZip -Force -ErrorAction SilentlyContinue
    }
}

# Locate extracted package folder and binary
$pkgDir = Join-Path $installDir "Wizard-$tag-windows-$targetArch"
$wizardSrc = $null

if (Test-Path $pkgDir) {
    $currentLink = Join-Path $installDir "current"
    if (Test-Path $currentLink) {
        Remove-Item $currentLink -Force -Recurse -ErrorAction SilentlyContinue
    }
    try {
        New-Item -ItemType Junction -Path $currentLink -Target $pkgDir -Force | Out-Null
    } catch {
        # Fallback if junctions not supported
    }
    $wizardSrc = Join-Path $pkgDir "cli\wizard.exe"
}

if (-not $wizardSrc -or -not (Test-Path $wizardSrc)) {
    $foundExe = Get-ChildItem -Path $installDir -Filter "wizard.exe" -Recurse -File | Select-Object -First 1
    if ($foundExe) {
        $wizardSrc = $foundExe.FullName
    }
}

$wizardExe = Join-Path $binDir "wizard.exe"
if ($wizardSrc -and (Test-Path $wizardSrc)) {
    Copy-Item -Path $wizardSrc -Destination $wizardExe -Force
}

# 6. Add to User PATH persistently
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$needsPathUpdate = $true

if ($userPath) {
    $paths = $userPath -split ';'
    if ($paths -contains $binDir) {
        $needsPathUpdate = $false
    }
}

if ($needsPathUpdate) {
    $newUserPath = if ($userPath) { "$userPath;$binDir" } else { $binDir }
    [Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")
    $env:Path = "$env:Path;$binDir"
    Write-WizardLog "Added $binDir to User PATH." "White"
}

Write-Host ""
Write-Host "[wizard-install] [OK] Wizard $tag installed successfully to $wizardExe!" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Initialize workspace: " -NoNewline; Write-Host "wizard init" -ForegroundColor Green
Write-Host "  2. Launch agent:         " -NoNewline; Write-Host "wizard start" -ForegroundColor Green
Write-Host ""
Write-Host "Note: The 'wizard' command is now globally available in any new terminal window." -ForegroundColor DarkGray
Write-Host ""
