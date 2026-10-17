import shutil
import subprocess
from pathlib import Path

import pytest


def test_period_navigation_and_monthly_ledger_are_independent():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js required for executable frontend contract")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [node, str(root / "tests/frontend_periods.cjs"), str(root / "app/static/app.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
