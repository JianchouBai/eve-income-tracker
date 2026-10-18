from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app.lp_models import LpAmounts, LpHistory
from app.models import DateRange
from app.reporting import BEIJING, parse_date_range


@dataclass(frozen=True, slots=True)
class LpCoverage:
    has_coverage: bool
    complete: bool
    started_at: datetime | None
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LpReportRow:
    amounts: LpAmounts
    coverage: LpCoverage


@dataclass(frozen=True, slots=True)
class LpReport:
    total: LpReportRow
    daily: dict[date, LpReportRow]
    characters: dict[int, LpReportRow]


def _sum(values) -> LpAmounts:
    result = LpAmounts.zero()
    for v in values:
        result = LpAmounts(
            result.gross + v.gross, result.deduction + v.deduction, result.net + v.net
        )
    return result


def _combine(rows: list[LpReportRow]) -> LpReportRow:
    starts = [r.coverage.started_at for r in rows if r.coverage.started_at is not None]
    return LpReportRow(
        _sum(r.amounts for r in rows),
        LpCoverage(
            any(r.coverage.has_coverage for r in rows),
            bool(rows) and all(r.coverage.complete for r in rows),
            min(starts) if starts else None,
            tuple(sorted({w for r in rows for w in r.coverage.warnings})),
        ),
    )


def build_lp_report(
    history: LpHistory,
    selected_ids: set[int],
    period: DateRange,
    *,
    current_warnings: dict[int, tuple[str, ...]] | None = None,
) -> LpReport:
    days = [
        period.start + timedelta(days=n)
        for n in range((period.end - period.start).days + 1)
    ]
    entries = [
        e
        for e in history.entries
        if e.character_id in selected_ids
        and period.start_utc <= e.observed_at < period.end_utc_exclusive
    ]
    per_character = {}
    for cid in selected_ids:
        segments = [s for s in history.segments if s.character_id == cid]
        starts = [s.started_at for s in segments]
        problems = [
            p
            for p in history.problems
            if p.character_id == cid and (p.irrecoverable or p.ended_at is None)
        ]
        per_character[cid] = {}
        for day in days:
            bounds = parse_date_range(day, day)
            covered = [
                s
                for s in segments
                if s.started_at.astimezone(BEIJING).date()
                <= day
                <= s.last_success_at.astimezone(BEIJING).date()
            ]
            warnings = {
                p.code
                for p in problems
                if p.started_at < bounds.end_utc_exclusive
                and (p.ended_at is None or p.ended_at > bounds.start_utc)
            }
            warnings.update((current_warnings or {}).get(cid, ()))
            amounts = _sum(
                e.amounts
                for e in entries
                if e.character_id == cid
                and e.observed_at.astimezone(BEIJING).date() == day
            )
            known = bool(covered) or amounts.gross > 0
            complete = (
                any(s.started_at <= bounds.start_utc for s in covered) and not warnings
            )
            if not known:
                warnings.add("lp_not_covered")
            elif not complete and not warnings:
                warnings.add("lp_partial_start")
            per_character[cid][day] = LpReportRow(
                amounts,
                LpCoverage(
                    known,
                    complete,
                    min(starts) if starts else None,
                    tuple(sorted(warnings)),
                ),
            )
    characters = {
        cid: _combine(list(rows.values())) for cid, rows in per_character.items()
    }
    daily = {
        day: _combine([rows[day] for rows in per_character.values()]) for day in days
    }
    return LpReport(_combine(list(characters.values())), daily, characters)
