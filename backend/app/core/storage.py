from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..config import settings
from .errors import ApiError

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".ogg",
    ".oga",
    ".opus",
    ".flac",
    ".amr",
    ".wma",
    ".webm",
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".mpga",
    ".mpeg",
    ".mpg",
}

AUDIO_EXTENSIONS = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".amr", ".wma", ".mpga", ".mpeg", ".mpg",
}
VIDEO_EXTENSIONS = {".webm", ".mp4", ".mov", ".mkv", ".avi", ".m4v"}

#: v1.0 video slides/keyframes live under ``<recording dir>/scenes/``
SCENES_DIRNAME = "scenes"
SCENE_IMAGE_SUFFIXES = {".jpg", ".jpeg"}


class StorageError(Exception):
    pass


def data_root() -> Path:
    return settings.data_path


def recording_dir(uuid: str) -> Path:
    p = settings.media_path / uuid
    p.mkdir(parents=True, exist_ok=True)
    return p


def storage_relpath(uuid: str) -> str:
    return f"media/{uuid}"


def resolve_storage_dir(storage_dir: str) -> Path:
    root = data_root().resolve()
    p = (root / (storage_dir or "")).resolve()
    if p != root and root not in p.parents:
        raise StorageError("非法存储路径")
    return p


def original_path(storage_dir: str, original_filename: str, uuid: str) -> Path:
    ext = Path(original_filename or "").suffix.lower() or ".bin"
    return resolve_storage_dir(storage_dir) / f"original{ext}"


def playback_path(storage_dir: str) -> Path:
    return resolve_storage_dir(storage_dir) / "playback.m4a"


def video_playback_path(storage_dir: str) -> Path:
    """Browser-compatible H.264/AAC playback video (``playback.mp4``)."""
    return resolve_storage_dir(storage_dir) / "playback.mp4"


def asr_wav_path(storage_dir: str) -> Path:
    return resolve_storage_dir(storage_dir) / "asr.wav"


def safe_scene_filename(filename: str) -> str:
    """Validate a scene image filename (single safe JPEG basename, no traversal)."""
    raw = str(filename or "").strip()
    if not raw:
        raise StorageError("场景图片文件名为空")
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise StorageError("非法的场景图片文件名")
    name = Path(raw).name
    if name != raw:
        raise StorageError("非法的场景图片文件名")
    if Path(name).suffix.lower() not in SCENE_IMAGE_SUFFIXES:
        raise StorageError("不支持的场景图片格式（仅支持 JPEG）")
    return name


def scene_dir_path(storage_dir: str) -> Path:
    """Local directory containing extracted video scene images."""
    return resolve_storage_dir(storage_dir) / SCENES_DIRNAME


def ensure_scene_dir(storage_dir: str) -> Path:
    path = scene_dir_path(storage_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def scene_image_path(storage_dir: str, filename: str) -> Path:
    """Local path of one scene image (validated filename, no path traversal)."""
    return scene_dir_path(storage_dir) / safe_scene_filename(filename)


def safe_extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ApiError(
            415,
            f"不支持的媒体格式: {ext or '未知'}；支持 mp3/wav/m4a/aac/ogg/flac/webm/mp4/mov/mkv 等",
            "UNSUPPORTED_MEDIA",
        )
    return ext


def save_upload(stream, dest: Path, max_bytes: int) -> int:
    """Stream a file-like object to dest, enforcing a size cap. Returns byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ApiError(
                        413,
                        f"文件超过大小限制（{max_bytes // (1024 * 1024)} MB）",
                        "UPLOAD_TOO_LARGE",
                    )
                out.write(chunk)
    except BaseException:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return total


def delete_recording_dir(storage_dir: str) -> None:
    try:
        path = resolve_storage_dir(storage_dir)
    except StorageError:
        logger.warning("refuse to delete unsafe path: %s", storage_dir)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def dir_size(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                continue
    return total
