import os
import re
import runpy
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from dotenv import dotenv_values

from app.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "scripts" / "smoke_app.py"


def test_repository_frontend_design_skill_is_complete():
    skill_path = ROOT / ".codex" / "skills" / "frontend-design" / "SKILL.md"
    license_path = skill_path.with_name("LICENSE.txt")

    assert skill_path.is_file()
    assert license_path.is_file()

    skill = skill_path.read_text(encoding="utf-8")
    license_text = license_path.read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    assert "name: frontend-design" in skill
    assert "license: Complete terms in LICENSE.txt" in skill
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text


def test_example_environment_covers_operational_settings_without_usable_secrets():
    example = ROOT / ".env.example"
    assert example.is_file()
    values = dotenv_values(example)
    assert set(values) == {
        "EVE_CLIENT_ID",
        "EVE_CALLBACK_URL",
        "APP_SESSION_SECRET",
        "APP_ENCRYPTION_KEY",
        "DATABASE_PATH",
        "EVE_USER_AGENT",
        "ESI_COMPATIBILITY_DATE",
    }
    assert values["EVE_CALLBACK_URL"] == "http://localhost:8000/auth/callback"
    assert values["ESI_COMPATIBILITY_DATE"] == "2026-05-19"
    assert values["DATABASE_PATH"] == "data/eve_income.sqlite3"
    for name in [
        "EVE_CLIENT_ID",
        "APP_SESSION_SECRET",
        "APP_ENCRYPTION_KEY",
    ]:
        assert values[name] == ""
    settings = Settings(
        _env_file=None, **{key.lower(): value for key, value in values.items()}
    )
    assert not settings.is_esi_ready
    assert "APP_ENCRYPTION_KEY" in settings.configuration_issues()
    assert "APP_SESSION_SECRET" in settings.configuration_issues()


def read_readme():
    path = ROOT / "README.md"
    assert path.is_file()
    return path.read_text(encoding="utf-8")


def test_readme_contains_machine_operational_setup_contract():
    readme = read_readme()
    assert "Python 3.11+" in readme
    assert "py -3.12 -m venv .venv" in readme
    assert r".\.venv\Scripts\python.exe -m pip install -r requirements.txt" in readme
    assert "uvicorn app.main:app --host 127.0.0.1 --port 8000" in readme
    assert "--no-access-log" in readme
    assert "http://localhost:8000/auth/callback" in readme
    assert "EVE Developers Portal" in readme
    assert "Authentication & API Access" in readme
    assert set(re.findall(r"esi-[a-z0-9._]+", readme)) == {
        "esi-wallet.read_character_wallet.v1"
    }


@pytest.mark.parametrize(
    "required_terms",
    [
        ("添加角色", "每个角色", "分别", "合计"),
        ("首次授权", "精确时刻", "之前", "丢弃", "重新授权"),
        ("北京时间", "Asia/Shanghai", "366", "同步钱包流水", "停用"),
        ("127.0.0.1", "HTTPS", "Secure", "只读", "Client Secret"),
        ("备份", "APP_ENCRYPTION_KEY", "丢失", "真实登录", "自己的"),
    ],
)
def test_readme_covers_binding_product_and_security_statements(required_terms):
    readme = read_readme()
    assert all(term in readme for term in required_terms)


def test_readme_documents_grouped_player_donation_operating_model():
    readme = read_readme()
    for statement in [
        "创建角色组并为它命名",
        "新授权角色起初处于未分组",
        "每个角色最多只能属于一个角色组",
        "按当前角色组手动同步",
        "`player_donation`",
        "`玩家转账`",
        "同一角色组内的角色相互转账不计入收入",
        "`player_trading`",
        "市场交易收入暂不统计",
        "删除角色组不会删除角色、授权或钱包历史",
    ]:
        assert statement in readme


@pytest.mark.parametrize(
    "kind, marker", [("session", "import secrets"), ("fernet", "Fernet.generate_key")]
)
def test_documented_secret_generation_commands_execute(kind, marker):
    readme = read_readme()
    blocks = re.findall(r"```powershell\n(.*?)```", readme, flags=re.DOTALL)
    commands = [
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if marker in line
    ]
    assert len(commands) == 1
    command = commands[0]
    prefix = r".\.venv\Scripts\python.exe"
    assert command.startswith(prefix + " -c ")
    if os.name == "nt":
        executable = sys.executable.replace("'", "''")
        command = "& '" + executable + "'" + command[len(prefix) :]
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        code = command.split(" -c ", 1)[1]
        assert code[0] == code[-1] and code[0] in {"\x22", "'"}
        args = [sys.executable, "-c", code[1:-1]]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=15, check=True
    )
    value = result.stdout.strip()
    if kind == "session":
        assert len(value) >= 32
        assert re.fullmatch(r"[A-Za-z0-9_-]+", value)
    else:
        cipher = Fernet(value.encode())
        assert cipher.decrypt(cipher.encrypt(b"example")) == b"example"


def smoke_environment():
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def assert_smoke_port_closed():
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", 8765)) != 0


def test_smoke_starts_real_app_and_releases_its_port_without_repository_data(tmp_path):
    assert SMOKE.is_file()
    original_data = set((ROOT / "data").glob("*"))
    result = subprocess.run(
        [sys.executable, str(SMOKE)],
        cwd=tmp_path,
        env=smoke_environment(),
        capture_output=True,
        text=True,
        timeout=35,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "SMOKE_OK healthz=200 home=200"
    assert_smoke_port_closed()
    assert set((ROOT / "data").glob("*")) == original_data


def test_smoke_refuses_occupied_port_without_stopping_its_owner(tmp_path):
    assert SMOKE.is_file()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8765))
        listener.listen()
        result = subprocess.run(
            [sys.executable, str(SMOKE)],
            cwd=tmp_path,
            env=smoke_environment(),
            capture_output=True,
            text=True,
            timeout=35,
            check=False,
        )
        assert result.returncode != 0
        assert "SMOKE_OK" not in result.stdout
        with socket.create_connection(("127.0.0.1", 8765), timeout=1):
            pass


def test_smoke_cleans_exact_child_when_home_assertion_fails(monkeypatch):
    assert SMOKE.is_file()
    module = runpy.run_path(str(SMOKE))

    def fail_home(opener):
        raise RuntimeError("synthetic home-check failure")

    monkeypatch.setitem(module["run_smoke"].__globals__, "check_home", fail_home)
    with pytest.raises(RuntimeError, match="synthetic home-check failure"):
        module["run_smoke"]()
    assert_smoke_port_closed()


def test_smoke_isolates_credentials_and_inherited_worker_configuration(monkeypatch):
    assert SMOKE.is_file()
    module = runpy.run_path(str(SMOKE))
    monkeypatch.setenv("UVICORN_RELOAD", "true")
    monkeypatch.setenv("UVICORN_WORKERS", "4")
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    monkeypatch.setenv("EVE_CLIENT_SECRET", "synthetic-inherited-secret")

    def inspect_spawn(arguments, **options):
        environment = options["env"]
        assert "UVICORN_RELOAD" not in environment
        assert "UVICORN_WORKERS" not in environment
        assert "WEB_CONCURRENCY" not in environment
        assert environment["EVE_CLIENT_SECRET"] == ""
        assert environment["EVE_CLIENT_ID"] == ""
        assert environment["APP_ENCRYPTION_KEY"] == ""
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert not Path(environment["DATABASE_PATH"]).is_relative_to(ROOT)
        assert arguments[arguments.index("--workers") + 1] == "1"
        assert arguments[arguments.index("--host") + 1] == "127.0.0.1"
        assert arguments[arguments.index("--port") + 1] == "8765"
        raise RuntimeError("synthetic intercepted spawn")

    monkeypatch.setattr(module["subprocess"], "Popen", inspect_spawn)
    with pytest.raises(RuntimeError, match="synthetic intercepted spawn"):
        module["run_smoke"]()


def test_readme_documents_desktop_and_frozen_smoke_commands():
    readme = read_readme()

    assert "requirements-windows.txt" in readme
    assert "python.exe -m app.desktop" in readme
    assert "python.exe -m app.desktop --smoke-test" in readme
    assert "%LOCALAPPDATA%\\EVE Income Tracker" in readme


def test_readme_leads_with_installed_windows_operation_contract():
    readme = read_readme()

    installed_section = readme.split("## 开发与维护", 1)[0]
    for statement in [
        "无需安装 Python",
        "无需打开终端",
        "系统默认浏览器",
        "PKCE",
        "每台电脑使用独立数据库",
        r"%LOCALAPPDATA%\EVE Income Tracker",
        "不使用 Client Secret",
        "旧版 v2",
        "重新授权",
        "升级不会删除",
        "默认保留个人数据",
        "同时删除个人数据",
        "手动创建 GitHub Release",
        "未知发布者",
        "重置本机授权",
        "::1",
    ]:
        assert statement in installed_section

    assert "Python 3.11+" in readme
    assert "uvicorn app.main:app" in readme
    assert "scripts\\build_windows.ps1" in readme


def test_readme_documents_private_local_files_never_enter_git():
    readme = read_readme()

    for local_item in [
        "数据库",
        "DPAPI 密钥",
        "日志",
        "备份",
        "release-config.local.json",
        "installer-output",
    ]:
        assert local_item in readme
    assert "不会进入 Git" in readme


def test_readme_documents_selected_storage_and_recovery_contract():
    installed = read_readme().split("## 开发与维护", 1)[0]
    assert "安装版的个人数据根目录为：" not in installed
    for statement in (
        "首次启动",
        "选择数据存储位置",
        "默认位置",
        "设置",
        "迁移",
        "离线",
        "定位已有数据目录",
        "恢复副本",
        "七天",
        "三次",
        "重装",
        "默认保留个人数据",
        "同时删除个人数据",
        "不会进入 Git",
    ):
        assert statement in installed


def test_changelog_records_completed_release_without_erasing_history():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 1.0.1（2026-10-06）" in changelog
    assert "## 1.0.0（2026-10-01）" in changelog
    assert "未发布（计划 v1.0.0）" not in changelog
    assert "## 0.1.0（2026-09-30）" in changelog


def test_windows_release_checklist_covers_configurable_storage_paths():
    checklist = (ROOT / "docs" / "windows-release-checklist.md").read_text(
        encoding="utf-8"
    )
    for statement in (
        "C→D",
        "D→测试卷",
        "离线",
        "恢复副本",
        "默认保留",
        "明确删除",
        "重装接回",
    ):
        assert statement in checklist


def test_windows_release_checklist_verifies_failed_migration_rollback():
    checklist = (ROOT / "docs" / "windows-release-checklist.md").read_text(
        encoding="utf-8"
    )
    assert any(
        all(
            term in item
            for term in ("故障注入", "迁移失败", "注册表指针", "原数据目录", "仍可启动")
        )
        for item in checklist.split("- [ ] ")
    )


def test_windows_release_checklist_verifies_settings_storage_status():
    checklist = (ROOT / "docs" / "windows-release-checklist.md").read_text(
        encoding="utf-8"
    )
    assert any(
        all(
            term in item
            for term in (
                "设置",
                "当前路径",
                "所在磁盘",
                "占用",
                "数据库",
                "日志",
                "备份",
            )
        )
        for item in checklist.split("- [ ] ")
    )


def test_windows_release_checklist_requires_isolated_destructive_acceptance():
    checklist = (ROOT / "docs" / "windows-release-checklist.md").read_text(
        encoding="utf-8"
    )
    preamble = checklist.split("## 构建前", 1)[0]
    for term in (
        "所有破坏性验收步骤",
        "可丢弃的测试数据",
        "隔离的测试卷",
        "真实钱包数据库",
        "迁移",
        "故障注入",
        "恢复副本删除",
        "卸载删除",
    ):
        assert term in preamble


def test_windows_release_checklist_covers_desktop_acceptance_contract():
    checklist_path = ROOT / "docs" / "windows-release-checklist.md"
    assert checklist_path.is_file()
    checklist = checklist_path.read_text(encoding="utf-8")

    for statement in [
        "全新安装",
        "开始菜单",
        "桌面快捷方式",
        "重复启动",
        "授权",
        "同步钱包流水",
        "关闭并重新打开",
        "覆盖升级",
        "旧版 v2",
        "源数据库",
        "缺少 WebView2",
        "保留个人数据",
        "删除个人数据",
        "第二台干净电脑",
        "卸载",
        "损坏密钥",
    ]:
        assert statement in checklist
