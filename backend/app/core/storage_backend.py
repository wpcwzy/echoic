"""Object storage abstraction for Echoic (v0.3.0 R-21).

The rest of the application keeps working with *logical keys* such as
``media/{uuid}/original.mp3`` / ``media/{uuid}/playback.m4a`` (exactly the
layout ``core.storage`` uses on the local filesystem), while this module hides
whether the bytes actually live under ``ECHOIC_DATA_DIR`` or in an
S3/MinIO bucket.

Public surface
--------------
``StorageBackend``
    Structural protocol implemented by :class:`LocalStorage` and
    :class:`S3Storage`.
``get_storage()``
    Process wide singleton honouring ``settings.storage_backend`` (``local`` /
    ``s3``).  It is safe to call from import time / request handlers: neither
    backend touches the network until a media operation is executed and S3
    configuration problems never crash application startup.
``storage_status()``
    Cheap ``{"backend", "status": "ok"|"degraded", "detail"}`` snapshot used by
    ``GET /api/system/health`` (Chinese detail, never raises).
``serve_key()``
    Starlette response helper for the media proxy: full/partial responses
    (200/206), HTTP Range validation (416), RFC 5987 ``Content-Disposition``
    and opt-in 307 redirects to S3 presigned URLs.
``storage_migrate`` CLI (``app/tools/storage_migrate.py``) uses the same
protocol to copy objects local <-> S3.

Configuration (``backend/app/config.py``)
-----------------------------------------
``ECHOIC_STORAGE_BACKEND``      ``local`` (default) or ``s3``
``ECHOIC_S3_ENDPOINT``          empty -> AWS S3, otherwise MinIO et al.
``ECHOIC_S3_BUCKET``            bucket name (default ``echoic``)
``ECHOIC_S3_ACCESS_KEY``        empty -> default boto3 credential chain (IAM)
``ECHOIC_S3_SECRET_KEY``
``ECHOIC_S3_REGION``            signature region (default ``us-east-1``)
``ECHOIC_S3_PATH_STYLE``        ``true`` -> path style addressing (MinIO)
``ECHOIC_S3_PREFIX``            optional key prefix inside the bucket
``ECHOIC_S3_PRESIGN_REDIRECT``  opt-in 307 media redirects (default proxy)

Naming note: credentials are only ever read from the environment/settings,
never written to the database nor logged.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from ..config import settings
from .storage import StorageError as _CoreStorageError

logger = logging.getLogger(__name__)

#: streaming chunk size used by both backends (256 KiB, per docs/09 §4)
CHUNK_SIZE = 256 * 1024
#: default lifetime of S3 presigned URLs (docs/09 §4 recommends 300s)
DEFAULT_PRESIGN_EXPIRES = 300
#: AWS S3 hard limit for presigned URLs (7 days)
MAX_PRESIGN_EXPIRES = 7 * 24 * 3600
#: cache TTL for ``storage_status()`` so docker healthchecks do not hammer S3
STATUS_TTL_SECONDS = 10.0


class StorageError(_CoreStorageError):
    """Storage failure whose message is already user friendly (Chinese).

    Subclasses :class:`app.core.storage.StorageError` so existing call sites
    (e.g. ``services.pipeline._friendly_error``) keep catching it while the
    new abstraction is adopted.
    """


# --------------------------------------------------------------------------- keys
def _clean_parts(raw: str, *, original: str) -> list[str]:
    text = str(raw or "").replace("\\", "/").strip()
    if text.startswith("/"):
        raise StorageError(f"非法存储路径（不允许绝对路径）：{original}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise StorageError(f"非法存储路径（不允许路径穿越）：{original}")
        parts.append(part)
    return parts


def normalize_key(key: str) -> str:
    """Return a canonical, safe logical key (``media/<uuid>/playback.m4a``)."""
    original = str(key or "")
    parts = _clean_parts(original, original=original)
    if not parts:
        raise StorageError("存储 key 不能为空")
    return "/".join(parts)


def normalize_prefix(prefix: str) -> str:
    """Return a canonical directory prefix, always ending with ``/`` (or ``""``)."""
    original = str(prefix or "")
    parts = _clean_parts(original, original=original)
    if not parts:
        return ""
    return "/".join(parts) + "/"


def _normalize_scan_prefix(prefix: str) -> str:
    """Like :func:`normalize_prefix` but preserves the caller's trailing slash."""
    original = str(prefix or "")
    parts = _clean_parts(original, original=original)
    if not parts:
        return ""
    normalized = "/".join(parts)
    if original.replace("\\", "/").strip().endswith("/"):
        normalized += "/"
    return normalized


def recording_prefix(storage_dir: str, uuid: str = "") -> str:
    """Logical prefix for a recording (``media/<uuid>``)."""
    raw = str(storage_dir or "").strip().strip("/")
    if not raw and uuid:
        raw = f"media/{uuid}"
    if not raw:
        raise StorageError("录音存储路径为空")
    return normalize_key(raw)


def original_key(storage_dir: str, original_filename: str = "", original_ext: str = "") -> str:
    """Key of the uploaded original file: ``media/<uuid>/original<ext>``."""
    ext = Path(original_filename or "").suffix.lower() or str(original_ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if not ext:
        ext = ".bin"
    return f"{recording_prefix(storage_dir)}/original{ext}"


def playback_key(storage_dir: str) -> str:
    """Key of the browser friendly playback file: ``media/<uuid>/playback.m4a``."""
    return f"{recording_prefix(storage_dir)}/playback.m4a"


def asr_wav_key(storage_dir: str) -> str:
    """Key of the 16 kHz mono WAV used by ASR: ``media/<uuid>/asr.wav``."""
    return f"{recording_prefix(storage_dir)}/asr.wav"


# ---------------------------------------------------------------------- protocol
@runtime_checkable
class StorageBackend(Protocol):
    """Structural interface implemented by local and S3 storage backends."""

    name: str

    def save_file(
        self, local_path: Path, key: str, *, content_type: str | None = None
    ) -> int:
        """Copy ``local_path`` to ``key``; returns the stored byte size.

        ``content_type`` is optional (S3 stores it as object metadata; local
        ignores it) so callers can use both backends uniformly.
        """

    def download_file(self, key: str, dest: Path) -> None:
        """Materialise ``key`` at ``dest`` (parents are created)."""

    def open_range(self, key: str, start: int, length: int) -> Iterator[bytes]:
        """Yield exactly the requested byte range (``start``, ``length``)."""

    def open_stream(self, key: str) -> Iterator[bytes]:
        """Yield the whole object as chunks of at most ``CHUNK_SIZE`` bytes."""

    def exists(self, key: str) -> bool:
        """Whether the object/key exists."""

    def size(self, key: str) -> int:
        """Object size in bytes; raises :class:`StorageError` when missing."""

    def delete_prefix(self, prefix: str) -> int:
        """Delete everything under a directory prefix; idempotent."""

    def list_prefix(self, prefix: str) -> list[str]:
        """List logical keys under ``prefix`` (sorted)."""

    def presigned_url(self, key: str, expires_sec: int = 3600) -> str | None:
        """Time limited download URL; ``None`` for backends without one."""

    def local_path_if_any(self, key: str) -> Path | None:
        """Path when the object is already available on the local filesystem."""


# ------------------------------------------------------------------------- local
class LocalStorage:
    """Storage under ``<data_dir>/<key>``; byte-for-byte v0.2 compatible."""

    name = "local"

    def __init__(self, root: Path | str | None = None) -> None:
        base = Path(root) if root is not None else settings.data_path
        self.root = Path(base).expanduser().resolve()

    # -- helpers ---------------------------------------------------------
    def _resolve(self, rel: str) -> Path:
        root = self.root
        candidate = (root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise StorageError(f"非法存储路径（越界）：{rel}")
        return candidate

    def _path_required(self, key: str) -> Path:
        rel = normalize_key(key)
        path = self._resolve(rel)
        if not path.is_file():
            raise StorageError(f"媒体文件不存在：{rel}")
        return path

    @staticmethod
    def _atomic_copy(src: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.part-{os.getpid()}-{threading.get_ident()}")
        try:
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
            raise

    # -- contract --------------------------------------------------------
    def save_file(self, local_path: Path, key: str, *, content_type: str | None = None) -> int:
        # ``content_type`` mirrors the S3 signature so callers can pass it
        # uniformly; the local filesystem does not persist content metadata.
        src = Path(local_path)
        if not src.is_file():
            raise StorageError(f"待保存的文件不存在：{src}")
        rel = normalize_key(key)
        dest = self._resolve(rel)
        if dest == src.resolve():
            return dest.stat().st_size
        try:
            self._atomic_copy(src, dest)
        except OSError as exc:
            raise StorageError(f"写入本地媒体文件失败：{exc}") from exc
        return dest.stat().st_size

    def download_file(self, key: str, dest: Path) -> None:
        src = self._path_required(key)
        target = Path(dest)
        try:
            self._atomic_copy(src, target)
        except OSError as exc:
            raise StorageError(f"复制本地媒体文件失败：{exc}") from exc

    def open_range(self, key: str, start: int, length: int) -> Iterator[bytes]:
        start = int(start)
        length = int(length)
        if start < 0 or length <= 0:
            raise StorageError("无效的媒体读取范围")
        path = self._path_required(key)
        total = path.stat().st_size
        if start >= total:
            raise StorageError("媒体读取范围超出文件大小")

        def _iter() -> Iterator[bytes]:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = min(length, total - start)
                while remaining > 0:
                    chunk = handle.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return _iter()

    def open_stream(self, key: str) -> Iterator[bytes]:
        path = self._path_required(key)

        def _iter() -> Iterator[bytes]:
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        return _iter()

    def exists(self, key: str) -> bool:
        try:
            return self._path_required(key).is_file()
        except StorageError:
            return False

    def size(self, key: str) -> int:
        try:
            return self._path_required(key).stat().st_size
        except OSError as exc:
            raise StorageError(f"读取本地媒体文件失败：{exc}") from exc

    def delete_prefix(self, prefix: str) -> int:
        logical = normalize_prefix(prefix)
        if not logical:
            raise StorageError("拒绝删除空的存储前缀")
        target = self._resolve(logical.rstrip("/"))
        if target == self.root:
            raise StorageError("拒绝删除数据根目录")
        if target.is_dir():
            count = sum(1 for item in target.rglob("*") if item.is_file())
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise StorageError(f"删除本地媒体目录失败：{exc}") from exc
            return count
        if target.is_file():
            try:
                target.unlink()
            except OSError as exc:
                raise StorageError(f"删除本地媒体文件失败：{exc}") from exc
            return 1
        return 0

    def list_prefix(self, prefix: str) -> list[str]:
        scan = _normalize_scan_prefix(prefix)
        if not scan:
            if not self.root.is_dir():
                return []
            keys = [
                path.relative_to(self.root).as_posix()
                for path in self.root.rglob("*")
                if path.is_file()
            ]
            return sorted(keys)
        target = self._resolve(scan.rstrip("/"))
        if target.is_file() and not scan.endswith("/"):
            return [scan]
        directory = target if target.is_dir() else target.parent
        if not directory.is_dir():
            return []
        keys = []
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            try:
                key = path.relative_to(self.root).as_posix()
            except ValueError:  # pragma: no cover - defensive
                continue
            if key.startswith(scan):
                keys.append(key)
        return sorted(keys)

    def presigned_url(self, key: str, expires_sec: int = 3600) -> str | None:
        return None

    def local_path_if_any(self, key: str) -> Path | None:
        try:
            path = self._path_required(key)
        except StorageError:
            return None
        return path

    # -- diagnostics -----------------------------------------------------
    def status(self) -> dict[str, Any]:
        root = self.root
        if not root.is_dir():
            return {
                "backend": self.name,
                "status": "degraded",
                "detail": f"本地存储目录不存在或不可访问：{root}",
                "root": str(root),
            }
        try:
            writable = os.access(root, os.W_OK)
            free_bytes = int(shutil.disk_usage(root).free)
        except OSError as exc:  # pragma: no cover - rare platform errors
            return {
                "backend": self.name,
                "status": "degraded",
                "detail": f"本地存储目录不可用：{exc}",
                "root": str(root),
            }
        if not writable:
            return {
                "backend": self.name,
                "status": "degraded",
                "detail": f"本地存储目录无写入权限：{root}",
                "root": str(root),
            }
        return {
            "backend": self.name,
            "status": "ok",
            "detail": f"本地存储可用（{root}）",
            "root": str(root),
            "free_bytes": free_bytes,
        }


# ---------------------------------------------------------------------------- s3
class S3Storage:
    """S3 compatible object storage (AWS S3, MinIO, ...), boto3 imported lazily."""

    name = "s3"

    def __init__(
        self,
        *,
        bucket: str | None = None,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        path_style: bool | None = None,
        prefix: str | None = None,
        presign_expires: int | None = None,
        presign_redirect: bool | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 120.0,
        verify_tls: bool = True,
        client: Any | None = None,
    ) -> None:
        current = settings
        self.bucket = str(bucket if bucket is not None else current.s3_bucket or "").strip()
        endpoint_value = str(endpoint if endpoint is not None else current.s3_endpoint or "").strip()
        if endpoint_value and "://" not in endpoint_value:
            endpoint_value = f"http://{endpoint_value}"
        self.endpoint = endpoint_value
        self.access_key = str(access_key if access_key is not None else current.s3_access_key or "")
        self.secret_key = str(secret_key if secret_key is not None else current.s3_secret_key or "")
        self.region = str(region if region is not None else current.s3_region or "us-east-1") or "us-east-1"
        self.path_style = bool(path_style if path_style is not None else current.s3_path_style)
        raw_prefix = prefix if prefix is not None else current.s3_prefix
        self.prefix = normalize_prefix(raw_prefix or "")
        self.presign_expires = int(
            presign_expires if presign_expires is not None else DEFAULT_PRESIGN_EXPIRES
        )
        self.presign_redirect = bool(
            presign_redirect if presign_redirect is not None else current.s3_presign_redirect
        )
        self.connect_timeout = float(connect_timeout)
        self.read_timeout = float(read_timeout)
        self.verify_tls = bool(verify_tls)
        self._client_obj = client
        self._lock = threading.Lock()

    # -- client ----------------------------------------------------------
    def _build_client(self) -> Any:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - boto3 is a hard requirement
            raise StorageError(
                "未安装 boto3，无法使用 S3/MinIO 对象存储；请安装后重启（pip install boto3）"
            ) from exc
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if self.path_style else "auto"},
            connect_timeout=self.connect_timeout,
            read_timeout=self.read_timeout,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        kwargs: dict[str, Any] = {
            "region_name": self.region,
            "config": config,
            "verify": self.verify_tls,
        }
        if self.endpoint:
            kwargs["endpoint_url"] = self.endpoint
        if self.access_key:
            kwargs["aws_access_key_id"] = self.access_key
        if self.secret_key:
            kwargs["aws_secret_access_key"] = self.secret_key
        return boto3.client("s3", **kwargs)

    @property
    def client(self) -> Any:
        if self._client_obj is not None:
            return self._client_obj
        with self._lock:
            if self._client_obj is None:
                self._client_obj = self._build_client()
            return self._client_obj

    # -- helpers ---------------------------------------------------------
    def _require_bucket(self) -> None:
        if not self.bucket:
            raise StorageError(
                "对象存储未配置 bucket：请设置 ECHOIC_S3_BUCKET 与 ECHOIC_S3_ENDPOINT（S3/MinIO）"
            )

    def _object_key(self, key: str) -> str:
        return self.prefix + normalize_key(key)

    def _logical_key(self, object_key: str) -> str:
        if self.prefix and object_key.startswith(self.prefix):
            return object_key[len(self.prefix):]
        return object_key

    @staticmethod
    def _guess_content_type(suffix: str) -> str | None:
        guessed, _encoding = mimetypes.guess_type(f"file{(suffix or '').lower()}")
        return guessed

    # -- contract --------------------------------------------------------
    def save_file(self, local_path: Path, key: str, *, content_type: str | None = None) -> int:
        src = Path(local_path)
        if not src.is_file():
            raise StorageError(f"待上传的文件不存在：{src}")
        self._require_bucket()
        object_key = self._object_key(key)
        size = src.stat().st_size
        extra: dict[str, Any] = {}
        content = content_type or self._guess_content_type(src.suffix)
        if content:
            extra["ContentType"] = content
        try:
            if extra:
                self.client.upload_file(str(src), self.bucket, object_key, ExtraArgs=extra)
            else:  # pragma: no cover - mimetypes guesses almost everything
                self.client.upload_file(str(src), self.bucket, object_key)
        except Exception as exc:
            raise _friendly_s3_error(exc, action="上传媒体文件") from exc
        return size

    def download_file(self, key: str, dest: Path) -> None:
        self._require_bucket()
        object_key = self._object_key(key)
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.part-{os.getpid()}-{threading.get_ident()}")
        try:
            self.client.download_file(self.bucket, object_key, str(tmp))
            os.replace(tmp, target)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - defensive
                pass
            raise _friendly_s3_error(exc, action="下载媒体文件") from exc

    def open_range(self, key: str, start: int, length: int) -> Iterator[bytes]:
        start = int(start)
        length = int(length)
        if start < 0 or length <= 0:
            raise StorageError("无效的媒体读取范围")
        self._require_bucket()
        object_key = self._object_key(key)
        end = start + length - 1
        try:
            response = self.client.get_object(
                Bucket=self.bucket, Key=object_key, Range=f"bytes={start}-{end}"
            )
        except Exception as exc:
            raise _friendly_s3_error(exc, action="读取媒体文件") from exc
        return _iter_s3_body(response)

    def open_stream(self, key: str) -> Iterator[bytes]:
        self._require_bucket()
        object_key = self._object_key(key)
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise _friendly_s3_error(exc, action="读取媒体文件") from exc
        return _iter_s3_body(response)

    def exists(self, key: str) -> bool:
        self._require_bucket()
        object_key = self._object_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=object_key)
            return True
        except Exception as exc:
            code = _s3_error_code(exc)
            if code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise _friendly_s3_error(exc, action="检查媒体文件") from exc

    def size(self, key: str) -> int:
        self._require_bucket()
        object_key = self._object_key(key)
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=object_key)
        except Exception as exc:
            raise _friendly_s3_error(exc, action="读取媒体文件大小") from exc
        return int(response.get("ContentLength") or 0)

    def delete_prefix(self, prefix: str) -> int:
        logical_prefix = normalize_prefix(prefix)
        if not logical_prefix:
            raise StorageError("拒绝删除空的对象存储前缀")
        self._require_bucket()
        object_prefix = self.prefix + logical_prefix
        keys: list[str] = []
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=object_prefix):
                for item in page.get("Contents") or []:
                    keys.append(item["Key"])
        except Exception as exc:
            raise _friendly_s3_error(exc, action="列举待删除对象") from exc
        deleted = 0
        for index in range(0, len(keys), 1000):
            batch = [{"Key": key} for key in keys[index: index + 1000]]
            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
                )
            except Exception as exc:
                raise _friendly_s3_error(exc, action="删除对象存储文件") from exc
            errors = response.get("Errors") or []
            if errors:
                first = errors[0]
                raise StorageError(
                    f"删除对象存储文件失败（{len(errors)} 个）："
                    f"{first.get('Code')} {first.get('Message')}"
                )
            deleted += len(batch)
        return deleted

    def list_prefix(self, prefix: str) -> list[str]:
        self._require_bucket()
        object_prefix = self.prefix + _normalize_scan_prefix(prefix)
        keys: list[str] = []
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=object_prefix):
                for item in page.get("Contents") or []:
                    keys.append(self._logical_key(item["Key"]))
        except Exception as exc:
            raise _friendly_s3_error(exc, action="列举存储对象") from exc
        return sorted(keys)

    def presigned_url(
        self,
        key: str,
        expires_sec: int = 3600,
        *,
        filename: str | None = None,
        download: bool = False,
    ) -> str | None:
        try:
            self._require_bucket()
            params: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": self._object_key(key),
            }
            if filename:
                params["ResponseContentDisposition"] = content_disposition(
                    filename, inline=not download
                )
            expires = max(1, min(int(expires_sec or DEFAULT_PRESIGN_EXPIRES), MAX_PRESIGN_EXPIRES))
            return self.client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires
            )
        except StorageError as exc:
            logger.warning("S3 presign unavailable: %s", exc)
            return None
        except Exception as exc:  # pragma: no cover - signing failures degrade to proxy
            logger.warning("S3 presign failed: %s", exc)
            return None

    def local_path_if_any(self, key: str) -> Path | None:
        return None

    # -- diagnostics / setup --------------------------------------------
    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "backend": self.name,
            "bucket": self.bucket,
            "endpoint": self.endpoint or "AWS S3",
            "prefix": self.prefix,
        }
        if not self.bucket:
            return {
                **info,
                "status": "degraded",
                "detail": "对象存储未配置 bucket（ECHOIC_S3_BUCKET），当前媒体读写将不可用",
            }
        try:
            client = self.client
        except StorageError as exc:
            return {**info, "status": "degraded", "detail": str(exc)}
        except Exception as exc:  # pragma: no cover - defensive
            return {**info, "status": "degraded", "detail": f"对象存储客户端初始化失败：{exc}"}
        try:
            client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            return {
                **info,
                "status": "degraded",
                "detail": f"对象存储不可用：{_describe_s3_error(exc)}",
            }
        return {
            **info,
            "status": "ok",
            "detail": f"对象存储可用（bucket={self.bucket}，endpoint={self.endpoint or 'AWS S3'}）",
        }

    def ensure_bucket(self) -> bool:
        """Create the configured bucket when missing; returns ``True`` if created."""
        self._require_bucket()
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return False
        except Exception as exc:
            code = _s3_error_code(exc)
            if code not in ("404", "NoSuchBucket", "NotFound"):
                raise _friendly_s3_error(exc, action="检查对象存储 bucket") from exc
        kwargs: dict[str, Any] = {"Bucket": self.bucket}
        if self.region and self.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
        try:
            self.client.create_bucket(**kwargs)
        except Exception as exc:
            raise _friendly_s3_error(exc, action="创建对象存储 bucket") from exc
        logger.info("created S3 bucket %s at %s", self.bucket, self.endpoint or "AWS S3")
        return True


def _iter_s3_body(response: Any) -> Iterator[bytes]:
    body = response.get("Body") if isinstance(response, dict) else getattr(response, "Body", None)
    if body is None:  # pragma: no cover - boto3 always returns a body
        return
    try:
        while True:
            chunk = body.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            body.close()
        except Exception:  # pragma: no cover - defensive
            pass


# --------------------------------------------------------------------- S3 errors
_S3_CODE_HINTS = {
    # missing objects
    "404": "对象不存在",
    "NoSuchKey": "对象不存在",
    "NotFound": "对象不存在",
    # bucket
    "NoSuchBucket": "bucket 不存在，请先创建或检查 ECHOIC_S3_BUCKET",
    # auth
    "AccessDenied": "访问被拒绝，请检查 ECHOIC_S3_ACCESS_KEY/SECRET_KEY 的权限",
    "403": "访问被拒绝，请检查对象存储密钥权限",
    "InvalidAccessKeyId": "Access Key 无效，请检查 ECHOIC_S3_ACCESS_KEY",
    "SignatureDoesNotMatch": "密钥签名不匹配，请检查 ECHOIC_S3_SECRET_KEY",
    "ExpiredToken": "访问凭据已过期，请刷新对象存储凭据",
    "NoCredentialsError": "未配置对象存储凭据（ECHOIC_S3_ACCESS_KEY/SECRET_KEY 或 IAM 角色）",
    # connectivity
    "EndpointConnectionError": "无法连接对象存储服务，请检查 ECHOIC_S3_ENDPOINT 与网络",
    "ConnectTimeoutError": "连接对象存储超时，请检查 ECHOIC_S3_ENDPOINT 与网络",
    "ProxyConnectionError": "代理连接对象存储失败，请检查代理配置",
    "SSLError": "对象存储 TLS 握手失败，请检查证书/ECHOIC_S3_ENDPOINT 协议",
    "InvalidURL": "对象存储地址无效，请检查 ECHOIC_S3_ENDPOINT",
    "RequestTimeTooSkewed": "服务器与对象存储时钟偏差过大，请校准系统时间",
    # request
    "InvalidRange": "请求的媒体范围无效",
    "EntityTooLarge": "对象超过对象存储允许的大小限制",
    "SlowDown": "对象存储限流，请稍后重试",
    "InvalidObjectState": "对象状态不允许当前操作",
}


def _s3_error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error") or {}
        code = str(error.get("Code") or "")
        if code:
            return code
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if status:
            return str(status)
    return type(exc).__name__


def _s3_error_message(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        message = str((response.get("Error") or {}).get("Message") or "")
        if message:
            return message[:300]
    return str(exc)[:300]


def _describe_s3_error(exc: BaseException) -> str:
    code = _s3_error_code(exc)
    hint = _S3_CODE_HINTS.get(code) or _S3_CODE_HINTS.get(type(exc).__name__)
    if hint:
        return f"{hint}（{code}）"
    return f"{type(exc).__name__}: {_s3_error_message(exc)}"


def _friendly_s3_error(exc: BaseException, *, action: str) -> StorageError:
    """Wrap a boto3/botocore exception in a Chinese, user friendly error."""
    if isinstance(exc, StorageError):
        return exc
    return StorageError(f"{action}失败：{_describe_s3_error(exc)}")


# --------------------------------------------------------------------- factory
_STORAGE_LOCK = threading.Lock()
_STATUS_LOCK = threading.Lock()
_storage: StorageBackend | None = None
_storage_signature: tuple[Any, ...] | None = None
_status_cache: dict[str, Any] | None = None

_BACKEND_ALIASES = {
    "local": "local",
    "fs": "local",
    "file": "local",
    "filesystem": "local",
    "s3": "s3",
    "minio": "s3",
    "object": "s3",
    "object_storage": "s3",
}


def backend_name(raw: str | None = None) -> str:
    """Normalise ``ECHOIC_STORAGE_BACKEND`` to ``local`` or ``s3``."""
    value = str(raw if raw is not None else settings.storage_backend or "").strip().lower()
    if value in _BACKEND_ALIASES:
        return _BACKEND_ALIASES[value]
    logger.warning("未知的存储后端配置 %r，回退为 local", value or "<empty>")
    return "local"


def build_storage(name: str | None = None) -> StorageBackend:
    """Build a *new* backend instance for ``name`` (does not touch the cache)."""
    resolved = backend_name(name)
    if resolved == "s3":
        return S3Storage()
    return LocalStorage()


def _config_signature() -> tuple[Any, ...]:
    current = settings
    return (
        backend_name(),
        str(current.data_dir),
        str(current.s3_endpoint or ""),
        str(current.s3_bucket or ""),
        str(current.s3_access_key or ""),
        str(current.s3_secret_key or ""),
        str(current.s3_region or ""),
        bool(current.s3_path_style),
        str(current.s3_prefix or ""),
    )


def get_storage(backend: str | None = None, *, force_reload: bool = False) -> StorageBackend:
    """Return the process wide storage backend selected by configuration.

    Passing ``backend`` explicitly builds a fresh instance of that backend
    (used by the migration CLI); the default factory mirrors
    ``settings.storage_backend`` and caches the instance until configuration
    changes.
    """
    if backend:
        return build_storage(backend)
    global _storage, _storage_signature
    signature = _config_signature()
    with _STORAGE_LOCK:
        if force_reload or _storage is None or _storage_signature != signature:
            _storage = build_storage()
            _storage_signature = signature
            logger.info(
                "storage backend initialised: %s (data_dir=%s)",
                _storage.name,
                settings.data_path,
            )
        return _storage


def reset_storage_cache() -> None:
    """Drop the cached backend/status (tests, or after config changes)."""
    global _storage, _storage_signature, _status_cache
    with _STORAGE_LOCK:
        _storage = None
        _storage_signature = None
    with _STATUS_LOCK:
        _status_cache = None


def storage_status(*, force: bool = False) -> dict[str, Any]:
    """Health snapshot for ``GET /api/system/health``; never raises."""
    global _status_cache
    signature = _config_signature()
    now = time.monotonic()
    with _STATUS_LOCK:
        cached = _status_cache
        if (
            not force
            and cached is not None
            and cached.get("signature") == signature
            and now - float(cached.get("at") or 0) < STATUS_TTL_SECONDS
        ):
            return dict(cached.get("value") or {})
    try:
        backend = get_storage(force_reload=force)
    except Exception as exc:  # never let health checks crash the app
        value: dict[str, Any] = {
            "backend": backend_name(),
            "status": "degraded",
            "detail": f"存储后端初始化失败：{exc}",
        }
    else:
        status_fn = getattr(backend, "status", None)
        if callable(status_fn):
            try:
                value = dict(status_fn())
            except Exception as exc:  # pragma: no cover - defensive
                value = {
                    "backend": backend.name,
                    "status": "degraded",
                    "detail": f"存储状态检查失败：{exc}",
                }
        else:  # pragma: no cover - all bundled backends implement status()
            value = {"backend": backend.name, "status": "ok", "detail": "存储后端可用"}
        value.setdefault("backend", backend.name)
    value.setdefault("status", "ok")
    value.setdefault("detail", "")
    with _STATUS_LOCK:
        _status_cache = {"signature": signature, "at": now, "value": value}
    return dict(value)


# ------------------------------------------------------------------ media proxy
def content_disposition(filename: str, *, inline: bool = False) -> str:
    """RFC 5987 compliant Content-Disposition with an ASCII fallback."""
    safe_name = filename or "download"
    fallback = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name).strip()
    if not re.search(r"[A-Za-z0-9]", fallback):
        extension = safe_name.rsplit(".", 1)[-1] if "." in safe_name else ""
        extension = re.sub(r"[^A-Za-z0-9]", "", extension)
        fallback = f"download.{extension}" if extension else "download"
    quoted = quote(safe_name, safe="")
    kind = "inline" if inline else "attachment"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quoted}"


def parse_range_header(header: str, size: int) -> tuple[int, int] | None:
    """Parse a single ``bytes=`` Range header; ``None`` means 416."""
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        return None
    first, last = (part.strip() for part in spec.split("-", 1))
    try:
        if first == "":
            if last == "":
                return None
            suffix = int(last)
            if suffix <= 0:
                return None
            suffix = min(suffix, size)
            start, end = size - suffix, size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None
    if size <= 0 or start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def file_size(backend: StorageBackend, key: str) -> int:
    """Object size in bytes, or ``0`` when it cannot be read (for tolerant callers)."""
    try:
        return int(backend.size(key))
    except (StorageError, OSError):
        logger.debug("file_size unavailable for key %s", key)
        return 0


def recording_original_key(
    backend: StorageBackend, storage_dir: str, original_ext: str = ""
) -> str | None:
    """Find the stored original key for a recording (``original<ext>``).

    The deterministic key is probed with a cheap ``exists`` call first; only an
    extension mismatch falls back to listing the recording prefix (legacy data).
    """
    try:
        prefix = f"{recording_prefix(storage_dir)}/"
    except StorageError:
        return None
    ext = str(original_ext or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if ext:
        exact = f"{prefix}original{ext}"
        if backend.exists(exact):
            return exact
    for key in backend.list_prefix(prefix):
        name = key.rsplit("/", 1)[-1]
        if name.startswith("original.") and len(name) > len("original."):
            return key
    return None


def recording_playback_key(backend: StorageBackend, storage_dir: str) -> str | None:
    """Find the stored playback key (``playback.m4a``) for a recording."""
    try:
        prefix = f"{recording_prefix(storage_dir)}/"
    except StorageError:
        return None
    key = f"{prefix}playback.m4a"
    return key if backend.exists(key) else None


def fetch_to_local(backend: StorageBackend, key: str, dest: Path) -> Path:
    """Make ``key`` available as a local file: returns the existing local path or copies.

    Used by the pipeline to obtain a staging copy for ffmpeg/ASR.  For a local
    backend no copy happens (zero extra IO); for S3 the object is downloaded to
    ``dest`` and that path is returned.
    """
    local = backend.local_path_if_any(key)
    if local is not None:
        return local
    target = Path(dest)
    backend.download_file(key, target)
    return target


def staging_dir(prefix: str = "storage-stage-") -> Path:
    """Create a temporary directory under ``<data_dir>/tmp`` for pipeline staging."""
    base = settings.data_path / "tmp"
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def _header_get(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except Exception:  # pragma: no cover - exotic header containers
        value = None
    if value:
        return str(value)
    if isinstance(headers, dict):
        for raw_name, raw_value in headers.items():
            if str(raw_name).lower() == name.lower() and raw_value:
                return str(raw_value)
    return None


def _json_response(
    status_code: int, detail: str, code: str, *, headers: dict[str, str] | None = None
) -> Any:
    from starlette.responses import Response

    body = json.dumps({"detail": detail, "code": code}, ensure_ascii=False).encode("utf-8")
    return Response(content=body, status_code=status_code, media_type="application/json", headers=headers)


def serve_key(
    backend: StorageBackend,
    key: str,
    *,
    filename: str,
    request_headers: Any,
    media_type: str,
    download: bool = False,
    presign_redirect: bool | None = None,
) -> Any:
    """Build a Starlette response proxying (or redirecting to) a stored media key.

    * no ``Range`` -> 200 streaming response with ``Content-Length``
    * valid ``Range`` -> 206 with ``Content-Range`` / rounded ``Content-Length``
    * invalid/unsatisfiable ``Range`` -> 416 with ``Content-Range: bytes */size``
    * ``ECHOIC_S3_PRESIGN_REDIRECT=true`` + S3 -> 307 to a presigned URL
      (falls back to the proxy when signing is unavailable)
    * missing key -> 404 ``{"detail","code"}``

    ``media_type`` is the MIME type the browser should receive (the proxy sets
    it explicitly instead of relying on the storage object metadata).
    """
    redirect_enabled = (
        bool(settings.s3_presign_redirect) if presign_redirect is None else bool(presign_redirect)
    )
    if redirect_enabled and getattr(backend, "name", "") == "s3":
        expires = int(getattr(backend, "presign_expires", DEFAULT_PRESIGN_EXPIRES) or DEFAULT_PRESIGN_EXPIRES)
        try:
            url = backend.presigned_url(
                key, expires_sec=expires, filename=filename, download=download
            )
        except TypeError:  # minimal protocol implementation without the extra kwargs
            url = backend.presigned_url(key, expires_sec=expires)
        if url:
            from starlette.responses import RedirectResponse

            return RedirectResponse(
                url,
                status_code=307,
                headers={"Cache-Control": "private, max-age=0, must-revalidate"},
            )
        logger.info("presign redirect requested but signing unavailable; falling back to proxy")

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": content_disposition(filename, inline=not download),
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    try:
        total = int(backend.size(key))
    except StorageError:
        return _json_response(404, "媒体文件不存在", "NOT_FOUND")
    except Exception as exc:  # pragma: no cover - unexpected backend failure
        logger.warning("media size lookup failed for %s: %s", key, exc)
        return _json_response(500, f"读取媒体信息失败：{exc}", "STORAGE_ERROR")

    from starlette.responses import StreamingResponse

    range_header = _header_get(request_headers, "range")
    if not range_header:
        headers = {**base_headers, "Content-Length": str(total)}
        return StreamingResponse(
            backend.open_stream(key), status_code=200, media_type=media_type, headers=headers
        )
    parsed = parse_range_header(range_header, total)
    if parsed is None:
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes */{total}",
            "Cache-Control": "private, max-age=0, must-revalidate",
        }
        return _json_response(416, "请求的 Range 不合法", "VALIDATION_ERROR", headers=headers)
    start, end = parsed
    length = end - start + 1
    headers = {
        **base_headers,
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        backend.open_range(key, start, length),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


__all__ = [
    "CHUNK_SIZE",
    "DEFAULT_PRESIGN_EXPIRES",
    "LocalStorage",
    "MAX_PRESIGN_EXPIRES",
    "S3Storage",
    "STATUS_TTL_SECONDS",
    "StorageBackend",
    "StorageError",
    "asr_wav_key",
    "backend_name",
    "build_storage",
    "content_disposition",
    "fetch_to_local",
    "file_size",
    "get_storage",
    "normalize_key",
    "normalize_prefix",
    "original_key",
    "parse_range_header",
    "playback_key",
    "recording_original_key",
    "recording_playback_key",
    "recording_prefix",
    "reset_storage_cache",
    "serve_key",
    "staging_dir",
    "storage_status",
]
