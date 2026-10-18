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
from app.lp_storage import LpRepository
from app.lp_sync import LpProjectSelectionError, LpSyncService, choose_lp_project
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

    async def fetch_character_corporation(self, character_id):
        return self.corporation_id

    async def fetch_corporation_projects(self, token, corp):
        return tuple(p for p in self.projects.values() if p.corporation_id == corp)

    async def fetch_project(self, token, corp, pid):
        if isinstance(self.failure, Exception):
            raise self.failure
        if pid not in self.projects:
            raise EsiNotFoundError("not_found", "safe")
        return self.projects[pid]

    async def fetch_project_contribution(self, token, corp, pid, character):
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
    assert any(p.irrecoverable for p in history(repo).problems)


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
