import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zipfile import ZipFile

import pytest

from app.migration_recovery import RecoverySnapshotError, RecoverySnapshotService
from app.paths import resolve_app_paths

NOW = datetime(2026, 10, 2, tzinfo=UTC)


def fixture_paths(tmp_path):
    def paths(name):
        return resolve_app_paths(
            frozen=True,
            bundle_root=tmp_path / "program",
            data_root=tmp_path / name / "EVE Income Tracker",
        )

    source, destination = paths("source"), paths("destination")
    source.data_dir.mkdir(parents=True)
    source.config_dir.mkdir()
    destination.backups_dir.mkdir(parents=True)
    source.key_path.write_bytes(b"private local key")
    with sqlite3.connect(source.database_path) as db:
        db.execute("CREATE TABLE income (amount INTEGER)")
        db.execute("INSERT INTO income VALUES (42)")
    source.logs_dir.mkdir()
    (source.logs_dir / "private.log").write_bytes(b"log")
    source.webview_storage_dir.mkdir()
    (source.webview_storage_dir / "cache").write_bytes(b"cache")
    source.backups_dir.mkdir()
    (source.backups_dir / "ordinary.zip").write_bytes(b"backup")
    return source, destination


def test_archive_contains_only_consistent_database_key_and_manifest(tmp_path):
    source, destination = fixture_paths(tmp_path)
    db = sqlite3.connect(source.database_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO income VALUES (73)")
    db.commit()
    try:
        info = RecoverySnapshotService(clock=lambda: NOW).create(source, destination)
    finally:
        db.close()

    assert info.created_at == NOW
    assert info.successful_starts == 0
    assert info.size_bytes == info.archive_path.stat().st_size
    with ZipFile(info.archive_path) as archive:
        assert set(archive.namelist()) == {
            "data/eve_income.sqlite3",
            "config/local.key",
            "recovery-manifest.json",
        }
        assert archive.read("config/local.key") == b"private local key"
        manifest = json.loads(archive.read("recovery-manifest.json"))
        assert manifest["created_at"] == "2026-10-02T00:00:00Z"
        restored = tmp_path / "restored.sqlite3"
        restored.write_bytes(archive.read("data/eve_income.sqlite3"))
    with sqlite3.connect(restored) as connection:
        assert connection.execute(
            "SELECT amount FROM income ORDER BY amount"
        ).fetchall() == [(42,), (73,)]
    assert RecoverySnapshotService(clock=lambda: NOW).inspect(destination) == info


def test_failed_new_snapshot_keeps_old_and_successful_replacement_is_unique(
    tmp_path, monkeypatch
):
    source, destination = fixture_paths(tmp_path)
    service = RecoverySnapshotService(clock=lambda: NOW)
    old = service.create(source, destination)
    old_bytes = old.archive_path.read_bytes()
    source.key_path.write_bytes(b"replacement key")
    real_validate = service._validate_archive

    def fail_validation(_path):
        raise RecoverySnapshotError("snapshot_invalid")

    monkeypatch.setattr(service, "_validate_archive", fail_validation)
    with pytest.raises(RecoverySnapshotError) as caught:
        service.create(source, destination)
    assert caught.value.code == "snapshot_invalid"
    assert old.archive_path.read_bytes() == old_bytes
    monkeypatch.setattr(service, "_validate_archive", real_validate)
    new = service.create(source, destination)
    assert list(destination.backups_dir.glob("*.zip")) == [new.archive_path]
    with ZipFile(new.archive_path) as archive:
        assert archive.read("config/local.key") == b"replacement key"


def test_prune_requires_both_three_starts_and_seven_days(tmp_path):
    source, destination = fixture_paths(tmp_path)
    now = [NOW]
    service = RecoverySnapshotService(clock=lambda: now[0])
    service.create(source, destination)
    now[0] = NOW + timedelta(days=7) - timedelta(microseconds=1)
    for count in range(1, 4):
        assert service.record_success_and_prune(destination).successful_starts == count
    assert service.inspect(destination) is not None
    now[0] = NOW + timedelta(days=7)
    assert service.record_success_and_prune(destination) is None
    assert service.inspect(destination) is None


def test_old_snapshot_remains_until_third_successful_start(tmp_path):
    source, destination = fixture_paths(tmp_path)
    now = [NOW]
    service = RecoverySnapshotService(clock=lambda: now[0])
    service.create(source, destination)
    now[0] = NOW + timedelta(days=7)
    assert service.record_success_and_prune(destination).successful_starts == 1
    assert service.record_success_and_prune(destination).successful_starts == 2
    assert service.record_success_and_prune(destination) is None


def test_manual_delete_is_idempotent_and_info_reports_freed_archive_bytes(tmp_path):
    source, destination = fixture_paths(tmp_path)
    service = RecoverySnapshotService(clock=lambda: NOW)
    info = service.create(source, destination)
    actual_bytes = info.archive_path.stat().st_size
    assert service.delete(destination) is True
    assert info.size_bytes == actual_bytes
    assert not info.archive_path.exists()
    assert service.delete(destination) is False


@pytest.mark.parametrize("damage", ["bad-zip", "extra-entry", "bad-manifest"])
def test_inspect_rejects_corruption_with_stable_non_leaking_code(tmp_path, damage):
    source, destination = fixture_paths(tmp_path)
    service = RecoverySnapshotService(clock=lambda: NOW)
    info = service.create(source, destination)
    if damage == "bad-zip":
        info.archive_path.write_bytes(b"secret invalid zip")
    else:
        with ZipFile(info.archive_path) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        if damage == "extra-entry":
            entries["logs/private.log"] = b"secret log"
        else:
            entries["recovery-manifest.json"] = b'{"created_at":"secret path"}'
        with ZipFile(info.archive_path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
    with pytest.raises(RecoverySnapshotError) as caught:
        service.inspect(destination)
    assert caught.value.code == "snapshot_invalid"
    assert str(tmp_path) not in str(caught.value)
    assert "secret" not in str(caught.value)
