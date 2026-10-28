#!/usr/bin/env python3
"""Echoic 实时字幕（DashScope realtime ASR）真实长稳 soak 工具 — v1.0 staging。

用途
----
不启动 Echoic 服务，直接用后端**现有 provider 协议**连接阿里云百炼 DashScope
实时转写 WebSocket，把真实音频按**真实时间节奏**（1x）持续推流 N 分钟，记录：

* 连接生命周期：连接/任务启动（ready）、断线、重连、优雅结束；
* 事件计数：ready / partial / final / error（每段/总计）；
* 重连：意外断线自动重连（新 provider 会话，续传音频位置），有上限；
* 进程 RSS（Linux /proc，采样间隔 5s）；
* 最终 JSON 报告（含逐会话统计与时间线）+ 终端人类可读摘要。

**诚实性约定**：默认 60 分钟，必须真实跑满才有 "ok" 证据；跑不满/有 provider
错误/断线重连都会反映在 status 与退出码里（见下）。本工具不会模拟真实结果。

用法
----
    # 推荐：Key 走环境变量（避免出现在进程参数/历史记录里）
    export ECHOIC_REALTIME_ASR_API_KEY=sk-xxxx
    backend/.venv/bin/python scripts/soak_live.py \
        --minutes 60 --audio /path/to/meeting-10min.wav \
        --out e2e/soak-live-$(date -u +%Y%m%dT%H%M%SZ).json

    # 快速冒烟（1–2 分钟；真实 provider，但时长不足以作为 60 分钟证据）
    ECHOIC_REALTIME_ASR_API_KEY=sk-xxxx backend/.venv/bin/python scripts/soak_live.py \
        --minutes 2 --audio /path/to/meeting.wav --out /tmp/soak-2min.json

    # 无 Key 自检（mock provider，仅验证脚本机制：节奏/统计/报告；不验证 DashScope）
    backend/.venv/bin/python scripts/soak_live.py --provider mock --minutes 0.2 \
        --out .cache/soak-live-mock-selftest.json

音频要求
--------
* `--audio` 支持 ffmpeg 可解码的任意格式；内部用 ffmpeg 解码为 PCM16LE /
  16 kHz / 单声道后按 100ms 帧推送（解码结果缓存在 `.cache/soak-live/`）。
* 音频短于目标时长时会**循环播放**（报告中记录 loops），建议用 ≥10 分钟的真实
  中文会议音频；循环接缝对 ASR 无影响（实时转写逐句输出）。
* 不传 `--audio` 时使用内置合成音频（类语音调制音，非真实语音）：只适合验证
  连接/传输稳定性，DashScope 可能不产生转写文本，报告中会显式标注。
* 若 `--audio` 是 `.pcm` / `.raw` 文件，则按原始 PCM16LE 16 kHz mono 直接使用。

退出码（staging 判定用；任何 provider 错误都不会被吞掉）
------------------------------------------------------
* 0  跑满计划时长，且无 provider 错误/断线；
* 1  跑满计划时长，但记录了 provider 错误/重连（degraded，需人工判断）；
* 2  无法完成（首次连接失败、Key 缺失、音频/ffmpeg 问题、不可恢复错误）；
* 130 被 Ctrl-C 中断（仍会写出已运行部分报告）。

报告字段（JSON）详见 `--out` 文件；关键字段：`status`、`totals`、`sessions`、
`provider_errors`、`audio`、`rss`、`timeline`、`summary_lines`。

注意
----
* 本工具只依赖后端已安装依赖（websockets 等），不会写入数据库、不修改应用代码；
* API Key 绝不写入报告/日志（只记录 key 来源，如 `env:ECHOIC_REALTIME_ASR_API_KEY`）；
* 单次实时会话有并发/时长配额限制，长时间运行请确认百炼账号的实时转写额度；
* 60 分钟真实长稳 = 1x 实时推流 + 真实 Key + 网络稳定，无法在 CI 里伪造。
"""

from __future__ import annotations

import argparse
import array
import asyncio
import hashlib
import json
import math
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:  # reuse the exact provider protocol from the app
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.errors import ProviderError  # noqa: E402
from app.providers.realtime_dashscope import (  # noqa: E402
    DEFAULT_MODEL,
    DEFAULT_REALTIME_URL,
    DashScopeRealtimeProvider,
)
from app.providers.realtime_mock import MockRealtimeProvider  # noqa: E402

SAMPLE_RATE = 16000
BYTES_PER_MS = 32  # PCM16LE mono @16 kHz: 32 bytes/ms
DEFAULT_FRAME_MS = 100
DEFAULT_FLUSH_TIMEOUT_S = 90.0
DEFAULT_MAX_CONSECUTIVE_START_FAILURES = 3
DEFAULT_MAX_TOTAL_RECONNECTS = 20
RSS_SAMPLE_INTERVAL_S = 5.0
REPORT_VERSION = 1
CACHE_DIR = REPO_ROOT / ".cache" / "soak-live"

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_FATAL = 2
EXIT_INTERRUPTED = 130


# ============================================================== small utilities
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_rss_mb() -> float | None:
    """Current process RSS in MiB (Linux /proc; None elsewhere)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


def human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, remainder = divmod(int(round(seconds)), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def env_or_empty(name: str) -> str:
    return (os.environ.get(name) or "").strip()


# ================================================================ audio source
class PcmAudioSource:
    """Looping PCM16LE mono 16 kHz source read in fixed-size frames."""

    def __init__(self, path: Path, *, label: str, synthetic: bool = False) -> None:
        self.path = path
        self.label = label
        self.synthetic = synthetic
        self._handle = path.open("rb")
        self.size = path.stat().st_size
        if self.size <= 0:
            raise RuntimeError(f"PCM 音频为空：{path}")
        self.pos = 0
        self.loops = 0
        self.bytes_read = 0

    @property
    def duration_s(self) -> float:
        return self.size / (SAMPLE_RATE * 2)

    def read(self, nbytes: int) -> bytes:
        output = bytearray()
        guard = 0
        while len(output) < nbytes:
            guard += 1
            if guard > 1000:  # defensive: never spin forever
                raise RuntimeError("读取 PCM 音频失败（循环保护）")
            if self.pos >= self.size:
                self.loops += 1
                self.pos = 0
                self._handle.seek(0)
                continue
            chunk = self._handle.read(min(nbytes - len(output), self.size - self.pos))
            if not chunk:  # truncated file; restart from the beginning
                self.loops += 1
                self.pos = 0
                self._handle.seek(0)
                continue
            self.pos += len(chunk)
            self.bytes_read += len(chunk)
            output.extend(chunk)
        return bytes(output)

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


def decode_audio_to_pcm(audio_path: Path) -> Path:
    """Decode any ffmpeg-readable audio to raw PCM16LE 16 kHz mono (cached)."""
    if audio_path.suffix.lower() in {".pcm", ".raw"}:
        return audio_path
    if not audio_path.is_file():
        raise RuntimeError(f"音频文件不存在：{audio_path}")
    stat = audio_path.stat()
    digest = hashlib.sha1(
        f"{audio_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode("utf-8")
    ).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"decode-{digest}.pcm"
    if target.is_file() and target.stat().st_size > 0:
        return target
    ffmpeg_bin = env_or_empty("ECHOIC_FFMPEG") or "ffmpeg"
    command = [
        ffmpeg_bin,
        "-nostdin",
        "-v",
        "error",
        "-y",
        "-i",
        str(audio_path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        str(target),
    ]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"无法执行 ffmpeg 解码音频（{type(exc).__name__}: {exc}）") from exc
    if proc.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        detail = (proc.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"ffmpeg exit={proc.returncode}"
        raise RuntimeError(f"ffmpeg 解码音频失败：{tail}")
    return target


def make_synthetic_pcm(seconds: int = 10) -> Path:
    """Generate a deterministic voice-like (not real speech) PCM file for self-tests."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / f"synthetic-voice-like-{seconds}s.pcm"
    expected = seconds * SAMPLE_RATE * 2
    if target.is_file() and target.stat().st_size == expected:
        return target
    samples = array.array("h")
    for index in range(seconds * SAMPLE_RATE):
        t = index / SAMPLE_RATE
        # 4 Hz syllable-like envelope with short gaps + 180 Hz fundamental/harmonics
        gate = max(0.0, 0.5 + 0.5 * math.sin(2 * math.pi * 4.0 * t) - 0.30)
        f0 = 180.0 + 18.0 * math.sin(2 * math.pi * 0.5 * t)
        value = math.sin(2 * math.pi * f0 * t) + 0.45 * math.sin(2 * math.pi * 2 * f0 * t)
        samples.append(max(-32767, min(32767, int(0.30 * gate * value * 32767))))
    temporary = target.with_suffix(".pcm.tmp")
    temporary.write_bytes(samples.tobytes())
    temporary.replace(target)
    return target


# ==================================================================== recorder
class SoakRecorder:
    """Mutable soak state; finalised into a JSON report + console summary."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        language: str,
        key_source: str,
        requested_minutes: float,
        frame_ms: int,
        audio_label: str,
        audio_synthetic: bool,
        audio_source_duration_s: float,
    ) -> None:
        self.started_at = utc_now_iso()
        self.started_mono = time.monotonic()
        self.finished_at = ""
        self.duration_s = 0.0
        self.status = "running"
        self.exit_code = EXIT_FATAL
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.language = language
        self.key_source = key_source
        self.requested_minutes = requested_minutes
        self.planned_s = max(0.0, requested_minutes * 60.0)
        self.frame_ms = frame_ms
        self.audio_label = audio_label
        self.audio_synthetic = audio_synthetic
        self.audio_source_duration_s = audio_source_duration_s
        self.audio_ms_streamed = 0
        self.frames_sent = 0
        self.ready = 0
        self.partial = 0
        self.final = 0
        self.errors = 0
        self.drops = 0
        self.reconnects = 0
        self.sessions: list[dict[str, Any]] = []
        self.provider_errors: list[dict[str, Any]] = []
        self.timeline: list[dict[str, Any]] = []
        self.final_samples: list[dict[str, Any]] = []
        self.rss_samples: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.audio_loops = 0
        self.completed_planned_duration = False
        self._current_session: dict[str, Any] | None = None
        self._last_rss_sample = -RSS_SAMPLE_INTERVAL_S
        self._rss_max: float | None = None
        self._sessions_started = 0

    # ------------------------------------------------------------------ clock
    def elapsed(self) -> float:
        return time.monotonic() - self.started_mono

    def deadline_mono(self) -> float:
        return self.started_mono + self.planned_s

    # --------------------------------------------------------------- tracking
    def add_timeline(self, event: str, **data: Any) -> None:
        entry: dict[str, Any] = {"offset_s": round(self.elapsed(), 3), "event": event}
        entry.update(data)
        self.timeline.append(entry)

    def sample_rss(self, *, force: bool = False) -> None:
        now = self.elapsed()
        if not force and now - self._last_rss_sample < RSS_SAMPLE_INTERVAL_S:
            return
        self._last_rss_sample = now
        rss = read_rss_mb()
        if rss is None:
            return
        self.rss_samples.append({"offset_s": round(now, 1), "rss_mb": rss})
        if self._rss_max is None or rss > self._rss_max:
            self._rss_max = rss

    def begin_session(self, index: int) -> dict[str, Any]:
        session = {
            "index": index,
            "outcome": "started",
            "started_offset_s": round(self.elapsed(), 3),
            "ended_offset_s": None,
            "audio_ms_streamed": 0,
            "ready": 0,
            "partial": 0,
            "final": 0,
            "reconnects": 0,
            "error": "",
        }
        self.sessions.append(session)
        self._current_session = session
        self._sessions_started += 1
        return session

    def finish_session(self, outcome: str, error: str = "") -> None:
        session = self._current_session
        if session is None:
            return
        session["outcome"] = outcome
        session["ended_offset_s"] = round(self.elapsed(), 3)
        if error:
            session["error"] = error
        if outcome == "started":  # defensive; should not normally be left open
            session["outcome"] = "incomplete"
        self._current_session = None

    def record_ready(self) -> None:
        self.ready += 1
        if self._current_session is not None:
            self._current_session["ready"] += 1
        self.add_timeline("ready")

    def record_provider_error(self, message: str, *, code: str = "") -> None:
        self.errors += 1
        entry = {
            "offset_s": round(self.elapsed(), 3),
            "message": message[:500],
            "code": code,
            "session": int(self._current_session["index"]) if self._current_session else None,
        }
        self.provider_errors.append(entry)
        self.add_timeline("provider_error", message=entry["message"][:200])

    def record_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "partial":
            self.partial += 1
            if self._current_session is not None:
                self._current_session["partial"] += 1
        elif kind == "final":
            self.final += 1
            if self._current_session is not None:
                self._current_session["final"] += 1
            text = str(event.get("text") or "").strip()
            if text and len(self.final_samples) < 12:
                self.final_samples.append(
                    {
                        "offset_s": round(self.elapsed(), 1),
                        "text": text[:160],
                        "start_ms": int(event.get("start_ms") or 0),
                        "end_ms": int(event.get("end_ms") or 0),
                    }
                )
        elif kind == "error":
            message = str(event.get("message") or "实时转写 provider 错误")
            self.record_provider_error(message, code=str(event.get("code") or ""))

    def mark_drop(self, reason: str) -> None:
        self.drops += 1
        self.add_timeline("dropped", reason=reason[:300])

    def mark_reconnect(self, attempt: int, backoff_s: float) -> None:
        self.reconnects += 1
        if self._current_session is not None:
            self._current_session["reconnects"] += 1
        self.add_timeline("reconnect", attempt=attempt, backoff_s=round(backoff_s, 2))

    def add_warning(self, warning: str) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)

    # ---------------------------------------------------------------- finalize
    def finalise(self) -> None:
        now = time.monotonic()
        self.duration_s = round(now - self.started_mono, 3)
        self.finished_at = utc_now_iso()
        self.exit_code = {
            "ok": EXIT_OK,
            "degraded": EXIT_DEGRADED,
            "failed": EXIT_FATAL,
            "interrupted": EXIT_INTERRUPTED,
        }.get(self.status, EXIT_FATAL)

    def build_report(self) -> dict[str, Any]:
        rss_max = self._rss_max
        coverage = (
            round(100.0 * self.audio_ms_streamed / (self.planned_s * 1000.0), 1)
            if self.planned_s > 0
            else 0.0
        )
        return {
            "report_version": REPORT_VERSION,
            "kind": "echoic-live-soak",
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.duration_s,
            "duration_human": human_duration(self.duration_s),
            "planned_minutes": self.requested_minutes,
            "planned_seconds": round(self.planned_s, 1),
            "audio_streamed_seconds": round(self.audio_ms_streamed / 1000.0, 1),
            "planned_duration_completed": self.completed_planned_duration,
            "streaming_coverage_pct": coverage,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "language": self.language,
            "key_source": self.key_source,
            "key_configured": self.key_source.startswith(("env:", "cli:")),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "git_revision": git_revision(),
                "repo_root": str(REPO_ROOT),
            },
            "audio": {
                "label": self.audio_label,
                "synthetic": self.audio_synthetic,
                "source_duration_s": round(self.audio_source_duration_s, 2),
                "streamed_ms": int(self.audio_ms_streamed),
                "frames_sent": self.frames_sent,
                "frame_ms": self.frame_ms,
                "loops": self.audio_loops,
            },
            "totals": {
                "sessions": len(self.sessions),
                "ready": self.ready,
                "partial": self.partial,
                "final": self.final,
                "provider_errors": self.errors,
                "drops": self.drops,
                "reconnects": self.reconnects,
            },
            "sessions": self.sessions,
            "provider_errors": self.provider_errors,
            "final_samples": self.final_samples,
            "rss": {
                "max_mb": rss_max,
                "samples": self.rss_samples,
            },
            "warnings": self.warnings,
            "timeline": self.timeline,
            "summary_lines": self.summary_lines(),
        }

    def summary_lines(self) -> list[str]:
        lines = [
            "=== Echoic 实时字幕 soak 报告 ===",
            f"状态: {self.status}（退出码 {self.exit_code}）",
            f"Provider: {self.provider} / model={self.model} / base_url={self.base_url}",
            f"Key 来源: {self.key_source or '未配置'}（报告中不记录 Key 明文）",
            f"计划时长: {self.requested_minutes:g} 分钟；实际墙钟: {human_duration(self.duration_s)}",
            f"实际推流音频: {human_duration(self.audio_ms_streamed / 1000.0)}"
            f"（帧 {self.frames_sent} × {self.frame_ms}ms）",
            f"会话: {len(self.sessions)} 个；ready={self.ready}；"
            f"partial={self.partial}；final={self.final}",
            f"断线: {self.drops}；重连: {self.reconnects}；provider 错误: {self.errors}",
            f"音频源: {self.audio_label}"
            + ("（合成音频，仅验证连接/传输）" if self.audio_synthetic else ""),
            f"RSS 峰值: {self._rss_max if self._rss_max is not None else 'unavailable'} MiB",
        ]
        for warning in self.warnings:
            lines.append(f"警告: {warning}")
        return lines


# =================================================================== streaming
async def _send_audio_loop(
    provider: Any,
    source: PcmAudioSource,
    recorder: SoakRecorder,
    *,
    loop: asyncio.AbstractEventLoop,
    deadline_mono: float,
) -> Any:
    """Stream PCM at real-time pace until the deadline. Returns 'deadline' or error tuple."""
    frame_bytes = BYTES_PER_MS * recorder.frame_ms
    next_tick = loop.time()
    last_progress = loop.time()
    while True:
        now = loop.time()
        if now >= deadline_mono:
            return "deadline"
        if next_tick > now:
            await asyncio.sleep(min(next_tick - now, 0.5))
        if loop.time() >= deadline_mono:
            return "deadline"
        chunk = source.read(frame_bytes)
        try:
            await provider.send_audio(chunk)
        except ProviderError as exc:
            return ("provider_error", str(exc))
        except Exception as exc:  # noqa: BLE001 - network/websocket surprises are drops
            return ("send_error", f"{type(exc).__name__}: {exc}")
        recorder.frames_sent += 1
        recorder.audio_ms_streamed += recorder.frame_ms
        recorder.sample_rss()
        if loop.time() - last_progress >= 60.0:
            last_progress = loop.time()
            print(
                f"[soak] {human_duration(recorder.elapsed())} / "
                f"{human_duration(recorder.planned_s)} "
                f"partial={recorder.partial} final={recorder.final} "
                f"rss={read_rss_mb()}MiB",
                file=sys.stderr,
                flush=True,
            )
        next_tick += recorder.frame_ms / 1000.0
        if next_tick < loop.time() - 1.0:  # recover from a blocking send
            next_tick = loop.time()


async def _consume_events_loop(
    provider: Any,
    recorder: SoakRecorder,
    session: dict[str, Any],
) -> Any:
    """Drain provider events. Returns 'closed' or ('provider_error', message)."""
    session["audio_ms_streamed"] = 0
    start_audio = recorder.audio_ms_streamed
    while True:
        try:
            event = await provider.events.get()
        except asyncio.CancelledError:
            raise
        if event is None:
            return "closed"
        recorder.record_event(event)
        session["audio_ms_streamed"] = recorder.audio_ms_streamed - start_audio
        if str(event.get("type") or "") == "error":
            return ("provider_error", str(event.get("message") or "provider error"))


async def _finish_and_collect(
    provider: Any,
    consumer: asyncio.Task,
    recorder: SoakRecorder,
    *,
    flush_timeout: float,
) -> None:
    """Send finish-task, wait for the provider to flush finals, then stop the consumer."""
    recorder.add_timeline("finish_requested")
    try:
        await asyncio.wait_for(provider.finish(), timeout=flush_timeout)
    except asyncio.TimeoutError:
        recorder.add_warning(f"finish-task 在 {flush_timeout:g}s 内未收到 task-finished（可能丢尾部 final）")
        recorder.record_provider_error(
            f"finish-task 未见 task-finished（{flush_timeout:g}s 超时）", code="FINISH_TIMEOUT"
        )
    except ProviderError as exc:
        recorder.add_warning(f"finish-task 发送失败：{exc}")
        recorder.record_provider_error(f"finish-task 失败：{exc}", code="FINISH_FAILED")
    except Exception as exc:  # noqa: BLE001 - best effort flush
        recorder.add_warning(f"finish-task 异常：{type(exc).__name__}: {exc}")
    try:
        await asyncio.wait_for(asyncio.shield(consumer), timeout=15.0)
    except asyncio.TimeoutError:
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):  # noqa: B014 - best-effort cleanup
            pass


async def _stream_one_session(
    provider: Any,
    source: PcmAudioSource,
    recorder: SoakRecorder,
    session: dict[str, Any],
    *,
    deadline_mono: float,
    flush_timeout: float,
) -> Any:
    """Run one provider session; returns 'graceful' or an abnormal result tuple."""
    loop = asyncio.get_running_loop()
    sender = asyncio.create_task(
        _send_audio_loop(provider, source, recorder, loop=loop, deadline_mono=deadline_mono)
    )
    consumer = asyncio.create_task(_consume_events_loop(provider, recorder, session))
    done, _pending = await asyncio.wait(
        {sender, consumer}, return_when=asyncio.FIRST_COMPLETED
    )
    sender_result = sender.result() if sender in done else None
    consumer_result = consumer.result() if consumer in done else None

    # Graceful: sender reached the planned deadline.
    if sender_result == "deadline":
        if consumer_result is not None and consumer_result != "closed":
            # Provider broke exactly at the deadline; still try to flush what we have.
            recorder.mark_drop(f"deadline 时 provider 已异常：{consumer_result}")
        await _finish_and_collect(provider, consumer, recorder, flush_timeout=flush_timeout)
        recorder.add_timeline("session_finished")
        return "graceful"

    # Abnormal: cancel the peer task, report the drop reason.
    if isinstance(sender_result, tuple) and len(sender_result) == 2:
        reason = f"音频发送失败（{sender_result[0]}）：{sender_result[1]}"
    elif isinstance(consumer_result, tuple) and len(consumer_result) == 2:
        reason = f"provider 断开（{consumer_result[0]}）：{consumer_result[1]}"
    elif consumer_result == "closed":
        reason = "provider 连接提前关闭（未到计划时长）"
    else:
        reason = f"会话异常结束（sender={sender_result!r}, consumer={consumer_result!r}）"
    sender.cancel()
    consumer.cancel()
    for task in (sender, consumer):
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: B014 - cleanup
            pass
    recorder.mark_drop(str(reason))
    return ("abnormal", str(reason))


# ==================================================================== orchestrate
def build_provider(args: argparse.Namespace, *, key: str, base_url: str, model: str) -> Any:
    if args.provider == "mock":
        return MockRealtimeProvider(model=model or "mock-realtime", language=args.language)
    return DashScopeRealtimeProvider(
        base_url=base_url,
        model=model,
        api_key=key,
        language=args.language,
    )


async def orchestrate(
    args: argparse.Namespace,
    recorder: SoakRecorder,
    source: PcmAudioSource,
    *,
    key: str,
    base_url: str,
    model: str,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + recorder.planned_s
    consecutive_start_failures = 0
    reconnect_attempts = 0
    recorder.sample_rss(force=True)
    recorder.add_timeline("soak_start")

    while time.monotonic() < deadline:
        session = recorder.begin_session(len(recorder.sessions) + 1)
        provider = build_provider(args, key=key, base_url=base_url, model=model)
        try:
            await provider.start(
                language=args.language,
                resume_from_ms=int(recorder.audio_ms_streamed),
            )
        except ProviderError as exc:
            recorder.record_provider_error(str(exc), code="START_FAILED")
            recorder.finish_session("start_failed", str(exc))
            try:
                await provider.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
            consecutive_start_failures += 1
            if len(recorder.sessions) == 1:
                recorder.status = "failed"
                recorder.add_warning("首次连接 DashScope 实时转写失败，soak 未开始")
                return
            if consecutive_start_failures >= args.max_consecutive_start_failures:
                recorder.status = "failed"
                recorder.add_warning(
                    f"连续 {consecutive_start_failures} 次重连启动失败，放弃 soak"
                )
                return
            backoff = min(args.max_backoff, args.reconnect_backoff * (2 ** (consecutive_start_failures - 1)))
            recorder.mark_reconnect(consecutive_start_failures, backoff)
            await asyncio.sleep(backoff)
            continue

        consecutive_start_failures = 0
        recorder.record_ready()
        recorder.add_timeline("session_started", session=(session["index"]))
        try:
            result = await _stream_one_session(
                provider,
                source,
                recorder,
                session,
                deadline_mono=deadline,
                flush_timeout=args.flush_timeout,
            )
        finally:
            try:
                await provider.close()
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

        if result == "graceful":
            recorder.completed_planned_duration = True
            recorder.finish_session("graceful")
            return

        # Abnormal drop: session already marked dropped by _stream_one_session.
        recorder.finish_session("dropped")
        if time.monotonic() >= deadline:
            recorder.status = "degraded"
            recorder.add_warning("结束时仍有 provider 错误/断线记录")
            return
        reconnect_attempts += 1
        if reconnect_attempts > args.max_total_reconnects:
            recorder.status = "failed"
            recorder.add_warning(f"重连超过上限（{args.max_total_reconnects}），放弃 soak")
            return
        backoff = min(args.max_backoff, args.reconnect_backoff * (2 ** min(reconnect_attempts - 1, 4)))
        recorder.mark_reconnect(reconnect_attempts, backoff)
        recorder.add_timeline("reconnect_wait", backoff_s=round(backoff, 2))
        await asyncio.sleep(backoff)

    # Reached here only if the loop condition expired mid-reconnect.
    recorder.status = "degraded"
    recorder.add_warning("计划时长已到但最后一次会话非正常结束")


# ======================================================================== CLI
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Echoic DashScope 实时转写真实长稳（soak）工具（v1.0 staging）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  ECHOIC_REALTIME_ASR_API_KEY=sk-xxx backend/.venv/bin/python scripts/soak_live.py \\\n"
            "      --minutes 60 --audio /path/to/meeting.wav \\\n"
            "      --out e2e/soak-live-$(date -u +%Y%m%dT%H%M%SZ).json\n"
            "\n"
            "无 Key 机制自检：\n"
            "  backend/.venv/bin/python scripts/soak_live.py --provider mock --minutes 0.2 \\\n"
            "      --out .cache/soak-live-mock-selftest.json\n"
        ),
    )
    parser.add_argument("--minutes", type=float, default=60.0, help="推流时长（分钟，默认 60）")
    parser.add_argument("--audio", type=Path, default=None, help="真实音频文件（ffmpeg 可解码；缺省用合成音频）")
    parser.add_argument("--key", default=None, help="DashScope API Key（建议改用环境变量 ECHOIC_REALTIME_ASR_API_KEY）")
    parser.add_argument("--model", default=None, help=f"实时转写模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--base-url", default=None, help=f"实时转写 WebSocket 地址（默认 {DEFAULT_REALTIME_URL}）")
    parser.add_argument("--out", type=Path, default=None, help="JSON 报告路径（默认 e2e/soak-live-<UTC>.json）")
    parser.add_argument(
        "--provider",
        choices=["dashscope", "mock"],
        default="dashscope",
        help="dashscope=真实长稳（默认）；mock=无网络机制自检，不能作为真实证据",
    )
    parser.add_argument("--language", default=None, help="识别语言（默认 zh 或 ECHOIC_REALTIME_ASR_LANGUAGE）")
    parser.add_argument("--frame-ms", type=int, default=DEFAULT_FRAME_MS, help="音频帧大小 ms（默认 100）")
    parser.add_argument("--flush-timeout", type=float, default=DEFAULT_FLUSH_TIMEOUT_S, help="finish 后等待尾部 final 的秒数")
    parser.add_argument(
        "--max-consecutive-start-failures",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_START_FAILURES,
        help="重连时连续启动失败上限（默认 3）",
    )
    parser.add_argument(
        "--max-total-reconnects",
        type=int,
        default=DEFAULT_MAX_TOTAL_RECONNECTS,
        help="整场 soak 允许的重连总次数上限（默认 20）",
    )
    parser.add_argument("--reconnect-backoff", type=float, default=1.0, help="首次重连退避秒数（默认 1，指数退避）")
    parser.add_argument("--max-backoff", type=float, default=30.0, help="重连退避上限秒数（默认 30）")
    parser.add_argument("--quiet", action="store_true", help="不打印人类摘要（仍写 JSON）")
    return parser.parse_args(argv)


def resolve_config(args: argparse.Namespace) -> tuple[str, str, str, str, str]:
    """Resolve (key, base_url, model, language, key_source) from CLI + env, like app config."""
    key_source = ""
    key = (args.key or "").strip()
    if key:
        key_source = "cli:--key"
    else:
        for env_name in (
            "ECHOIC_REALTIME_ASR_API_KEY",
            "ECHOIC_ASR_API_KEY",
            "DASHSCOPE_API_KEY",
        ):
            value = env_or_empty(env_name)
            if value:
                key = value
                key_source = f"env:{env_name}"
                break
    base_url = (args.base_url or "").strip() or env_or_empty("ECHOIC_REALTIME_ASR_BASE_URL") or DEFAULT_REALTIME_URL
    model = (args.model or "").strip() or env_or_empty("ECHOIC_REALTIME_ASR_MODEL") or DEFAULT_MODEL
    language = (
        (args.language or "").strip()
        or env_or_empty("ECHOIC_REALTIME_ASR_LANGUAGE")
        or env_or_empty("ECHOIC_ASR_LANGUAGE")
        or "zh"
    )
    return key, base_url, model, language, key_source


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def print_summary(report: dict[str, Any]) -> None:
    lines = report.get("summary_lines") or []
    print("\n".join(str(line) for line in lines))
    if report.get("provider_errors"):
        print("\nprovider 错误（前 5 条）：")
        for entry in report["provider_errors"][:5]:
            print(f"  - +{entry.get('offset_s')}s: {entry.get('message')}")
    if report.get("final_samples"):
        print("\n转写样本（前 5 条 final）：")
        for entry in report["final_samples"][:5]:
            print(f"  - +{entry.get('offset_s')}s: {entry.get('text')}")
    if report.get("warnings"):
        print("\n警告：")
        for warning in report["warnings"]:
            print(f"  - {warning}")
    if report.get("provider") == "mock":
        print("\n注意：本次为 --provider mock 机制自检，不代表真实 DashScope 长稳结果。")
    print(f"\nJSON 报告：{report.get('_report_path', '(not persisted)')}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.minutes <= 0:
        print("错误：--minutes 必须大于 0", file=sys.stderr)
        return EXIT_FATAL
    if args.frame_ms <= 0 or args.frame_ms > 5000:
        print("错误：--frame-ms 需在 1..5000 之间", file=sys.stderr)
        return EXIT_FATAL

    key, base_url, model, language, key_source = resolve_config(args)
    args.language = language
    if args.provider == "dashscope" and not key:
        print(
            "错误：未找到 DashScope API Key。请设置环境变量（推荐）：\n"
            "  export ECHOIC_REALTIME_ASR_API_KEY=sk-xxxx\n"
            "或使用 --key（注意会出现在进程参数/命令历史中），也可复用 "
            "ECHOIC_ASR_API_KEY / DASHSCOPE_API_KEY。\n"
            "无网络机制自检可加 --provider mock。",
            file=sys.stderr,
        )
        return EXIT_FATAL

    # ------------------------------------------------------------ prepare audio
    try:
        if args.audio is not None:
            pcm_path = decode_audio_to_pcm(args.audio)
            audio_label = str(args.audio)
            audio_synthetic = False
        else:
            pcm_path = make_synthetic_pcm(seconds=10)
            audio_label = "synthetic-voice-like-10s.pcm"
            audio_synthetic = True
        source = PcmAudioSource(
            pcm_path,
            label=audio_label,
            synthetic=audio_synthetic,
        )
    except RuntimeError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_FATAL

    key_source = key_source if args.provider == "dashscope" else "none(mock)"

    recorder = SoakRecorder(
        provider="dashscope_realtime" if args.provider == "dashscope" else "mock",
        model=model,
        base_url=base_url if args.provider == "dashscope" else "in-process",
        language=language,
        key_source=key_source,
        requested_minutes=args.minutes,
        frame_ms=args.frame_ms,
        audio_label=audio_label,
        audio_synthetic=audio_synthetic,
        audio_source_duration_s=source.duration_s,
    )
    if audio_synthetic:
        recorder.add_warning(
            "未提供 --audio，使用合成音频（非真实语音）：仅验证连接/传输/长稳，"
            "不代表真实语音识别质量；staging 60 分钟证据必须用真实音频。"
        )
    recorder.add_warning(
        "实时 ASR 按音频时长计费；60 分钟即约 60 分钟音频费用，请确认账号额度。"
    )

    report_path = args.out or (REPO_ROOT / "e2e" / f"soak-live-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json")

    # ------------------------------------------------------------ run the soak
    interrupted = False
    report: dict[str, Any] | None = None
    try:
        asyncio.run(
            orchestrate(
                args,
                recorder,
                source,
                key=key,
                base_url=base_url,
                model=model,
            )
        )
        if recorder.status == "running":
            recorder.status = "ok" if not recorder.provider_errors and recorder.drops == 0 else "degraded"
    except KeyboardInterrupt:
        interrupted = True
        recorder.status = "interrupted"
        recorder.add_warning("soak 被 Ctrl-C 中断，报告为部分结果")
        print("\n收到中断信号，正在写出部分报告…", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - always persist a failure report
        recorder.status = "failed"
        recorder.add_warning(f"未处理异常：{type(exc).__name__}: {exc}")
        print(f"错误：soak 异常终止：{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        source.close()
        recorder.audio_loops = source.loops
        # status precedence: failures > degraded > ok; interrupted keeps its status
        if not interrupted and recorder.status == "running":
            recorder.status = "ok" if not recorder.provider_errors and recorder.drops == 0 else "degraded"
        if recorder.status in ("ok", "degraded"):
            if not recorder.completed_planned_duration:
                recorder.status = "degraded"
                recorder.add_warning(
                    "未跑满计划时长（soak 未推进到 deadline），不能作为完整长稳证据"
                )
        recorder.sample_rss(force=True)
        recorder.finalise()
        report = recorder.build_report()
        try:
            write_report(report_path, report)
        except OSError as exc:
            print(f"错误：写入报告失败：{exc}", file=sys.stderr)
            report_path = None  # type: ignore[assignment]
        if report_path is not None:
            report["_report_path"] = str(report_path)
    if not args.quiet and report is not None:
        print_summary(report)
    return recorder.exit_code


if __name__ == "__main__":
    sys.exit(main())
