# Tests for scripts/install.ps1 against a local fake release server.
#
#   $env:WIZARD_TEST_EXE = 'C:\path\to\wizard.exe'   # a wizard.exe built with -X ...BuildVersion=v9.9.9
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
function New-Release([string]$Tag, [string]$Reported = $Tag) {
    $pkg = "Wizard-$Tag-windows-amd64"
    $build = Join-Path $work "build-$Tag"
    if (Test-Path $build) { Remove-Item $build -Recurse -Force }
    foreach ($d in 'backend', 'frontend', 'cli') { [void](New-Item -ItemType Directory -Path (Join-Path $build "$pkg\$d") -Force) }
    Set-Content -LiteralPath (Join-Path $build "$pkg\backend\main.py") -Value ''
    Set-Content -LiteralPath (Join-Path $build "$pkg\frontend\package.json") -Value '{}'
    Copy-Item -LiteralPath $env:WIZARD_TEST_EXE -Destination (Join-Path $build "$pkg\cli\wizard.exe")
    $out = Join-Path $server $Tag
    if (Test-Path $out) { Remove-Item $out -Recurse -Force }
    [void](New-Item -ItemType Directory -Path $out)
    Compress-Archive -LiteralPath (Join-Path $build $pkg) -DestinationPath (Join-Path $out "$pkg.zip")
    $hash = (Get-FileHash -LiteralPath (Join-Path $out "$pkg.zip") -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -LiteralPath (Join-Path $out 'SHA256SUMS') -Value "$hash  $pkg.zip" -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $server 'LATEST') -Value $Tag -Encoding Ascii
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

function Invoke-Installer([string[]]$Arguments, [hashtable]$Environment = @{}, [string]$Base = $base) {
    $saved = @{}
    $vars = @{ WIZARD_RELEASE_BASE_URL = $Base } + $Environment
    foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $vars[$k]) }
    try {
        $log = Join-Path $work "out$script:case.txt"
        $p = Start-Process -FilePath $psExe -ArgumentList (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $installer) + $Arguments) `
            -Wait -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError "$log.err"
        $text = (Get-Content -LiteralPath $log -Raw -ErrorAction SilentlyContinue) + (Get-Content -LiteralPath "$log.err" -Raw -ErrorAction SilentlyContinue)
        return [pscustomobject]@{ Code = $p.ExitCode; Text = $text }
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
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
    Assert-True ($current.Target -like '*Wizard-v9.9.9-windows-amd64') 'current is a junction to the versioned package' ([string]$current.Target)
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

    New-Release 'v9.9.6'
    $sumLine = (Get-Content "$server\v9.9.6\SHA256SUMS" -Raw).Trim() -replace '  ', '  ./'
    Set-Content "$server\v9.9.6\SHA256SUMS" $sumLine -Encoding Ascii
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.6')
    Assert-True ($r.Code -eq 0) "a SHA256SUMS written as './name' (releases through v1.0.12) is accepted" $r.Text

    New-Release 'v9.9.5' 'v0.0.1'
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir, '-Version', '9.9.5')
    # The test exe always reports v9.9.9, so a request for 9.9.5 is a version mismatch.
    Assert-True ($r.Code -eq 1 -and $r.Text -match 'reports') 'a binary reporting another version is refused' $r.Text

    # 5. platform
    $dir = New-Case
    $r = Invoke-Installer @('-InstallDir', $dir) @{ PROCESSOR_ARCHITECTURE = 'ARM64'; PROCESSOR_ARCHITEW6432 = '' }
    Assert-True ($r.Code -eq 3 -and $r.Text -match 'ARM64') 'Windows on ARM64 fails cleanly (exit 3), no 404' $r.Text
    Assert-True (-not (Test-Path "$dir\bin")) 'nothing was installed on ARM64'
    $r = Invoke-Installer @('-InstallDir', $dir) @{ PROCESSOR_ARCHITECTURE = 'x86'; PROCESSOR_ARCHITEW6432 = '' }
    Assert-True ($r.Code -eq 3) 'an unsupported architecture is exit 3' $r.Text

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

    # 9. irm | iex must never close the caller's terminal, even on failure
    $script = (Get-Content -LiteralPath $installer -Raw)
    $tmpScript = Join-Path $work 'iex-host.ps1'
    Set-Content -LiteralPath $tmpScript -Value @"
`$env:WIZARD_RELEASE_BASE_URL = 'http://127.0.0.1:1'
`$env:WIZARD_VERSION = '9.9.9'
`$env:WIZARD_INSTALL_DIR = '$((New-Case).Replace("'", "''"))'
Invoke-Expression (Get-Content -LiteralPath '$($installer.Replace("'", "''"))' -Raw)
Write-Output 'HOST-STILL-ALIVE'
"@
    $out = & $psExe -NoProfile -ExecutionPolicy Bypass -File $tmpScript 2>&1 | Out-String
    Assert-True ($out -match 'HOST-STILL-ALIVE') 'a failing install under Invoke-Expression does not exit the host' $out
}
finally {
    if ($origPath -ne $null) { $envKey.SetValue('Path', $origPath, $(if ($origKind) { $origKind } else { [Microsoft.Win32.RegistryValueKind]::ExpandString })) } else { $envKey.DeleteValue('Path', $false) }
    $envKey.Close()
    if ($serverProc) { Stop-Process -Id $serverProc.Id -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n$script:pass passed, $script:fail failed"
if ($script:fail -gt 0) { exit 1 }
