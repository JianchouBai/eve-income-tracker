from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.models import _require_utc

LP_PROJECT_NAME = "LP对账-C海军"
CALDARI_NAVY_ID = 1000035
DEFAULT_LP_DEDUCTION_RATE = Decimal("0.05")
MAX_LP = 2**63 - 1


def require_count(value: int) -> None:
    if type(value) is not int or not 0 <= value <= MAX_LP:
        raise ValueError("invalid LP count")


@dataclass(frozen=True, slots=True)
class LpProject:
    corporation_id: int
    project_id: str
    name: str
    state: str = "Active"
    source_corporation_ids: tuple[int, ...] | None = (CALDARI_NAVY_ID,)
    multiplier: Decimal | None = Decimal(1)
    current: int = 0
    desired: int = MAX_LP
    participation_limit: int | None = None
    kind: str = "earn_loyalty_point"

    def __post_init__(self):
        object.__setattr__(self, "project_id", str(UUID(self.project_id)))
        if (
            type(self.corporation_id) is not int
            or not 0 < self.corporation_id <= MAX_LP
        ):
            raise ValueError("invalid corporation ID")


@dataclass(frozen=True, slots=True)
class LpContribution:
    contributed: int
    last_modified: datetime | None = None

    def __post_init__(self):
        require_count(self.contributed)
        if self.last_modified is not None:
            _require_utc(self.last_modified, "last_modified")


@dataclass(frozen=True, slots=True)
class LpObservation:
    character_id: int
    project: LpProject
    contribution: LpContribution
    observed_at: datetime

    def __post_init__(self):
        _require_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class LpAmounts:
    gross: Decimal
    deduction: Decimal
    net: Decimal

    @classmethod
    def zero(cls):
        return cls(Decimal(0), Decimal(0), Decimal(0))


def split_lp(gross: int, rate: Decimal = DEFAULT_LP_DEDUCTION_RATE) -> LpAmounts:
    require_count(gross)
    if not isinstance(rate, Decimal) or not rate.is_finite() or not 0 <= rate <= 1:
        raise ValueError("invalid LP deduction rate")
    amount = Decimal(gross)
    deduction = amount * rate
    return LpAmounts(amount, deduction, amount - deduction)


@dataclass(frozen=True, slots=True)
class LpTracking:
    character_id: int
    corporation_id: int
    project_id: str
    segment_id: int
    contributed: int
    started_at: datetime
    last_success_at: datetime
    tail_settled: bool


@dataclass(frozen=True, slots=True)
class LpSegment:
    segment_id: int
    character_id: int
    corporation_id: int
    project_id: str
    started_at: datetime
    ended_at: datetime | None
    reason: str
    last_success_at: datetime


@dataclass(frozen=True, slots=True)
class LpIncomeEntry:
    snapshot_id: int
    character_id: int
    corporation_id: int
    project_id: str
    segment_id: int
    previous_contributed: int
    current_contributed: int
    previous_observed_at: datetime
    observed_at: datetime
    deduction_rate: Decimal
    amounts: LpAmounts


@dataclass(frozen=True, slots=True)
class LpProblem:
    character_id: int
    corporation_id: int | None
    project_id: str | None
    code: str
    started_at: datetime
    ended_at: datetime | None
    irrecoverable: bool


@dataclass(frozen=True, slots=True)
class LpHistory:
    entries: tuple[LpIncomeEntry, ...] = ()
    segments: tuple[LpSegment, ...] = ()
    problems: tuple[LpProblem, ...] = ()
    tracking: tuple[LpTracking, ...] = ()


@dataclass(frozen=True, slots=True)
class LpSyncResult:
    character_id: int
    status: str
    completed_at: datetime
    baseline_created: bool = False
    amounts: LpAmounts = field(default_factory=LpAmounts.zero)
    error_code: str | None = None
    warnings: tuple[str, ...] = ()
