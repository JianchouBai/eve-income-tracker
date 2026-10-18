from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.esi import EsiAuthorizationError, EsiError, EsiNotFoundError
from app.lp_models import (
    CALDARI_NAVY_ID,
    LP_PROJECT_NAME,
    LpAmounts,
    LpObservation,
    LpProject,
    LpSyncResult,
)
from app.lp_storage import LpRepository
from app.sso import PROJECTS_SCOPE

CONFIGURATION_ERRORS = {
    "project_type_mismatch",
    "project_source_mismatch",
    "project_multiplier_unsupported",
}
TERMINAL_STATES = {"Closed", "Completed", "Expired", "Deleted"}


class LpProjectSelectionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def validate_lp_project(project: LpProject) -> str | None:
    if project.kind != "earn_loyalty_point":
        return "project_type_mismatch"
    if project.source_corporation_ids != (CALDARI_NAVY_ID,):
        return "project_source_mismatch"
    if project.multiplier not in (None, 1):
        return "project_multiplier_unsupported"
    return None


def choose_lp_project(projects: tuple[LpProject, ...]) -> LpProject:
    named = {
        p.project_id: p
        for p in projects
        if p.name == LP_PROJECT_NAME and p.state != "Deleted"
    }
    valid = [p for p in named.values() if validate_lp_project(p) is None]
    active = [p for p in valid if p.state == "Active"]
    candidates = active or valid
    if len(candidates) > 1:
        raise LpProjectSelectionError("project_ambiguous")
    if not candidates:
        code = (
            validate_lp_project(next(iter(named.values())))
            if named
            else "project_not_found"
        )
        raise LpProjectSelectionError(code or "project_not_found")
    return candidates[0]


class LpSyncService:
    def __init__(
        self,
        repository: LpRepository,
        esi: object,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.repository, self.esi, self.clock = repository, esi, clock

    async def _discover(self, token, corporation_id, *, active_only=False):
        listings = await self.esi.fetch_corporation_projects(token, corporation_id)
        candidates = []
        for item in listings:
            if (
                item.name == LP_PROJECT_NAME
                and item.state != "Deleted"
                and (not active_only or item.state == "Active")
            ):
                candidates.append(
                    await self.esi.fetch_project(token, corporation_id, item.project_id)
                )
        return choose_lp_project(tuple(candidates))

    async def _capture(self, character_id, token, project, *, final=False):
        invalid = validate_lp_project(project)
        if invalid:
            raise LpProjectSelectionError(invalid)
        contribution = await self.esi.fetch_project_contribution(
            token, project.corporation_id, project.project_id, character_id
        )
        at = self.clock()
        previous = self.repository.report_history({character_id}, at, at)
        rebaseline = any(
            p.ended_at is None
            and p.code in CONFIGURATION_ERRORS
            and (p.corporation_id, p.project_id)
            == (project.corporation_id, project.project_id)
            for p in previous.problems
        )
        result = self.repository.commit_observation(
            LpObservation(character_id, project, contribution, at),
            rebaseline=rebaseline,
            tail_settled=final,
        )
        warnings = []
        if project.current >= project.desired or (
            project.participation_limit is not None
            and contribution.contributed >= project.participation_limit
        ):
            warnings.append("project_limit_reached")
        if project.state in TERMINAL_STATES:
            warnings.append("project_stopped")
        return replace(result, warnings=tuple(warnings))

    async def sync_character(
        self, character_id: int, access_token: str, scopes: tuple[str, ...]
    ) -> LpSyncResult:
        if PROJECTS_SCOPE not in scopes:
            at = self.clock()
            self.repository.record_problem(character_id, "lp_scope_required", at)
            return LpSyncResult(
                character_id,
                "authorization_required",
                at,
                error_code="lp_scope_required",
            )
        corporation_id = None
        selected = None
        amounts = LpAmounts.zero()
        warnings = []
        try:
            corporation_id = await self.esi.fetch_character_corporation(character_id)
            selected = self.repository.get_selected_project(corporation_id)
            tracking = self.repository.list_tracking(character_id)
            # A corporation-wide binding can advance before another character gets its tail.
            for old in tracking:
                if old.tail_settled or (
                    selected
                    and (old.corporation_id, old.project_id)
                    == (corporation_id, selected.project_id)
                ):
                    continue
                try:
                    old_project = await self.esi.fetch_project(
                        access_token, old.corporation_id, old.project_id
                    )
                    tail = await self._capture(
                        character_id, access_token, old_project, final=True
                    )
                    amounts = self._add(amounts, tail.amounts)
                    if tail.status == "anomaly":
                        warnings.append("tail_unavailable")
                except EsiAuthorizationError:
                    raise
                except (EsiError, LpProjectSelectionError):
                    self.repository.record_problem(
                        character_id,
                        "tail_unavailable",
                        old.last_success_at,
                        corporation_id=old.corporation_id,
                        project_id=old.project_id,
                        irrecoverable=True,
                    )
                    warnings.append("tail_unavailable")
            if selected is not None:
                try:
                    selected = await self.esi.fetch_project(
                        access_token, corporation_id, selected.project_id
                    )
                except EsiNotFoundError:
                    old = next(
                        (
                            t
                            for t in tracking
                            if t.corporation_id == corporation_id
                            and t.project_id == selected.project_id
                        ),
                        None,
                    )
                    if old:
                        self.repository.record_problem(
                            character_id,
                            "tail_unavailable",
                            old.last_success_at,
                            corporation_id=corporation_id,
                            project_id=selected.project_id,
                            irrecoverable=True,
                        )
                        warnings.append("tail_unavailable")
                    selected = None
            if selected is None:
                selected = await self._discover(access_token, corporation_id)
                self.repository.select_project(selected, self.clock())
            result = await self._capture(
                character_id,
                access_token,
                selected,
                final=selected.state in TERMINAL_STATES,
            )
            amounts = self._add(amounts, result.amounts)
            if result.status == "anomaly":
                return replace(result, amounts=amounts, warnings=tuple(warnings))
            if selected.state in TERMINAL_STATES:
                try:
                    new_project = await self._discover(
                        access_token, corporation_id, active_only=True
                    )
                except LpProjectSelectionError as error:
                    if error.code != "project_not_found":
                        raise
                else:
                    if new_project.project_id != selected.project_id:
                        selected = new_project
                        self.repository.select_project(selected, self.clock())
                        result = await self._capture(
                            character_id, access_token, selected
                        )
                        amounts = self._add(amounts, result.amounts)
            self.repository.resolve_problems(character_id, result.completed_at)
            for code in result.warnings:
                self.repository.record_problem(
                    character_id,
                    code,
                    result.completed_at,
                    corporation_id=corporation_id,
                    project_id=selected.project_id,
                    irrecoverable=True,
                )
            return replace(
                result,
                amounts=amounts,
                warnings=tuple(dict.fromkeys((*warnings, *result.warnings))),
            )
        except EsiAuthorizationError:
            raise
        except (EsiError, LpProjectSelectionError) as error:
            at = self.clock()
            self.repository.record_problem(
                character_id,
                error.code,
                at,
                corporation_id=corporation_id,
                project_id=selected.project_id if selected else None,
            )
            return LpSyncResult(
                character_id,
                "unavailable",
                at,
                amounts=amounts,
                error_code=error.code,
                warnings=tuple(warnings),
            )
        except Exception:  # noqa: BLE001 - public boundary never exposes ESI or SQLite details.
            at = self.clock()
            self.repository.record_problem(character_id, "lp_sync_failed", at)
            return LpSyncResult(
                character_id,
                "failed",
                at,
                amounts=amounts,
                error_code="lp_sync_failed",
                warnings=tuple(warnings),
            )

    @staticmethod
    def _add(a: LpAmounts, b: LpAmounts) -> LpAmounts:
        return LpAmounts(a.gross + b.gross, a.deduction + b.deduction, a.net + b.net)
