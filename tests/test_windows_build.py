from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "packaging" / "eve-income-tracker.iss"
BUILD = ROOT / "scripts" / "build_windows.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_installer_uses_stable_per_user_identity_and_shortcuts():
    script = read(ISS)

    app_ids = re.findall(r"(?im)^AppId=(.+)$", script)
    assert app_ids == ["{{59D95F12-7D3B-4C8A-A57D-98F836769441}"]
    assert "PrivilegesRequired=lowest" in script
    assert "PrivilegesRequiredOverridesAllowed" not in script
    assert r"DefaultDirName={localappdata}\Programs\EVE Income Tracker" in script
    assert "CloseApplications=yes" in script
    assert "RestartApplications=no" in script
    assert r"AppMutex=Local\EVEIncomeTracker.SingleInstance" in script
    assert r"{autoprograms}\EVE Income Tracker" in script
    assert r"{autodesktop}\EVE Income Tracker" in script
    assert r'Filename: "{app}\EVE Income Tracker.exe"' in script


def test_installer_upgrade_and_uninstall_preserve_data_by_default():
    script = read(ISS)
    lowered = script.lower()

    assert "UsePreviousAppDir=yes" in script
    assert "CreateUninstallRegKey=yes" in script
    assert "[installdelete]" not in lowered
    assert "[uninstalldelete]" not in lowered
    assert "UninstallSilent" in script
    assert "DeleteUserData := False" in script
    assert "usPostUninstall" in script
    assert "DelTree(UserDataRoot, True, True, True)" in script
    assert "默认保留钱包数据库、授权密钥、日志和备份" in script
    assert "确认永久删除以下完整目录" in script
    assert "RegWrite" not in script
    assert "[Registry]" not in script
    assert "RegDeleteKeyIncludingSubkeys" not in script


def test_explicit_uninstall_requires_complete_current_registry_and_marker():
    script = read(ISS)

    for value in ("'DataRoot'", "'DataRootId'", "'DataRootSchema'"):
        assert value in script
    assert "RegQueryStringValue(HKCU" in script
    assert "RegQueryDWordValue(HKCU" in script
    for residue in (
        "PreviousDataRoot",
        "PreviousDataRootId",
        "PreviousDataRootSchema",
        "PreviousDataRootAbsent",
        "CurrentSwitchCommitted",
    ):
        assert residue in script
    for guard in (
        "IsUuidV4",
        "IsSafeDataRoot",
        "IsCanonicalMarker",
        "IsReparsePoint(CurrentPath)",
        "IsReparsePoint(MarkerPath)",
        "JsonEscape",
    ):
        assert guard in script
    assert ".eve-income-tracker-data.json" in script
    assert "DataRootSchema = 1" in script
    assert "Pos('root_id'" not in script


def test_uninstall_deletes_only_verified_root_and_keeps_registry_on_failure():
    script = read(ISS)
    uninstall = script.split("procedure CurUninstallStepChanged", 1)[1]

    assert "ValidateDeleteTarget(UserDataRoot" in uninstall
    assert "if DelTree(UserDataRoot, True, True, True) then" in uninstall
    assert "RegDeleteKeyIfEmpty" in uninstall
    assert uninstall.index("ValidateDeleteTarget(UserDataRoot") < uninstall.index(
        "DelTree(UserDataRoot, True, True, True)"
    )
    assert uninstall.index("DelTree(UserDataRoot, True, True, True)") < uninstall.index(
        "RegDeleteKeyIfEmpty"
    )
    assert r"{localappdata}\EVE Income Tracker" not in uninstall

def _inno_function(script: str, name: str) -> str:
    body = script.split(f"function {name}", 1)[1]
    return body.split("\nfunction ", 1)[0].split("\nprocedure ", 1)[0]


@pytest.mark.parametrize(
    ("case", "function", "guard"),
    [
        ("missing triple", "ReadDeleteTarget", "HasRegistryType('DataRoot'"),
        ("wrong schema", "ReadDeleteTarget", "(DataRootSchema = 1)"),
        ("previous root residue", "ReadDeleteTarget", "'PreviousDataRoot'"),
        ("switch residue", "ReadDeleteTarget", "'CurrentSwitchCommitted'"),
        ("pending migration", "ReadDeleteTarget", "'PendingMigrationTarget'"),
        ("invalid UUID", "ReadDeleteTarget", "IsUuidV4(RootId)"),
        ("disk or broad root", "IsSafeDataRoot", "ExtractFileName(Root)"),
        ("system path", "IsSafeDataRoot", "ExpandConstant('{win}')"),
        ("install overlap", "IsSafeDataRoot", "ExpandConstant('{app}')"),
        ("root reparse", "IsSafeDataRoot", "IsReparsePoint(CurrentPath)"),
        ("marker reparse", "IsCanonicalMarker", "IsReparsePoint(MarkerPath)"),
        ("missing marker", "IsCanonicalMarker", "LoadStringFromFile(MarkerPath"),
        ("spoofed marker", "IsCanonicalMarker", "Contents = Utf8Encode(Expected)"),
    ],
)
def test_installer_malicious_state_matrix_has_delete_guards(case, function, guard):
    script = read(ISS)
    assert guard in _inno_function(script, function), case


def test_installer_rejects_short_malformed_values_before_indexing():
    script = read(ISS)
    uuid = _inno_function(script, "IsUuidV4")
    path = _inno_function(script, "IsSafeDataRoot")
    timestamp = _inno_function(script, "IsUtcTimestamp")

    assert uuid.index("if Length(Value) <> 36 then") < uuid.index("Value[15]")
    assert path.index("if Length(Root) < 5 then") < path.index("Root[2]")
    assert timestamp.index("if Length(Value) < 20 then") < timestamp.index("Value[5]")

def test_installer_checks_and_bootstraps_official_webview2_runtime():
    script = read(ISS)

    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in script
    assert r"Software\Microsoft\EdgeUpdate\Clients" in script
    assert "HasPositiveWebView2Version(HKLM32)" in script
    assert "HasPositiveWebView2Version(HKCU)" in script
    assert "'pv'" in script
    assert "StrToVersion" in script
    assert "ComparePackedVersion" in script
    assert "ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe')" in script
    assert "'/silent /install'" in script
    assert script.count("IsWebView2Installed") >= 3
    assert "WebView2 Runtime 安装失败" in script
    assert "MicrosoftEdgeWebview2Setup.exe" in script
    assert "Flags: dontcopy noencryption" in script


def test_installer_metadata_is_supplied_by_build_and_output_is_versioned():
    script = read(ISS)

    for define in [
        "MyAppVersion",
        "MySourceDir",
        "MyOutputDir",
        "MyWebView2Bootstrapper",
        "MyIcon",
    ]:
        assert f"#ifndef {define}" in script
    assert "AppVersion={#MyAppVersion}" in script
    assert "VersionInfoVersion={#MyAppVersion}" in script
    assert "OutputBaseFilename=EVE-Income-Tracker-{#MyAppVersion}-x64-Setup" in script
    assert 'Source: "{#MySourceDir}\\*"' in script
    assert "recursesubdirs createallsubdirs" in script


def test_build_pipeline_has_fail_fast_release_gates_and_scoped_cleanup():
    script = read(BUILD)

    for token in [
        '$ErrorActionPreference = "Stop"',
        "Windows x64",
        "Python 3.12",
        "[Environment]::Is64BitProcess",
        "git status --porcelain",
        "release-config.local.json",
        "MicrosoftEdgeWebview2Setup.exe",
        "Get-AuthenticodeSignature",
        "ISCC.exe",
        "Inno Setup 7",
        "ChineseSimplified.isl",
        "python -m pytest",
        "ruff check app scripts tests",
        "git diff --check",
        "PyInstaller",
        "scripts.smoke_desktop",
        "eve-income-tracker.iss",
        "Get-FileHash",
        "SHA256",
    ]:
        assert token in script

    assert '"build\\windows"' in script
    assert '"dist\\windows"' in script
    assert '"installer-output"' in script
    assert "Remove-OwnedDirectory" in script
    assert "Remove-Item -LiteralPath" in script
    assert "Resolve-CanonicalPath" in script
    assert "Remove-Item $" not in script
    assert "Invoke-Expression" not in script
    assert "Start-Process" not in script
    assert '[string]$ReleaseBase = ""' in script
    assert "$headCommit = (& git rev-parse HEAD).Trim()" in script
    assert "$mergeBaseOutput = @(& git merge-base origin/main HEAD)" in script
    assert "ReleaseBase is required when HEAD equals origin/main" in script
    assert '@("merge-base", "--is-ancestor", $releaseBase, "HEAD")' in script
    assert '@("diff", "--check", $releaseBase, "HEAD")' in script
    assert 'rev-parse "HEAD^"' not in script
    assert "hash-object -t tree NUL" not in script
    assert '@("diff", "--check")' not in script


def test_build_pipeline_quotes_external_arguments_and_checks_exact_artifacts():
    script = read(BUILD)

    assert "& $FilePath @Arguments" in script
    assert '"/DMyAppVersion=$version"' in script
    assert '"/DMySourceDir=$onedirPath"' in script
    assert '"/DMyOutputDir=$installerOutputPath"' in script
    assert '"/DMyWebView2Bootstrapper=$bootstrapperPath"' in script
    assert '"/DMyIcon=$iconPath"' in script
    assert "EVE-Income-Tracker-$version-x64-Setup.exe" in script
    assert "EVE Income Tracker.exe" in script
    assert "Test-Path -LiteralPath" in script


def test_generated_and_release_outputs_are_ignored_and_scoped():
    ignored = read(ROOT / ".gitignore").splitlines()

    assert "release-config.local.json" in ignored
    assert "vendor/MicrosoftEdgeWebview2Setup.exe" in ignored
    assert "build/" in ignored
    assert "dist/" in ignored
    assert "installer-output/" in ignored
    for pattern in [
        "*.sqlite",
        "*.sqlite-*",
        "*.sqlite3-*",
        "*.db",
        "*.db-*",
        "*-wal",
        "*-shm",
        "*-journal",
    ]:
        assert pattern in ignored


def test_readme_documents_reproducible_windows_installer_build():
    readme = read(ROOT / "README.md")

    for token in [
        "requirements-build.txt",
        "release-config.local.json",
        "MicrosoftEdgeWebview2Setup.exe",
        "Inno Setup 7",
        r"scripts\build_windows.ps1",
        "installer-output",
        "SHA-256",
        "-ReleaseBase",
        "上一个版本标签或提交",
        "不包含 Client Secret",
    ]:
        assert token in readme


def _powershell() -> str:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    return executable


def run_fixture(root: Path, action: str, *, delete_user_data: bool = False):
    command = [
        _powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(BUILD),
        "-FixtureMode",
        "-FixtureRoot",
        str(root),
        "-FixtureAction",
        action,
    ]
    if delete_user_data:
        command.append("-DeleteUserData")
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def assert_fixture_ok(result: subprocess.CompletedProcess[str]):
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FIXTURE_OK" in result.stdout


def test_pipeline_fixture_cleanup_is_exact_and_handles_spaced_unicode_paths(
    tmp_path,
):
    root = tmp_path / "构建 fixture with spaces"
    targets = [
        root / "build" / "windows",
        root / "dist" / "windows",
        root / "installer-output",
    ]
    for target in targets:
        target.mkdir(parents=True)
        (target / "remove.txt").write_text("remove", encoding="utf-8")
    siblings = [
        root / "build" / "keep.txt",
        root / "dist" / "keep.txt",
        root / "keep.txt",
    ]
    for sibling in siblings:
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text("keep", encoding="utf-8")

    result = run_fixture(root, "Clean")

    assert_fixture_ok(result)
    assert all(not target.exists() for target in targets)
    assert all(sibling.read_text(encoding="utf-8") == "keep" for sibling in siblings)


def test_pipeline_fixture_upgrade_and_uninstall_data_choices_are_safe(tmp_path):
    root = tmp_path / "安装 fixture with spaces"
    source = root / "source" / "EVE Income Tracker"
    source.mkdir(parents=True)
    program_payload = source / "EVE Income Tracker.exe"
    program_payload.write_text("v1", encoding="utf-8")

    first = run_fixture(root, "Install")
    assert_fixture_ok(first)

    local = root / "LocalAppData"
    program = local / "Programs" / "EVE Income Tracker"
    data = local / "EVE Income Tracker"
    sentinel = data / "data" / "wallet.sqlite3"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("private-history", encoding="utf-8")
    sibling = local / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")

    program_payload.write_text("v2", encoding="utf-8")
    second = run_fixture(root, "Install")
    assert_fixture_ok(second)
    assert (program / program_payload.name).read_text(encoding="utf-8") == "v2"
    assert sentinel.read_text(encoding="utf-8") == "private-history"

    preserve = run_fixture(root, "Uninstall")
    assert_fixture_ok(preserve)
    assert not program.exists()
    assert sentinel.read_text(encoding="utf-8") == "private-history"

    delete = run_fixture(root, "Uninstall", delete_user_data=True)
    assert_fixture_ok(delete)
    assert not data.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"
