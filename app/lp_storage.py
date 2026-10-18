import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal

from app.lp_models import (
    DEFAULT_LP_DEDUCTION_RATE,
    LpAmounts,
    LpHistory,
    LpIncomeEntry,
    LpObservation,
    LpProblem,
    LpProject,
    LpSegment,
    LpSyncResult,
    LpTracking,
    split_lp,
)
from app.storage import Database, _decimal_text, _parse_utc, _utc_iso


def create_lp_schema(connection: sqlite3.Connection) -> None:
    statements = [
        ("CREATE TABLE lp_projects (corporation_id INTEGER NOT NULL, project_id TEXT NOT NULL, "
        "details TEXT NOT NULL, selected_at TEXT, PRIMARY KEY(corporation_id, project_id))"),
        "CREATE UNIQUE INDEX lp_selected_project_idx ON lp_projects(corporation_id) WHERE selected_at IS NOT NULL",
        ("CREATE TABLE lp_segments (segment_id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL REFERENCES characters(character_id), "
        "corporation_id INTEGER NOT NULL, project_id TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, reason TEXT NOT NULL, "
        "FOREIGN KEY(corporation_id, project_id) REFERENCES lp_projects(corporation_id, project_id))"),
        ("CREATE TABLE lp_tracking (character_id INTEGER NOT NULL REFERENCES characters(character_id), corporation_id INTEGER NOT NULL, "
        "project_id TEXT NOT NULL, segment_id INTEGER NOT NULL REFERENCES lp_segments(segment_id), "
        "contributed INTEGER NOT NULL CHECK(contributed >= 0), started_at TEXT NOT NULL, last_success_at TEXT NOT NULL, "
        "tail_settled INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(character_id, corporation_id, project_id))"),
        ("CREATE TABLE lp_snapshots (snapshot_id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL REFERENCES characters(character_id), "
        "corporation_id INTEGER NOT NULL, project_id TEXT NOT NULL, segment_id INTEGER NOT NULL REFERENCES lp_segments(segment_id), "
        "contributed INTEGER NOT NULL CHECK(contributed >= 0), observed_at TEXT NOT NULL, last_modified TEXT, accepted INTEGER NOT NULL)"),
        ("CREATE TABLE lp_income_entries (snapshot_id INTEGER PRIMARY KEY REFERENCES lp_snapshots(snapshot_id), "
        "character_id INTEGER NOT NULL REFERENCES characters(character_id), corporation_id INTEGER NOT NULL, project_id TEXT NOT NULL, "
        "segment_id INTEGER NOT NULL REFERENCES lp_segments(segment_id), previous_contributed INTEGER NOT NULL, "
        "current_contributed INTEGER NOT NULL, previous_observed_at TEXT NOT NULL, observed_at TEXT NOT NULL, "
        "deduction_rate TEXT NOT NULL, gross TEXT NOT NULL, deduction TEXT NOT NULL, net TEXT NOT NULL)"),
        "CREATE INDEX lp_income_character_date_idx ON lp_income_entries(character_id, observed_at)",
        ("CREATE TABLE lp_problems (problem_id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL REFERENCES characters(character_id), "
        "corporation_id INTEGER, project_id TEXT, code TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, irrecoverable INTEGER NOT NULL)"),
    ]
    for statement in statements:
        connection.execute(statement)


def _project(row) -> LpProject:
    payload = json.loads(row["details"])
    if payload["source_corporation_ids"] is not None:
        payload["source_corporation_ids"] = tuple(payload["source_corporation_ids"])
    if payload["multiplier"] is not None:
        payload["multiplier"] = Decimal(payload["multiplier"])
    return LpProject(**payload)


def _tracking(row) -> LpTracking:
    payload = dict(row)
    for key in ("started_at", "last_success_at"):
        payload[key] = _parse_utc(payload[key])
    payload["tail_settled"] = bool(payload["tail_settled"])
    return LpTracking(**payload)


class LpRepository:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _save_project(db, project):
        db.execute(
            "INSERT INTO lp_projects(corporation_id, project_id, details) VALUES(?,?,?) "
            "ON CONFLICT(corporation_id, project_id) DO UPDATE SET details=excluded.details",
            (
                project.corporation_id,
                project.project_id,
                json.dumps(asdict(project), default=str),
            ),
        )

    def get_selected_project(self, corporation_id: int) -> LpProject | None:
        with self.database.connection() as db:
            row = db.execute(
                "SELECT * FROM lp_projects WHERE corporation_id=? AND selected_at IS NOT NULL",
                (corporation_id,),
            ).fetchone()
        return _project(row) if row else None

    def select_project(self, project: LpProject, selected_at: datetime) -> None:
        at = _utc_iso(selected_at)
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save_project(db, project)
            db.execute(
                "UPDATE lp_projects SET selected_at=NULL WHERE corporation_id=?",
                (project.corporation_id,),
            )
            db.execute(
                "UPDATE lp_projects SET selected_at=? WHERE corporation_id=? AND project_id=?",
                (at, project.corporation_id, project.project_id),
            )

    def list_tracking(self, character_id: int) -> tuple[LpTracking, ...]:
        with self.database.connection() as db:
            rows = db.execute(
                "SELECT * FROM lp_tracking WHERE character_id=? ORDER BY started_at, project_id",
                (character_id,),
            ).fetchall()
        return tuple(_tracking(row) for row in rows)

    @staticmethod
    def _problem(
        db,
        character_id,
        code,
        at,
        corporation_id=None,
        project_id=None,
        irrecoverable=False,
    ):
        row = db.execute(
            "SELECT problem_id FROM lp_problems WHERE character_id=? AND corporation_id IS ? "
            "AND project_id IS ? AND code=? AND ended_at IS NULL",
            (character_id, corporation_id, project_id, code),
        ).fetchone()
        if row is None:
            db.execute(
                "INSERT INTO lp_problems(character_id, corporation_id, project_id, code, started_at, irrecoverable) VALUES(?,?,?,?,?,?)",
                (
                    character_id,
                    corporation_id,
                    project_id,
                    code,
                    _utc_iso(at),
                    int(irrecoverable),
                ),
            )

    def record_problem(
        self,
        character_id: int,
        code: str,
        at: datetime,
        *,
        corporation_id: int | None = None,
        project_id: str | None = None,
        irrecoverable: bool = False,
    ) -> None:
        with self.database.connection() as db:
            self._problem(
                db, character_id, code, at, corporation_id, project_id, irrecoverable
            )

    def resolve_problems(self, character_id: int, at: datetime) -> None:
        with self.database.connection() as db:
            db.execute(
                "UPDATE lp_problems SET ended_at=? WHERE character_id=? AND ended_at IS NULL",
                (_utc_iso(at), character_id),
            )

    def commit_observation(
        self,
        observation: LpObservation,
        *,
        deduction_rate: Decimal = DEFAULT_LP_DEDUCTION_RATE,
        rebaseline: bool = False,
        tail_settled: bool = False,
    ) -> LpSyncResult:
        o = observation
        value = o.contribution.contributed
        split_lp(value, deduction_rate)
        at = _utc_iso(o.observed_at)
        key = (o.character_id, o.project.corporation_id, o.project.project_id)
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._save_project(db, o.project)
            old = db.execute(
                "SELECT * FROM lp_tracking WHERE character_id=? AND corporation_id=? AND project_id=?",
                key,
            ).fetchone()
            baseline = old is None or rebaseline
            if baseline:
                if old is not None:
                    db.execute(
                        "UPDATE lp_segments SET ended_at=? WHERE segment_id=?",
                        (at, old["segment_id"]),
                    )
                    self._problem(
                        db,
                        o.character_id,
                        "configuration_gap",
                        _parse_utc(old["last_success_at"]),
                        *key[1:],
                        True,
                    )
                    db.execute(
                        "UPDATE lp_problems SET ended_at=? WHERE character_id=? AND ended_at IS NULL AND code='configuration_gap'",
                        (at, o.character_id),
                    )
                segment = db.execute(
                    "INSERT INTO lp_segments(character_id, corporation_id, project_id, started_at, reason) VALUES(?,?,?,?,?)",
                    (
                        *key,
                        at,
                        "configuration_restored" if rebaseline else "first_observation",
                    ),
                ).lastrowid
            else:
                segment = old["segment_id"]
            accepted = baseline or (
                value >= old["contributed"] and at >= old["last_success_at"]
            )
            snapshot = db.execute(
                "INSERT INTO lp_snapshots(character_id, corporation_id, project_id, segment_id, contributed, observed_at, last_modified, accepted) VALUES(?,?,?,?,?,?,?,?)",
                (
                    *key,
                    segment,
                    value,
                    at,
                    _utc_iso(o.contribution.last_modified)
                    if o.contribution.last_modified
                    else None,
                    int(accepted),
                ),
            ).lastrowid
            amounts = LpAmounts.zero()
            if not accepted:
                self._problem(
                    db, o.character_id, "reading_decreased", o.observed_at, *key[1:]
                )
                return LpSyncResult(
                    o.character_id,
                    "anomaly",
                    o.observed_at,
                    error_code="reading_decreased",
                )
            if not baseline and value > old["contributed"]:
                amounts = split_lp(value - old["contributed"], deduction_rate)
                db.execute(
                    "INSERT INTO lp_income_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot,
                        *key,
                        segment,
                        old["contributed"],
                        value,
                        old["last_success_at"],
                        at,
                        _decimal_text(deduction_rate),
                        _decimal_text(amounts.gross),
                        _decimal_text(amounts.deduction),
                        _decimal_text(amounts.net),
                    ),
                )
            db.execute(
                "INSERT INTO lp_tracking VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(character_id, corporation_id, project_id) "
                "DO UPDATE SET segment_id=excluded.segment_id, contributed=excluded.contributed, started_at=excluded.started_at, "
                "last_success_at=excluded.last_success_at, tail_settled=excluded.tail_settled",
                (
                    *key,
                    segment,
                    value,
                    at if baseline else old["started_at"],
                    at,
                    int(tail_settled),
                ),
            )
        return LpSyncResult(
            o.character_id,
            "baseline" if baseline else "ok",
            o.observed_at,
            baseline,
            amounts,
        )

    def report_history(
        self, character_ids: set[int], start_utc: datetime, end_utc: datetime
    ) -> LpHistory:
        start, end = _utc_iso(start_utc), _utc_iso(end_utc)
        if not character_ids:
            return LpHistory()
        ids = tuple(sorted(character_ids))
        marks = ",".join("?" for _ in ids)
        with self.database.connection() as db:
            entries = db.execute(
                f"SELECT * FROM lp_income_entries WHERE character_id IN ({marks}) AND observed_at>=? AND observed_at<? ORDER BY observed_at,snapshot_id",
                (*ids, start, end),
            ).fetchall()
            segments = db.execute(
                f"SELECT s.*, MAX(p.observed_at) AS last_success_at FROM lp_segments s JOIN lp_snapshots p ON p.segment_id=s.segment_id AND p.accepted=1 WHERE s.character_id IN ({marks}) GROUP BY s.segment_id ORDER BY s.segment_id",
                ids,
            ).fetchall()
            problems = db.execute(
                f"SELECT * FROM lp_problems WHERE character_id IN ({marks}) ORDER BY problem_id",
                ids,
            ).fetchall()
            tracking = db.execute(
                f"SELECT * FROM lp_tracking WHERE character_id IN ({marks}) ORDER BY started_at,project_id",
                ids,
            ).fetchall()
        income = []
        for row in entries:
            data = dict(row)
            data["amounts"] = LpAmounts(
                *(Decimal(data.pop(k)) for k in ("gross", "deduction", "net"))
            )
            data["deduction_rate"] = Decimal(data["deduction_rate"])
            for k in ("previous_observed_at", "observed_at"):
                data[k] = _parse_utc(data[k])
            income.append(LpIncomeEntry(**data))
        segment_rows = []
        for row in segments:
            data = dict(row)
            for k in ("started_at", "ended_at", "last_success_at"):
                data[k] = _parse_utc(data[k]) if data[k] else None
            segment_rows.append(LpSegment(**data))
        problem_rows = []
        for row in problems:
            data = dict(row)
            data.pop("problem_id")
            for k in ("started_at", "ended_at"):
                data[k] = _parse_utc(data[k]) if data[k] else None
            data["irrecoverable"] = bool(data["irrecoverable"])
            problem_rows.append(LpProblem(**data))
        return LpHistory(
            tuple(income),
            tuple(segment_rows),
            tuple(problem_rows),
            tuple(_tracking(r) for r in tracking),
        )
