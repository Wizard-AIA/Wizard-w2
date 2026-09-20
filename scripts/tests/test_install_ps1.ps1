# Tests for scripts/install.ps1 against a local fake release server.
#
#   $env:WIZARD_TEST_EXE = 'C:\path\to\wizard.exe'   # a wizard.exe built with -X ...BuildVersion=v9.9.9
#   $env:WIZARD_TEST_EXE_BETA = 'C:\path\to\wizard-beta.exe'   # the same, with BuildVersion=v9.9.10-beta.1
#   powershell -NoProfile -File scripts\tests\test_install_ps1.ps1     # Windows PowerShell 5.1
#   pwsh -NoProfile -File scripts\tests\test_install_ps1.ps1           # PowerShell 7+
#
# The installer is run as a child process of the same PowerShell edition, so its
# exit code and environment are real. The user PATH in the registry is saved and
# restored around the run. Windows only.
$ErrorActionPreference = 'Stop'

if (-not $env:WIZARD_TEST_EXE -or -not (Test-Path -LiteralPath $env:WIZARD_TEST_EXE)) {
    throw 'set WIZARD_TEST_EXE to a wizard.exe built with -X wizard/internal/compat.BuildVersion=v9.9.9'
}
if (-not $env:WIZARD_TEST_EXE_BETA -or -not (Test-Path -LiteralPath $env:WIZARD_TEST_EXE_BETA)) {
    throw 'set WIZARD_TEST_EXE_BETA to a wizard.exe built with -X wizard/internal/compat.BuildVersion=v9.9.10-beta.1'
}
$installer = (Resolve-Path (Join-Path $PSScriptRoot '..\install.ps1')).Path
$psExe = (Get-Process -Id $PID).Path
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('wizard-ps-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$server = Join-Path $work 'server'
[void](New-Item -ItemType Directory -Path $server -Force)
$script:pass = 0; $script:fail = 0; $script:case = 0

function Assert-True([bool]$Condition, [string]$Description, [string]$Detail = '') {
    if ($Condition) { $script:pass++; Write-Host "  ok   $Description" }
    else { $script:fail++; Write-Host "  FAIL $Description"; if ($Detail) { Write-Host ($Detail -split "`n" | ForEach-Object { "  | $_" }) -Separator "`n" } }
}

# --- fake release -------------------------------------------------------------
function New-Release([string]$Tag, [string]$Reported = $Tag, [string]$Exe = $env:WIZARD_TEST_EXE) {
    $pkg = "Wizard-$Tag-windows-amd64"
    $build = Join-Path $work "build-$Tag"
    if (Test-Path $build) { Remove-Item $build -Recurse -Force }
    foreach ($d in 'backend', 'frontend', 'cli') { [void](New-Item -ItemType Directory -Path (Join-Path $build "$pkg\$d") -Force) }
    Set-Content -LiteralPath (Join-Path $build "$pkg\backend\main.py") -Value ''
    Set-Content -LiteralPath (Join-Path $build "$pkg\frontend\package.json") -Value '{}'
    Copy-Item -LiteralPath $Exe -Destination (Join-Path $build "$pkg\cli\wizard.exe")
    $out = Join-Path $server $Tag
    if (Test-Path $out) { Remove-Item $out -Recurse -Force }
    [void](New-Item -ItemType Directory -Path $out)
    Compress-Archive -LiteralPath (Join-Path $build $pkg) -DestinationPath (Join-Path $out "$pkg.zip")
    $hash = (Get-FileHash -LiteralPath (Join-Path $out "$pkg.zip") -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $out 'SHA256SUMS') -Value "$hash  $pkg.zip" -Encoding Ascii
    # A mirror names the newest stable release in LATEST and, optionally, the
    # newest pre-release in LATEST-PRERELEASE. A pre-release never moves LATEST.
    $marker = if ($Tag -like '*-*') { 'LATEST-PRERELEASE' } else { 'LATEST' }
    Set-Content -LiteralPath (Join-Path $server $marker) -Value $Tag -Encoding Ascii
}

New-Release 'v9.9.9'
$port = (New-Object System.Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0))
$port.Start(); $portNumber = $port.LocalEndpoint.Port; $port.Stop()
$serverProc = Start-Process -FilePath python -ArgumentList @('-m', 'http.server', $portNumber, '--bind', '127.0.0.1', '--directory', $server) -PassThru -WindowStyle Hidden
$base = "http://127.0.0.1:$portNumber"
for ($i = 0; $i -lt 50; $i++) { try { Invoke-WebRequest "$base/LATEST" -UseBasicParsing | Out-Null; break } catch { Start-Sleep -Milliseconds 200 } }

# --- registry save/restore ----------------------------------------------------
$envKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$origPath = $envKey.GetValue('Path', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
$origKind = $null; try { $origKind = $envKey.GetValueKind('Path') } catch { }
function Set-TestPath([string]$Value) { $envKey.SetValue('Path', $Value, [Microsoft.Win32.RegistryValueKind]::ExpandString) }
function Get-RawPath { [string]$envKey.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) }

# Start-Process joins -ArgumentList with spaces and does not quote, so a path with
# spaces or a Unicode name would be split into several arguments.
function ConvertTo-ArgumentString([string[]]$Arguments) {
    ($Arguments | ForEach-Object {
        if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ }
    }) -join ' '
}

function Invoke-Installer([string[]]$Arguments, [hashtable]$Environment = @{}, [string]$Base = $base) {
    $saved = @{}
    $vars = @{ WIZARD_RELEASE_BASE_URL = $Base } + $Environment
    foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $vars[$k]) }
    try {
        $log = Join-Path $work "out$script:case.txt"
        $p = Start-Process -FilePath $psExe -ArgumentList (ConvertTo-ArgumentString (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer) + $Arguments)) `
            -Wait -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $text = (Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath "$log.err" -Raw -ErrorAction SilentlyContinue)
        return [pscustomobject]@{ Code = $p.ExitCode; Text = $text }
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
    }
}

# Runs the installer with a forged CPU architecture. The variables are set inside
# the child so this process's own PROCESSOR_ARCHITECTURE is never touched.
function Invoke-InstallerAsArch([string]$Arch, [string[]]$Arguments) {
    $quoted = ($Arguments | ForEach-Object { if ($_ -match '^-[A-Za-z]') { $_ } else { "'" + $_.Replace("'", "''") + "'" } }) -join ' '
    $script = "`$env:PROCESSOR_ARCHITECTURE = '$Arch'; Remove-Item Env:PROCESSOR_ARCHITEW6432 -ErrorAction SilentlyContinue; " +
        "& '$($installer.Replace("'", "''"))' $quoted; exit `$LASTEXITCODE"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    $saved = [Environment]::GetEnvironmentVariable('WIZARD_RELEASE_BASE_URL')
    [Environment]::SetEnvironmentVariable('WIZARD_RELEASE_BASE_URL', $base)
    try {
        $log = Join-Path $work "arch$script:case.txt"
        $p = Start-Process -FilePath $psExe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', $encoded) `
            -Wait -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $text = (Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath "$log.err" -Raw -ErrorAction SilentlyContinue)
        return [pscustomobject]@{ Code = $p.ExitCode; Text = $text }
    } finally {
        [Environment]::SetEnvironmentVariable('WIZARD_RELEASE_BASE_URL', $saved)
    }
}

function New-Case([string]$Name = 'Wizard') {
    $script:case++
    $dir = Join-Path $work "case$script:case\$Name"
    return $dir
}

try {
    Write-Host "== install.ps1 under PowerShell $($PSVersionTable.PSVersion) =="

    # 1. fresh install + PATH handling that preserves REG_EXPAND_SZ
    $dir = New-Case
    Set-TestPath '%USERPROFILE%\bin;C:\Windows'
    $r = Invoke-Installer @('-InstallDir', $dir, '-Yes')
    Assert-True ($r.Code -eq 0) 'fresh install succeeds' $r.Text
    Assert-True (Test-Path "$dir\bin\wizard.exe") 'bin\wizard.exe exists'
    Assert-True ((& "$dir\bin\wizard.exe" --version) -match 'v9\.9\.9') 'wizard --version reports the release'
    $current = Get-Item -LiteralPath "$dir\current" -Force
    # Windows PowerShell 5.1 returns a junction's Target as string[]; PowerShell 7 as string.
    $target = [string](@($current.Target)[0])
    Assert-True ($target -like '*Wizard-v9.9.9-windows-amd64') 'current is a junction to the versioned package' $target
    $raw = Get-RawPath
    Assert-True ($raw -like "*$dir\bin*") 'the bin directory is on the user PATH'
    Assert-True ($raw -like '%USERPROFILE%\bin*') 'the existing %VARIABLE% entry was not expanded'
    Assert-True ($envKey.GetValueKind('Path') -eq [Microsoft.Win32.RegistryValueKind]::ExpandString) 'the PATH value is still REG_EXPAND_SZ'

    # 2. idempotent: no duplicate PATH entry, exe replaced while old one is set aside
    $r = Invoke-Installer @('-InstallDir', $dir)
    Assert-True ($r.Code -eq 0) 're-running the installer succeeds' $r.Text
    $occurrences = ([regex]::Matches((Get-RawPath), [regex]::Escape("$dir\bin"))).Count
    Assert-True ($occurrences -eq 1) 'the PATH entry is not duplicated' "found $occurrences"
    Assert-True (@(Get-ChildItem "$dir\bin" -Filter 'wizard.exe.old-*' -ErrorAction SilentlyContinue).Count -eq 0) 'no stale wizard.exe.old-* left behind'
    Assert-True (@(Get-ChildItem $dir -Force -Filter '.wizard-update-*' -ErrorAction SilentlyContinue).Count -eq 0) 'no staging directory left behind'

    # 3. -NoModifyPath leaves the registry alone
    Set-TestPath 'C:\Windows'
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-NoModifyPath')
    Assert-True ($r.Code -eq 0 -and (Get-RawPath) -eq 'C:\Windows') '-NoModifyPath does not touch PATH' $r.Text

    # 4. integrity
    New-Release 'v9.9.8'
    Add-Content -LiteralPath "$server\v9.9.8\Wizard-v9.9.8-windows-amd64.zip" -Value 'corrupt' -Encoding Ascii
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.8')
    Assert-True ($r.Code -eq 1 -and $r.Text -match 'checksum mismatch') 'a corrupted archive is refused (exit 1)' $r.Text
    Assert-True (-not (Test-Path "$dir\current") -and -not (Test-Path "$dir\Wizard-v9.9.8-windows-amd64")) 'nothing was installed'

    New-Release 'v9.9.7'; Set-Content "$server\v9.9.7\SHA256SUMS" ''
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.7')
    Assert-True ($r.Code -eq 1 -and $r.Text -match 'not listed') 'an archive missing from SHA256SUMS is refused' $r.Text

    # The test exe always reports v9.9.9, so the './name' case must install 9.9.9.
    $dotDir = Join-Path $server 'v9.9.9'
    $sumLine = (Get-Content "$dotDir\SHA256SUMS" -Raw).Trim() -replace '  ', '  ./'
    Set-Content "$dotDir\SHA256SUMS" $sumLine -Encoding Ascii
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.9')
    Assert-True ($r.Code -eq 0) "a SHA256SUMS written as './name' (releases through v1.0.12) is accepted" $r.Text
    New-Release 'v9.9.9'   # restore a bare-name SHA256SUMS for the cases below

    New-Release 'v9.9.5' 'v0.0.1'
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.5')
    # The test exe always reports v9.9.9, so a request for 9.9.5 is a version mismatch.
    Assert-True ($r.Code -eq 1 -and $r.Text -match 'reports') 'a binary reporting another version is refused' $r.Text

    # 5. platform
    $dir = New-Case
    $r = Invoke-InstallerAsArch 'ARM64' @('-InstallDir', $dir)
    Assert-True ($r.Code -eq 3 -and $r.Text -match 'ARM64') 'Windows on ARM64 fails cleanly (exit 3), no 404' $r.Text
    Assert-True (-not (Test-Path "$dir\bin")) 'nothing was installed on ARM64'
    $r = Invoke-InstallerAsArch 'x86' @('-InstallDir', $dir)
    Assert-True ($r.Code -eq 3 -and $r.Text -match "unsupported CPU architecture: 'x86'") 'an unsupported architecture is exit 3' $r.Text

    # 6. usage and network
    $r = Invoke-Installer @('-Version', 'not-a-version')
    Assert-True ($r.Code -eq 2) 'a malformed version is exit 2' $r.Text
    $r = Invoke-Installer @('-InstallDir', $env:USERPROFILE)
    Assert-True ($r.Code -eq 2) 'installing into the profile directory itself is refused (exit 2)' $r.Text
    $r = Invoke-Installer @('-InstallDir', (New-Case), '-Version', '9.9.9') @{} 'http://127.0.0.1:1'
    Assert-True ($r.Code -eq 4 -and $r.Text -match 'HTTPS_PROXY') 'an unreachable server is a network error (exit 4)' $r.Text

    # 7. upgrade keeps .env and the old package; the junction swap must not delete it
    $dir = New-Case
    Assert-True ((Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.9')).Code -eq 0) 'install 9.9.9'
    Set-Content "$dir\current\backend\.env" 'GEMINI_API_KEY=keep-me'
    $sentinel = Join-Path $dir 'Wizard-v9.9.9-windows-amd64\backend\main.py'
    New-Release 'v9.9.9'   # the exe reports 9.9.9, so a same-version reinstall exercises the "already unpacked" path
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.9')
    Assert-True ($r.Code -eq 0 -and $r.Text -match 'already unpacked') 'reinstalling the same version keeps the unpacked package' $r.Text
    Assert-True (Test-Path $sentinel) 'the package directory survived the junction being re-pointed'
    Assert-True ((Get-Content "$dir\current\backend\.env" -Raw) -match 'keep-me') 'the user backend\.env is untouched'

    # 8. awkward paths
    $dir = New-Case 'Wizard with spaces'
    $r = Invoke-Installer @('-InstallDir', $dir)
    Assert-True ($r.Code -eq 0 -and (& "$dir\bin\wizard.exe" --version) -match 'v9.9.9') 'a path with spaces works' $r.Text
    $unicode = New-Case ("W" + [char]0x00EF + "z" + [char]0x00E4 + "rd " + [char]0x00FC + "ser")
    $r = Invoke-Installer @('-InstallDir', $unicode)
    Assert-True ($r.Code -eq 0 -and (Test-Path "$unicode\bin\wizard.exe")) 'a Unicode path works' $r.Text

    # 10. pre-releases and channels. The real wizard.exe does the channel saving, so
    # WIZARD_CONFIG_DIR points each case at its own directory to read the result.
    function Get-CurrentTarget([string]$Dir) { [string](@((Get-Item -LiteralPath "$Dir\current" -Force).Target)[0]) }

    foreach ($bad in '9.9.9-beta', '9.9.9-beta.0', '9.9.9-beta.01', '9.9.9-preview.1', '9.9.9-Beta.1', '9.9.9-beta.1.2', '9.9.9-beta.1+x', '9.9.9+x', '09.9.9', '9.9.9.1', '9.9', 'latest') {
        $r = Invoke-Installer @('-InstallDir', (New-Case), '-Version', $bad)
        Assert-True ($r.Code -eq 2) "-Version $bad is not a release version (exit 2)" $r.Text
    }
    $r = Invoke-Installer @('-InstallDir', (New-Case), '-Channel', 'beta')
    Assert-True ($r.Code -eq 2) 'an unknown channel is exit 2' $r.Text
    $r = Invoke-Installer @('-InstallDir', (New-Case), '-PreRelease', '-Channel', 'stable')
    Assert-True ($r.Code -eq 2) '-PreRelease with -Channel stable is a contradiction (exit 2)' $r.Text

    New-Release 'v9.9.9'
    New-Release 'v9.9.10-beta.1' 'v9.9.10-beta.1' $env:WIZARD_TEST_EXE_BETA   # LATEST stays v9.9.9; LATEST-PRERELEASE names the beta

    $cfg = Join-Path $work 'cfg-default'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Yes') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.9-windows-amd64') 'a default install ignores a newer pre-release' "$(Get-CurrentTarget $dir)`n$($r.Text)"
    Assert-True (-not (Test-Path "$cfg\update-channel")) 'no channel was written, none was chosen'

    $cfg = Join-Path $work 'cfg-pre'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-PreRelease') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.10-beta.1-windows-amd64') '-PreRelease installs a pre-release that is newer than stable' "$(Get-CurrentTarget $dir)`n$($r.Text)"
    Assert-True ((& "$dir\bin\wizard.exe" --version) -match 'v9\.9\.10-beta\.1') 'bin\wizard.exe reports the pre-release'
    Assert-True ((Test-Path "$cfg\update-channel") -and ((Get-Content "$cfg\update-channel" -Raw).Trim() -eq 'pre-release')) 'the real wizard.exe saved the pre-release channel' $r.Text
    Assert-True ($r.Text -match 'wizard channel stable') 'the closing note says how to leave the pre-release channel' $r.Text

    $cfg = Join-Path $work 'cfg-env'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Yes') @{ WIZARD_CONFIG_DIR = $cfg; WIZARD_CHANNEL = 'pre-release' }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.10-beta.1-windows-amd64') 'WIZARD_CHANNEL=pre-release does the same' $r.Text

    $cfg = Join-Path $work 'cfg-channel'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Channel', 'pre-release') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.10-beta.1-windows-amd64') '-Channel pre-release is the same as -PreRelease' $r.Text

    $cfg = Join-Path $work 'cfg-version'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.10-beta.1') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.10-beta.1-windows-amd64') '-Version names a pre-release directly' $r.Text
    Assert-True (-not (Test-Path "$cfg\update-channel")) 'naming a version chooses no channel'

    $cfg = Join-Path $work 'cfg-stable'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Channel', 'stable', '-Version', '9.9.10-beta.1') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Test-Path "$cfg\update-channel") -and ((Get-Content "$cfg\update-channel" -Raw).Trim() -eq 'stable')) '-Channel stable with a pre-release version keeps the stable choice' $r.Text

    # A pre-release older than the stable release, or a candidate of the same
    # version, is not offered.
    New-Release 'v9.9.9'; New-Release 'v9.9.8-beta.1'
    $cfg = Join-Path $work 'cfg-older'; $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-PreRelease') @{ WIZARD_CONFIG_DIR = $cfg }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.9-windows-amd64') '-PreRelease with only an older pre-release installs the stable release' $r.Text
    Assert-True ((Test-Path "$cfg\update-channel") -and ((Get-Content "$cfg\update-channel" -Raw).Trim() -eq 'pre-release')) 'the channel choice is still saved' $r.Text

    New-Release 'v9.9.9-rc.3'
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-PreRelease') @{ WIZARD_CONFIG_DIR = (Join-Path $work 'cfg-rc') }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.9-windows-amd64') 'a release candidate of the current stable version does not replace it' $r.Text

    Remove-Item -LiteralPath "$server\LATEST-PRERELEASE" -Force
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-PreRelease') @{ WIZARD_CONFIG_DIR = (Join-Path $work 'cfg-nomirror') }
    Assert-True ($r.Code -eq 0 -and (Get-CurrentTarget $dir) -like '*Wizard-v9.9.9-windows-amd64') '-PreRelease against a mirror without pre-releases installs stable' $r.Text

    # 11. the pure functions, taken from the installer itself
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($installer, [ref]$null, [ref]$null)
    $wanted = @('Fail', 'Remove-TagPrefix', 'ConvertTo-Tag', 'Get-ReleaseKey', 'Get-NewestTag', 'Select-PublishedPreReleases')
    $definitions = $ast.FindAll({ param($node) ($node -is [System.Management.Automation.Language.FunctionDefinitionAst]) -and ($wanted -contains $node.Name) }, $true)
    Assert-True (@($definitions).Count -eq 6) 'the helper functions were found in the installer' "found $(@($definitions).Count)"
    foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }
    $installerText = Get-Content -LiteralPath $installer -Raw
    $script:PreReleaseTagPattern = [regex]::Match($installerText, "PreReleaseTagPattern = '([^']+)'").Groups[1].Value
    $script:ReleaseVersionPattern = [regex]::Match($installerText, "ReleaseVersionPattern = '([^']+)'").Groups[1].Value
    Assert-True (($script:PreReleaseTagPattern.Length -gt 10) -and ($script:ReleaseVersionPattern.Length -gt 10)) 'the tag patterns were found in the installer' "$script:PreReleaseTagPattern | $script:ReleaseVersionPattern"

    # The grammar, at its edges. The other three implementations reject every one
    # of these; this one has to agree.
    foreach ($good in @('1.0.14', 'v1.0.14', '1.0.14-beta.1', 'v10.20.30-rc.12', ' v1.0.14 ')) {
        $accepted = try { $null = ConvertTo-Tag $good; $true } catch { $false }
        Assert-True $accepted "the grammar accepts '$good'"
    }
    $arabicDigit = "1.0.1$([char]0x0664)"
    foreach ($bad in @('V1.0.14', 'vv1.0.14', "1.0.14`nx", '1.0.14 x', '01.0.14', '1.0.14-beta', '1.0.14-beta.0', '1.0.14-Beta.1', '1.0.14+build', '1.0', $arabicDigit, '')) {
        $accepted = try { $null = ConvertTo-Tag $bad; $true } catch { $false }
        Assert-True (-not $accepted) "the grammar rejects $($bad -replace "`n", '<LF>')"
    }

    Assert-True ((Get-NewestTag @('v1.0.13', 'v1.0.14-beta.2', 'v1.0.14-beta.10')) -eq 'v1.0.14-beta.10') 'beta.10 outranks beta.2 (numeric, not text)'
    Assert-True ((Get-NewestTag @('v1.0.14-rc.9', 'v1.0.14', 'v1.0.14-beta.1')) -eq 'v1.0.14') 'a stable release outranks its own candidates'
    Assert-True ((Get-NewestTag @('v1.0.14-beta.9', 'v1.0.14-rc.1', 'v1.0.14-alpha.20')) -eq 'v1.0.14-rc.1') 'rc outranks beta outranks alpha'
    Assert-True ((Get-NewestTag @('v1.0.9', 'v1.0.10')) -eq 'v1.0.10') '1.0.10 outranks 1.0.9 (numeric, not text)'
    Assert-True ((Get-NewestTag @('v1.0.13', 'v1.0.14-alpha.1')) -eq 'v1.0.14-alpha.1') 'a pre-release of a later version outranks stable'
    Assert-True ((Get-NewestTag @('v2.0.0', 'v1.9.9-rc.1')) -eq 'v2.0.0') 'a later stable outranks an earlier pre-release'

    $fixture = @(
        [pscustomobject]@{ tag_name = 'v1.0.14-beta.2'; prerelease = $true; draft = $false },
        [pscustomobject]@{ tag_name = 'v1.0.14-beta.10'; prerelease = $true; draft = $false },
        [pscustomobject]@{ tag_name = 'v1.0.13'; prerelease = $false; draft = $false },
        [pscustomobject]@{ tag_name = 'v1.0.14-rc.1'; prerelease = $false; draft = $false },
        [pscustomobject]@{ tag_name = 'v9.9.9-beta.1'; prerelease = $true; draft = $true },
        [pscustomobject]@{ tag_name = 'nightly'; prerelease = $true; draft = $false },
        [pscustomobject]@{ tag_name = 'v2.2.1'; prerelease = $false; draft = $false },
        [pscustomobject]@{ tag_name = 'v2.0.0-w2-planning'; prerelease = $false; draft = $false },
        [pscustomobject]@{ tag_name = 'v1.0.14-alpha.1'; prerelease = $true; draft = $false })
    $got = @(Select-PublishedPreReleases $fixture) -join ','
    Assert-True ($got -eq 'v1.0.14-beta.2,v1.0.14-beta.10,v1.0.14-alpha.1') 'published pre-releases: drafts, unflagged, odd tags and the v2.x line are excluded' $got

    # 9. irm | iex must never close the caller's terminal, even on failure
    $script = (Get-Content -LiteralPath $installer -Raw)
    # `irm | iex` is typed at a prompt, where $MyInvocation.MyCommand.Path is empty;
    # a host script *file* has a Path and the installer would rightly exit. Run the
    # host as an in-memory command so it behaves like the prompt.
    $hostScript = @"
`$env:WIZARD_RELEASE_BASE_URL = 'http://127.0.0.1:1'
`$env:WIZARD_VERSION = '9.9.9'
`$env:WIZARD_INSTALL_DIR = '$((New-Case).Replace("'", "''"))'
`$ErrorActionPreference = 'SilentlyContinue'
`$ProgressPreference = 'Continue'
Invoke-Expression (Get-Content -LiteralPath '$($installer.Replace("'", "''"))' -Raw)
Write-Output 'HOST-STILL-ALIVE'
Write-Output "PREFERENCES=`$ErrorActionPreference/`$ProgressPreference"
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($hostScript))
    $out = & $psExe -NoProfile -ExecutionPolicy Bypass -EncodedCommand $encoded 2>&1 | Out-String
    Assert-True ($out -match 'HOST-STILL-ALIVE') 'a failing install under Invoke-Expression does not exit the host' $out
    Assert-True ($out -match 'PREFERENCES=SilentlyContinue/Continue') 'Invoke-Expression restores the caller preference variables' $out
}
finally {
    if ($origPath -ne $null) { $envKey.SetValue('Path', $origPath, $(if ($origKind) { $origKind } else { [Microsoft.Win32.RegistryValueKind]::ExpandString })) } else { $envKey.DeleteValue('Path', $false) }
    $envKey.Close()
    if ($serverProc) { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n$script:pass passed, $script:fail failed"
if ($script:fail -gt 0) { exit 1 }
