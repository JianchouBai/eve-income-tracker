from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.models import CharacterRecord, JournalEntry
from app.reporting import build_dashboard, classify_entry, parse_date_range

UTC = timezone.utc  # noqa: UP017 - test runtime is Python 3.10.


def enabled_character(character_id: int, *, enabled: bool = True) -> CharacterRecord:
    return CharacterRecord(
        character_id=character_id,
        name=f"Pilot {character_id}",
        encrypted_refresh_token="encrypted-token",
        enabled=enabled,
        authorization_required=False,
        tracking_started_at=datetime(2026, 9, 1, tzinfo=UTC),
        last_synced_at=None,
    )


def journal(
    *,
    character_id: int = 1,
    journal_id: int = 1,
    date_utc: str = "2026-09-27T12:00:00Z",
    amount: str = "100.00",
    ref_type: str = "bounty_prizes",
    tax: str | None = None,
) -> JournalEntry:
    return JournalEntry(
        journal_id=journal_id,
        character_id=character_id,
        date_utc=datetime.fromisoformat(
            date_utc[:-1] + "+00:00" if date_utc.endswith("Z") else date_utc
        ),
        amount=Decimal(amount),
        ref_type=ref_type,
        tax=Decimal(tax) if tax is not None else None,
    )


def period(start: str, end: str):
    return parse_date_range(date.fromisoformat(start), date.fromisoformat(end))


@pytest.mark.parametrize(
    ("ref_type", "category"),
    [
        ("bounty_prizes", "赏金"),
        ("ess_escrow_transfer", "ESS"),
        ("agent_mission_reward", "任务奖励"),
        ("agent_mission_time_bonus_reward", "任务限时奖励"),
    ],
)
def test_income_ref_types_require_positive_amount(ref_type: str, category: str):
    classified = classify_entry(journal(ref_type=ref_type, amount="12.34"))

    assert classified is not None
    assert classified.kind == "income"
    assert classified.category == category
    assert classified.amount == Decimal("12.34")
    assert classify_entry(journal(ref_type=ref_type, amount="0")) is None
    assert classify_entry(journal(ref_type=ref_type, amount="-12.34")) is None


@pytest.mark.parametrize(
    "ref_type",
    [
        "bounty_prize_corporation_tax",
        "agent_mission_reward_corporation_tax",
        "agent_mission_time_bonus_reward_corporation_tax",
        "npc_bounty_security_tax",
        "agent_mission_security_tax",
    ],
)
def test_tax_ref_types_require_negative_amount(ref_type: str):
    classified = classify_entry(journal(ref_type=ref_type, amount="-4.56"))

    assert classified is not None
    assert classified.kind == "tax"
    assert classified.amount == Decimal("4.56")
    assert classify_entry(journal(ref_type=ref_type, amount="0")) is None
    assert classify_entry(journal(ref_type=ref_type, amount="4.56")) is None


def test_unknown_and_wrong_sign_records_are_ignored():
    assert classify_entry(journal(ref_type="player_donation", amount="99.99")) is None
    assert classify_entry(journal(ref_type="bounty_prizes", amount="-99.99")) is None
    assert (
        classify_entry(journal(ref_type="bounty_prize_corporation_tax", amount="99.99"))
        is None
    )


def test_tax_metadata_is_not_added_to_tax_totals():
    report = build_dashboard(
        [
            journal(ref_type="bounty_prizes", amount="100.00", tax="20.00"),
            journal(
                journal_id=2,
                ref_type="bounty_prize_corporation_tax",
                amount="-10.00",
                tax="20.00",
            ),
        ],
        [enabled_character(1)],
        {1},
        period("2026-09-27", "2026-09-27"),
    )

    assert report.tax == Decimal("10.00")
    assert report.net == Decimal("90.00")


def test_utc_1600_belongs_to_next_beijing_day():
    entry = journal(
        date_utc="2026-09-26T16:00:00Z",
        amount="100.10",
        ref_type="bounty_prizes",
    )
    report = build_dashboard(
        [entry],
        [enabled_character(1)],
        {1},
        period("2026-09-27", "2026-09-27"),
    )

    assert report.daily[0].date.isoformat() == "2026-09-27"
    assert report.daily[0].income == Decimal("100.10")


def test_naive_journal_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        JournalEntry(
            journal_id=1,
            character_id=1,
            date_utc=datetime.fromisoformat("2026-09-27T12:00:00"),
            amount=Decimal("1.00"),
            ref_type="bounty_prizes",
        )


def test_default_selection_aggregates_enabled_characters_with_daily_categories_and_roles():
    report = build_dashboard(
        [
            journal(
                character_id=1,
                journal_id=7,
                date_utc="2026-09-27T01:00:00Z",
                amount="100.10",
            ),
            journal(
                character_id=1,
                journal_id=8,
                date_utc="2026-09-27T02:00:00Z",
                amount="-10.01",
                ref_type="bounty_prize_corporation_tax",
            ),
            journal(
                character_id=2,
                journal_id=7,
                date_utc="2026-09-28T01:00:00Z",
                amount="200.20",
                ref_type="ess_escrow_transfer",
            ),
            journal(
                character_id=2,
                journal_id=8,
                date_utc="2026-09-28T02:00:00Z",
                amount="-3.03",
                ref_type="agent_mission_security_tax",
            ),
            journal(
                character_id=3,
                journal_id=7,
                date_utc="2026-09-28T03:00:00Z",
                amount="999.99",
            ),
        ],
        [
            enabled_character(1),
            enabled_character(2),
            enabled_character(3, enabled=False),
        ],
        None,
        period("2026-09-27", "2026-09-29"),
    )

    assert report.income == Decimal("300.30")
    assert report.tax == Decimal("13.04")
    assert report.net == Decimal("287.26")
    assert [
        (total.date.isoformat(), total.income, total.tax, total.net)
        for total in report.daily
    ] == [
        ("2026-09-27", Decimal("100.10"), Decimal("10.01"), Decimal("90.09")),
        ("2026-09-28", Decimal("200.20"), Decimal("3.03"), Decimal("197.17")),
        ("2026-09-29", Decimal(0), Decimal(0), Decimal(0)),
    ]
    assert [
        (total.category, total.income, total.tax, total.net)
        for total in report.categories
    ] == [
        ("赏金", Decimal("100.10"), Decimal("10.01"), Decimal("90.09")),
        ("ESS", Decimal("200.20"), Decimal(0), Decimal("200.20")),
        ("任务奖励", Decimal(0), Decimal("3.03"), Decimal("-3.03")),
        ("任务限时奖励", Decimal(0), Decimal(0), Decimal(0)),
    ]
    assert [
        (total.character_id, total.name, total.income, total.tax, total.net)
        for total in report.characters
    ] == [
        (1, "Pilot 1", Decimal("100.10"), Decimal("10.01"), Decimal("90.09")),
        (2, "Pilot 2", Decimal("200.20"), Decimal("3.03"), Decimal("197.17")),
    ]


def test_explicit_selection_includes_disabled_character_without_cross_attribution():
    report = build_dashboard(
        [
            journal(character_id=1, journal_id=10, amount="10.00"),
            journal(character_id=3, journal_id=10, amount="30.00"),
        ],
        [enabled_character(1), enabled_character(3, enabled=False)],
        {3},
        period("2026-09-27", "2026-09-27"),
    )

    assert report.selected_ids == frozenset({3})
    assert report.income == Decimal("30.00")
    assert [(total.character_id, total.income) for total in report.characters] == [
        (3, Decimal("30.00")),
    ]


def test_inclusive_366_day_range_is_accepted():
    date_range = parse_date_range(date(2024, 1, 1), date(2024, 12, 31))

    assert date_range.start.isoformat() == "2024-01-01"
    assert date_range.end.isoformat() == "2024-12-31"


def test_367_day_range_is_rejected():
    with pytest.raises(ValueError, match="366 days"):
        parse_date_range(date(2025, 1, 1), date(2026, 1, 2))


def test_start_after_end_is_rejected():
    with pytest.raises(ValueError, match="must not be after"):
        parse_date_range(date(2026, 9, 28), date(2026, 9, 27))
