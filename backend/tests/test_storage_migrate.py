"""Migration CLI tests: dry-run/apply/verify/prune/limit, local <-> moto S3 (R-21)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
from moto import mock_aws

from app.core.storage_backend import LocalStorage, S3Storage, StorageError, reset_storage_cache
from app.tools import storage_migrate as sm
from app.tools.storage_migrate import RecordingEntry, build_parser, load_recordings

BUCKET = "echoic-migrate-test"


@pytest.fixture()
def db():
    """Local session fixture so this module can run with or without conftest."""
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _clear_storage_cache():
    reset_storage_cache()
    yield
    reset_storage_cache()


@pytest.fixture()
def local_tree(tmp_path: Path):
    root = tmp_path / "data"
    storage = LocalStorage(root=root)
    payloads = {
        "u1": {"original.mp3": b"audio-one" * 100, "playback.m4a": b"playback-one" * 100},
        "u2": {},
        "u3": {"original.mp4": b"video" * 50},
    }
    for recording_uuid, files in payloads.items():
        for name, payload in files.items():
            source = tmp_path / f"src-{recording_uuid}-{name}"
            source.write_bytes(payload)
            storage.save_file(source, f"media/{recording_uuid}/{name}")
    entries = [
        RecordingEntry(id=1, uuid="u1", title="一", storage_dir="media/u1"),
        RecordingEntry(id=2, uuid="u2", title="二", storage_dir="media/u2"),
        RecordingEntry(id=3, uuid="u3", title="三", storage_dir="media/u3"),
    ]
    return storage, entries


@pytest.fixture()
def s3_target():
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_bucket(Bucket=BUCKET)
        yield S3Storage(
            bucket=BUCKET,
            endpoint="",
            access_key="testing",
            secret_key="testing",
            region="us-east-1",
            path_style=True,
        )


def run_cli(args, *, source, target, rows):
    out = io.StringIO()
    report = sm.run(args, source=source, target=target, rows=rows, out=out)
    payload = json.loads(out.getvalue())
    assert payload["exit_code"] == report.exit_code
    assert payload["mode"] == report.mode
    return report, payload


# -------------------------------------------------------------------- dry-run
def test_dry_run_writes_nothing(local_tree, s3_target) -> None:
    local, entries = local_tree
    report, payload = run_cli(
        ["--from", "local", "--to", "s3", "--dry-run"], source=local, target=s3_target, rows=entries
    )
    assert report.exit_code == 0
    assert report.mode == "dry-run"
    assert report.recordings == 3
    assert report.recordings_with_files == 2
    assert report.recordings_empty == 1
    assert report.files == 3
    assert report.copied == 3, "dry-run reports the planned copies"
    assert report.skipped == 0
    assert s3_target.list_prefix("media/") == [], "dry-run must not upload anything"
    assert len(local.list_prefix("media/")) == 3, "dry-run must not move local files"
    assert payload["copied"] == 3


# ---------------------------------------------------------------------- apply
def test_apply_copies_and_keeps_source(local_tree, s3_target) -> None:
    local, entries = local_tree
    report, _payload = run_cli(
        ["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries
    )
    assert report.exit_code == 0
    assert report.copied == 3
    assert report.verified == 3
    assert report.failed == 0
    assert sorted(s3_target.list_prefix("media/")) == [
        "media/u1/original.mp3",
        "media/u1/playback.m4a",
        "media/u3/original.mp4",
    ]
    assert s3_target.size("media/u1/original.mp3") == len(b"audio-one" * 100)
    assert len(local.list_prefix("media/")) == 3, "source is preserved unless --prune"

    verify, _ = run_cli(
        ["--from", "local", "--to", "s3", "--verify"], source=local, target=s3_target, rows=entries
    )
    assert verify.exit_code == 0
    assert verify.verified == 3
    assert verify.failed == 0


def test_apply_is_resumable(local_tree, s3_target) -> None:
    local, entries = local_tree
    run_cli(["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries)
    s3_target.client.delete_object(Bucket=s3_target.bucket, Key="media/u1/playback.m4a")
    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries
    )
    assert report.failed == 0
    assert report.skipped == 2
    assert report.copied == 1
    assert s3_target.exists("media/u1/playback.m4a")


def test_apply_limit_batches_recordings(local_tree, s3_target) -> None:
    local, entries = local_tree
    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--apply", "--limit", "1"],
        source=local,
        target=s3_target,
        rows=entries,
    )
    assert report.recordings == 1
    assert report.copied == 2
    assert s3_target.list_prefix("media/") == [
        "media/u1/original.mp3",
        "media/u1/playback.m4a",
    ]


def test_apply_prune_removes_source_after_verification(local_tree, s3_target) -> None:
    local, entries = local_tree
    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--apply", "--prune"],
        source=local,
        target=s3_target,
        rows=entries,
    )
    assert report.exit_code == 0
    assert report.copied == 3
    assert report.pruned == 2, "only recordings with files are pruned"
    assert local.list_prefix("media/") == []
    assert len(s3_target.list_prefix("media/")) == 3


# --------------------------------------------------------------------- verify
def test_verify_detects_corruption(local_tree, s3_target, tmp_path: Path) -> None:
    local, entries = local_tree
    run_cli(["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries)
    corrupt = tmp_path / "corrupt.bin"
    corrupt.write_bytes(b"X" * (len(b"audio-one" * 100)))  # same size, different content
    s3_target.save_file(corrupt, "media/u1/original.mp3")

    report, payload = run_cli(
        ["--from", "local", "--to", "s3", "--verify"], source=local, target=s3_target, rows=entries
    )
    assert report.exit_code == 1
    assert report.failed == 1
    assert report.verified == 2
    assert any("sha256" in item["error"] for item in payload["errors"])


def test_verify_detects_missing_object(local_tree, s3_target) -> None:
    local, entries = local_tree
    run_cli(["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries)
    s3_target.delete_prefix("media/u3")
    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--verify"], source=local, target=s3_target, rows=entries
    )
    assert report.exit_code == 1
    assert report.missing == 1
    assert report.failed == 1


def test_verify_reports_extra_target_objects(local_tree, s3_target, tmp_path: Path) -> None:
    local, entries = local_tree
    run_cli(["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries)
    extra = tmp_path / "extra.bin"
    extra.write_bytes(b"orphan")
    s3_target.save_file(extra, "media/u1/orphan.bin")
    report, payload = run_cli(
        ["--from", "local", "--to", "s3", "--verify"], source=local, target=s3_target, rows=entries
    )
    assert report.exit_code == 0
    assert report.extra_target == 1
    assert payload["warnings"], "extra target objects are reported as warnings"


# ------------------------------------------------------------------ direction
def test_s3_to_local(s3_target, tmp_path: Path) -> None:
    root = tmp_path / "data"
    local = LocalStorage(root=root)
    uuids = ["aaa", "bbb"]
    for recording_uuid in uuids:
        source = tmp_path / f"src-{recording_uuid}"
        source.write_bytes(recording_uuid.encode() * 10)
        s3_target.save_file(source, f"media/{recording_uuid}/original.mp3")
    entries = [
        RecordingEntry(id=1, uuid="aaa", storage_dir="media/aaa"),
        RecordingEntry(id=2, uuid="bbb", storage_dir="media/bbb"),
    ]
    dry, _ = run_cli(
        ["--from", "s3", "--to", "local", "--dry-run"], source=s3_target, target=local, rows=entries
    )
    assert dry.copied == 2
    assert local.list_prefix("media/") == []

    apply_report, _ = run_cli(
        ["--from", "s3", "--to", "local", "--apply"], source=s3_target, target=local, rows=entries
    )
    assert apply_report.exit_code == 0
    assert apply_report.copied == 2
    assert local.exists("media/aaa/original.mp3")
    assert (root / "media" / "bbb" / "original.mp3").read_bytes() == b"bbb" * 10


# ------------------------------------------------------------- entry handling
def test_missing_storage_dir_falls_back_to_uuid(tmp_path: Path) -> None:
    local = LocalStorage(root=tmp_path / "data")
    source = tmp_path / "orig"
    source.write_bytes(b"fallback")
    local.save_file(source, "media/fallback-uuid/original.mp3")
    entry = RecordingEntry(id=7, uuid="fallback-uuid", storage_dir="")
    assert entry.prefix == "media/fallback-uuid"

    class _EmptyTarget:
        name = "fake"

        def list_prefix(self, prefix):
            return []

        def exists(self, key):
            return False

    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--dry-run"],
        source=local,
        target=_EmptyTarget(),
        rows=[entry],
    )
    assert report.copied == 1


def test_invalid_entry_reports_warning(local_tree, s3_target) -> None:
    local, _entries = local_tree
    entries = [RecordingEntry(id=9, uuid="", storage_dir="")]
    report, payload = run_cli(
        ["--from", "local", "--to", "s3", "--dry-run"], source=local, target=s3_target, rows=entries
    )
    assert report.recordings == 0
    assert payload["warnings"]
    assert report.exit_code == 0


def test_rows_callable(local_tree, s3_target) -> None:
    local, entries = local_tree
    report = sm.run(
        ["--from", "local", "--to", "s3", "--dry-run"],
        source=local,
        target=s3_target,
        rows=lambda: entries[:1],
        out=io.StringIO(),
    )
    assert report.files == 2


# ------------------------------------------------------------ CLI / exit codes
@pytest.mark.parametrize(
    "args",
    [
        ["--from", "local", "--to", "local", "--dry-run"],
        ["--from", "local", "--to", "s3", "--dry-run", "--prune"],
        ["--from", "local", "--to", "s3", "--verify", "--prune"],
        ["--from", "local", "--to", "s3", "--verify", "--create-bucket"],
        ["--from", "local", "--to", "s3", "--apply", "--limit", "0"],
    ],
)
def test_invalid_argument_combinations(local_tree, s3_target, args) -> None:
    local, entries = local_tree
    with pytest.raises(sm.MigrationError):
        sm.run(args, source=local, target=s3_target, rows=entries, out=io.StringIO())


def test_argparse_requires_a_mode() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--from", "local", "--to", "s3"])


def test_main_prints_json_and_returns_exit_code(local_tree, s3_target, monkeypatch, capsys) -> None:
    local, entries = local_tree
    monkeypatch.setattr(sm, "load_recordings", lambda **kwargs: entries)
    monkeypatch.setattr(
        sm, "build_storage", lambda name=None: local if name == "local" else s3_target
    )
    code = sm.main(["--from", "local", "--to", "s3", "--apply"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["copied"] == 3

    code = sm.main(["--from", "local", "--to", "s3", "--verify"])
    assert code == 0


def test_main_setup_error_returns_two(monkeypatch, capsys) -> None:
    def boom(**kwargs):
        raise sm.MigrationError("数据库不可用")

    monkeypatch.setattr(sm, "load_recordings", boom)
    monkeypatch.setattr(sm, "build_storage", lambda name=None: LocalStorage())
    code = sm.main(["--from", "local", "--to", "s3", "--dry-run"])
    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert "数据库不可用" in payload["error"]


# ------------------------------------------------------------------- DB rows
def test_load_recordings_includes_trashed_and_limits(db, tmp_path: Path) -> None:
    from app.core.utils import utcnow
    from app.db import init_db
    from app.models import Recording

    init_db()
    recording = Recording(
        uuid=str(uuid4()),
        title="迁移 CLI 读取测试",
        storage_dir="media/db-row",
        deleted_at=utcnow(),
    )
    db.add(recording)
    db.commit()
    try:
        entries = load_recordings(include_trashed=True)
        matched = [entry for entry in entries if entry.uuid == recording.uuid]
        assert matched, "trashed recordings are included by default"
        assert matched[0].storage_dir == "media/db-row"
        assert matched[0].deleted_at is not None

        active_only = load_recordings(include_trashed=False)
        assert all(entry.uuid != recording.uuid for entry in active_only)

        assert len(load_recordings(limit=1)) == 1
    finally:
        db.delete(recording)
        db.commit()


def test_apply_prune_skips_failed_recording(local_tree, s3_target) -> None:
    """A failed copy must keep the source of that recording (no data loss)."""
    local, entries = local_tree

    class FlakyTarget(S3Storage):
        def save_file(self, local_path, key, **kwargs):
            if key.endswith("original.mp4"):
                raise StorageError("模拟上传失败")
            return super().save_file(local_path, key, **kwargs)

    flaky = FlakyTarget(
        bucket=s3_target.bucket,
        endpoint="",
        access_key="testing",
        secret_key="testing",
        region="us-east-1",
        path_style=True,
    )
    report, _ = run_cli(
        ["--from", "local", "--to", "s3", "--apply", "--prune"],
        source=local,
        target=flaky,
        rows=entries,
    )
    assert report.exit_code == 1
    assert report.failed == 1
    assert report.copied == 2
    assert report.pruned == 1, "only the fully verified recording is pruned"
    assert not local.exists("media/u1/original.mp3"), "verified recording source should be pruned"
    assert local.exists("media/u3/original.mp4"), "failed recording source must be kept"


def test_verify_no_hash_only_checks_size(local_tree, s3_target, tmp_path: Path) -> None:
    local, entries = local_tree
    run_cli(["--from", "local", "--to", "s3", "--apply"], source=local, target=s3_target, rows=entries)
    corrupt = tmp_path / "same-size.bin"
    corrupt.write_bytes(b"X" * len(b"audio-one" * 100))
    s3_target.save_file(corrupt, "media/u1/original.mp3")

    hashed, _ = run_cli(
        ["--from", "local", "--to", "s3", "--verify"], source=local, target=s3_target, rows=entries
    )
    assert hashed.exit_code == 1

    size_only, _ = run_cli(
        ["--from", "local", "--to", "s3", "--verify", "--no-hash"],
        source=local,
        target=s3_target,
        rows=entries,
    )
    assert size_only.exit_code == 0, "--no-hash documents the weaker size-only check"
    assert size_only.verified == 3
