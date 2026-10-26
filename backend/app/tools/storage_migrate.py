"""Object storage migration CLI (v0.3.0 R-21).

Copies media objects between the local data directory and an S3/MinIO bucket
using the :mod:`app.core.storage_backend` abstraction, without touching the
database schema (``settings.storage_backend`` decides where the application
reads/writes; this CLI only moves the bytes).

Usage (run from the ``backend`` directory / inside the container)::

    # 1) 查看计划（不写任何文件）
    python -m app.tools.storage_migrate --from local --to s3 --dry-run

    # 2) 执行迁移（默认保留源文件，可重复执行，已存在且校验通过的会跳过）
    python -m app.tools.storage_migrate --from local --to s3 --apply

    # 3) 校验源/目标一致（size + sha256）
    python -m app.tools.storage_migrate --from local --to s3 --verify

    # 可选：分批迁移（每次 50 条录音，失败后可重跑续传）
    python -m app.tools.storage_migrate --from local --to s3 --apply --limit 50

    # 可选：确认无误后清理源文件（谨慎，建议先备份 data 卷）
    python -m app.tools.storage_migrate --from local --to s3 --apply --prune

    # 反向迁移（S3 -> 本地）
    python -m app.tools.storage_migrate --from s3 --to local --apply

Design notes
------------
* Recordings are enumerated from the database (active **and** trashed rows) in
  id order.  ``run(..., rows=...)`` accepts an injected iterable/callable of
  :class:`RecordingEntry` objects so tests never need a database.
* Source files are preserved by default; ``--prune`` only removes a
  recording's source prefix after every key for that recording was copied and
  verified.
* Verification uses object size plus sha256 (multipart ETag != MD5, so ETag is
  never used as a checksum).  ``--no-hash`` skips hashing for speed.
* Exit code ``0`` = success, ``1`` = one or more failures, ``2`` = setup error
  (bad arguments / unreadable DB / storage unavailable).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from ..core.storage_backend import (
    StorageBackend,
    StorageError,
    build_storage,
    recording_prefix,
)

logger = logging.getLogger("echoic.storage_migrate")

MODE_DRY_RUN = "dry-run"
MODE_APPLY = "apply"
MODE_VERIFY = "verify"

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class MigrationError(Exception):
    """Fatal migration setup problem (never raised for per-file failures)."""


# --------------------------------------------------------------------- entries
@dataclass(frozen=True)
class RecordingEntry:
    """Minimal recording descriptor needed by the migration."""

    id: int
    uuid: str
    title: str = ""
    storage_dir: str = ""
    original_ext: str = ""
    size_bytes: int = 0
    deleted_at: str | None = None

    @property
    def prefix(self) -> str:
        """Logical storage prefix (``media/<uuid>``); ``""`` when unknown."""
        raw = (self.storage_dir or "").strip().strip("/")
        if not raw and self.uuid:
            raw = f"media/{self.uuid}"
        if not raw:
            return ""
        try:
            return recording_prefix(raw)
        except StorageError:
            return ""


def load_recordings(
    *, limit: int | None = None, include_trashed: bool = True
) -> list[RecordingEntry]:
    """Enumerate recordings from the application database (read only)."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from ..db import SessionLocal
    from ..models import Recording

    deleted_column = getattr(Recording, "deleted_at", None)
    columns: list[Any] = [
        Recording.id.label("id"),
        Recording.uuid.label("uuid"),
        Recording.title.label("title"),
        Recording.storage_dir.label("storage_dir"),
        Recording.original_ext.label("original_ext"),
        Recording.size_bytes.label("size_bytes"),
    ]
    if deleted_column is not None:
        columns.append(deleted_column.label("deleted_at"))
    stmt = select(*columns).order_by(Recording.id)
    if deleted_column is not None and not include_trashed:
        stmt = stmt.where(deleted_column.is_(None))
    if limit is not None and int(limit) > 0:
        stmt = stmt.limit(int(limit))
    try:
        with SessionLocal() as session:
            rows = session.execute(stmt).all()
    except SQLAlchemyError as exc:
        raise MigrationError(
            f"读取录音列表失败：{exc}（请先启动一次应用以完成数据库迁移）"
        ) from exc
    entries: list[RecordingEntry] = []
    for row in rows:
        mapping = row._mapping
        deleted = mapping.get("deleted_at")
        entries.append(
            RecordingEntry(
                id=int(mapping["id"]),
                uuid=str(mapping.get("uuid") or ""),
                title=str(mapping.get("title") or ""),
                storage_dir=str(mapping.get("storage_dir") or ""),
                original_ext=str(mapping.get("original_ext") or ""),
                size_bytes=int(mapping.get("size_bytes") or 0),
                deleted_at=deleted.isoformat() if hasattr(deleted, "isoformat") else deleted,
            )
        )
    return entries


# ---------------------------------------------------------------------- report
@dataclass
class MigrationReport:
    """Aggregated migration result (also printed as JSON)."""

    mode: str
    source: str
    target: str
    recordings: int = 0
    recordings_with_files: int = 0
    recordings_empty: int = 0
    files: int = 0
    bytes: int = 0
    copied: int = 0
    skipped: int = 0
    verified: int = 0
    failed: int = 0
    missing: int = 0
    pruned: int = 0
    extra_target: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 0 if self.failed == 0 and not self.errors else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.exit_code == 0,
            "mode": self.mode,
            "from": self.source,
            "to": self.target,
            "exit_code": self.exit_code,
            "recordings": self.recordings,
            "recordings_with_files": self.recordings_with_files,
            "recordings_empty": self.recordings_empty,
            "files": self.files,
            "bytes": self.bytes,
            "copied": self.copied,
            "skipped": self.skipped,
            "verified": self.verified,
            "failed": self.failed,
            "missing": self.missing,
            "pruned": self.pruned,
            "extra_target": self.extra_target,
            "errors": self.errors,
            "warnings": self.warnings,
        }

    def summary_line(self) -> str:
        return (
            f"[{self.mode}] {self.source} -> {self.target}: "
            f"{self.recordings} 条录音 / {self.files} 个文件 / "
            f"复制 {self.copied} / 跳过 {self.skipped} / 校验 {self.verified} / "
            f"失败 {self.failed} / 清理 {self.pruned}"
        )


# ---------------------------------------------------------------- copy helpers
def _sha256(backend: StorageBackend, key: str) -> str:
    digest = hashlib.sha256()
    for chunk in backend.open_stream(key):
        digest.update(chunk)
    return digest.hexdigest()


def _copy_key(source: StorageBackend, target: StorageBackend, key: str) -> int:
    local = source.local_path_if_any(key)
    if local is not None:
        return int(target.save_file(local, key))
    handle, temp_name = tempfile.mkstemp(prefix="echoic-migrate-", suffix=Path(key).suffix)
    os.close(handle)
    temp_path = Path(temp_name)
    try:
        source.download_file(key, temp_path)
        return int(target.save_file(temp_path, key))
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - defensive
            pass


def _record_error(
    report: MigrationReport, entry: RecordingEntry, message: str, *, key: str | None = None
) -> None:
    report.failed += 1
    item: dict[str, Any] = {"recording_id": entry.id, "uuid": entry.uuid, "error": message}
    if key is not None:
        item["key"] = key
    report.errors.append(item)
    logger.warning("recording %s (%s): %s", entry.id, entry.uuid, message)


def _same_content(
    source: StorageBackend,
    target: StorageBackend,
    key: str,
    *,
    size: int,
    verify_hash: bool,
) -> bool:
    """Whether ``target`` already holds an identical copy of ``key``."""
    if not target.exists(key):
        return False
    if int(target.size(key)) != int(size):
        return False
    if verify_hash and _sha256(source, key) != _sha256(target, key):
        return False
    return True


# ------------------------------------------------------------------ per-mode
def _dry_run_entry(
    report: MigrationReport,
    entry: RecordingEntry,
    source: StorageBackend,
    target: StorageBackend,
    keys: list[str],
) -> None:
    for key in keys:
        report.files += 1
        try:
            size = int(source.size(key))
        except StorageError as exc:
            _record_error(report, entry, f"读取源文件大小失败：{exc}", key=key)
            continue
        report.bytes += size
        try:
            exists = target.exists(key)
        except StorageError as exc:
            _record_error(report, entry, f"检查目标对象失败：{exc}", key=key)
            continue
        if not exists:
            report.copied += 1
            continue
        try:
            target_size = int(target.size(key))
        except StorageError as exc:
            _record_error(report, entry, f"读取目标文件大小失败：{exc}", key=key)
            continue
        if target_size == size:
            report.skipped += 1
        else:
            report.copied += 1
            report.warnings.append(
                f"录音 {entry.id} 的目标对象大小不一致，apply 时将覆盖：{key}"
            )


def _apply_entry(
    report: MigrationReport,
    entry: RecordingEntry,
    source: StorageBackend,
    target: StorageBackend,
    keys: list[str],
    *,
    verify_hash: bool,
    prune: bool,
) -> None:
    recording_failed = 0
    before_failed = report.failed
    for key in keys:
        report.files += 1
        try:
            source_size = int(source.size(key))
        except StorageError as exc:
            _record_error(report, entry, f"读取源文件大小失败：{exc}", key=key)
            continue
        report.bytes += source_size
        try:
            if _same_content(source, target, key, size=source_size, verify_hash=verify_hash):
                report.skipped += 1
                report.verified += 1
                continue
        except StorageError as exc:
            _record_error(report, entry, f"校验已有目标对象失败：{exc}", key=key)
            continue
        try:
            copied_size = _copy_key(source, target, key)
        except StorageError as exc:
            _record_error(report, entry, f"复制文件失败：{exc}", key=key)
            continue
        report.copied += 1
        if copied_size != source_size:
            _record_error(
                report,
                entry,
                f"复制后大小不一致（源 {source_size} / 目标 {copied_size}）",
                key=key,
            )
            continue
        if verify_hash:
            try:
                source_hash = _sha256(source, key)
                target_hash = _sha256(target, key)
            except StorageError as exc:
                _record_error(report, entry, f"校验 sha256 失败：{exc}", key=key)
                continue
            if source_hash != target_hash:
                _record_error(report, entry, "复制后 sha256 不一致", key=key)
                continue
        report.verified += 1
    if report.failed > before_failed:
        recording_failed = report.failed - before_failed
    if prune and recording_failed == 0:
        try:
            removed = source.delete_prefix(entry.prefix)
        except StorageError as exc:
            report.warnings.append(f"录音 {entry.id} 清理源文件失败：{exc}")
        else:
            report.pruned += 1
            logger.info("pruned source objects for recording %s (%s files)", entry.id, removed)


def _verify_entry(
    report: MigrationReport,
    entry: RecordingEntry,
    source: StorageBackend,
    target: StorageBackend,
    keys: list[str],
    *,
    verify_hash: bool,
) -> None:
    try:
        target_keys = set(target.list_prefix(f"{entry.prefix}/"))
    except StorageError as exc:
        _record_error(report, entry, f"列举目标对象失败：{exc}")
        return
    source_set = set(keys)
    for key in keys:
        report.files += 1
        try:
            source_size = int(source.size(key))
        except StorageError as exc:
            _record_error(report, entry, f"读取源文件大小失败：{exc}", key=key)
            continue
        report.bytes += source_size
        if key not in target_keys:
            report.missing += 1
            _record_error(report, entry, "目标缺失该对象（未迁移）", key=key)
            continue
        try:
            target_size = int(target.size(key))
            if target_size != source_size:
                _record_error(
                    report,
                    entry,
                    f"大小不一致（源 {source_size} / 目标 {target_size}）",
                    key=key,
                )
                continue
            if verify_hash and _sha256(source, key) != _sha256(target, key):
                _record_error(report, entry, "sha256 不一致", key=key)
                continue
        except StorageError as exc:
            _record_error(report, entry, f"校验目标对象失败：{exc}", key=key)
            continue
        report.verified += 1
    extras = target_keys - source_set
    if extras:
        report.extra_target += len(extras)
        report.warnings.append(
            f"录音 {entry.id} 的目标存储存在 {len(extras)} 个源中不存在的对象（可忽略）"
        )


# ----------------------------------------------------------------------- runner
def _resolve_entries(
    rows: Iterable[RecordingEntry] | Callable[[], Iterable[RecordingEntry]] | None,
    *,
    limit: int | None,
) -> list[RecordingEntry]:
    if rows is None:
        return load_recordings(limit=limit)
    values = rows() if callable(rows) else rows
    entries = list(values or [])
    if limit is not None and int(limit) > 0:
        entries = entries[: int(limit)]
    return entries


def _preflight(backend: StorageBackend, role: str) -> None:
    """Fail fast (with a Chinese message) when an S3 endpoint is unusable."""
    if getattr(backend, "name", "") != "s3":
        return
    status_fn = getattr(backend, "status", None)
    if not callable(status_fn):
        return
    try:
        status = dict(status_fn())
    except Exception as exc:  # pragma: no cover - defensive
        raise MigrationError(f"{role}对象存储状态检查失败：{exc}") from exc
    if status.get("status") != "ok":
        detail = status.get("detail") or "未知错误"
        raise MigrationError(f"{role}对象存储不可用：{detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.tools.storage_migrate",
        description="在本地 data 目录与 S3/MinIO 之间迁移媒体对象（v0.3.0 R-21）",
    )
    parser.add_argument("--from", dest="from_name", required=True, choices=("local", "s3"), help="源存储后端")
    parser.add_argument("--to", dest="to_name", required=True, choices=("local", "s3"), help="目标存储后端")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="只统计迁移计划，不写入目标")
    mode.add_argument("--apply", action="store_true", help="执行复制（默认保留源文件）")
    mode.add_argument("--verify", action="store_true", help="校验源与目标一致性（不写入）")
    parser.add_argument("--limit", type=int, default=None, help="最多处理 N 条录音（分批迁移/续跑）")
    parser.add_argument(
        "--prune",
        "--prune-source",
        dest="prune",
        action="store_true",
        help="apply 全部校验通过后删除源对象（默认保留，谨慎使用）",
    )
    parser.add_argument(
        "--no-hash",
        dest="verify_hash",
        action="store_false",
        default=True,
        help="只按大小校验，跳过 sha256（更快但弱校验）",
    )
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="apply 前自动创建目标 S3 bucket（MinIO 首次部署）",
    )
    parser.add_argument("--quiet", action="store_true", help="仅输出 JSON 结果，不打印日志")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    source: StorageBackend | None = None,
    target: StorageBackend | None = None,
    rows: Iterable[RecordingEntry] | Callable[[], Iterable[RecordingEntry]] | None = None,
    out: TextIO | None = None,
) -> MigrationReport:
    """Execute the migration and return a report (also prints it as JSON).

    ``source``/``target``/``rows`` are injection points for tests and for
    embedding the migration in other tooling.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    mode = MODE_DRY_RUN if args.dry_run else MODE_APPLY if args.apply else MODE_VERIFY
    if args.from_name == args.to_name:
        raise MigrationError("--from 与 --to 不能相同")
    if args.prune and mode != MODE_APPLY:
        raise MigrationError("--prune 只能与 --apply 一起使用")
    if args.create_bucket and mode != MODE_APPLY:
        raise MigrationError("--create-bucket 只能与 --apply 一起使用")
    if args.limit is not None and int(args.limit) <= 0:
        raise MigrationError("--limit 必须大于 0")

    source_backend = source if source is not None else build_storage(args.from_name)
    target_backend = target if target is not None else build_storage(args.to_name)
    _preflight(source_backend, "源")
    _preflight(target_backend, "目标")

    report = MigrationReport(mode=mode, source=args.from_name, target=args.to_name)
    if args.create_bucket:
        ensure = getattr(target_backend, "ensure_bucket", None)
        if not callable(ensure):
            raise MigrationError("--create-bucket 仅支持 S3 目标后端")
        try:
            created = bool(ensure())
        except StorageError as exc:
            raise MigrationError(f"创建目标 bucket 失败：{exc}") from exc
        if created:
            report.warnings.append(f"已创建目标 bucket：{getattr(target_backend, 'bucket', '')}")

    entries = _resolve_entries(rows, limit=args.limit)
    logger.info("migrating %s recordings (%s -> %s, mode=%s)", len(entries), args.from_name, args.to_name, mode)
    for entry in entries:
        prefix = entry.prefix
        if not prefix:
            report.warnings.append(f"录音 {entry.id} 缺少存储路径，已跳过")
            continue
        report.recordings += 1
        scan_prefix = f"{prefix}/"
        try:
            keys = source_backend.list_prefix(scan_prefix)
        except StorageError as exc:
            _record_error(report, entry, f"列举源文件失败：{exc}")
            continue
        if not keys:
            report.recordings_empty += 1
            continue
        report.recordings_with_files += 1
        if mode == MODE_DRY_RUN:
            _dry_run_entry(report, entry, source_backend, target_backend, keys)
        elif mode == MODE_APPLY:
            _apply_entry(
                report,
                entry,
                source_backend,
                target_backend,
                keys,
                verify_hash=bool(args.verify_hash),
                prune=bool(args.prune),
            )
        else:
            _verify_entry(
                report,
                entry,
                source_backend,
                target_backend,
                keys,
                verify_hash=bool(args.verify_hash),
            )

    payload = report.to_dict()
    stream = out if out is not None else sys.stdout
    stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    stream.flush()
    logger.info(report.summary_line())
    if report.exit_code != 0:
        logger.warning("%s 项失败，可修复后重跑（已完成的文件会被跳过）", report.failed)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(argv) if argv is not None else list(sys.argv[1:])
    if not logging.getLogger().handlers:
        level = logging.WARNING if "--quiet" in arguments else logging.INFO
        logging.basicConfig(level=level, format=DEFAULT_LOG_FORMAT)
    try:
        report = run(arguments)
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess by operators
    raise SystemExit(main())
