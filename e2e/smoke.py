#!/usr/bin/env python3
"""Echoic 端到端冒烟测试（生产式本地服务，mock 管线）。

覆盖：
  认证 / 未授权 → 健康检查 → 资料库与演示数据 → AI 分析结果 / 转录稿 →
  上传真实生成的音频并轮询处理完成 → 说话人改名 / 转录段编辑 → 转录导出（SRT/TXT/JSON）→
  全局搜索 → AI 问答 SSE 流（start/delta/done）与历史持久化 → 设置读取/局部更新 →
  标签 CRUD → 媒体 HTTP Range(206) → 重新生成洞察 → 删除清理 → 登出。

用法：
  # 先启动服务（生产式，见 scripts/run-local.sh），然后：
  backend/.venv/bin/python e2e/smoke.py
  backend/.venv/bin/python e2e/smoke.py --base-url http://127.0.0.1:8100

退出码：0 = 全部通过（允许 SKIP）；1 = 存在 FAIL。

环境变量：
  ECHOIC_E2E_BASE_URL       服务地址（默认 http://127.0.0.1:8100）
  ECHOIC_E2E_ADMIN_USERNAME 登录名（默认取 ECHOIC_ADMIN_USERNAME 或 admin）
  ECHOIC_E2E_ADMIN_PASSWORD 密码（默认取 ECHOIC_ADMIN_PASSWORD 或 admin123）
  ECHOIC_E2E_PROCESS_TIMEOUT 上传处理后等待上限秒数（默认 180）
  ECHOIC_E2E_LOG            日志文件（默认 e2e/last-run.log）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent

try:
    import httpx
except ImportError:  # pragma: no cover
    print("错误：未安装 httpx。请使用 backend/.venv/bin/python 运行本脚本。", file=sys.stderr)
    sys.exit(2)


class Tee:
    """同时写标准输出与日志文件。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self):
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass


def expect(condition, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Smoke:
    def __init__(self, base_url: str, username: str, password: str, process_timeout: float):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.process_timeout = process_timeout
        self.client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": "echoic-e2e-smoke/0.1"},
            trust_env=False,  # 本地验收不受 HTTP(S)_PROXY/ALL_PROXY 环境变量影响
        )
        self.results: list[dict] = []
        self.ctx: dict = {}
        self.tmpdir = tempfile.TemporaryDirectory(prefix="echoic-e2e-")
        self.tmp = Path(self.tmpdir.name)
        self.ffmpeg = shutil.which("ffmpeg")

    # ------------------------------------------------------------------ 结果记录
    def record(self, name: str, status: str, detail: str = "", duration: float = 0.0) -> None:
        self.results.append({"name": name, "status": status, "detail": detail, "duration": duration})
        color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(status, "")
        print(f"{color}[{status}]\033[0m {name} ({duration:.2f}s)")
        if detail:
            for line in str(detail).rstrip().splitlines():
                print(f"        {line}")

    def check(self, name: str, fn) -> bool:
        started = time.perf_counter()
        try:
            detail = fn() or ""
            self.record(name, "PASS", detail, time.perf_counter() - started)
            return True
        except AssertionError as exc:
            self.record(name, "FAIL", str(exc), time.perf_counter() - started)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=4)
            self.record(name, "FAIL", f"{type(exc).__name__}: {exc}\n{tb}", time.perf_counter() - started)
        return False

    def skip(self, name: str, reason: str) -> None:
        self.record(name, "SKIP", reason)

    def section(self, title: str) -> None:
        print(f"\n\033[1;36m━━━ {title} ━━━\033[0m")

    # ------------------------------------------------------------------ HTTP 辅助
    def _json(self, resp: httpx.Response, expected: int | tuple[int, ...] = 200) -> dict:
        expected_tuple = expected if isinstance(expected, tuple) else (expected,)
        if resp.status_code not in expected_tuple:
            raise AssertionError(
                f"HTTP {resp.status_code}（期望 {expected_tuple}）：{resp.request.method} "
                f"{resp.request.url} 响应={resp.text[:400]}"
            )
        try:
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(f"响应不是合法 JSON：{resp.text[:300]}") from exc

    def _wait_health(self, timeout: float = 60.0) -> dict:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                resp = self.client.get("/api/system/health")
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(1.0)
        raise AssertionError(f"服务 {self.base_url} 在 {timeout:.0f}s 内未就绪（最近错误：{last}）")

    def _poll_recording(self, recording_id: int, timeout: float) -> tuple[dict, list[str]]:
        stages: list[str] = []
        deadline = time.time() + timeout
        detail: dict = {}
        while time.time() < deadline:
            detail = self._json(self.client.get(f"/api/recordings/{recording_id}"))
            stage = str(detail.get("stage"))
            if not stages or stages[-1] != stage:
                stages.append(stage)
            if detail.get("status") in ("ready", "failed", "cancelled"):
                return detail, stages
            time.sleep(1.5)
        raise AssertionError(
            f"录音 {recording_id} 在 {timeout:.0f}s 内未处理完成：status={detail.get('status')} "
            f"stage={detail.get('stage')} progress={detail.get('progress')} "
            f"error={detail.get('error_message')} stages={stages}"
        )

    def make_audio(self, seconds: int = 25) -> Path:
        expect(self.ffmpeg, "未找到 ffmpeg，无法生成测试音频")
        out = self.tmp / f"e2e-tone-{seconds}s.wav"
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=shortest[a]",
            "-map", "[a]", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        expect(proc.returncode == 0, f"ffmpeg 生成音频失败：{proc.stderr[-500:]}")
        expect(out.exists() and out.stat().st_size > 10000, f"生成的音频文件异常：{out}")
        return out

    # ------------------------------------------------------------------ 检查项
    def t_health(self):
        data = self._wait_health()
        expect(data.get("status") == "ok", f"健康检查 status != ok：{data}")
        for key in ("db", "media", "ffmpeg"):
            expect(data.get(key) == "ok", f"健康检查 {key} != ok：{data.get(key)}")
        self.ctx["health"] = data
        return (
            f"version={data.get('version')} demo_mode={data.get('demo_mode')} "
            f"worker={data.get('worker')} db={data.get('db')} ffmpeg={data.get('ffmpeg')}"
        )

    def t_unauthorized(self):
        with httpx.Client(base_url=self.base_url, timeout=15.0, trust_env=False) as anon:
            resp = anon.get("/api/recordings")
            expect(resp.status_code == 401, f"未登录访问 /api/recordings 期望 401，实际 {resp.status_code}")
            resp2 = anon.get("/api/dashboard")
            expect(resp2.status_code == 401, f"未登录访问 /api/dashboard 期望 401，实际 {resp2.status_code}")
        return "未登录访问受保护接口返回 401"

    def t_login_reject(self):
        with httpx.Client(base_url=self.base_url, timeout=15.0, trust_env=False) as anon:
            resp = anon.post("/api/auth/login", json={"username": self.username, "password": "definitely-wrong"})
        expect(resp.status_code == 401, f"错误密码期望 401，实际 {resp.status_code}")
        return "错误密码返回 401"

    def t_login(self):
        resp = self.client.post("/api/auth/login", json={"username": self.username, "password": self.password})
        data = self._json(resp)
        expect(data.get("token"), f"登录响应缺少 token：{data}")
        user = data.get("user") or {}
        expect(user.get("username") == self.username, f"登录用户不匹配：{user}")
        expect(bool(user.get("is_admin")), f"管理员标记缺失：{user}")
        cookie_headers = ", ".join(resp.headers.get_list("set-cookie"))
        expect("echoic_token" in cookie_headers, f"Set-Cookie 缺少 echoic_token：{cookie_headers!r}")
        lowered = cookie_headers.lower()
        for flag in ("httponly", "samesite=lax", "path=/", "max-age=604800"):
            expect(flag in lowered, f"Cookie 缺少 {flag}：{cookie_headers!r}")
        self.ctx["token"] = data["token"]
        return f"user={user.get('display_name') or user.get('username')} cookie 标志齐全"

    def t_me(self):
        data = self._json(self.client.get("/api/auth/me"))
        user = data.get("user") or {}
        expect(user.get("username") == self.username, f"/auth/me 返回异常：{data}")
        return f"cookie 会话有效：{user.get('username')}"

    def t_bearer(self):
        token = self.ctx.get("token")
        expect(token, "缺少登录 token")
        with httpx.Client(
            base_url=self.base_url, timeout=20.0, trust_env=False, headers={"Authorization": f"Bearer {token}"}
        ) as bearer:
            data = self._json(bearer.get("/api/auth/me"))
        expect((data.get("user") or {}).get("username") == self.username, f"Bearer 鉴权失败：{data}")
        return "Authorization: Bearer <jwt> 鉴权有效"

    def t_system_info(self):
        data = self._json(self.client.get("/api/system/info"))
        for key in ("version", "data_dir", "recordings", "segments", "chunks", "worker"):
            expect(key in data, f"/api/system/info 缺少字段 {key}：{data}")
        self.ctx["chunks_before"] = int(data.get("chunks") or 0)
        self.ctx["recordings_before"] = int(data.get("recordings") or 0)
        return (
            f"recordings={data.get('recordings')} segments={data.get('segments')} "
            f"chunks={data.get('chunks')} worker={data.get('worker')}"
        )

    def t_dashboard(self):
        data = self._json(self.client.get("/api/dashboard"))
        stats = data.get("stats") or {}
        expect(int(stats.get("total") or 0) >= 1, f"仪表盘 stats.total 应 >=1：{stats}")
        expect(isinstance(data.get("recent"), list) and data["recent"], "仪表盘 recent 为空")
        expect("storage" in data, "仪表盘缺少 storage 字段")
        return (
            f"total={stats.get('total')} ready={stats.get('ready')} processing={stats.get('processing')} "
            f"hours={stats.get('hours')}"
        )

    def t_library_and_demo(self):
        data = self._json(self.client.get("/api/recordings", params={"page": 1, "page_size": 50}))
        items = data.get("items") or []
        if not items:
            # 空库时主动 seed 演示数据（管理员接口，幂等）
            self._json(self.client.post("/api/system/seed-demo"))
            data = self._json(self.client.get("/api/recordings", params={"page": 1, "page_size": 50}))
            items = data.get("items") or []
        expect(items, f"资料库为空（已尝试 seed-demo）：{data}")
        expect(all(k in data for k in ("items", "total", "page", "page_size", "stats")), f"列表响应缺字段：{data.keys()}")
        demo = next((item for item in items if "TraceOpt" in str(item.get("title", ""))), None)
        if demo is None:
            demo = next((item for item in items if item.get("status") == "ready"), items[0])
        self.ctx["demo_id"] = demo["id"]
        self.ctx["demo_title"] = demo.get("title")
        expect(demo.get("status") == "ready", f"演示录音状态应为 ready：{demo.get('status')}")
        stats = data["stats"]
        expect(int(stats.get("total") or 0) >= 1, f"stats.total 异常：{stats}")
        return (
            f"total={data['total']} demo_id={demo['id']} title={demo.get('title')!r} "
            f"demo_status={demo.get('status')}"
        )

    def t_recording_detail(self):
        rid = self.ctx.get("demo_id")
        expect(rid, "资料库中不存在演示录音（TraceOpt），无法校验详情（请确认 ECHOIC_DEMO_SEED=true 且演示数据已写入）")
        data = self._json(self.client.get(f"/api/recordings/{rid}"))
        expect(data.get("status") == "ready", f"演示录音状态 {data.get('status')} != ready")
        expect(data.get("stage") == "done", f"演示录音 stage {data.get('stage')} != done")
        expect(data.get("progress") == 100, f"演示录音 progress {data.get('progress')} != 100")
        expect(int(data.get("duration_ms") or 0) > 0, "演示录音 duration_ms <= 0")
        expect(bool(data.get("has_transcript")), "演示录音 has_transcript 为 false")
        expect(len(data.get("insight_kinds") or []) >= 3, f"演示录音 insight_kinds 不足：{data.get('insight_kinds')}")
        expect("job" in data, "详情响应缺少 job 字段")
        expect("chat_message_count" in data, "详情响应缺少 chat_message_count")
        return (
            f"id={rid} duration_ms={data.get('duration_ms')} insights={data.get('insight_kinds')} "
            f"job={data.get('job')}"
        )

    def t_insights(self):
        rid = self.ctx.get("demo_id")
        expect(rid, "资料库中不存在演示录音（TraceOpt），无法校验 AI 分析结果")
        data = self._json(self.client.get(f"/api/recordings/{rid}/insights"))
        expect(data.get("summary") and str(data["summary"].get("markdown", "")).strip(), "insights.summary 为空")
        expect(isinstance(data.get("chapters"), list) and data["chapters"], "insights.chapters 为空")
        expect(data.get("viewpoint_tree") and data["viewpoint_tree"].get("root"), "insights.viewpoint_tree 为空")
        expect(isinstance(data.get("insights"), list) and data["insights"], "insights.insights 为空")
        expect(isinstance(data.get("suggested_questions"), list) and data["suggested_questions"], "suggested_questions 为空")
        expect(isinstance(data.get("missing"), list), "insights.missing 字段缺失")
        expect(data.get("generated_at"), "insights.generated_at 为空")
        return (
            f"chapters={len(data['chapters'])} insights={len(data['insights'])} "
            f"questions={len(data['suggested_questions'])} missing={data.get('missing')} "
            f"model={data.get('model')}"
        )

    def t_transcript(self):
        rid = self.ctx.get("demo_id")
        expect(rid, "资料库中不存在演示录音（TraceOpt），无法校验转录稿")
        data = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        speakers = data.get("speakers") or []
        segments = data.get("segments") or []
        expect(speakers, "转录 speakers 为空")
        expect(len(segments) >= 5, f"转录 segments 过少：{len(segments)}")
        first = segments[0]
        for key in ("id", "idx", "speaker_id", "start_ms", "end_ms", "text"):
            expect(key in first, f"segment 缺少字段 {key}：{first}")
        expect(str(first.get("text", "")).strip(), "segment 文本为空")
        expect(int(first.get("end_ms") or 0) >= int(first.get("start_ms") or 0), "segment 时间区间异常")
        return f"speakers={len(speakers)} segments={len(segments)} language={data.get('language')}"

    def t_upload_invalid_media(self):
        files = {"file": ("not-audio.txt", b"this is not audio", "text/plain")}
        resp = self.client.post("/api/recordings/upload", files=files, data={"title": "invalid"})
        expect(resp.status_code == 415, f"上传 .txt 期望 415，实际 {resp.status_code}：{resp.text[:200]}")
        return "不支持的扩展名返回 415"

    def t_upload(self):
        expect(self.ffmpeg, "未找到 ffmpeg，跳过真实音频上传")
        audio = self.make_audio(seconds=25)
        title = f"E2E 冒烟测试 {time.strftime('%H%M%S')}"
        self.ctx["upload_title"] = title
        with audio.open("rb") as fh:
            files = {"file": (audio.name, fh, "audio/wav")}
            data = {
                "title": title,
                "description": "e2e/smoke.py 自动生成的双音正弦波测试音频",
                "language": "zh",
                "source": "upload",
            }
            resp = self.client.post("/api/recordings/upload", files=files, data=data, timeout=180.0)
        rec = self._json(resp, expected=201)
        expect(rec.get("id"), f"上传响应缺少 id：{rec}")
        expect(rec.get("status") in ("queued", "uploaded", "processing"), f"上传后状态异常：{rec.get('status')}")
        self.ctx["rid"] = rec["id"]
        return f"id={rec['id']} status={rec.get('status')} size={rec.get('size_bytes')} 文件={audio.name}"

    def t_process_ready(self):
        rid = self.ctx.get("rid")
        expect(rid, "上传未成功，缺少录音 id")
        started = time.time()
        detail, stages = self._poll_recording(rid, self.process_timeout)
        elapsed = time.time() - started
        expect(detail.get("status") == "ready", f"处理后状态 {detail.get('status')} != ready，错误：{detail.get('error_message')}")
        expect(detail.get("stage") == "done", f"处理后 stage {detail.get('stage')} != done")
        expect(detail.get("progress") == 100, f"处理后 progress {detail.get('progress')} != 100")
        expect(detail.get("has_transcript"), "处理后 has_transcript 为 false")
        self.ctx["stages"] = stages
        return f"耗时 {elapsed:.1f}s；状态阶段序列={stages}"

    def t_pipeline_artifacts(self):
        rid = self.ctx["rid"]
        transcript = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        segments = transcript.get("segments") or []
        expect(segments, "上传录音处理完成后没有转录分段")
        expect(transcript.get("speakers"), "上传录音处理完成后没有说话人")

        insights = self._json(self.client.get(f"/api/recordings/{rid}/insights"))
        kinds = [k for k in ("summary", "chapters", "viewpoint_tree", "insights", "action_items", "keywords") if insights.get(k)]
        expect(insights.get("summary"), f"上传录音缺少 summary 分析：missing={insights.get('missing')}")
        expect(len(kinds) >= 2, f"上传录音 AI 分析种类过少：{kinds}，missing={insights.get('missing')}")

        info = self._json(self.client.get("/api/system/info"))
        chunks_before = int(self.ctx.get("chunks_before") or 0)
        chunks_after = int(info.get("chunks") or 0)
        expect(chunks_after > chunks_before, f"RAG chunks 未增加（before={chunks_before}, after={chunks_after}）")
        self.ctx["chunks_after"] = chunks_after
        return (
            f"segments={len(segments)} speakers={len(transcript['speakers'])} "
            f"analysis_kinds={kinds} chunks={chunks_before}->{chunks_after}"
        )

    def t_speaker_rename(self):
        rid = self.ctx["rid"]
        transcript = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        speaker = transcript["speakers"][0]
        new_name = "E2E说话人"
        data = self._json(self.client.patch(f"/api/recordings/{rid}/speakers/{speaker['id']}", json={"display_name": new_name}))
        expect(data.get("display_name") == new_name, f"说话人改名未生效：{data}")
        transcript2 = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        renamed = next((s for s in transcript2["speakers"] if s["id"] == speaker["id"]), None)
        expect(renamed and renamed.get("display_name") == new_name, f"转录接口中说话人名称未更新：{renamed}")
        self.ctx["speaker_id"] = speaker["id"]
        return f"speaker_id={speaker['id']} display_name -> {new_name}"

    def t_segment_edit(self):
        rid = self.ctx["rid"]
        transcript = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        segment = transcript["segments"][0]
        new_text = "【E2E已编辑】" + str(segment.get("text", ""))[:30]
        data = self._json(self.client.patch(f"/api/recordings/{rid}/segments/{segment['id']}", json={"text": new_text}))
        expect(data.get("text") == new_text, f"分段文本编辑未生效：{data}")
        transcript2 = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        updated = next((s for s in transcript2["segments"] if s["id"] == segment["id"]), None)
        expect(updated and updated.get("text") == new_text, "重新获取转录后编辑文本不一致")
        self.ctx["edited_text"] = new_text
        return f"segment_id={segment['id']} 文本已更新（{new_text[:24]}…）"

    def t_export(self):
        rid = self.ctx["rid"]
        results = {}
        for fmt in ("srt", "txt", "json", "md"):
            resp = self.client.get(f"/api/recordings/{rid}/transcript/export", params={"format": fmt})
            expect(resp.status_code == 200, f"导出 {fmt} 失败：HTTP {resp.status_code} {resp.text[:200]}")
            disposition = resp.headers.get("content-disposition", "")
            expect("attachment" in disposition.lower(), f"导出 {fmt} 缺少 Content-Disposition attachment：{disposition!r}")
            expect("filename" in disposition.lower(), f"导出 {fmt} 缺少文件名：{disposition!r}")
            body = resp.text
            expect(len(body.strip()) > 10, f"导出 {fmt} 内容为空")
            if fmt == "srt":
                expect("-->" in body, "SRT 内容缺少时间轴 -->")
                expect(re.search(r"\d{2}:\d{2}:\d{2},\d{3}", body), "SRT 时间格式不符合预期")
            elif fmt == "txt":
                expect(re.search(r"\[\d{2}:\d{2}:\d{2}\]", body), "TXT 内容缺少 [hh:mm:ss] 时间戳")
                expect("：" in body or ":" in body, "TXT 内容缺少说话人分隔符")
            elif fmt == "json":
                payload = json.loads(body)
                expect(payload.get("segments"), "JSON 导出缺少 segments")
            results[fmt] = len(body)
        return "格式全部可导出：" + ", ".join(f"{k}={v}B" for k, v in results.items())

    def t_search(self):
        rid = self.ctx["rid"]
        edited = self.ctx.get("edited_text") or ""
        keyword = edited.replace("【E2E已编辑】", "")[:12] or self.ctx.get("upload_title") or "E2E"
        data = self._json(self.client.get("/api/search", params={"q": keyword, "limit": 20}))
        expect(data.get("query") == keyword, f"搜索回显 query 不一致：{data.get('query')}")
        hits = data.get("segments") or []
        expect(hits, f"搜索 {keyword!r} 无转录片段命中：{str(data)[:300]}")
        target = [h for h in hits if int(h.get("recording_id") or 0) == int(rid)]
        expect(target, f"搜索未命中本次上传录音（rid={rid}）：{[(h.get('recording_id'), h.get('snippet')) for h in hits][:5]}")
        hit = target[0]
        for key in ("recording_id", "segment_id", "speaker", "start_ms", "snippet"):
            expect(key in hit, f"搜索片段缺少字段 {key}：{hit}")
        # 标题搜索命中（演示数据存在时校验）
        title_hits = 0
        if self.ctx.get("demo_id"):
            demo_hits = self._json(self.client.get("/api/search", params={"q": "TraceOpt", "limit": 20}))
            expect(demo_hits.get("recordings"), "标题搜索 'TraceOpt' 无录音命中")
            title_hits = len(demo_hits["recordings"])
        return f"片段命中 {len(hits)} 条（本次录音 {len(target)} 条），标题命中 {title_hits} 条"

    def t_chat_sse(self):
        rid = self.ctx.get("demo_id") or self.ctx.get("rid")
        expect(rid, "无可用录音（演示录音与上传录音均缺失），无法测试问答 SSE")
        self._json(self.client.delete(f"/api/recordings/{rid}/chat"))  # 清空历史，保证断言干净
        events: list[dict] = []
        with self.client.stream(
            "POST",
            f"/api/recordings/{rid}/chat",
            json={"message": "这次会议确定了哪些交付节点？"},
            timeout=httpx.Timeout(180.0, connect=10.0),
        ) as resp:
            # 注意：流式响应在读取前不能访问 resp.text（会抛 ResponseNotRead），仅用状态码与响应头断言
            status = resp.status_code
            if status != 200:
                body = resp.read().decode("utf-8", "replace")[:300]
                raise AssertionError(f"问答 SSE 期望 200，实际 {status}：{body}")
            content_type = resp.headers.get("content-type", "")
            expect("text/event-stream" in content_type, f"Content-Type 不是 event-stream：{content_type}")
            expect(resp.headers.get("x-accel-buffering") == "no", f"缺少 X-Accel-Buffering: no（实际 {resp.headers.get('x-accel-buffering')!r}）")
            expect(resp.headers.get("cache-control") and "no-cache" in resp.headers.get("cache-control", ""), "缺少 Cache-Control: no-cache")
            for raw_line in resp.iter_lines():
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload or payload == "[DONE]":
                    continue
                event = json.loads(payload)
                events.append(event)
                if event.get("type") in ("done", "error"):
                    break
        types = [event.get("type") for event in events]
        expect("error" not in types, f"问答流返回 error 事件：{[e for e in events if e.get('type') == 'error']}")
        expect("start" in types, f"缺少 start 事件，收到：{types}")
        expect("delta" in types, f"缺少 delta 事件，收到：{types}")
        expect("done" in types, f"缺少 done 事件，收到：{types}")
        answer = "".join(str(event.get("text", "")) for event in events if event.get("type") == "delta")
        expect(answer.strip(), "问答流 delta 文本为空")
        self.ctx["chat_answer"] = answer[:120]
        return f"事件={types[:8]}{'…' if len(types) > 8 else ''} 回答长度={len(answer)} 预览={answer[:40]!r}"

    def t_chat_history(self):
        rid = self.ctx.get("demo_id") or self.ctx.get("rid")
        expect(rid, "无可用录音（演示录音与上传录音均缺失），无法测试问答历史")
        data = self._json(self.client.get(f"/api/recordings/{rid}/chat"))
        messages = data.get("messages") or []
        expect(len(messages) >= 2, f"问答历史消息不足：{len(messages)}")
        roles = [m.get("role") for m in messages]
        expect(roles[-1] == "assistant", f"最后一条消息角色应为 assistant：{roles}")
        assistant = messages[-1]
        expect(str(assistant.get("content", "")).strip(), "assistant 消息内容为空")
        expect(isinstance(assistant.get("citations"), list), "assistant 消息缺少 citations")
        return f"历史消息 {len(messages)} 条，最后回答长度={len(str(assistant.get('content', '')))}，引用={len(assistant.get('citations') or [])}"

    def t_settings_get(self):
        resp = self.client.get("/api/settings")
        data = self._json(resp)
        for key in ("ai", "prompts", "system"):
            expect(key in data, f"/api/settings 缺少 {key}：{str(data)[:300]}")
        ai = data["ai"]
        for kind in ("asr", "llm", "embeddings"):
            expect(kind in ai, f"设置缺少 ai.{kind}")
            expect("provider" in ai[kind], f"设置 ai.{kind} 缺少 provider")
            expect("has_api_key" in ai[kind] and "api_key_masked" in ai[kind], f"设置 ai.{kind} 缺少掩码字段")
            expect("api_key" not in ai[kind], f"设置 ai.{kind} 暴露了明文 api_key")
        system = data["system"]
        expect("version" in system and "worker" in system, "设置 system 缺少 version/worker")
        self.ctx["settings"] = data
        worker = system.get("worker") or {}
        return (
            f"asr={ai['asr']['provider']} llm={ai['llm']['provider']} emb={ai['embeddings']['provider']} "
            f"demo_mode={system.get('demo_mode')} worker={worker}"
        )

    def t_settings_put(self):
        original = (self.ctx.get("settings") or {}).get("ai", {}).get("llm", {}).get("temperature", 0.3)
        payload = {
            "ai": {"llm": {"temperature": 0.25}},
            "system": {"auto_process": True, "default_language": "zh"},
        }
        data = self._json(self.client.put("/api/settings", json=payload))
        expect(abs(float(data["ai"]["llm"]["temperature"]) - 0.25) < 1e-6, f"temperature 更新失败：{data['ai']['llm']}")
        expect(data["system"].get("default_language") == "zh", f"default_language 更新失败：{data['system']}")
        # 恢复默认，避免影响后续 UI 截图
        restore = {"ai": {"llm": {"temperature": float(original)}}}
        restored = self._json(self.client.put("/api/settings", json=restore))
        expect(abs(float(restored["ai"]["llm"]["temperature"]) - float(original)) < 1e-6, "temperature 恢复失败")
        return f"局部合并 PUT 有效（temperature {original} -> 0.25 -> {original}）"

    def t_prompts_reset(self):
        data = self._json(self.client.post("/api/settings/prompts/reset"))
        prompts = data.get("prompts") if "prompts" in data else data
        expect(isinstance(prompts, dict) and prompts, f"重置提示词响应异常：{str(data)[:300]}")
        expect(len(prompts) >= 4, f"默认提示词数量过少：{list(prompts)[:8]}")
        return f"默认提示词恢复，项数={len(prompts)}"

    def t_tags_crud(self):
        stamp = time.strftime("%H%M%S")
        name = f"E2E标签{stamp}"
        created = self._json(self.client.post("/api/tags", json={"name": name, "color": "#1677ff"}), expected=201)
        tag_id = created.get("id")
        expect(tag_id, f"创建标签响应缺少 id：{created}")
        dup = self.client.post("/api/tags", json={"name": name, "color": "#1677ff"})
        expect(dup.status_code == 409, f"重名标签期望 409，实际 {dup.status_code}")
        tags = self._json(self.client.get("/api/tags"))
        expect(any(t.get("id") == tag_id for t in tags.get("items", [])), "标签列表未包含新建标签")
        renamed = self._json(self.client.patch(f"/api/tags/{tag_id}", json={"name": name + "改"}))
        expect(renamed.get("name") == name + "改", f"标签改名失败：{renamed}")
        self._json(self.client.delete(f"/api/tags/{tag_id}"))
        tags2 = self._json(self.client.get("/api/tags"))
        expect(not any(t.get("id") == tag_id for t in tags2.get("items", [])), "标签删除后仍存在")
        return f"CRUD + 重名 409 正常（tag_id={tag_id}）"

    def t_media_range(self):
        rid = self.ctx["rid"]
        resp = self.client.get(f"/api/recordings/{rid}/media", headers={"Range": "bytes=0-1023"})
        expect(resp.status_code == 206, f"Range 请求期望 206，实际 {resp.status_code}：{resp.text[:200]}")
        content_range = resp.headers.get("content-range", "")
        expect(content_range.startswith("bytes 0-1023/"), f"Content-Range 异常：{content_range!r}")
        expect(len(resp.content) == 1024, f"Range 返回字节数 {len(resp.content)} != 1024")
        download = self.client.get(f"/api/recordings/{rid}/media", params={"download": 1})
        expect(download.status_code == 200, f"下载媒体期望 200，实际 {download.status_code}")
        disposition = download.headers.get("content-disposition", "")
        expect("attachment" in disposition.lower(), f"?download=1 缺少 attachment：{disposition!r}")
        with httpx.Client(base_url=self.base_url, timeout=15.0, trust_env=False) as anon:
            anon_resp = anon.get(f"/api/recordings/{rid}/media")
            expect(anon_resp.status_code == 401, f"未登录访问媒体期望 401，实际 {anon_resp.status_code}")
        return f"206 Partial Content，Content-Range={content_range}，类型={resp.headers.get('content-type')}"

    def t_reprocess(self):
        rid = self.ctx["rid"]
        queued = self._json(self.client.post(f"/api/recordings/{rid}/reprocess", json={"from": "insights"}))
        expect(queued.get("ok") is True, f"重处理响应 ok != true：{queued}")
        detail, stages = self._poll_recording(rid, self.process_timeout)
        expect(detail.get("status") == "ready", f"重处理后状态 {detail.get('status')} != ready")
        insights = self._json(self.client.get(f"/api/recordings/{rid}/insights"))
        expect(insights.get("summary"), f"重处理后 insights.summary 为空：missing={insights.get('missing')}")
        expect(insights.get("chapters"), "重处理后 insights.chapters 为空")
        return f"from=insights 重处理完成，阶段={stages}，summary/chapters 仍在"

    def t_cleanup(self):
        rid = self.ctx.get("rid")
        expect(rid, "缺少待清理录音 id")
        self._json(self.client.delete(f"/api/recordings/{rid}"))
        resp = self.client.get(f"/api/recordings/{rid}")
        expect(resp.status_code == 404, f"删除后详情期望 404，实际 {resp.status_code}")
        self.ctx["cleaned"] = True
        return f"测试录音 {rid} 已删除（DB + 媒体目录）"

    def t_logout(self):
        data = self._json(self.client.post("/api/auth/logout"))
        expect(data.get("ok") is True, f"登出响应异常：{data}")
        me = self.client.get("/api/auth/me")
        expect(me.status_code == 401, f"登出后 /api/auth/me 期望 401，实际 {me.status_code}")
        return "登出并清除会话 Cookie"

    # ------------------------------------------------------------------ 主流程
    def run(self) -> int:
        print(f"Echoic E2E Smoke — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"目标服务: {self.base_url}")
        print(f"Python: {sys.version.split()[0]} | httpx: {httpx.__version__} | ffmpeg: {self.ffmpeg or '缺失'}")
        print(f"临时目录: {self.tmp}")
        print("=" * 78)

        self.section("0. 环境与健康检查")
        if not self.check("health", self.t_health):
            print("\n\033[1;31m服务不可用，终止执行。\033[0m")
            return 1

        self.section("1. 鉴权")
        self.check("auth.unauthorized", self.t_unauthorized)
        self.check("auth.login-reject-bad-password", self.t_login_reject)
        self.check("auth.login", self.t_login)
        self.check("auth.me", self.t_me)
        self.check("auth.bearer", self.t_bearer)

        self.section("2. 系统与资料库")
        self.check("system.info", self.t_system_info)
        self.check("dashboard", self.t_dashboard)
        self.check("library.demo-recording", self.t_library_and_demo)
        self.check("library.recording-detail", self.t_recording_detail)
        self.check("insights.demo", self.t_insights)
        self.check("transcript.demo", self.t_transcript)

        self.section("3. 上传与 mock 管线处理")
        if not self.ffmpeg:
            self.skip("upload", "ffmpeg 不可用，无法生成测试音频")
            self.skip("pipeline.process-ready", "缺少上传录音")
            self.skip("pipeline.artifacts", "缺少上传录音")
        else:
            self.check("upload.invalid-media-415", self.t_upload_invalid_media)
            if self.check("upload.audio", self.t_upload):
                self.check("pipeline.process-ready", self.t_process_ready)
                self.check("pipeline.artifacts", self.t_pipeline_artifacts)

        self.section("4. 转录编辑与导出")
        if self.ctx.get("rid"):
            self.check("transcript.speaker-rename", self.t_speaker_rename)
            self.check("transcript.segment-edit", self.t_segment_edit)
            self.check("transcript.export", self.t_export)
            self.check("search.global", self.t_search)
        else:
            for name in ("transcript.speaker-rename", "transcript.segment-edit", "transcript.export", "search.global"):
                self.skip(name, "缺少上传录音")

        self.section("5. AI 问答（SSE 流式）")
        self.check("chat.sse-stream", self.t_chat_sse)
        self.check("chat.history", self.t_chat_history)

        self.section("6. 设置")
        self.check("settings.get", self.t_settings_get)
        self.check("settings.put-partial", self.t_settings_put)
        self.check("settings.prompts-reset", self.t_prompts_reset)

        self.section("7. 标签与媒体 Range")
        self.check("tags.crud", self.t_tags_crud)
        if self.ctx.get("rid"):
            self.check("media.range-206", self.t_media_range)
        else:
            self.skip("media.range-206", "缺少上传录音")
        if self.ctx.get("rid"):
            self.check("pipeline.reprocess-insights", self.t_reprocess)
        else:
            self.skip("pipeline.reprocess-insights", "缺少上传录音")

        self.section("8. 清理与登出")
        if self.ctx.get("rid"):
            self.check("recordings.delete", self.t_cleanup)
        self.check("auth.logout", self.t_logout)

        # -------------------------------------------------------------- 汇总
        total = len(self.results)
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        failed = [r for r in self.results if r["status"] == "FAIL"]
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        print("\n" + "=" * 78)
        print("\033[1mPASS/FAIL 矩阵\033[0m")
        print("-" * 78)
        for row in self.results:
            color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(row["status"], "")
            print(f"{color}{row['status']:<4}\033[0m {row['name']:<38} {row['duration']:6.2f}s  {row['detail'].splitlines()[0] if row['detail'] else ''}")
        print("-" * 78)
        print(f"总计 {total} 项：\033[1;32m{passed} 通过\033[0m，\033[1;31m{len(failed)} 失败\033[0m，{skipped} 跳过")
        if failed:
            print("\n失败详情：")
            for row in failed:
                print(f"\n\033[1;31m✗ {row['name']}\033[0m")
                print(row["detail"])
            print("\n\033[1;31mE2E SMOKE: FAIL\033[0m")
            return 1
        print("\n\033[1;32mE2E SMOKE: PASS ✔\033[0m")
        return 0

    def close(self):
        try:
            self.client.close()
        finally:
            self.tmpdir.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description="Echoic 端到端冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ECHOIC_E2E_BASE_URL") or os.environ.get("ECHOIC_BASE_URL") or "http://127.0.0.1:8100",
    )
    parser.add_argument("--username", default=os.environ.get("ECHOIC_E2E_ADMIN_USERNAME") or os.environ.get("ECHOIC_ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or os.environ.get("ECHOIC_ADMIN_PASSWORD") or "admin123")
    parser.add_argument(
        "--process-timeout",
        type=float,
        default=float(os.environ.get("ECHOIC_E2E_PROCESS_TIMEOUT") or 180),
        help="上传录音等待处理完成的上限秒数（默认 180）",
    )
    parser.add_argument("--log", default=os.environ.get("ECHOIC_E2E_LOG") or str(E2E_DIR / "last-run.log"))
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    stdout_orig, stderr_orig = sys.stdout, sys.stderr
    sys.stdout = Tee(stdout_orig, log_fh)
    sys.stderr = Tee(stderr_orig, log_fh)
    smoke = None
    try:
        smoke = Smoke(args.base_url, args.username, args.password, args.process_timeout)
        code = smoke.run()
    finally:
        if smoke is not None:
            smoke.close()
        print(f"\n日志已保存：{log_path}")
        sys.stdout, sys.stderr = stdout_orig, stderr_orig
        log_fh.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
