from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _require_decimal(value: Decimal, field_name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be a finite Decimal")


@dataclass(frozen=True, slots=True)
class CharacterGroupRecord:
    group_id: int
    name: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_utc(self.created_at, "created_at")


@dataclass(frozen=True, slots=True)
class CharacterRecord:
    character_id: int
    name: str
    encrypted_refresh_token: str
    enabled: bool
    authorization_required: bool
    tracking_started_at: datetime
    last_synced_at: datetime | None
    group_id: int | None = None
    granted_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_utc(self.tracking_started_at, "tracking_started_at")
        if self.last_synced_at is not None:
            _require_utc(self.last_synced_at, "last_synced_at")


@dataclass(frozen=True, slots=True)
class JournalEntry:
    journal_id: int
    character_id: int
    date_utc: datetime
    amount: Decimal
    ref_type: str
    tax: Decimal | None = None
    balance: Decimal | None = None
    description: str = ""
    reason: str | None = None
    context_id: int | None = None
    context_id_type: str | None = None
    first_party_id: int | None = None
    first_party_type: str | None = None
    second_party_id: int | None = None
    second_party_type: str | None = None
    tax_receiver_id: int | None = None

    def __post_init__(self) -> None:
        _require_utc(self.date_utc, "date_utc")
        _require_decimal(self.amount, "amount")
        if self.tax is not None:
            _require_decimal(self.tax, "tax")
        if self.balance is not None:
            _require_decimal(self.balance, "balance")


@dataclass(frozen=True, slots=True)
class ClassifiedEntry:
    entry: JournalEntry
    kind: str
    category: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date
    start_utc: datetime
    end_utc_exclusive: datetime


@dataclass(frozen=True, slots=True)
class DailyTotal:
    date: date
    income: Decimal
    tax: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class CategoryTotal:
    category: str
    income: Decimal
    tax: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class CharacterTotal:
    character_id: int
    name: str
    income: Decimal
    tax: Decimal
    net: Decimal


@dataclass(frozen=True, slots=True)
class DashboardReport:
    period: DateRange
    selected_ids: frozenset[int]
    income: Decimal
    tax: Decimal
    net: Decimal
    daily: tuple[DailyTotal, ...]
    categories: tuple[CategoryTotal, ...]
    characters: tuple[CharacterTotal, ...]
