# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent.resolve()
sys.path.insert(0, str(ROOT))

from scripts.build_support import packaging_manifest

manifest = packaging_manifest(project_root=ROOT)
required_generated_names = {
    "release-config.json",
    "app-icon.ico",
    "version-info.txt",
}
if {
    manifest.release_config.name,
    manifest.icon.name,
    manifest.version_info.name,
} != required_generated_names:
    raise ValueError("Unexpected generated build inputs")
datas = [(str(source), destination) for source, destination in manifest.datas]
for package in manifest.package_data:
    datas += collect_data_files(package)
for subdirectory in manifest.webview_data:
    datas += collect_data_files("webview", subdir=subdirectory)

binaries = collect_dynamic_libs("webview")

analysis = Analysis(
    [str(ROOT / "app" / "desktop.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(manifest.hidden_imports),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=list(manifest.excludes),
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="EVE Income Tracker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(manifest.icon),
    version=str(manifest.version_info),
    uac_admin=False,
)

application = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="EVE Income Tracker",
)