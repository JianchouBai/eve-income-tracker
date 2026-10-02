from __future__ import annotations

import json
import struct
import subprocess
import zlib
from hashlib import sha256
from pathlib import Path

import pytest

from app.desktop import SMOKE_RESULT_NAME, SMOKE_SUCCESS
from app.paths import APP_DATA_DIRECTORY
from app.version import APP_VERSION
from scripts.build_support import (
    BuildInputError,
    packaging_manifest,
    validate_release_config,
    write_release_config,
    write_windows_version_info,
)
from scripts.generate_windows_icon import (
    ICON_SIZES,
    PNG_SIGNATURE,
    generate_windows_icon,
)
from scripts.smoke_desktop import FrozenSmokeError, run_frozen_smoke


def write_config(path, **updates):
    payload = {
        "eve_client_id": "AbCdEf0123456789AbCdEf0123456789",
        "eve_callback_url": "http://localhost:8000/auth/callback",
        "eve_user_agent": "eve-income-tracker/0.1 (owner@jianchou.dev)",
        "esi_compatibility_date": "2026-05-19",
    }
    payload.update(updates)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def png_bytes(size, source):
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(name, payload):
        return (
            struct.pack(">I", len(payload))
            + name
            + payload
            + struct.pack(">I", zlib.crc32(name + payload) & 0xFFFFFFFF)
        )

    digest = sha256(source).digest()
    pixel = bytes((digest[0], digest[1], digest[2], 255))
    row = b"\x00" + pixel * size
    image = zlib.compress(row * size, level=9)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        signature + chunk(b"IHDR", header) + chunk(b"IDAT", image) + chunk(b"IEND", b"")
    )


def fake_svg_renderer(*, bytestring, output_width, output_height, unsafe):
    assert output_width == output_height
    assert unsafe is False
    return png_bytes(output_width, bytestring)


def test_release_config_validation_and_generated_copy_are_public_and_exact(tmp_path):
    source = tmp_path / "release-config.local.json"
    payload = write_config(source)
    generated = tmp_path / "build" / "generated"

    config = validate_release_config(source)
    output = write_release_config(source, generated)

    assert config.eve_client_id == payload["eve_client_id"]
    assert output == generated / "release-config.json"
    assert json.loads(output.read_text(encoding="utf-8")) == payload
    assert output.read_bytes().endswith(b"\n")


@pytest.mark.parametrize(
    "updates",
    [
        {"eve_client_id": "replace-with-public-client-id"},
        {"eve_client_id": "too-short"},
        {"eve_callback_url": "http://127.0.0.1:8000/auth/callback"},
        {"eve_callback_url": "https://localhost:8000/auth/callback"},
        {"eve_user_agent": "eve-income-tracker/0.1 (your-email@example.com)"},
        {"eve_user_agent": "generic-agent"},
        {"esi_compatibility_date": "not-a-date"},
    ],
)
def test_release_validation_rejects_placeholders_without_echoing_values(
    tmp_path,
    updates,
):
    source = tmp_path / "release-config.local.json"
    payload = write_config(source, **updates)

    with pytest.raises(BuildInputError) as caught:
        validate_release_config(source)

    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.code == "release_config_invalid"
    for value in payload.values():
        assert value not in rendered


def test_release_validation_rejects_extra_or_missing_keys(tmp_path):
    source = tmp_path / "release-config.local.json"
    payload = write_config(source)
    payload["client_secret"] = "private-secret"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(BuildInputError) as caught:
        validate_release_config(source)

    assert caught.value.code == "release_config_invalid"
    assert "private-secret" not in repr(caught.value)


def test_windows_version_info_is_generated_from_only_app_version(tmp_path):
    output = write_windows_version_info(tmp_path / "build" / "generated")
    content = output.read_text(encoding="utf-8")
    major, minor, patch = APP_VERSION.split(".")

    assert f"filevers=({major}, {minor}, {patch}, 0)" in content
    assert f"prodvers=({major}, {minor}, {patch}, 0)" in content
    assert f"StringStruct('FileVersion', '{APP_VERSION}')" in content
    assert f"StringStruct('ProductVersion', '{APP_VERSION}')" in content
    assert "EVE Income Tracker" in content


def test_icon_generation_is_deterministic_and_contains_required_png_sizes(tmp_path):
    source = tmp_path / "icon.svg"
    source.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg' width='256' height='256'>"
        "<circle cx='128' cy='128' r='80'/></svg>",
        encoding="utf-8",
    )
    first = tmp_path / "first.ico"
    second = tmp_path / "second.ico"

    generate_windows_icon(source, first, renderer=fake_svg_renderer)
    generate_windows_icon(source, second, renderer=fake_svg_renderer)

    assert first.read_bytes() == second.read_bytes()
    data = first.read_bytes()
    reserved, image_type, count = struct.unpack_from("<HHH", data)
    assert (reserved, image_type, count) == (0, 1, len(ICON_SIZES))
    entries = [
        struct.unpack_from("<BBBBHHII", data, 6 + index * 16) for index in range(count)
    ]
    assert [256 if entry[0] == 0 else entry[0] for entry in entries] == list(ICON_SIZES)
    for entry in entries:
        size, offset = entry[6], entry[7]
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert offset + size <= len(data)


def test_packaging_manifest_and_spec_include_only_windows_edge_resources(tmp_path):
    generated = tmp_path / "build" / "generated"
    manifest = packaging_manifest(project_root=tmp_path, generated_dir=generated)

    destinations = {destination for _, destination in manifest.datas}
    hidden = {name.lower() for name in manifest.hidden_imports}
    excluded = {name.lower() for name in manifest.excludes}
    assert {"app/templates", "app/static", "."} <= destinations
    assert "tzdata" in manifest.package_data
    assert "webview.platforms.edgechromium" in hidden
    assert "webview.platforms.winforms" in hidden
    assert "clr" in hidden
    assert "webview.platforms.webview2core" not in hidden
    assert {"pyqt5", "pyqt6", "pyside2", "pyside6", "cefpython3"} <= excluded
    assert manifest.release_config == generated / "release-config.json"
    assert manifest.icon == generated / "app-icon.ico"
    assert manifest.version_info == generated / "version-info.txt"

    rendered = " ".join(str(value).lower() for pair in manifest.datas for value in pair)
    for forbidden in [".env", "sqlite3", "local.key", ".log", "tests"]:
        assert forbidden not in rendered

    spec = Path("packaging/eve_income_tracker.spec").read_text(encoding="utf-8")
    for token in [
        "COLLECT(",
        "console=False",
        "EVE Income Tracker",
        "app-icon.ico",
        "version-info.txt",
        "release-config.json",
        "collect_data_files",
        "collect_dynamic_libs",
    ]:
        assert token in spec
    assert "onefile" not in spec.lower()
    assert "ROOT = Path(SPECPATH).parent.resolve()" in spec
    assert "Path(SPECPATH).parent.parent" not in spec


def test_source_icon_is_self_contained_vector_asset():
    icon = Path("assets/app-icon.svg").read_text(encoding="utf-8")

    assert "<svg" in icon
    assert 'viewBox="0 0 256 256"' in icon
    assert "http://" not in icon.replace("http://www.w3.org/2000/svg", "")
    assert "https://" not in icon
    assert "data:" not in icon


def test_frozen_smoke_isolates_environment_and_deletes_only_owned_directory(
    tmp_path,
    monkeypatch,
):
    executable = tmp_path / "EVE Income Tracker.exe"
    executable.write_bytes(b"fake")
    parent = tmp_path / "smoke-parent"
    parent.mkdir()
    sibling = parent / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")
    captured = {}

    for name in [
        "EVE_CLIENT_ID",
        "EVE_CLIENT_SECRET",
        "APP_SESSION_SECRET",
        "APP_ENCRYPTION_KEY",
        "DATABASE_PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "UVICORN_RELOAD",
        "UVICORN_WORKERS",
        "WEB_CONCURRENCY",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ]:
        monkeypatch.setenv(name, "private-environment-value")

    def runner(command, **kwargs):
        captured["command"] = command
        captured["environment"] = kwargs["env"]
        owned = Path(command[command.index("--local-app-data") + 1])
        captured["owned"] = owned
        result = owned / APP_DATA_DIRECTORY / "config" / SMOKE_RESULT_NAME
        result.parent.mkdir(parents=True)
        result.write_bytes((SMOKE_SUCCESS + "\n").encode("utf-8"))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    run_frozen_smoke(
        executable,
        parent,
        timeout=5,
        runner=runner,
    )

    assert captured["command"][0] == str(executable.resolve())
    assert captured["command"][1] == "--smoke-test"
    assert captured["environment"]["LOCALAPPDATA"] == str(captured["owned"])
    assert captured["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    for name in ("APPDATA", "USERPROFILE", "HOME", "TEMP", "TMP"):
        assert Path(captured["environment"][name]).is_relative_to(captured["owned"])
    assert not any(
        value == "private-environment-value"
        for value in captured["environment"].values()
    )
    assert not captured["owned"].exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("mode", ["timeout", "failure", "wrong_marker"])
def test_frozen_smoke_failures_are_safe_and_clean_owned_directory(
    tmp_path,
    mode,
):
    executable = tmp_path / "EVE Income Tracker.exe"
    executable.write_bytes(b"fake")
    parent = tmp_path / "smoke-parent"
    parent.mkdir()
    owned_paths = []

    def runner(command, **kwargs):
        owned = Path(command[command.index("--local-app-data") + 1])
        owned_paths.append(owned)
        if mode == "timeout":
            raise subprocess.TimeoutExpired(
                command,
                kwargs["timeout"],
                output="private-output",
                stderr="private-error",
            )
        if mode == "wrong_marker":
            marker = owned / APP_DATA_DIRECTORY / "config" / SMOKE_RESULT_NAME
            marker.parent.mkdir(parents=True)
            marker.write_text("private-wrong-result", encoding="utf-8")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="private-output",
                stderr="private-error",
            )
        return subprocess.CompletedProcess(
            command,
            9,
            stdout="private-output",
            stderr="private-error",
        )

    with pytest.raises(FrozenSmokeError) as caught:
        run_frozen_smoke(executable, parent, timeout=0.01, runner=runner)

    rendered = f"{caught.value!s} {caught.value!r}"
    assert caught.value.code in {
        "smoke_timeout",
        "smoke_process_failed",
        "smoke_contract_failed",
    }
    assert "private-" not in rendered
    assert owned_paths and not owned_paths[0].exists()


def test_real_windows_icon_renderer_produces_all_sizes(tmp_path):
    pytest.importorskip("resvg_py")
    output = tmp_path / "real.ico"

    generate_windows_icon(Path("assets/app-icon.svg"), output)

    data = output.read_bytes()
    _, image_type, count = struct.unpack_from("<HHH", data)
    assert image_type == 1
    assert count == len(ICON_SIZES)
    entries = [
        struct.unpack_from("<BBBBHHII", data, 6 + index * 16) for index in range(count)
    ]
    assert [256 if entry[0] == 0 else entry[0] for entry in entries] == list(ICON_SIZES)
    for entry in entries:
        payload_size, offset = entry[6], entry[7]
        payload = data[offset : offset + payload_size]
        assert payload.startswith(PNG_SIGNATURE)


def test_build_requirements_pin_pyinstaller_and_one_icon_generator():
    requirements = (
        Path("requirements-build.txt").read_text(encoding="utf-8").splitlines()
    )

    assert requirements == [
        "-r requirements-windows.txt",
        "pyinstaller==6.22.3",
        "resvg_py==0.5.0",
    ]
