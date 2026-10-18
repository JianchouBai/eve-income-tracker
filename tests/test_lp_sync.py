import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from test_lp_storage import PID, T0, T1, history, project
from test_lp_storage import repo as repo  # noqa: PLC0414 - pytest fixture re-export.
from test_sync import NOW, FakeEsi, FakeSso, entry, services

from app.esi import EsiError, EsiNotFoundError, EsiProjectPermissionError
from app.lp_models import LpContribution
from app.lp_reporting import build_lp_report
from app.lp_storage import LpRepository
from app.lp_sync import LpProjectSelectionError, LpSyncService, choose_lp_project
from app.reporting import parse_date_range
from app.sso import PROJECTS_SCOPE, WALLET_SCOPE, CharacterIdentity, TokenSet
from app.sync import SyncService

SCOPES = (WALLET_SCOPE, PROJECTS_SCOPE)
PID2 = "22222222-2222-4222-8222-222222222222"


class ProjectEsi:
    def __init__(self):
        self.projects = {PID: project()}
        self.value = 100
        self.corporation_id = 9001
        self.failure = None
        self.project_failures = {}
        self.contribution_failures = {}

    async def fetch_character_corporation(self, character_id):
        return self.corporation_id

    async def fetch_corporation_projects(self, token, corp):
        return tuple(p for p in self.projects.values() if p.corporation_id == corp)

    async def fetch_project(self, token, corp, pid):
        if pid in self.project_failures:
            raise self.project_failures[pid]
        if isinstance(self.failure, Exception):
            raise self.failure
        if pid not in self.projects:
            raise EsiNotFoundError("not_found", "safe")
        return self.projects[pid]

    async def fetch_project_contribution(self, token, corp, pid, character):
        if pid in self.contribution_failures:
            raise self.contribution_failures[pid]
        if isinstance(self.failure, Exception):
            raise self.failure
        value = self.value[pid] if isinstance(self.value, dict) else self.value
        return LpContribution(value)


def sync(repo, esi, at=T1, scopes=SCOPES):
    return asyncio.run(
        LpSyncService(repo, esi, lambda: at).sync_character(1, "token", scopes)
    )


def test_selects_only_unique_valid_active_named_project():
    selected = project()
    candidates = (
        project(name="Other"),
        project(state="Closed", project_id=PID2),
        project(source_corporation_ids=(1,)),
        selected,
    )
    assert choose_lp_project(candidates) == selected
    with pytest.raises(LpProjectSelectionError):
        choose_lp_project((selected, replace(selected, project_id=PID2)))


def test_renamed_binding_uses_original_id(repo):
    esi = ProjectEsi()
    assert sync(repo, esi, T0).baseline_created
    esi.projects[PID] = project(name="Renamed")
    esi.value = 200
    assert sync(repo, esi).amounts.net == Decimal(95)


def test_closed_project_tail_precedes_new_project_baseline(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.projects = {PID: project(state="Completed"), PID2: project(project_id=PID2)}
    esi.value = {PID: 150, PID2: 700}
    result = sync(repo, esi)
    assert result.amounts.net == Decimal("47.5")
    assert repo.get_selected_project(9001).project_id == PID2
    assert len(repo.list_tracking(1)) == 2
    assert sync(repo, esi).amounts.net == 0
    assert sum(x.amounts.net for x in history(repo).entries) == Decimal("47.5")


def test_deleted_old_project_keeps_gap_and_allows_new_baseline(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.projects = {PID2: project(project_id=PID2)}
    esi.value = 700
    result = sync(repo, esi)
    assert result.baseline_created and result.amounts.net == 0
    assert any(
        p.code == "tail_unavailable" and p.ended_at is None
        for p in history(repo).problems
    )


def test_incompatible_configuration_recovery_opens_new_segment(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.projects[PID] = project(source_corporation_ids=(1000035, 1))
    esi.value = 200
    assert sync(repo, esi).status == "unavailable"
    esi.projects[PID] = project()
    esi.value = 300
    assert sync(repo, esi, T1 + timedelta(hours=1)).baseline_created
    esi.value = 310
    assert sync(repo, esi, T1 + timedelta(hours=2)).amounts.net == Decimal("9.5")


@pytest.mark.parametrize("paused_before_switch", [False, True])
def test_new_source_does_not_clear_old_configuration_pause(repo, paused_before_switch):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.projects[PID] = project(source_corporation_ids=(1,))
    esi.value = 200
    if paused_before_switch:
        assert sync(repo, esi).error_code == "project_source_mismatch"
    replacement = project(project_id=PID2)
    esi.projects[PID2] = replacement
    repo.select_project(replacement, T1)
    esi.value = {PID: 200, PID2: 700}
    assert sync(repo, esi).baseline_created
    assert any(
        p.project_id == PID and p.code == "project_source_mismatch" and p.ended_at is None
        for p in history(repo).problems
    )
    esi.projects[PID] = project(state="Completed")
    esi.value[PID] = 300
    result = sync(repo, esi, T1 + timedelta(hours=1))
    assert result.amounts.net == 0
    assert any(p.code == "configuration_gap" for p in history(repo).problems)
    assert next(t for t in repo.list_tracking(1) if t.project_id == PID).tail_settled


def test_new_source_cannot_hide_decreased_old_tail_and_recovery_clears_it(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.value = 110
    sync(repo, esi, T1)
    replacement = project(project_id=PID2)
    repo.select_project(replacement, T1)
    esi.projects = {PID: project(state="Completed"), PID2: replacement}
    esi.value = {PID: 100, PID2: 700}
    result = sync(repo, esi, T1 + timedelta(hours=1))
    assert "tail_unavailable" in result.warnings
    period = parse_date_range(T1.date(), T1.date())
    report = build_lp_report(history(repo), {1}, period)
    assert not report.total.coverage.complete
    assert "tail_unavailable" in report.total.coverage.warnings
    esi.value[PID] = 120
    assert sync(repo, esi, T1 + timedelta(hours=2)).amounts.net == Decimal("9.5")
    report = build_lp_report(history(repo), {1}, period)
    assert report.total.coverage.complete
    assert report.total.coverage.warnings == ()


@pytest.mark.parametrize("old_failure", ["deleted", "not_found", "decreased"])
def test_unavailable_terminal_tail_does_not_block_replacement(repo, old_failure):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.projects = {PID: project(state="Completed"), PID2: project(project_id=PID2)}
    esi.value = {PID: 150, PID2: 700}
    if old_failure == "deleted":
        esi.projects[PID] = project(
            state="Deleted", kind="unknown", source_corporation_ids=None
        )
    elif old_failure == "not_found":
        esi.contribution_failures[PID] = EsiNotFoundError("not_found", "safe")
    else:
        esi.value[PID] = 80
    result = sync(repo, esi)
    assert result.baseline_created and result.amounts.net == 0
    assert repo.get_selected_project(9001).project_id == PID2
    assert "tail_unavailable" in result.warnings
    assert any(
        p.project_id == PID and p.code == "tail_unavailable" and p.ended_at is None
        for p in history(repo).problems
    )
    esi.value[PID2] = 710
    assert sync(repo, esi, T1 + timedelta(hours=1)).amounts.net == Decimal("9.5")


def test_recovered_old_tail_permission_failure_does_not_leave_permanent_gap(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    replacement = project(project_id=PID2)
    repo.select_project(replacement, T1)
    esi.projects = {PID: project(state="Completed"), PID2: replacement}
    esi.value = {PID: 200, PID2: 700}
    esi.project_failures[PID] = EsiProjectPermissionError(
        "project_permission_denied", "safe"
    )
    assert "tail_unavailable" in sync(repo, esi).warnings
    esi.project_failures.clear()
    assert sync(repo, esi, T1 + timedelta(hours=1)).amounts.net == Decimal(95)
    report = build_lp_report(
        history(repo), {1}, parse_date_range(T1.date(), T1.date())
    )
    assert report.total.amounts.net == Decimal(95)
    assert report.total.coverage.complete
    assert report.total.coverage.warnings == ()


def test_project_and_personal_caps_warn_without_inventing_income(repo):
    esi = ProjectEsi()
    esi.projects[PID] = project(current=100, desired=100, participation_limit=100)
    result = sync(repo, esi, T0)
    assert result.warnings and result.amounts.net == 0


def test_permission_failure_and_scope_absence_preserve_baseline(repo):
    esi = ProjectEsi()
    sync(repo, esi, T0)
    esi.failure = EsiProjectPermissionError("project_permission_denied", "secret")
    assert sync(repo, esi).status == "unavailable"
    assert sync(repo, esi, scopes=(WALLET_SCOPE,)).status == "authorization_required"
    assert repo.list_tracking(1)[0].contributed == 100


@pytest.mark.parametrize("wallet_fails", [False, True])
def test_wallet_and_lp_commit_independently(tmp_path, wallet_fails):
    async def scenario():
        chars, journal, cipher = services(tmp_path)
        lp_repo = LpRepository(chars.database)
        esi = ProjectEsi()
        lp = LpSyncService(lp_repo, esi, lambda: NOW)
        wallet = FakeEsi(
            EsiError("request_failed", "safe") if wallet_fails else [entry(1, 9, NOW)]
        )

        class Sso(FakeSso):
            async def verify_access_token(self, token):
                return CharacterIdentity(1, "Pilot", SCOPES)

        service = SyncService(chars, journal, cipher, Sso(), wallet, lambda: NOW, lp=lp)
        first = await service.sync_character(1)
        esi.value = 200
        second = await service.sync_character(1)
        assert first.lp.baseline_created
        assert second.lp.amounts.net == Decimal(95)
        assert second.status == ("failed" if wallet_fails else "ok")
        assert not chars.list_all()[0].authorization_required

    asyncio.run(scenario())


def test_refresh_verifies_subject_and_saves_scope_once(tmp_path):
    async def scenario():
        chars, journal, cipher = services(tmp_path)

        class WrongSso(FakeSso):
            async def verify_access_token(self, token):
                return CharacterIdentity(999, "Wrong", SCOPES)

        sso = WrongSso()
        result = await SyncService(
            chars, journal, cipher, sso, FakeEsi([]), lambda: NOW
        ).sync_character(1)
        assert result.status == "failed"
        assert chars.list_all()[0].authorization_required
        assert len(sso.calls) == 1
        assert chars.list_all()[0].granted_scopes == ()

    asyncio.run(scenario())


def test_concurrent_sync_and_reauthorization_do_not_overwrite_fresh_token(tmp_path):
    async def scenario():
        chars, journal, cipher = services(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()

        class SlowSso(FakeSso):
            async def refresh(self, token):
                entered.set()
                await release.wait()
                return TokenSet("old-access", "old-rotated", NOW)

            async def verify_access_token(self, token):
                return CharacterIdentity(1, "Pilot", SCOPES)

        service = SyncService(
            chars, journal, cipher, SlowSso(), FakeEsi([]), lambda: NOW
        )
        old = asyncio.create_task(service.sync_character(1))
        await entered.wait()
        new = asyncio.create_task(
            service.save_authorization_and_sync(
                CharacterIdentity(1, "Pilot", SCOPES),
                TokenSet("new-access", "new-refresh", NOW),
                NOW,
            )
        )
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(old, new)
        assert (
            cipher.decrypt(chars.list_all()[0].encrypted_refresh_token) == "new-refresh"
        )
        assert chars.list_all()[0].granted_scopes == tuple(sorted(SCOPES))

    asyncio.run(scenario())
