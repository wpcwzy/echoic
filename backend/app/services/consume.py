"""Consume-directory auto-import (R-22, paperless-style “投递即入库”).

Drop an audio/video file into ``ECHOIC_CONSUME_DIR`` (default ``<data>/consume``)
and the worker imports it automatically:

1. only stable top-level files are considered (``(size, mtime)`` unchanged since
   the previous poll, or older than ``2 × interval``);
2. the file is hashed (sha256) and deduplicated through the ``imported_files``
   table;
3. ``core.media.probe`` extracts duration / media type;
4. the file is moved into ``data/media/{uuid}/original.ext`` (atomic rename,
   copy+delete fallback across devices);
5. a ``Recording`` is created (``source="consume"``, title = filename stem,
   ``recorded_at`` = file mtime, owner = configured user / first admin) and
   queued for the normal AI pipeline.

Behaviour decisions (documented for the API/frontend contract):

* **Duplicates** — the file is left in place (never silently deleted) and
  counted in ``duplicates``; a duplicate is logged once per sha256 to avoid log
  spam. The dedupe record keeps future drops from creating new recordings.
* **Failures** — unsupported/zero-byte/unstable/partial files are skipped (they
  stay in place for a later scan). Files that pass the extension/stability
  checks but fail hashing/probing/moving are quarantined to
  ``<consume>/failed/<name>`` with a ``<name>.error.txt`` sidecar and recorded in
  ``imported_files`` with ``status="failed"``. A failed sha256 can be retried by
  dropping the file back into the consume directory.
* **Retry** — if the dedupe row exists with ``status="failed"`` the import is
  attempted again and the row is updated in place on success.
* A single bad file never aborts the scan or crashes the worker.

Public entry points:
    scan_once(db, force_stability=False) -> ScanResult
    scan_now() -> dict                  # admin "立即扫描" helper
    maybe_scan(force=False)             # worker loop integration
    scan_loop(stop_event, interval_sec) # optional dedicated loop
    consume_status() -> dict            # /api/system/info consume block
    last_scan_info() -> dict | None     # worker_status()["last_consume_scan"]
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..core import media, storage
from ..core.utils import to_iso, utcnow
from ..db import SessionLocal
from ..models import ImportedFile, Recording, RecordingStatus, Stage, User
from .queue import enqueue
from .settings_store import load_ai_config

logger = logging.getLogger(__name__)

STATUS_IMPORTED = "imported"
STATUS_FAILED = "failed"

# Partially-written downloads / temp files that must never be imported.
SKIP_SUFFIXES = {
    ".part",
    ".partial",
    ".partial~",
    ".tmp",
    ".temp",
    ".crdownload",
    ".download",
    ".opdownload",
}
HASH_CHUNK_BYTES = 1024 * 1024
MAX_ERROR_CHARS = 1000
MIN_INTERVAL_SEC = 5


@dataclass
class ScanResult:
    """One consume-directory scan result."""

    scanned: int = 0
    imported: int = 0
    duplicates: int = 0
    failed: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": int(self.scanned),
            "imported": int(self.imported),
            "duplicates": int(self.duplicates),
            "failed": int(self.failed),
        }


# Worker/API share one process: serialise scans so the same file cannot be
# imported twice concurrently (manual "scan now" while the worker is scanning).
_scan_lock = threading.RLock()
# path -> (size, mtime_ns) at the previous poll, used for stability checks.
_seen: dict[str, tuple[int, int]] = {}
# path -> (size, mtime_ns, sha256), avoids re-hashing unchanged duplicates.
_hash_cache: dict[str, tuple[int, int, str]] = {}
_logged_duplicates: set[str] = set()
_state: dict[str, Any] = {"last_scan": None}
_last_scan_monotonic: float | None = None


# --------------------------------------------------------------------- helpers
def _interval_sec() -> int:
    try:
        value = int(settings.consume_interval_sec or 15)
    except (TypeError, ValueError):
        value = 15
    return max(MIN_INTERVAL_SEC, value)


def _consume_dir() -> Path:
    try:
        return settings.consume_path
    except Exception:  # pragma: no cover - defensive (unusable path config)
        logger.exception("invalid consume directory configuration")
        return settings.data_path / "consume"


def _is_ignored_name(name: str) -> bool:
    """Hidden files and partial-download names are never imported."""
    if not name or name.startswith(".") or name.startswith("~"):
        return True
    lowered = name.lower()
    if lowered.endswith("~"):
        return True
    return Path(lowered).suffix in SKIP_SUFFIXES


def _iter_candidates(root: Path) -> list[Path]:
    """Top-level regular files (no dirs/hidden/temp files), sorted by name."""
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        logger.warning("consume directory is not readable: %s", root, exc_info=True)
        return []
    candidates: list[Path] = []
    for path in entries:
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        if _is_ignored_name(path.name):
            continue
        candidates.append(path)
    return candidates


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_cached(path: Path, stat: os.stat_result) -> str:
    key = str(path)
    fingerprint = (int(stat.st_size), int(stat.st_mtime_ns))
    cached = _hash_cache.get(key)
    if cached is not None and (cached[0], cached[1]) == fingerprint:
        return cached[2]
    sha = _sha256(path)
    _hash_cache[key] = (fingerprint[0], fingerprint[1], sha)
    return sha


def _is_stable(path: Path, stat: os.stat_result) -> bool:
    """True when the file did not change between two polls, or it is old enough."""
    key = str(path)
    fingerprint = (int(stat.st_size), int(stat.st_mtime_ns))
    previous = _seen.get(key)
    _seen[key] = fingerprint
    if previous == fingerprint:
        return True
    try:
        age = time.time() - float(stat.st_mtime)
    except (TypeError, ValueError):
        age = 0.0
    return age > _interval_sec() * 2


def _prune_seen(candidates: list[Path]) -> None:
    current = {str(path) for path in candidates}
    for key in [key for key in _seen if key not in current]:
        _seen.pop(key, None)
    for key in [key for key in _hash_cache if key not in current]:
        _hash_cache.pop(key, None)


def _move_file(source: Path, destination: Path) -> None:
    """Atomic rename when possible, copy+delete fallback across filesystems."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        return
    except OSError:
        pass
    shutil.copy2(source, destination)
    try:
        source.unlink()
    except OSError:
        pass


def _unique_destination(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    for index in range(2, 1000):
        candidate = directory / f"{stem}.{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise OSError("无法为失败文件生成唯一文件名")  # pragma: no cover - defensive


def _write_error_sidecar(quarantined: Path, message: str) -> None:
    sidecar = quarantined.with_name(quarantined.name + ".error.txt")
    try:
        sidecar.write_text(
            f"Echoic consume 导入失败\n时间：{to_iso(utcnow())}\n文件：{quarantined.name}\n原因：{message}\n",
            encoding="utf-8",
        )
    except OSError:
        logger.warning("failed to write consume error sidecar for %s", quarantined, exc_info=True)


def _resolve_owner_id(db: Session) -> int | None:
    """Configured owner user, else the first admin, else None."""
    username = (settings.consume_owner_username or "").strip()
    if username:
        user = db.scalar(select(User).where(User.username == username).limit(1))
        if user is not None:
            return int(user.id)
        logger.warning("ECHOIC_CONSUME_OWNER_USERNAME=%r not found, falling back to first admin", username)
    admin = db.scalar(
        select(User)
        .where(or_(User.is_admin.is_(True), User.role == "admin"))
        .order_by(User.id)
        .limit(1)
    )
    return int(admin.id) if admin is not None else None


def _default_language(db: Session) -> str:
    try:
        config = load_ai_config(db)
        return str(config.system.get("default_language") or "zh")
    except Exception:  # noqa: BLE001 - language is cosmetic here
        logger.warning("could not load default language for consume import", exc_info=True)
        return "zh"


def _record_failure(
    db: Session,
    *,
    source: Path,
    consume_root: Path,
    sha256: str,
    stat: os.stat_result,
    message: str,
    existing: ImportedFile | None,
) -> Path | None:
    """Quarantine a failing file + write its .error.txt + upsert imported_files."""
    message = (message or "导入失败").strip()[:MAX_ERROR_CHARS]
    quarantined: Path | None = None
    try:
        quarantined = _unique_destination(consume_root / "failed", source.name)
        _move_file(source, quarantined)
        _write_error_sidecar(quarantined, message)
    except OSError as exc:  # pragma: no cover - disk/permission issues
        logger.warning("failed to quarantine consume file %s: %s", source, exc)
        quarantined = None
    _upsert_imported_file(
        db,
        sha256=sha256,
        filename=source.name,
        size_bytes=int(stat.st_size),
        status=STATUS_FAILED,
        recording_id=None,
        error=message,
        existing=existing,
    )
    logger.warning("consume import failed for %r: %s", source.name, message)
    return quarantined


def _upsert_imported_file(
    db: Session,
    *,
    sha256: str,
    filename: str,
    size_bytes: int,
    status: str,
    recording_id: int | None,
    error: str | None,
    existing: ImportedFile | None,
) -> ImportedFile:
    row = existing or db.scalar(select(ImportedFile).where(ImportedFile.sha256 == sha256).limit(1))
    if row is None:
        row = ImportedFile(
            sha256=sha256,
            filename=filename[:255],
            size_bytes=int(size_bytes),
            status=status,
            recording_id=recording_id,
            error=error,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:  # concurrent import of the same content
            db.rollback()
            row = db.scalar(select(ImportedFile).where(ImportedFile.sha256 == sha256).limit(1))
            if row is None:  # pragma: no cover - unexpected
                raise
    if row is not None:
        row.filename = filename[:255]
        row.size_bytes = int(size_bytes)
        row.status = status
        row.recording_id = recording_id
        row.error = error
        db.commit()
    return row  # type: ignore[return-value]


def _mtime_datetime(stat: os.stat_result) -> datetime:
    try:
        return datetime.fromtimestamp(float(stat.st_mtime), tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):  # pragma: no cover - exotic mtimes
        return utcnow()


# ---------------------------------------------------------------- single file
def _process_candidate(
    db: Session,
    path: Path,
    consume_root: Path,
    result: ScanResult,
    *,
    owner_id: int | None,
    language: str,
    force_stability: bool,
) -> None:
    try:
        stat = path.stat()
    except OSError:
        logger.warning("consume: cannot stat %s, skipping", path, exc_info=True)
        return
    if int(stat.st_size) <= 0:
        return  # zero-byte / placeholder: wait for content
    extension = path.suffix.lower()
    if extension not in storage.ALLOWED_EXTENSIONS:
        logger.debug("consume: unsupported extension, skipping %s", path.name)
        return
    if not force_stability and not _is_stable(path, stat):
        logger.debug("consume: file not stable yet, skipping %s", path.name)
        return
    # Hash errors are per-file failures: quarantine (best effort) without a DB sha.
    try:
        sha256 = _sha256_cached(path, stat)
    except OSError as exc:
        message = f"读取文件失败：{exc}"
        logger.warning("consume: %s", message)
        try:
            quarantined = _unique_destination(consume_root / "failed", path.name)
            _move_file(path, quarantined)
            _write_error_sidecar(quarantined, message)
        except OSError:  # pragma: no cover
            logger.warning("consume: could not quarantine %s", path.name, exc_info=True)
        result.failed += 1
        return

    existing = db.scalar(select(ImportedFile).where(ImportedFile.sha256 == sha256).limit(1))
    if existing is not None and existing.status != STATUS_FAILED:
        result.duplicates += 1
        if sha256 not in _logged_duplicates:
            _logged_duplicates.add(sha256)
            logger.info(
                "consume: duplicate file skipped %r (sha256=%s…, existing status=%s)",
                path.name,
                sha256[:12],
                existing.status,
            )
        return

    # Probe before moving so a broken file never reaches the media directory.
    try:
        info = media.probe(path, settings.ffprobe)
    except media.MediaError as exc:
        _record_failure(
            db,
            source=path,
            consume_root=consume_root,
            sha256=sha256,
            stat=stat,
            message=str(exc),
            existing=existing,
        )
        result.failed += 1
        return

    recording_uuid = str(uuid_lib.uuid4())
    try:
        directory = storage.recording_dir(recording_uuid)
    except OSError as exc:  # pragma: no cover - disk issues
        _record_failure(
            db,
            source=path,
            consume_root=consume_root,
            sha256=sha256,
            stat=stat,
            message=f"无法创建媒体目录：{exc}",
            existing=existing,
        )
        result.failed += 1
        return
    target = directory / f"original{extension}"
    try:
        _move_file(path, target)
    except OSError as exc:
        _record_failure(
            db,
            source=path,
            consume_root=consume_root,
            sha256=sha256,
            stat=stat,
            message=f"无法移动文件到媒体目录：{exc}",
            existing=existing,
        )
        result.failed += 1
        return

    title = Path(path.name).stem.strip() or "未命名录音"
    recording = Recording(
        uuid=recording_uuid,
        title=title[:255],
        description=None,
        status=RecordingStatus.UPLOADED,
        stage=Stage.PREPARING,
        progress=0,
        media_type=info.media_type or storage.guess_media_type(extension),
        mime_type=info.mime_type or storage.guess_mime(extension),
        original_filename=path.name[:255],
        original_ext=extension,
        storage_dir=storage.storage_relpath(recording_uuid),
        size_bytes=int(stat.st_size),
        duration_ms=int(info.duration_ms or 0),
        recorded_at=_mtime_datetime(stat),
        language=language or "zh",
        source="consume",
        owner_id=owner_id,
    )
    db.add(recording)
    db.flush()
    _upsert_imported_file(
        db,
        sha256=sha256,
        filename=path.name,
        size_bytes=int(stat.st_size),
        status=STATUS_IMPORTED,
        recording_id=int(recording.id),
        error=None,
        existing=existing,
    )
    try:
        enqueue(db, recording.id)
    except Exception:  # noqa: BLE001 - the recording is imported; admin can reprocess
        logger.exception("consume: imported recording %s but enqueue failed", recording.id)
    result.imported += 1
    logger.info(
        "consume: imported %r -> recording %s (%.1fs, %s)",
        path.name,
        recording.id,
        int(info.duration_ms or 0) / 1000,
        info.media_type,
    )


# ------------------------------------------------------------------- scanning
def _scan_locked(db: Session, *, force_stability: bool) -> ScanResult:
    result = ScanResult()
    root = _consume_dir()
    if not root.is_dir():
        logger.debug("consume directory missing: %s", root)
        return result
    candidates = _iter_candidates(root)
    _prune_seen(candidates)
    result.scanned = len(candidates)
    if not candidates:
        return result
    owner_id = _resolve_owner_id(db)
    language = _default_language(db)
    for path in candidates:
        try:
            _process_candidate(
                db,
                path,
                root,
                result,
                owner_id=owner_id,
                language=language,
                force_stability=force_stability,
            )
        except Exception:  # noqa: BLE001 - one bad file must never stop the scan
            result.failed += 1
            logger.exception("consume: unexpected error processing %s", path)
            try:
                db.rollback()
            except Exception:  # pragma: no cover
                pass
    return result


def scan_once(db: Session, *, force_stability: bool = False) -> ScanResult:
    """Scan the consume directory once using the given session."""
    with _scan_lock:
        return _scan_locked(db, force_stability=force_stability)


def _record_state(result: ScanResult) -> None:
    _state["last_scan"] = {"at": to_iso(utcnow()), **result.to_dict()}
    logger.info(
        "consume scan: scanned=%s imported=%s duplicates=%s failed=%s",
        result.scanned,
        result.imported,
        result.duplicates,
        result.failed,
    )


def scan_now(*, force_stability: bool = True) -> dict[str, Any]:
    """Run an immediate scan (admin “立即扫描” / startup smoke helper).

    ``force_stability=True`` (default) imports candidates without waiting for the
    two-poll stability window because the admin explicitly asked for a scan;
    zero-byte/hidden/partial files are still skipped. Returns a dict with the
    counters, an ISO ``at`` timestamp and the nested ``result`` used by
    ``POST /api/system/consume/scan``.
    """
    with _scan_lock:
        with SessionLocal() as db:
            result = _scan_locked(db, force_stability=force_stability)
        _record_state(result)
    payload: dict[str, Any] = {"at": _state["last_scan"]["at"], **result.to_dict()}
    payload["result"] = result.to_dict()
    return payload


def maybe_scan(*, force: bool = False) -> ScanResult | None:
    """Worker-loop hook: scan at most once per ``consume_interval_sec``."""
    global _last_scan_monotonic
    if not settings.consume_enabled:
        return None
    interval = _interval_sec()
    now = time.monotonic()
    if not force and _last_scan_monotonic is not None and (now - _last_scan_monotonic) < interval:
        return None
    if not _scan_lock.acquire(blocking=False):
        return None  # another scan (manual) is already running
    try:
        _last_scan_monotonic = now
        with SessionLocal() as db:
            result = _scan_locked(db, force_stability=False)
        _record_state(result)
        return result
    except Exception:  # noqa: BLE001 - the worker must survive every scan error
        logger.exception("consume: periodic scan failed")
        _state["last_scan"] = {"at": to_iso(utcnow()), "scanned": 0, "imported": 0, "duplicates": 0, "failed": 0}
        return None
    finally:
        _scan_lock.release()


def scan_loop(stop_event: threading.Event | None = None, interval_sec: int | None = None) -> None:
    """Blocking consume loop for a dedicated thread/process (optional)."""
    interval = max(MIN_INTERVAL_SEC, int(interval_sec or _interval_sec()))
    logger.info("consume scan loop started (interval=%ss, dir=%s)", interval, _consume_dir())
    while stop_event is None or not stop_event.is_set():
        try:
            maybe_scan(force=True)
        except Exception:  # noqa: BLE001
            logger.exception("consume scan loop iteration failed")
        if stop_event is not None:
            stop_event.wait(interval)
        else:  # pragma: no cover - only used by an ad-hoc caller
            time.sleep(interval)


# --------------------------------------------------------------- status hooks
def last_scan_info() -> dict[str, Any] | None:
    """Last scan summary for ``worker_status()["last_consume_scan"]``."""
    value = _state.get("last_scan")
    return dict(value) if isinstance(value, dict) else None


def consume_status() -> dict[str, Any]:
    """Read-only consume block for ``GET /api/system/info``."""
    root = _consume_dir()
    pending = 0
    failed = 0
    try:
        for path in _iter_candidates(root):
            if path.suffix.lower() in storage.ALLOWED_EXTENSIONS:
                pending += 1
        failed_dir = root / "failed"
        if failed_dir.is_dir():
            failed = sum(
                1
                for entry in failed_dir.iterdir()
                if entry.is_file() and not entry.name.lower().endswith(".error.txt")
            )
    except OSError:  # pragma: no cover - defensive
        logger.warning("failed to count consume directory files", exc_info=True)
    return {
        "enabled": bool(settings.consume_enabled),
        "dir": str(root),
        "interval_sec": _interval_sec(),
        "pending_files": int(pending),
        "failed_files": int(failed),
        "last_scan": last_scan_info(),
    }


def reset_scan_state() -> None:
    """Reset in-memory scan state (test isolation / worker restart)."""
    global _last_scan_monotonic
    with _scan_lock:
        _seen.clear()
        _hash_cache.clear()
        _logged_duplicates.clear()
        _state["last_scan"] = None
        _last_scan_monotonic = None


__all__ = [
    "MIN_INTERVAL_SEC",
    "ScanResult",
    "consume_status",
    "last_scan_info",
    "maybe_scan",
    "reset_scan_state",
    "scan_loop",
    "scan_now",
    "scan_once",
]
