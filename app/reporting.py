import logging
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models import (
    CategoryTotal,
    CharacterRecord,
    CharacterTotal,
    ClassifiedEntry,
    DailyTotal,
    DashboardReport,
    DateRange,
    JournalEntry,
)

BEIJING = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc  # noqa: UP017 - test runtime is Python 3.10.
ZERO = Decimal(0)
logger = logging.getLogger(__name__)

INCOME_CATEGORIES = {
    "bounty_prizes": "赏金",
    "ess_escrow_transfer": "ESS",
    "agent_mission_reward": "任务奖励",
    "agent_mission_time_bonus_reward": "任务限时奖励",
}
TAX_CATEGORIES = {
    "bounty_prize_corporation_tax": "赏金",
    "npc_bounty_security_tax": "赏金",
    "agent_mission_reward_corporation_tax": "任务奖励",
    "agent_mission_security_tax": "任务奖励",
    "agent_mission_time_bonus_reward_corporation_tax": "任务限时奖励",
}
CATEGORY_ORDER = (*INCOME_CATEGORIES.values(), "玩家转账")


def classify_entry(
    entry: JournalEntry, group_member_ids: AbstractSet[int]
) -> ClassifiedEntry | None:
    category = INCOME_CATEGORIES.get(entry.ref_type)
    if category is not None and entry.amount > ZERO:
        return ClassifiedEntry(entry, "income", category, entry.amount)

    if entry.ref_type == "player_donation" and entry.amount > ZERO:
        counterpart_ids = {
            party_id
            for party_id in (entry.first_party_id, entry.second_party_id)
            if party_id is not None and party_id != entry.character_id
        }
        if not counterpart_ids:
            logger.warning("Ignoring player_donation without identifiable counterpart")
            return None
        if counterpart_ids & group_member_ids:
            return None
        return ClassifiedEntry(entry, "income", "玩家转账", entry.amount)

    category = TAX_CATEGORIES.get(entry.ref_type)
    if category is not None and entry.amount < ZERO:
        return ClassifiedEntry(entry, "tax", category, -entry.amount)

    return None


def parse_date_range(start: date, end: date) -> DateRange:
    if start > end:
        raise ValueError("start date must not be after end date")
    if (end - start).days + 1 > 366:
        raise ValueError("date range must not exceed 366 days")
    start_utc = datetime.combine(start, time.min, BEIJING).astimezone(UTC)
    end_utc_exclusive = datetime.combine(
        end + timedelta(days=1), time.min, BEIJING
    ).astimezone(UTC)
    return DateRange(start, end, start_utc, end_utc_exclusive)


def build_dashboard(
    entries: Sequence[JournalEntry],
    characters: Sequence[CharacterRecord],
    selected_ids: set[int] | None,
    period: DateRange,
    *,
    group_member_ids: AbstractSet[int],
) -> DashboardReport:
    character_by_id = {character.character_id: character for character in characters}
    selected = (
        {character.character_id for character in characters if character.enabled}
        if selected_ids is None
        else set(selected_ids)
    )
    selected.intersection_update(character_by_id)
    selected_characters = tuple(
        character for character in characters if character.character_id in selected
    )
    daily_totals = {day: [ZERO, ZERO] for day in _days_in(period)}
    category_totals = {category: [ZERO, ZERO] for category in CATEGORY_ORDER}
    character_totals = {
        character.character_id: [ZERO, ZERO] for character in selected_characters
    }

    for entry in entries:
        if entry.character_id not in selected:
            continue
        if not period.start_utc <= entry.date_utc < period.end_utc_exclusive:
            continue
        classified = classify_entry(entry, group_member_ids)
        if classified is None:
            continue
        day = entry.date_utc.astimezone(BEIJING).date()
        if classified.kind == "income":
            tax = entry.tax if entry.tax is not None and entry.tax > ZERO else ZERO
            gross = classified.amount + tax
            daily_totals[day][0] += gross
            daily_totals[day][1] += tax
            category_totals[classified.category][0] += gross
            category_totals[classified.category][1] += tax
            character_totals[entry.character_id][0] += gross
            character_totals[entry.character_id][1] += tax
        else:
            daily_totals[day][1] += classified.amount
            category_totals[classified.category][1] += classified.amount
            character_totals[entry.character_id][1] += classified.amount

    daily = tuple(
        DailyTotal(day, income, tax, income - tax)
        for day, (income, tax) in daily_totals.items()
    )
    income = sum((total.income for total in daily), ZERO)
    tax = sum((total.tax for total in daily), ZERO)
    categories = tuple(
        CategoryTotal(
            category, category_income, category_tax, category_income - category_tax
        )
        for category, (category_income, category_tax) in category_totals.items()
    )
    character_rows = tuple(
        CharacterTotal(
            character_id,
            character_by_id[character_id].name,
            character_income,
            character_tax,
            character_income - character_tax,
        )
        for character_id, (character_income, character_tax) in character_totals.items()
    )
    return DashboardReport(
        period=period,
        selected_ids=frozenset(selected),
        income=income,
        tax=tax,
        net=income - tax,
        daily=daily,
        categories=categories,
        characters=character_rows,
    )


def _days_in(period: DateRange) -> tuple[date, ...]:
    return tuple(
        period.start + timedelta(days=offset)
        for offset in range((period.end - period.start).days + 1)
    )
