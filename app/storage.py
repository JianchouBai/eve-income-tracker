from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.models import CharacterRecord, JournalEntry

UTC = timezone.utc  # noqa: UP017 - test runtime is Python 3.10.


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS characters ("
                "character_id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
                "encrypted_refresh_token TEXT NOT NULL, enabled INTEGER NOT NULL, "
                "authorization_required INTEGER NOT NULL, "
                "tracking_started_at TEXT NOT NULL, last_synced_at TEXT)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS wallet_journal ("
                "character_id INTEGER NOT NULL, journal_id INTEGER NOT NULL, "
                "date_utc TEXT NOT NULL, amount TEXT NOT NULL, ref_type TEXT NOT NULL, "
                "tax TEXT, balance TEXT, description TEXT NOT NULL, reason TEXT, "
                "context_id INTEGER, context_id_type TEXT, first_party_id INTEGER, "
                "first_party_type TEXT, second_party_id INTEGER, second_party_type TEXT, "
                "tax_receiver_id INTEGER, PRIMARY KEY (character_id, journal_id), "
                "FOREIGN KEY (character_id) REFERENCES characters(character_id))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS wallet_journal_character_date_idx "
                "ON wallet_journal(character_id, date_utc)"
            )


def _character_from_row(row: sqlite3.Row) -> CharacterRecord:
    return CharacterRecord(
        character_id=row["character_id"],
        name=row["name"],
        encrypted_refresh_token=row["encrypted_refresh_token"],
        enabled=bool(row["enabled"]),
        authorization_required=bool(row["authorization_required"]),
        tracking_started_at=_parse_utc(row["tracking_started_at"]),
        last_synced_at=(
            _parse_utc(row["last_synced_at"]) if row["last_synced_at"] else None
        ),
    )


class CharacterRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def upsert_authorization(
        self,
        character_id: int,
        name: str,
        encrypted_refresh_token: str,
        authorized_at: datetime,
    ) -> CharacterRecord:
        with self.database.connection() as connection:
            connection.execute(
                "INSERT INTO characters ("
                "character_id, name, encrypted_refresh_token, enabled, "
                "authorization_required, tracking_started_at, last_synced_at) "
                "VALUES (?, ?, ?, 1, 0, ?, NULL) "
                "ON CONFLICT(character_id) DO UPDATE SET name = excluded.name, "
                "encrypted_refresh_token = excluded.encrypted_refresh_token, "
                "enabled = 1, authorization_required = 0",
                (character_id, name, encrypted_refresh_token, _utc_iso(authorized_at)),
            )
            row = connection.execute(
                "SELECT * FROM characters WHERE character_id = ?", (character_id,)
            ).fetchone()
        assert row is not None
        return _character_from_row(row)

    def list_all(self) -> list[CharacterRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM characters ORDER BY character_id"
            ).fetchall()
        return [_character_from_row(row) for row in rows]

    def set_enabled(self, character_id: int, enabled: bool) -> None:
        self._update(character_id, "enabled", int(enabled))

    def replace_refresh_token(
        self, character_id: int, encrypted_refresh_token: str
    ) -> None:
        self._update(character_id, "encrypted_refresh_token", encrypted_refresh_token)

    def mark_authorization_required(self, character_id: int) -> None:
        self._update(character_id, "authorization_required", 1)

    def _update(self, character_id: int, column: str, value: object) -> None:
        with self.database.connection() as connection:
            connection.execute(
                f"UPDATE characters SET {column} = ? WHERE character_id = ?",
                (value, character_id),
            )


def _journal_values(entry: JournalEntry) -> tuple[object, ...]:
    return (
        entry.character_id,
        entry.journal_id,
        _utc_iso(entry.date_utc),
        _decimal_text(entry.amount),
        entry.ref_type,
        _decimal_text(entry.tax) if entry.tax is not None else None,
        _decimal_text(entry.balance) if entry.balance is not None else None,
        entry.description,
        entry.reason,
        entry.context_id,
        entry.context_id_type,
        entry.first_party_id,
        entry.first_party_type,
        entry.second_party_id,
        entry.second_party_type,
        entry.tax_receiver_id,
    )


def _journal_from_row(row: sqlite3.Row) -> JournalEntry:
    return JournalEntry(
        journal_id=row["journal_id"],
        character_id=row["character_id"],
        date_utc=_parse_utc(row["date_utc"]),
        amount=Decimal(row["amount"]),
        ref_type=row["ref_type"],
        tax=Decimal(row["tax"]) if row["tax"] is not None else None,
        balance=Decimal(row["balance"]) if row["balance"] is not None else None,
        description=row["description"],
        reason=row["reason"],
        context_id=row["context_id"],
        context_id_type=row["context_id_type"],
        first_party_id=row["first_party_id"],
        first_party_type=row["first_party_type"],
        second_party_id=row["second_party_id"],
        second_party_type=row["second_party_type"],
        tax_receiver_id=row["tax_receiver_id"],
    )


class JournalRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def commit_sync_batch(
        self,
        character_id: int,
        entries: Sequence[JournalEntry],
        completed_at: datetime,
    ) -> int:
        if any(entry.character_id != character_id for entry in entries):
            raise ValueError("journal entry belongs to another character")
        journal_ids = {entry.journal_id for entry in entries}
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            existing = self._existing_ids(connection, character_id, journal_ids)
            new_count = len(journal_ids - existing)
            for entry in entries:
                self._upsert(connection, entry)
            connection.execute(
                "UPDATE characters SET last_synced_at = ? WHERE character_id = ?",
                (_utc_iso(completed_at), character_id),
            )
        return new_count

    @staticmethod
    def _existing_ids(
        connection: sqlite3.Connection, character_id: int, journal_ids: set[int]
    ) -> set[int]:
        if not journal_ids:
            return set()
        placeholders = ", ".join("?" for _ in journal_ids)
        rows = connection.execute(
            "SELECT journal_id FROM wallet_journal WHERE character_id = ? "
            f"AND journal_id IN ({placeholders})",
            (character_id, *journal_ids),
        ).fetchall()
        return {row["journal_id"] for row in rows}

    @staticmethod
    def _upsert(connection: sqlite3.Connection, entry: JournalEntry) -> None:
        connection.execute(
            "INSERT INTO wallet_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(character_id, journal_id) DO UPDATE SET "
            "date_utc = excluded.date_utc, amount = excluded.amount, "
            "ref_type = excluded.ref_type, tax = excluded.tax, "
            "balance = excluded.balance, description = excluded.description, "
            "reason = excluded.reason, context_id = excluded.context_id, "
            "context_id_type = excluded.context_id_type, "
            "first_party_id = excluded.first_party_id, "
            "first_party_type = excluded.first_party_type, "
            "second_party_id = excluded.second_party_id, "
            "second_party_type = excluded.second_party_type, "
            "tax_receiver_id = excluded.tax_receiver_id",
            _journal_values(entry),
        )

    def list_for_report(
        self,
        character_ids: set[int],
        start_utc: datetime,
        end_utc_exclusive: datetime,
    ) -> list[JournalEntry]:
        if not character_ids:
            return []
        placeholders = ", ".join("?" for _ in character_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM wallet_journal WHERE character_id IN "
                f"({placeholders}) AND date_utc >= ? AND date_utc < ? "
                "ORDER BY date_utc, character_id, journal_id",
                (*character_ids, _utc_iso(start_utc), _utc_iso(end_utc_exclusive)),
            ).fetchall()
        return [_journal_from_row(row) for row in rows]
