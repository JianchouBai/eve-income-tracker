import importlib
import re
import tomllib
from pathlib import Path

import app


def test_package_and_project_metadata_use_one_version_source():
    assert hasattr(app, "__version__"), "app.__version__ is missing"
    version_module = importlib.import_module("app.version")
    version = app.__version__

    assert version == version_module.APP_VERSION
    assert version == "2.0.1"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)

    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert "version" not in project["project"]
    assert "version" in project["project"]["dynamic"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "app.__version__"
    }


def test_desktop_smoke_user_agent_uses_release_version():
    from app.desktop import _smoke_release_config

    assert _smoke_release_config().eve_user_agent == (
        "eve-income-tracker/2.0.1 (smoke@example.com)"
    )
