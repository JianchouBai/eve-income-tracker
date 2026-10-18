from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from test_lp_storage import T0, T1, history, observe, project
from test_lp_storage import repo as repo  # noqa: PLC0414 - pytest fixture re-export.

from app.lp_models import LpContribution, LpObservation
from app.lp_reporting import build_lp_report
from app.reporting import parse_date_range


def period(start="2026-10-17", end="2026-10-19"):
    return parse_date_range(date.fromisoformat(start), date.fromisoformat(end))


def test_lp_uses_observed_beijing_day_not_commit_or_modified_time(repo):
    observe(repo, 100, T0)
    before = datetime(2026, 10, 18, 15, 59, 59, tzinfo=UTC)
    after = datetime(2026, 10, 18, 16, tzinfo=UTC)
    repo.commit_observation(
        LpObservation(1, project(), LpContribution(200, T0), before)
    )
    repo.commit_observation(LpObservation(1, project(), LpContribution(300, T0), after))
    result = build_lp_report(history(repo), {1}, period())
    assert result.daily[date(2026, 10, 18)].amounts.net == Decimal(95)
    assert result.daily[date(2026, 10, 19)].amounts.net == Decimal(95)
    assert result.total.amounts.net == Decimal(190)


def test_missing_days_are_not_backfilled(repo):
    observe(repo, 100, T0)
    observe(repo, 200, T1 + timedelta(days=3))
    result = build_lp_report(history(repo), {1}, period("2026-10-17", "2026-10-21"))
    assert result.daily[date(2026, 10, 19)].amounts.net == 0
    assert result.daily[date(2026, 10, 21)].amounts.net == Decimal(95)


def test_before_baseline_is_unknown_and_after_baseline_zero_is_known(repo):
    observe(repo, 100, T0)
    observe(repo, 100, T1)
    result = build_lp_report(history(repo), {1}, period("2026-10-16", "2026-10-19"))
    assert not result.daily[date(2026, 10, 16)].coverage.has_coverage
    assert result.daily[date(2026, 10, 18)].coverage.has_coverage
    assert result.daily[date(2026, 10, 18)].amounts.net == 0
    assert not result.daily[date(2026, 10, 19)].coverage.has_coverage
    assert not result.total.coverage.complete


def test_known_history_survives_current_permission_loss(repo):
    observe(repo, 1_000_000, T0)
    observe(repo, 1_200_000, T1)
    repo.record_problem(1, "project_permission_denied", T1)
    result = build_lp_report(history(repo), {1}, period())
    assert result.total.amounts.net == Decimal(190000)
    assert result.total.coverage.has_coverage
    assert not result.total.coverage.complete
    assert "project_permission_denied" in result.total.coverage.warnings


def test_selected_characters_and_saved_historical_rates_are_respected(repo):
    observe(repo, 100, T0)
    observe(repo, 200, T1, deduction_rate=Decimal("0.10"))
    assert build_lp_report(history(repo), {1}, period()).total.amounts.net == Decimal(
        90
    )
    assert build_lp_report(history(repo), {2}, period()).total.amounts.net == 0
    partial = build_lp_report(history(repo), {1, 2}, period())
    assert partial.total.amounts.net == Decimal(90)
    assert not partial.total.coverage.complete


def test_irrecoverable_gap_survives_resolution(repo):
    observe(repo, 100, T0)
    observe(repo, 500, T1, rebaseline=True)
    observe(repo, 600, T1 + timedelta(hours=1))
    repo.resolve_problems(1, T1 + timedelta(hours=1))
    result = build_lp_report(history(repo), {1}, period())
    assert "configuration_gap" in result.total.coverage.warnings
    assert result.total.amounts.net == Decimal(95)


def test_resolved_transient_failure_does_not_remain_a_permanent_gap(repo):
    observe(repo, 100, T0)
    repo.record_problem(1, "request_failed", T0 + timedelta(hours=1))
    observe(repo, 200, T1)
    repo.resolve_problems(1, T1)
    result = build_lp_report(history(repo), {1}, period("2026-10-18", "2026-10-18"))
    assert result.total.coverage.complete
    assert result.total.coverage.warnings == ()


def test_split_and_combined_sync_have_identical_net(repo):
    observe(repo, 100, T0)
    for n in range(1, 21):
        observe(repo, 100 + n, T1 + timedelta(seconds=n))
    result = build_lp_report(history(repo), {1}, period())
    assert result.total.amounts.net == Decimal(19)
