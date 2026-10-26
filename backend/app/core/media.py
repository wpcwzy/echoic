from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MIME_BY_EXT = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".amr": "audio/amr",
    ".wma": "audio/x-ms-wma",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".mpeg": "audio/mpeg",
    ".mpg": "audio/mpeg",
}

BROWSER_SAFE_PLAYBACK_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".webm", ".mp4", ".mov"}


class MediaError(Exception):
    pass


@dataclass
class MediaInfo:
    duration_ms: int = 0
    media_type: str = "audio"
    mime_type: str = "application/octet-stream"
    has_audio: bool = False
    has_video: bool = False
    format_name: str = ""
    sample_rate: int | None = None
    channels: int | None = None


def _which(binary: str) -> str | None:
    return shutil.which(binary)


def ffmpeg_available(ffmpeg_bin: str = "ffmpeg") -> bool:
    return _which(ffmpeg_bin) is not None


def ffprobe_available(ffprobe_bin: str = "ffprobe") -> bool:
    return _which(ffprobe_bin) is not None


def guess_mime(ext: str) -> str:
    return MIME_BY_EXT.get((ext or "").lower(), "application/octet-stream")


def guess_media_type(ext: str) -> str:
    mime = guess_mime(ext)
    return "video" if mime.startswith("video/") else "audio"


def probe(path: Path, ffprobe_bin: str = "ffprobe") -> MediaInfo:
    ext = path.suffix.lower()
    info = MediaInfo(
        media_type=guess_media_type(ext),
        mime_type=guess_mime(ext),
        has_audio=True,
        has_video=guess_media_type(ext) == "video",
    )
    if not ffprobe_available(ffprobe_bin) or not path.exists():
        return info
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise MediaError(f"ffprobe 执行失败: {exc}") from exc
    if proc.returncode != 0:
        raise MediaError(f"无法解析媒体文件: {proc.stderr.strip()[:300]}")
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise MediaError("ffprobe 输出解析失败") from exc
    streams = data.get("streams") or []
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    has_video = any(s.get("codec_type") == "video" for s in streams)
    info.has_audio = has_audio
    info.has_video = has_video
    if not has_audio and not has_video:
        raise MediaError("文件中没有可用的音视频流")
    info.media_type = "video" if has_video and not has_audio or has_video else "audio"
    if has_video and has_audio:
        info.media_type = "video"
    fmt = data.get("format") or {}
    info.format_name = fmt.get("format_name") or ""
    try:
        info.duration_ms = int(float(fmt.get("duration") or 0) * 1000)
    except (TypeError, ValueError):
        info.duration_ms = 0
    for stream in streams:
        if stream.get("codec_type") == "audio":
            try:
                info.sample_rate = int(stream.get("sample_rate") or 0) or None
            except (TypeError, ValueError):
                info.sample_rate = None
            info.channels = stream.get("channels")
            break
    return info


def run_ffmpeg(args: list[str], timeout: int = 1800, ffmpeg_bin: str = "ffmpeg") -> None:
    if not ffmpeg_available(ffmpeg_bin):
        raise MediaError("服务器未安装 ffmpeg，无法处理音视频")
    cmd = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffmpeg 处理超时") from exc
    except OSError as exc:
        raise MediaError(f"ffmpeg 执行失败: {exc}") from exc
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg 处理失败: {proc.stderr.strip()[:400]}")


def make_playback(src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    """Transcode the audio track to a browser friendly m4a/aac file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(dst)],
        ffmpeg_bin=ffmpeg_bin,
    )


def extract_asr_wav(src: Path, dst: Path, ffmpeg_bin: str = "ffmpeg") -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-i", str(src), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dst)],
        ffmpeg_bin=ffmpeg_bin,
    )


def generate_tone_audio(duration_ms: int, dst: Path, ffmpeg_bin: str = "ffmpeg") -> bool:
    """Synthesize a low-volume tone of the requested length (used by the demo seed)."""
    if not ffmpeg_available(ffmpeg_bin):
        return False
    seconds = max(1.0, duration_ms / 1000.0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_ffmpeg(
            [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=196:sample_rate=16000:duration={seconds:.3f}",
                "-af",
                "volume=0.02",
                "-c:a",
                "aac",
                "-b:a",
                "16k",
                "-ac",
                "1",
                "-movflags",
                "+faststart",
                str(dst),
            ],
            timeout=600,
            ffmpeg_bin=ffmpeg_bin,
        )
        return True
    except MediaError as exc:
        logger.warning("demo audio synthesis failed: %s", exc)
        return False


def media_duration_ms(path: Path, ffprobe_bin: str = "ffprobe") -> int:
    try:
        return probe(path, ffprobe_bin=ffprobe_bin).duration_ms
    except MediaError:
        return 0
