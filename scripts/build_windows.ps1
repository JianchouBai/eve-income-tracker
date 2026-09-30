[CmdletBinding()]
param(
    [string]$PythonExecutable = "",
    [string]$ReleaseConfig = "",
    [string]$InnoCompiler = "",
    [string]$WebView2Bootstrapper = "",
    [string]$ReleaseBase = "",
    [switch]$FixtureMode,
    [string]$FixtureRoot = "",
    [ValidateSet("Clean", "Install", "Uninstall")]
    [string]$FixtureAction = "Clean",
    [switch]$DeleteUserData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Resolve-CanonicalPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue,
        [switch]$RequireExisting
    )

    if ([string]::IsNullOrWhiteSpace($PathValue)) {
        throw "Path must not be empty."
    }
    $expanded = [Environment]::ExpandEnvironmentVariables($PathValue)
    $fullPath = [System.IO.Path]::GetFullPath($expanded)
    if ($RequireExisting) {
        return (Resolve-Path -LiteralPath $fullPath -ErrorAction Stop).Path
    }
    return $fullPath
}

function Test-PathSame {
    param(
        [Parameter(Mandatory = $true)]
        [string]$First,
        [Parameter(Mandatory = $true)]
        [string]$Second
    )

    $firstNormalized = (Resolve-CanonicalPath $First).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    $secondNormalized = (Resolve-CanonicalPath $Second).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
    return [StringComparer]::OrdinalIgnoreCase.Equals(
        $firstNormalized,
        $secondNormalized
    )
}

function Remove-OwnedDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PathValue,
        [Parameter(Mandatory = $true)]
        [string[]]$AllowedPaths
    )

    $candidate = Resolve-CanonicalPath $PathValue
    $allowed = $false
    foreach ($allowedPath in $AllowedPaths) {
        if (Test-PathSame $candidate $allowedPath) {
            $allowed = $true
            break
        }
    }
    if (-not $allowed) {
        throw "Refusing to clean a directory outside the exact allowlist."
    }

    $root = [System.IO.Path]::GetPathRoot($candidate)
    if (Test-PathSame $candidate $root) {
        throw "Refusing to clean a filesystem root."
    }
    if (Test-Path -LiteralPath $candidate) {
        $item = Get-Item -LiteralPath $candidate -Force
        if (-not $item.PSIsContainer) {
            throw "Refusing to recursively clean a non-directory path."
        }
        Remove-Item -LiteralPath $candidate -Recurse -Force
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [string]$Description = $FilePath
    )

    Write-Host "[run] $Description"
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Invoke-Fixture {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Root,
        [Parameter(Mandatory = $true)]
        [string]$Action,
        [switch]$DeleteData
    )

    $fixture = Resolve-CanonicalPath $Root
    [System.IO.Directory]::CreateDirectory($fixture) | Out-Null

    $cleanTargets = @(
        (Join-Path $fixture "build\windows"),
        (Join-Path $fixture "dist\windows"),
        (Join-Path $fixture "installer-output")
    )
    $localAppData = Join-Path $fixture "LocalAppData"
    $programRoot = Join-Path $localAppData "Programs\EVE Income Tracker"
    $userDataRoot = Join-Path $localAppData "EVE Income Tracker"
    $sourceRoot = Join-Path $fixture "source\EVE Income Tracker"

    switch ($Action) {
        "Clean" {
            foreach ($target in $cleanTargets) {
                Remove-OwnedDirectory $target $cleanTargets
            }
        }
        "Install" {
            if (-not (Test-Path -LiteralPath $sourceRoot -PathType Container)) {
                throw "Fixture source directory is missing."
            }
            Remove-OwnedDirectory $programRoot @($programRoot)
            [System.IO.Directory]::CreateDirectory(
                [System.IO.Path]::GetDirectoryName($programRoot)
            ) | Out-Null
            Copy-Item -LiteralPath $sourceRoot -Destination $programRoot -Recurse
        }
        "Uninstall" {
            Remove-OwnedDirectory $programRoot @($programRoot)
            if ($DeleteData) {
                Remove-OwnedDirectory $userDataRoot @($userDataRoot)
            }
        }
    }
    Write-Host "FIXTURE_OK action=$Action"
}

if ($FixtureMode) {
    if ([string]::IsNullOrWhiteSpace($FixtureRoot)) {
        throw "FixtureRoot is required in fixture mode."
    }
    Invoke-Fixture -Root $FixtureRoot -Action $FixtureAction -DeleteData:$DeleteUserData
    exit 0
}

$projectRoot = Resolve-CanonicalPath (Split-Path -Parent $PSScriptRoot) -RequireExisting
Set-Location -LiteralPath $projectRoot

if ($env:OS -ne "Windows_NT" -or -not [Environment]::Is64BitOperatingSystem) {
    throw "Windows x64 is required."
}
if (-not [Environment]::Is64BitProcess) {
    throw "The build shell must be a 64-bit process."
}

$gitStatus = & git status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0) {
    throw "git status failed."
}
if ($gitStatus) {
    throw "A clean release commit is required before building."
}

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $pythonProbe = @(& py -3.12 -c "import sys; print(sys.executable)")
    if ($LASTEXITCODE -ne 0 -or -not $pythonProbe) {
        throw "Python 3.12 was not found."
    }
    $PythonExecutable = $pythonProbe[-1].Trim()
}
$PythonExecutable = Resolve-CanonicalPath $PythonExecutable -RequireExisting
$pythonIdentity = @(& $PythonExecutable -c "import platform, struct, sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{struct.calcsize(chr(80))*8}|{platform.system()}')")
if ($LASTEXITCODE -ne 0 -or $pythonIdentity[-1].Trim() -ne "3.12|64|Windows") {
    throw "Python 3.12 in a 64-bit Windows process is required."
}

if ([string]::IsNullOrWhiteSpace($ReleaseConfig)) {
    $ReleaseConfig = Join-Path $projectRoot "release-config.local.json"
}
$releaseConfigPath = Resolve-CanonicalPath $ReleaseConfig -RequireExisting
$validateConfigCode = "from pathlib import Path; import sys; from scripts.build_support import validate_release_config; validate_release_config(Path(sys.argv[1]))"
Invoke-Checked $PythonExecutable @("-c", $validateConfigCode, $releaseConfigPath) "validate public release config"

if ([string]::IsNullOrWhiteSpace($WebView2Bootstrapper)) {
    $WebView2Bootstrapper = Join-Path $projectRoot "vendor\MicrosoftEdgeWebview2Setup.exe"
}
$bootstrapperPath = Resolve-CanonicalPath $WebView2Bootstrapper -RequireExisting
if ([System.IO.Path]::GetFileName($bootstrapperPath) -cne "MicrosoftEdgeWebview2Setup.exe") {
    throw "The exact MicrosoftEdgeWebview2Setup.exe bootstrapper is required."
}
$bootstrapperSignature = Get-AuthenticodeSignature -LiteralPath $bootstrapperPath
if (
    $bootstrapperSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid -or
    $null -eq $bootstrapperSignature.SignerCertificate -or
    $bootstrapperSignature.SignerCertificate.Subject -notmatch "Microsoft Corporation"
) {
    throw "The WebView2 bootstrapper must have a valid Microsoft signature."
}

if ([string]::IsNullOrWhiteSpace($InnoCompiler)) {
    $programFilesX86 = [Environment]::GetFolderPath("ProgramFilesX86")
    $programFiles = [Environment]::GetFolderPath("ProgramFiles")
    $innoCandidates = @(@(
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
        (Join-Path $programFiles "Inno Setup 7\ISCC.exe"),
        (Join-Path $programFilesX86 "Inno Setup 7\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
    if (-not $innoCandidates) {
        throw "ISCC.exe from Inno Setup 7 was not found."
    }
    $InnoCompiler = $innoCandidates[0]
}
$innoCompilerPath = Resolve-CanonicalPath $InnoCompiler -RequireExisting
if ([System.IO.Path]::GetFileName($innoCompilerPath) -cne "ISCC.exe") {
    throw "InnoCompiler must point to ISCC.exe."
}
$innoChineseLanguage = Join-Path (
    Split-Path -Parent $innoCompilerPath
) "Languages\ChineseSimplified.isl"
if (-not (Test-Path -LiteralPath $innoChineseLanguage -PathType Leaf)) {
    throw "Inno Setup 7 with the official ChineseSimplified.isl is required."
}

Write-Host "[gate] python -m pytest"
Invoke-Checked $PythonExecutable @("-m", "pytest", "-q") "python -m pytest"
Write-Host "[gate] ruff check app scripts tests"
Invoke-Checked $PythonExecutable @("-m", "ruff", "check", "app", "scripts", "tests") "ruff check app scripts tests"
Write-Host "[gate] git diff --check (release range)"
$headCommit = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $headCommit -notmatch "^[0-9a-f]{40,64}$") {
    throw "Unable to determine the current Git commit."
}
$requestedReleaseBase = $ReleaseBase.Trim()
if ($requestedReleaseBase) {
    $releaseBaseOutput = @(& git rev-parse --verify "$requestedReleaseBase^{commit}")
    if (
        $LASTEXITCODE -ne 0 -or
        -not $releaseBaseOutput -or
        $releaseBaseOutput[-1].Trim() -notmatch "^[0-9a-f]{40,64}$"
    ) {
        throw "ReleaseBase must identify a Git commit."
    }
    $releaseBase = $releaseBaseOutput[-1].Trim()
    Invoke-Checked "git" @("merge-base", "--is-ancestor", $releaseBase, "HEAD") "ReleaseBase ancestry check"
} else {
    $mergeBaseOutput = @(& git merge-base origin/main HEAD)
    if (
        $LASTEXITCODE -ne 0 -or
        -not $mergeBaseOutput -or
        $mergeBaseOutput[-1].Trim() -notmatch "^[0-9a-f]{40,64}$"
    ) {
        throw "ReleaseBase is required when origin/main cannot determine the release range."
    }
    $releaseBase = $mergeBaseOutput[-1].Trim()
    if ($releaseBase -eq $headCommit) {
        throw "ReleaseBase is required when HEAD equals origin/main."
    }
}
if ($releaseBase -eq $headCommit) {
    throw "ReleaseBase must precede HEAD."
}
Write-Host "[gate] release range $releaseBase..HEAD"
Invoke-Checked "git" @("diff", "--check", $releaseBase, "HEAD") "git diff --check"

$buildWindowsPath = Join-Path $projectRoot "build\windows"
$distWindowsPath = Join-Path $projectRoot "dist\windows"
$installerOutputPath = Join-Path $projectRoot "installer-output"
$cleanTargets = @(
    $buildWindowsPath,
    $distWindowsPath,
    $installerOutputPath
)
foreach ($target in $cleanTargets) {
    Remove-OwnedDirectory $target $cleanTargets
}
foreach ($target in $cleanTargets) {
    [System.IO.Directory]::CreateDirectory($target) | Out-Null
}

$generatedPath = Join-Path $buildWindowsPath "generated"
$iconPath = Join-Path $generatedPath "app-icon.ico"
$prepareCode = @"
from pathlib import Path
import sys
from scripts.build_support import write_release_config, write_windows_version_info
write_release_config(Path(sys.argv[1]), Path(sys.argv[2]))
write_windows_version_info(Path(sys.argv[2]))
"@
Invoke-Checked $PythonExecutable @(
    "-c",
    $prepareCode,
    $releaseConfigPath,
    $generatedPath
) "validate and generate public release metadata"
Invoke-Checked $PythonExecutable @(
    "-m",
    "scripts.generate_windows_icon",
    (Join-Path $projectRoot "assets\app-icon.svg"),
    $iconPath
) "generate Windows icon"

$versionCommand = "from app.version import APP_VERSION; print(APP_VERSION)"
$versionOutput = @(& $PythonExecutable -c $versionCommand)
if ($LASTEXITCODE -ne 0 -or -not $versionOutput) {
    throw "Unable to read APP_VERSION."
}
$version = $versionOutput[-1].Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "APP_VERSION must be semantic major.minor.patch."
}

Invoke-Checked $PythonExecutable @(
    "-m",
    "PyInstaller",
    "--noconfirm",
    "--clean",
    "--workpath",
    (Join-Path $buildWindowsPath "pyinstaller"),
    "--distpath",
    $distWindowsPath,
    (Join-Path $projectRoot "packaging\eve_income_tracker.spec")
) "PyInstaller onedir build"

$onedirPath = Join-Path $distWindowsPath "EVE Income Tracker"
$frozenExecutable = Join-Path $onedirPath "EVE Income Tracker.exe"
if (-not (Test-Path -LiteralPath $frozenExecutable -PathType Leaf)) {
    throw "PyInstaller did not produce the expected EVE Income Tracker.exe."
}

$smokeRoot = Join-Path $buildWindowsPath "smoke"
[System.IO.Directory]::CreateDirectory($smokeRoot) | Out-Null
Invoke-Checked $PythonExecutable @(
    "-m",
    "scripts.smoke_desktop",
    $frozenExecutable,
    "--temp-parent",
    $smokeRoot
) "frozen desktop smoke"

Invoke-Checked $innoCompilerPath @(
    "/DMyAppVersion=$version",
    "/DMySourceDir=$onedirPath",
    "/DMyOutputDir=$installerOutputPath",
    "/DMyWebView2Bootstrapper=$bootstrapperPath",
    "/DMyIcon=$iconPath",
    (Join-Path $projectRoot "packaging\eve-income-tracker.iss")
) "Inno Setup compiler"

$installerPath = Join-Path $installerOutputPath "EVE-Income-Tracker-$version-x64-Setup.exe"
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "Inno Setup did not produce the expected versioned installer."
}
$hash = Get-FileHash -LiteralPath $installerPath -Algorithm SHA256
Write-Host "INSTALLER=$($hash.Path)"
Write-Host "SHA256=$($hash.Hash)"
