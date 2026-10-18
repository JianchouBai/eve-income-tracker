import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.database_lifecycle import DatabaseLifecycle
from app.lp_models import (
    LpAmounts,
    LpContribution,
    LpObservation,
    LpProject,
    split_lp,
)
from app.lp_storage import LpRepository
from app.storage import CharacterRepository, Database

T0 = datetime(2026, 10, 17, tzinfo=UTC)
T1 = datetime(2026, 10, 18, tzinfo=UTC)
PID = "11111111-1111-4111-8111-111111111111"


def project(**changes):
    return replace(LpProject(9001, PID, "LP对账-C海军"), **changes)


@pytest.fixture
def repo(tmp_path):
    database = Database(tmp_path / "income.sqlite3")
    database.initialize()
    CharacterRepository(database).upsert_authorization(1, "Pilot", "encrypted", T0)
    return LpRepository(database)


def observe(repo, value, at=T1, **options):
    return repo.commit_observation(
        LpObservation(1, project(), LpContribution(value, None), at), **options
    )


def history(repo):
    return repo.report_history({1}, T0 - timedelta(days=1), T1 + timedelta(days=10))


def test_first_observation_is_only_baseline(repo):
    result = observe(repo, 1_000_000, T0)
    assert result.status == "baseline"
    assert result.baseline_created
    assert result.amounts == LpAmounts.zero()
    assert history(repo).entries == ()
    assert repo.list_tracking(1)[0].contributed == 1_000_000


def test_delta_persists_precise_amounts(repo):
    observe(repo, 1_000_000, T0)
    result = observe(repo, 1_200_000)
    assert result.amounts == LpAmounts(Decimal(200000), Decimal(10000), Decimal(190000))
    (entry,) = history(repo).entries
    assert (entry.previous_contributed, entry.current_contributed) == (
        1_000_000,
        1_200_000,
    )
    assert entry.amounts == result.amounts
    assert entry.observed_at == T1
    reopened = LpRepository(Database(repo.database.path))
    assert history(reopened).entries == history(repo).entries


def test_equal_and_lower_readings_do_not_rebook_or_lower_baseline(repo):
    observe(repo, 100, T0)
    assert observe(repo, 100).amounts == LpAmounts.zero()
    assert observe(repo, 80).status == "anomaly"
    assert repo.list_tracking(1)[0].contributed == 100
    assert observe(repo, 110).amounts.net == Decimal("9.5")
    assert len(history(repo).entries) == 1


def test_failed_income_insert_rolls_back_snapshot_and_baseline(repo):
    observe(repo, 100, T0)
    with repo.database.connection() as db:
        count = db.execute("SELECT COUNT(*) FROM lp_snapshots").fetchone()[0]
        db.execute(
            "CREATE TRIGGER reject_income BEFORE INSERT ON lp_income_entries "
            "BEGIN SELECT RAISE(ABORT, 'reject'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        observe(repo, 200)
    assert repo.list_tracking(1)[0].contributed == 100
    assert history(repo).entries == ()
    with repo.database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM lp_snapshots").fetchone()[0] == count


def test_two_connections_book_same_increment_once(repo):
    observe(repo, 100, T0)

    def run(_):
        return observe(LpRepository(Database(repo.database.path)), 200)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(x.amounts.net for x in results) == Decimal(95)
    assert len(history(repo).entries) == 1


def test_rebaseline_keeps_old_entries_and_excludes_paused_growth(repo):
    observe(repo, 100, T0)
    observe(repo, 200)
    result = observe(repo, 500, T1 + timedelta(hours=1), rebaseline=True)
    assert result.baseline_created and result.amounts.net == 0
    assert observe(repo, 510, T1 + timedelta(hours=2)).amounts.net == Decimal("9.5")
    assert [x.amounts.net for x in history(repo).entries] == [
        Decimal(95),
        Decimal("9.5"),
    ]
    assert len(history(repo).segments) == 2
    assert any(x.irrecoverable for x in history(repo).problems)


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.5])
def test_invalid_contribution_preserves_state(repo, value):
    observe(repo, 100, T0)
    with pytest.raises(ValueError):
        observe(repo, value)
    assert repo.list_tracking(1)[0].contributed == 100


def test_naive_observation_is_rejected(repo):
    with pytest.raises(ValueError):
        observe(repo, 1, datetime(2026, 10, 18))  # noqa: DTZ001 - deliberate invalid input.


def test_tax_precision_does_not_depend_on_sync_count():
    assert split_lp(1).net == Decimal("0.95")
    assert sum(split_lp(1).net for _ in range(20)) == split_lp(20).net == Decimal(19)


def test_project_selection_and_tracking_are_independent(repo):
    repo.select_project(project(), T0)
    changed = project(project_id="22222222-2222-4222-8222-222222222222")
    repo.select_project(changed, T1)
    assert repo.get_selected_project(9001) == changed
    observe(repo, 100, T0)
    assert repo.list_tracking(1)[0].project_id == PID


def test_v3_import_and_authorization_reset_preserve_lp_and_clear_scopes(repo, tmp_path):
    characters = CharacterRepository(repo.database)
    characters.replace_granted_scopes(1, ("wallet", "projects"))
    observe(repo, 100, T0)
    observe(repo, 200)
    target = Database(tmp_path / "imported.sqlite3")
    lifecycle = DatabaseLifecycle(target, tmp_path / "backups")
    lifecycle.import_legacy(repo.database.path)
    assert history(LpRepository(target)).entries == history(repo).entries
    assert CharacterRepository(target).list_all()[0].granted_scopes == ()
    assert characters.list_all()[0].granted_scopes == ("projects", "wallet")
    lifecycle.reset_authorization()
    assert len(history(LpRepository(target)).entries) == 1
    assert LpRepository(target).list_tracking(1)[0].contributed == 200


def test_reauthorization_preserves_lp_baseline_and_wallet_start(repo):
    observe(repo, 100, T0)
    characters = CharacterRepository(repo.database)
    characters.upsert_authorization(1, "Pilot", "new", T1, granted_scopes=("projects",))
    assert characters.list_all()[0].tracking_started_at == T0
    assert repo.list_tracking(1)[0].contributed == 100
