# wizard-targets: windows-amd64
<#
.SYNOPSIS
    Wizard installer for Windows (PowerShell 5.1 and 7+).

.DESCRIPTION
    Installs a published Wizard release for the current user. No administrator
    rights are needed. The archive is verified against the release's SHA256SUMS
    before anything is unpacked, and running the installer again is safe.

    Default location:  %LOCALAPPDATA%\Wizard   (the wizard command goes in its bin\ folder)

    Quick start:
        irm https://wizardw2.vercel.app/install.ps1 | iex

    With options (a script block accepts parameters; iex does not):
        & ([scriptblock]::Create((irm https://wizardw2.vercel.app/install.ps1))) -Version 1.0.13 -NoModifyPath

    Every option also has an environment variable, for use with the one-liner:
        $env:WIZARD_VERSION = '1.0.13'; irm https://wizardw2.vercel.app/install.ps1 | iex

    Advanced: WIZARD_RELEASE_BASE_URL fetches <base>/<tag>/<files> from a mirror
    instead of GitHub Releases (an internal mirror, an air-gapped copy, or tests).

.PARAMETER Version
    Release to install, for example 1.0.13. Default: the latest release. (WIZARD_VERSION)
.PARAMETER InstallDir
    Where to install. Default: %LOCALAPPDATA%\Wizard. (WIZARD_INSTALL_DIR)
.PARAMETER NoModifyPath
    Do not add the bin folder to your user PATH. (WIZARD_NO_MODIFY_PATH=1)
.PARAMETER Force
    Install even if another Wizard is already on PATH.
.PARAMETER Yes
    Accepted for scripts; the installer never prompts.

    Exit codes (when run as a file): 0 ok, 1 failure, 2 bad usage, 3 unsupported
    platform or missing tool, 4 network error. Under irm | iex the exit code is
    left in $LASTEXITCODE and the terminal is never closed.
#>
[CmdletBinding()]
param(
    [string]$Version = $env:WIZARD_VERSION,
    [string]$InstallDir = $(if ($env:WIZARD_INSTALL_DIR) { $env:WIZARD_INSTALL_DIR } else { $env:WIZARD_HOME }),
    [switch]$NoModifyPath,
    [switch]$Force,
    [switch]$Yes
)

$script:Repo = 'Wizard-AIA/Wizard-w2'
$script:Supported = 'windows-amd64'

# Stop on the first error, and make Invoke-WebRequest fast (its progress bar
# slows downloads dramatically on Windows PowerShell 5.1). These are restored
# below so `irm ... | iex` does not permanently change the caller's session.
$script:OriginalErrorActionPreference = $ErrorActionPreference
$script:OriginalProgressPreference = $ProgressPreference
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# A plain exception carrying its exit code. (A PowerShell class would be tidier,
# but a class defined by a script that is run twice in one session, as with
# repeated `irm | iex`, gives each run a different type, which breaks `catch [Type]`.)
function Fail([int]$Code, [string]$Message) {
    $exception = New-Object System.Exception($Message)
    $exception.Data['WizardCode'] = $Code
    throw $exception
}
function Step([string]$Text) { Write-Host '==> ' -NoNewline -ForegroundColor Cyan; Write-Host $Text }
function Ok([string]$Text) { Write-Host ' ok  ' -NoNewline -ForegroundColor Green; Write-Host $Text }
function Warn([string]$Text) { Write-Host 'warn ' -NoNewline -ForegroundColor Yellow; Write-Host $Text }
function Debug-Line([string]$Text) { Write-Verbose $Text }

function Get-Platform {
    # PROCESSOR_ARCHITEW6432 is set for a 32-bit PowerShell on a 64-bit OS.
    $arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    switch ($arch) {
        'AMD64' { return 'windows-amd64' }
        'ARM64' { Fail 3 "Windows on ARM64 is not supported yet: no Wizard release is published for windows-arm64. Supported: $script:Supported. You can build from source: https://github.com/$script:Repo#contributing--development" }
        default { Fail 3 "unsupported CPU architecture: '$arch'. Supported: $script:Supported." }
    }
}

# NO_PROXY is a comma-separated list of hosts and domain suffixes ("*" = all).
function Test-NoProxy([string]$HostName) {
    $list = if ($env:NO_PROXY) { $env:NO_PROXY } else { $env:no_proxy }
    if (-not $list -or -not $HostName) { return $false }
    foreach ($entry in ($list -split '[,\s]+')) {
        $entry = $entry.Trim().TrimStart('*').TrimStart('.')
        if (-not $entry) { if ($list.Trim() -eq '*') { return $true }; continue }
        if ($HostName -eq $entry -or $HostName.EndsWith(".$entry")) { return $true }
    }
    return $false
}

function Get-DownloadArgs([string]$Url) {
    # Invoke-WebRequest uses the system proxy on 5.1; honour the standard
    # environment variables too, as the other installers and the CLI do.
    $splat = @{ UseBasicParsing = $true }
    $proxy = if ($env:HTTPS_PROXY) { $env:HTTPS_PROXY } elseif ($env:https_proxy) { $env:https_proxy } elseif ($env:HTTP_PROXY) { $env:HTTP_PROXY } else { $null }
    $requestHost = try { ([System.Uri]$Url).Host } catch { '' }
    if ($proxy -and -not (Test-NoProxy $requestHost)) { $splat['Proxy'] = $proxy }
    return $splat
}

function Save-Url([string]$Url, [string]$Destination) {
    Debug-Line "GET $Url"
    $extra = Get-DownloadArgs $Url
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $Destination @extra
            return
        } catch {
            if ($attempt -eq 3) {
                Fail 4 "download failed: $Url`n       $($_.Exception.Message)`n       Check your network or proxy settings (HTTPS_PROXY / NO_PROXY are honoured), then re-run."
            }
            Start-Sleep -Seconds (2 * $attempt)
        }
    }
}

function ConvertTo-Tag([string]$Raw) {
    $v = $Raw.Trim().TrimStart('v', 'V')
    if ($v -notmatch '^\d+\.\d+\.\d+$') { Fail 2 "not a release version: '$Raw' (expected something like 1.0.13)" }
    return "v$v"
}

function Resolve-LatestTag([string]$BaseOverride, [string]$Temp) {
    if ($BaseOverride) {
        $file = Join-Path $Temp 'LATEST'
        Save-Url "$BaseOverride/LATEST" $file
        return (Get-Content -LiteralPath $file -Raw).Trim()
    }
    # The redirect target of /releases/latest names the tag and, unlike the
    # API, is not rate limited.
    try {
        $request = [System.Net.HttpWebRequest]::Create("https://github.com/$script:Repo/releases/latest")
        $request.AllowAutoRedirect = $false
        $request.UserAgent = 'wizard-installer'
        $response = $request.GetResponse()
        $location = $response.Headers['Location']
        $response.Close()
        if ($location -match '/releases/tag/([^/?#]+)$') { return $Matches[1] }
    } catch {
        Debug-Line "redirect lookup failed: $($_.Exception.Message)"
    }
    $extra = Get-DownloadArgs 'https://api.github.com/'
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$script:Repo/releases/latest" -Headers @{ 'User-Agent' = 'wizard-installer' } @extra
        if ($release.tag_name) { return [string]$release.tag_name }
    } catch {
        Fail 4 "could not determine the latest Wizard release: $($_.Exception.Message)`n       Pass -Version X.Y.Z, and check your network or proxy settings."
    }
    Fail 4 'could not determine the latest Wizard release. Pass -Version X.Y.Z.'
}

# --- PATH (user scope), done through the registry so the value keeps its type ---

function Normalize-PathEntry([string]$Entry) { return $Entry.Trim().Trim('"').TrimEnd('\', '/').ToLowerInvariant() }

function Add-UserPath([string]$Directory) {
    # [Environment]::SetEnvironmentVariable would expand every %VARIABLE% in the
    # existing value and store it back as a plain string; reading the raw value
    # and writing it back with its original type avoids both.
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
    try {
        $raw = [string]$key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
        try { $kind = $key.GetValueKind('Path') } catch { }
        $target = Normalize-PathEntry $Directory
        $present = $false
        foreach ($entry in ($raw -split ';')) {
            if ($entry -and (Normalize-PathEntry $entry) -eq $target) { $present = $true; break }
        }
        if (-not $present) {
            $updated = if ($raw) { $raw.TrimEnd(';') + ';' + $Directory } else { $Directory }
            $key.SetValue('Path', $updated, $kind)
            Send-EnvironmentChange
            Ok "added $Directory to your user PATH"
        } else {
            Debug-Line "$Directory is already on the user PATH"
        }
    } finally {
        $key.Close()
    }
    # Make it work in this window immediately, not only in new ones.
    $inSession = $false
    foreach ($entry in ($env:Path -split ';')) { if ($entry -and (Normalize-PathEntry $entry) -eq (Normalize-PathEntry $Directory)) { $inSession = $true; break } }
    if (-not $inSession) { $env:Path = $env:Path.TrimEnd(';') + ';' + $Directory }
}

function Send-EnvironmentChange {
    # Tell running programs (Explorer, new terminals) to re-read the environment.
    # Best effort: the PATH is already saved, and Add-Type is refused outright in
    # PowerShell's ConstrainedLanguage mode (AppLocker / WDAC managed machines),
    # where failing here would report a finished install as broken.
    try {
        if (-not ('Wizard.NativeMethods' -as [type])) {
            Add-Type -Namespace Wizard -Name NativeMethods -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Auto)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
        }
        $result = [UIntPtr]::Zero
        [void][Wizard.NativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 0x2, 5000, [ref]$result)
    } catch {
        Debug-Line "could not broadcast the environment change: $($_.Exception.Message)"
    }
}

# --- junctions: removing one must never touch the directory it points at ---

function Remove-Junction([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        # Remove-Item -Recurse follows the junction on Windows PowerShell 5.1 and
        # deletes the target's contents; Directory.Delete(path, false) removes
        # only the link.
        [System.IO.Directory]::Delete($Path, $false)
    }
}

function Set-CurrentJunction([string]$Current, [string]$Target) {
    $previous = $null
    if (Test-Path -LiteralPath $Current) { $previous = (Get-Item -LiteralPath $Current -Force).Target }
    Remove-Junction $Current
    try {
        [void](New-Item -ItemType Junction -Path $Current -Target $Target)
    } catch {
        # Put the old pointer back rather than leave the install without one.
        if ($previous) { try { [void](New-Item -ItemType Junction -Path $Current -Target ([string]($previous | Select-Object -First 1))) } catch { } }
        Fail 1 "could not point $Current at the new release: $($_.Exception.Message)"
    }
}

# --- archive handling ---

function Test-ArchiveSafe([string]$Archive) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if ($name.StartsWith('/') -or $name.StartsWith('\') -or $name -match '(^|[\\/])\.\.([\\/]|$)' -or $name -match '^[A-Za-z]:') {
                Fail 1 "the archive contains an unsafe path ($name); refusing to unpack it"
            }
        }
    } finally {
        $zip.Dispose()
    }
}

function Install-Wizard {
    $baseOverride = ''
    if ($env:WIZARD_RELEASE_BASE_URL) { $baseOverride = $env:WIZARD_RELEASE_BASE_URL.TrimEnd('/') }
    if ($baseOverride.StartsWith('http://')) { Warn 'WIZARD_RELEASE_BASE_URL is plain http: downloads are protected only by the checksum file served from the same place' }
    $modifyPath = -not ($NoModifyPath -or $env:WIZARD_NO_MODIFY_PATH -eq '1')

    # TLS 1.2 is not the default on older Windows PowerShell.
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

    $target = Get-Platform
    if (($script:Supported -split ' ') -notcontains $target) { Fail 3 "no Wizard release is published for $target. Supported: $script:Supported." }
    if (-not $env:LOCALAPPDATA -and -not $InstallDir) { Fail 3 '%LOCALAPPDATA% is not set; pass -InstallDir to choose a location' }
    if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA 'Wizard' }
    $InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
    # USERPROFILE can be unset for a service account or a scheduled task.
    $profileDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { [Environment]::GetFolderPath('UserProfile') }
    $userProfile = if ($profileDir) { [System.IO.Path]::GetFullPath($profileDir) } else { '' }
    if (($userProfile -and $InstallDir.TrimEnd('\') -eq $userProfile.TrimEnd('\')) -or $InstallDir.TrimEnd('\') -eq [System.IO.Path]::GetPathRoot($InstallDir).TrimEnd('\')) {
        Fail 2 "refusing to install into $InstallDir; choose a dedicated directory (default: $(Join-Path $env:LOCALAPPDATA 'Wizard'))"
    }

    $temp = Join-Path ([System.IO.Path]::GetTempPath()) ('wizard-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void](New-Item -ItemType Directory -Path $temp -Force)
    $stage = $null
    try {
        if ($Version) {
            $tag = ConvertTo-Tag $Version
        } else {
            Step 'Finding the latest release'
            $tag = ConvertTo-Tag (Resolve-LatestTag $baseOverride $temp)
        }
        $pkg = "Wizard-$tag-$target"
        $asset = "$pkg.zip"
        $baseUrl = if ($baseOverride) { "$baseOverride/$tag" } else { "https://github.com/$script:Repo/releases/download/$tag" }

        $binDir = Join-Path $InstallDir 'bin'
        $exe = Join-Path $binDir 'wizard.exe'
        $previous = $null
        if (Test-Path -LiteralPath $exe) {
            try { $previous = ((& $exe --version 2>$null) -split '\s+')[2].TrimEnd(',') } catch { }
        }

        # Another Wizard on PATH (Scoop's, or a copy elsewhere) would shadow or be
        # shadowed by this one, and the two update independently.
        $existing = Get-Command wizard -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($existing -and -not $Force) {
            $found = [string]$existing.Source
            if (-not $found.StartsWith($binDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                $how = if ($found -match '\\scoop\\') { 'scoop update wizard' } else { 'remove it first' }
                Fail 1 "another Wizard is already installed at $found.`n       Upgrade that one with its own tool ($how), or pass -Force to install here as well."
            }
        }

        Step "Installing Wizard $tag for $target"
        if ($previous) { Write-Host "    (replacing $previous)" }

        $sums = Join-Path $temp 'SHA256SUMS'
        $archive = Join-Path $temp $asset
        Save-Url "$baseUrl/SHA256SUMS" $sums
        Step "Downloading $asset"
        Save-Url "$baseUrl/$asset" $archive

        # The digest comes from the release's own SHA256SUMS. Names are compared
        # with any "./" or "*" prefix removed (older releases wrote "./name").
        $expected = @()
        foreach ($line in (Get-Content -LiteralPath $sums)) {
            if ($line -match '^([0-9a-fA-F]{64})\s+\*?(?:\./)?(.+?)\s*$' -and $Matches[2] -eq $asset) { $expected += $Matches[1].ToLowerInvariant() }
        }
        if ($expected.Count -eq 0) { Fail 1 "$asset is not listed in this release's SHA256SUMS; refusing to install it" }
        if ($expected.Count -gt 1) { Fail 1 "SHA256SUMS lists $asset more than once; refusing to install it" }
        $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected[0]) {
            Fail 1 "checksum mismatch for $asset`n       expected $($expected[0])`n       got      $actual`n       The download is corrupt or has been tampered with. Nothing was installed."
        }
        Ok 'checksum verified'

        [void](New-Item -ItemType Directory -Path $InstallDir -Force)
        $stage = Join-Path $InstallDir ('.wizard-update-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
        [void](New-Item -ItemType Directory -Path $stage)

        Step 'Unpacking'
        Test-ArchiveSafe $archive
        try {
            Expand-Archive -LiteralPath $archive -DestinationPath $stage -Force
        } catch {
            Fail 1 "could not unpack $asset`: $($_.Exception.Message)`n       If the message mentions a path that is too long, enable long paths (Group Policy 'Enable Win32 long paths') or choose a shorter -InstallDir."
        }

        $pkgDir = Join-Path $stage $pkg
        foreach ($required in @('backend\main.py', 'frontend\package.json', 'cli\wizard.exe')) {
            if (-not (Test-Path -LiteralPath (Join-Path $pkgDir $required))) { Fail 1 "the archive is not a complete Wizard package (missing $required)" }
        }

        # Run the new binary before anything is switched over.
        $stagedExe = Join-Path $pkgDir 'cli\wizard.exe'
        try { $reported = (& $stagedExe --version 2>&1) -join ' ' } catch { Fail 1 "the downloaded program does not run on this machine: $($_.Exception.Message)" }
        if ($LASTEXITCODE -ne 0) { Fail 1 "the downloaded program does not run on this machine: $reported" }
        if ($reported -notmatch [regex]::Escape($tag)) { Fail 1 "the downloaded program reports '$reported' but $tag was requested" }

        # --- switch over ---
        $destination = Join-Path $InstallDir $pkg
        $current = Join-Path $InstallDir 'current'
        if (Test-Path -LiteralPath $destination) {
            Write-Host "    $tag is already unpacked in $InstallDir; keeping it"
        } else {
            # Carry the existing configuration forward before the new package takes over.
            $oldEnv = Join-Path $current 'backend\.env'
            $newEnv = Join-Path $pkgDir 'backend\.env'
            if ((Test-Path -LiteralPath $oldEnv) -and -not (Test-Path -LiteralPath $newEnv)) {
                Copy-Item -LiteralPath $oldEnv -Destination $newEnv
                Debug-Line 'kept backend\.env from the previous release'
            }
            Move-Item -LiteralPath $pkgDir -Destination $destination
        }
        # bin\wizard.exe is a copy (a running program can be renamed but not
        # overwritten, and the update helper replaces it the same way).
        # Replace the launcher first, then change current. If the junction
        # swap fails, restore the old launcher as well as the old junction so
        # a working installation remains completely working.
        [void](New-Item -ItemType Directory -Path $binDir -Force)
        $nextExe = "$exe.next-" + [guid]::NewGuid().ToString('N').Substring(0, 8)
        $aside = $null
        Copy-Item -LiteralPath (Join-Path $destination 'cli\wizard.exe') -Destination $nextExe
        try {
            if (Test-Path -LiteralPath $exe) {
                $aside = "$exe.old-" + (Get-Date -Format 'yyyyMMddHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
                Move-Item -LiteralPath $exe -Destination $aside
            }
            Move-Item -LiteralPath $nextExe -Destination $exe
            try {
                Set-CurrentJunction $current $destination
            } catch {
                Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
                if ($aside -and (Test-Path -LiteralPath $aside)) { Move-Item -LiteralPath $aside -Destination $exe -Force }
                throw
            }
        } finally {
            Remove-Item -LiteralPath $nextExe -Force -ErrorAction SilentlyContinue
        }
        Get-ChildItem -LiteralPath $binDir -Filter 'wizard.exe.old-*' -ErrorAction SilentlyContinue | ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Force } catch { } }

        # --- PATH ---
        if ($modifyPath) { Add-UserPath $binDir } else { Debug-Line 'leaving PATH alone (-NoModifyPath)' }

        # --- verify ---
        $installed = (& $exe --version 2>&1) -join ' '
        if ($LASTEXITCODE -ne 0) { Fail 1 "the installed program failed to run: $installed" }
        Ok $installed

        Write-Host ''
        Write-Host "Wizard $tag is installed in $InstallDir"
        if (-not $modifyPath) {
            Write-Host 'PATH was not modified. To use wizard, add this directory to it:'
            Write-Host "    $binDir"
        }
        Write-Host ''
        Write-Host 'Next:'
        Write-Host '    wizard init      set up a provider and install dependencies'
        Write-Host '    wizard start     launch Wizard'
        Write-Host '    wizard doctor    check this installation'
    } finally {
        if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Never `exit` under irm | iex: it would close the user's terminal.
$code = 0
try {
    if ($Version -and $Version -notmatch '^[vV]?\d+\.\d+\.\d+$') { Fail 2 "not a release version: '$Version' (expected something like 1.0.13)" }
    Install-Wizard
} catch {
    Write-Host 'error ' -NoNewline -ForegroundColor Red
    Write-Host $_.Exception.Message
    $code = 1
    if ($_.Exception.Data.Contains('WizardCode')) { $code = [int]$_.Exception.Data['WizardCode'] }
} finally {
    $ErrorActionPreference = $script:OriginalErrorActionPreference
    $ProgressPreference = $script:OriginalProgressPreference
}
$global:LASTEXITCODE = $code
if ($code -ne 0 -and $MyInvocation.MyCommand.Path) { exit $code }
