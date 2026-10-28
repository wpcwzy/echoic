#!/usr/bin/env python3
"""Echoic v0.3.10 端到端冒烟：sqlite-vec / 实时 GA / 注册+资料 / 描述自动生成。

在独立数据目录 + mock AI 管线的生产式服务上运行（task-24）：
    ECHOIC_DATA_DIR=$PWD/.cache/e2e-v0310-data \\
    ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \\
    ECHOIC_ALLOW_REGISTRATION=true \\
    backend/.venv/bin/python e2e/smoke_v0310.py --base-url http://127.0.0.1:8100 \\
        --data-dir $PWD/.cache/e2e-v0310-data

覆盖：
  0) health/features：version 存在、storage ok、allow_registration；live 默认/显式启用；vector 有效后端
  1) 注册：allow_registration 开关（health 同步）、注册 201 member、重复 409、弱密码 422、登录
  2) 个人资料：PATCH /api/auth/me（昵称/邮箱）、特权字段被忽略、改密校验（旧密码错/新密码过短/成功）
  3) sqlite-vec：状态/后端/降级信息、mock embeddings + reindex、semantic/hybrid 搜索
  4) 实时 GA：创建会话 → WS ready（resumed/base_ms/total_ms/connection）→ ping/pong → 音频 →
     stop → 会话 ended 且片段持久化（深度断线重连见 e2e/live_reconnect_check.mjs）
  5) 描述自动生成：空描述上传→ready 后自动生成 ≤120 字；用户描述不被覆盖；demo 描述不变；
     backfill CLI dry-run/--apply 行为（需 --data-dir）

退出码：0 = 全部通过（允许 SKIP）；1 = 存在 FAIL。日志默认 e2e/last-run-v0310.log。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(E2E_DIR))

from smoke import Smoke, expect  # noqa: E402

DESCRIPTION_MAX = 120


class V0310Smoke(Smoke):
    def __init__(self, base_url: str, username: str, password: str, process_timeout: float,
                 data_dir: Path | None, expect_vector_backend: str):
        super().__init__(base_url, username, password, process_timeout)
        self.data_dir = data_dir
        self.expect_vector_backend = expect_vector_backend
        self.clients: list[httpx.Client] = []
        self.stamp = time.strftime("%H%M%S")

    # ------------------------------------------------------------------ helpers
    def _client(self) -> httpx.Client:
        client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(120.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        )
        self.clients.append(client)
        return client

    def _login_client(self, username: str, password: str) -> httpx.Client:
        client = self._client()
        data = self._json(client.post("/api/auth/login", json={"username": username, "password": password}))
        expect((data.get("user") or {}).get("username") == username, f"登录用户不匹配：{data.get('user')}")
        return client

    def _upload_wav(self, client: httpx.Client, title: str, seconds: int = 8, description: str | None = None) -> dict:
        audio = self.make_audio(seconds=seconds)
        data = {"title": title, "language": "zh"}
        if description is not None:
            data["description"] = description
        with audio.open("rb") as fh:
            resp = client.post(
                "/api/recordings/upload",
                files={"file": (audio.name, fh, "audio/wav")},
                data=data,
                timeout=180.0,
            )
        return self._json(resp, expected=201)

    def _poll_ready(self, client: httpx.Client, recording_id: int, timeout: float | None = None) -> dict:
        timeout = timeout or self.process_timeout
        deadline = time.time() + timeout
        detail: dict = {}
        while time.time() < deadline:
            detail = self._json(client.get(f"/api/recordings/{recording_id}"))
            if detail.get("status") in ("ready", "failed", "cancelled"):
                break
            time.sleep(1.0)
        expect(detail.get("status") == "ready",
               f"录音 {recording_id} 未就绪：status={detail.get('status')} stage={detail.get('stage')} "
               f"error={detail.get('error_message')}")
        return detail

    def _set_allow_registration(self, value: bool) -> dict:
        return self._json(self.client.put("/api/settings", json={"system": {"allow_registration": value}}))

    def _health(self, client: httpx.Client | None = None) -> dict:
        return self._json((client or self.client).get("/api/system/health"))

    # ================================================================== 0
    def section_env(self) -> None:
        self.section("0. health / features（v0.3.10）")
        if not self.check("v0310.health", self.t_health):
            raise RuntimeError("服务不可用")
        self.check("v0310.auth.login-admin", self.t_login)
        self.check("v0310.features", self.t_features)

    def t_health(self):
        data = self._wait_health(timeout=60)
        expect(data.get("status") == "ok", f"health 应为 ok：{data}")
        # v1.0 起版本号随发布升级（本次 QA 为 1.0.0）；不再硬编码 0.3.10，只要求 version 存在
        expect(str(data.get("version") or "").strip(), f"health 缺少 version：{data}")
        expect(data.get("storage") == "ok", f"storage 状态异常：{data.get('storage')}")
        expect(data.get("storage_backend") == "local", f"storage_backend 应为 local：{data.get('storage_backend')}")
        self.ctx["health"] = data
        return f"version={data.get('version')} storage={data.get('storage')}/{data.get('storage_backend')} allow_registration={data.get('allow_registration')}"

    def t_features(self):
        data = self._json(self.client.get("/api/system/features"))
        for key in ("live", "vector", "storage", "trash"):
            expect(key in data, f"features 缺少 {key}：{data}")
        live = data["live"]
        expect(live.get("enabled") is True, f"live.enabled 应为 true：{live}")
        expect(live.get("provider") == "mock", f"live.provider 应为 mock：{live}")
        expect(int(live.get("max_minutes") or 0) > 0, f"live.max_minutes 异常：{live}")
        vector = data["vector"]
        expect(vector.get("backend") == self.expect_vector_backend,
               f"vector.backend 期望 {self.expect_vector_backend}，实际 {vector.get('backend')}（features 应报告有效后端）")
        expect(vector.get("search_enabled") is True, f"vector.search_enabled 异常：{vector}")
        expect(data["storage"].get("backend") == "local", f"storage 异常：{data['storage']}")
        expect(int((data.get("trash") or {}).get("retention_days") or 0) >= 1, f"trash 异常：{data.get('trash')}")
        self.ctx["features"] = data
        return f"live={live} vector={vector} storage={data['storage']}"

    # ================================================================== 1
    def section_register(self) -> None:
        self.section("1. 注册开关 / 注册 / 登录")
        self.check("v0310.register.flag-toggle", self.t_register_flag_toggle)
        self.check("v0310.register.create", self.t_register_create)
        self.check("v0310.register.duplicate-409", self.t_register_duplicate)
        self.check("v0310.register.weak-password-422", self.t_register_weak)
        self.check("v0310.register.login", self.t_register_login)

    def t_register_flag_toggle(self):
        # 注册开关是运行时设置（不依赖服务环境变量），这里显式开启后开始测试
        self._set_allow_registration(True)
        health = self._health()
        expect(health.get("allow_registration") is True, f"初始 health.allow_registration 应为 true：{health.get('allow_registration')}")
        settings = self._json(self.client.get("/api/settings"))
        expect((settings.get("system") or {}).get("allow_registration") is True,
               f"settings.system.allow_registration 应为 true：{settings.get('system')}")
        self._set_allow_registration(False)
        health_off = self._health()
        expect(health_off.get("allow_registration") is False, f"关闭后 health.allow_registration 应为 false：{health_off.get('allow_registration')}")
        resp = self._client().post("/api/auth/register", json={"username": f"e2e_v0310_off_{self.stamp}", "password": "OffPass123"})
        expect(resp.status_code == 403, f"关闭注册后 /register 期望 403，实际 {resp.status_code}")
        self._set_allow_registration(True)
        health_on = self._health()
        expect(health_on.get("allow_registration") is True, "恢复注册开关后 health.allow_registration 应为 true")
        return "health.allow_registration 与设置同步：true → false(注册 403) → true"

    def t_register_create(self):
        self.ctx["reg_username"] = f"e2e_v0310_member_{self.stamp}"
        self.ctx["reg_password"] = "RegPass123"
        payload = {
            "username": self.ctx["reg_username"],
            "password": self.ctx["reg_password"],
            "display_name": "v0.3.10 注册成员",
            "email": f"{self.ctx['reg_username']}@example.com",
        }
        data = self._json(self._client().post("/api/auth/register", json=payload), expected=201)
        expect(data.get("role") == "member", f"注册用户角色应为 member：{data}")
        expect(data.get("username") == payload["username"], f"注册用户信息异常：{data}")
        self.ctx["reg_user"] = data
        return f"注册成功 id={data.get('id')} username={data.get('username')} role={data.get('role')}"

    def t_register_duplicate(self):
        payload = {"username": self.ctx["reg_username"], "password": "RegPass123"}
        resp = self._client().post("/api/auth/register", json=payload)
        expect(resp.status_code == 409, f"重复用户名期望 409，实际 {resp.status_code}")

    def t_register_weak(self):
        resp = self._client().post("/api/auth/register", json={"username": f"e2e_v0310_weak_{self.stamp}", "password": "short"})
        expect(resp.status_code == 422, f"短密码期望 422，实际 {resp.status_code}")

    def t_register_login(self):
        client = self._login_client(self.ctx["reg_username"], self.ctx["reg_password"])
        me = self._json(client.get("/api/auth/me"))["user"]
        expect(me.get("role") == "member" and me.get("email"), f"/auth/me 异常：{me}")
        self.ctx["reg_client"] = client
        return f"注册用户登录成功（role={me.get('role')} email={me.get('email')}）"

    # ================================================================== 2
    def section_profile(self) -> None:
        self.section("2. 个人资料（PATCH /api/auth/me）+ 改密")
        self.check("v0310.profile.patch", self.t_profile_patch)
        self.check("v0310.profile.privileged-ignored", self.t_profile_privileged)
        self.check("v0310.profile.password-validation", self.t_profile_password_validation)

    def t_profile_patch(self):
        client = self.ctx["reg_client"]
        display = "QA 资料名"
        email = f"qa_{self.stamp}@example.com"
        data = self._json(client.patch("/api/auth/me", json={"display_name": display, "email": email}))
        user = data.get("user") or {}
        expect(user.get("display_name") == display, f"display_name 未更新：{user}")
        expect(user.get("email") == email, f"email 未更新：{user}")
        me = self._json(client.get("/api/auth/me"))["user"]
        expect(me.get("display_name") == display and me.get("email") == email, f"重新获取资料不一致：{me}")
        return f"PATCH /auth/me 生效：display_name={display!r} email={email}"

    def t_profile_privileged(self):
        client = self.ctx["reg_client"]
        before = self._json(client.get("/api/auth/me"))["user"]
        data = self._json(
            client.patch("/api/auth/me", json={"display_name": before.get("display_name"), "role": "admin", "disabled": True})
        )
        user = data.get("user") or {}
        expect(user.get("role") == "member", f"PATCH /auth/me 不应提升角色：{user.get('role')}")
        expect(user.get("disabled") is False, f"PATCH /auth/me 不应停用账号：{user.get('disabled')}")
        expect(user.get("username") == before.get("username"), "PATCH /auth/me 不应修改用户名")
        return "role/disabled/username 等特权字段被忽略（仍为 member / 启用）"

    def t_profile_password_validation(self):
        client = self.ctx["reg_client"]
        wrong = self._client().post("/api/auth/change-password", json={})  # 未登录 401
        expect(wrong.status_code in (401, 403), f"未登录改密期望 401/403，实际 {wrong.status_code}")
        bad_old = client.post("/api/auth/change-password", json={"old_password": "wrong-pass", "new_password": "NewPass123"})
        expect(bad_old.status_code == 400, f"旧密码错误期望 400，实际 {bad_old.status_code}")
        short = client.post("/api/auth/change-password", json={"old_password": self.ctx["reg_password"], "new_password": "123"})
        expect(short.status_code == 400, f"新密码过短期望 400，实际 {short.status_code}")
        new_password = "RegPass456"
        ok = client.post("/api/auth/change-password", json={"old_password": self.ctx["reg_password"], "new_password": new_password})
        expect(ok.status_code == 200, f"合法改密失败：HTTP {ok.status_code} {ok.text[:200]}")
        old_login = self._client().post("/api/auth/login", json={"username": self.ctx["reg_username"], "password": self.ctx["reg_password"]})
        expect(old_login.status_code == 401, f"旧密码登录期望 401，实际 {old_login.status_code}")
        self.ctx["reg_password"] = new_password
        self._login_client(self.ctx["reg_username"], new_password)
        return "改密校验：未登录 401/403、旧密码错 400、新密码过短 400；成功改密后旧密码失效、新密码可登录"

    # ================================================================== 3
    def section_vector(self) -> None:
        self.section("3. sqlite-vec 向量后端 / 检索")
        self.check("v0310.vector.status", self.t_vector_status)
        self.check("v0310.vector.embeddings", self.t_vector_embeddings)
        self.check("v0310.vector.reindex", self.t_vector_reindex)
        self.check("v0310.vector.semantic", self.t_vector_semantic)
        self.check("v0310.vector.hybrid", self.t_vector_hybrid)

    def t_vector_status(self):
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("backend") == self.expect_vector_backend,
               f"vector status 后端期望 {self.expect_vector_backend}，实际 {status.get('backend')}：{status}")
        # 未配置 Embedding 时 available=false + fallback_reason（预期）；配置后的断言见下一个检查
        self.ctx["vector_status"] = status
        return f"backend={status.get('backend')} available={status.get('available')} " \
               f"chunks={status.get('chunks_indexed')}/{status.get('chunks_total')} stale={status.get('stale_chunks')} " \
               f"fallback={status.get('fallback_reason')}"

    def t_vector_embeddings(self):
        self._json(self.client.put("/api/settings", json={"ai": {"embeddings": {"provider": "mock"}}}))
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("embeddings_configured") is True, f"mock embeddings 未生效：{status}")
        expect(status.get("available") is True, f"配置 Embedding 后向量后端应可用：{status}")
        if self.expect_vector_backend == "sqlite_vec":
            expect(status.get("backend") == "sqlite_vec", f"配置后后端异常：{status.get('backend')}")
            expect(not status.get("fallback_reason"), f"sqlite-vec 生效时不应有降级原因：{status.get('fallback_reason')}")
        return f"embeddings=mock → backend={status.get('backend')} available={status.get('available')} fallback={status.get('fallback_reason')}"

    def t_vector_reindex(self):
        result = self._json(self.client.post("/api/vector/reindex", json={"force": True}))
        expect(result.get("ok") is True, f"reindex 失败：{result}")
        expect(int(result.get("chunks") or 0) > 0, f"reindex 未写入 chunks：{result}")
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("backend") == self.expect_vector_backend, f"reindex 后后端变化：{status.get('backend')}")
        expect(int(status.get("chunks_indexed") or 0) > 0, f"chunks_indexed=0：{status}")
        expect(int(status.get("stale_chunks") or 0) == 0, f"reindex 后仍有 stale：{status}")
        return f"reindex: recordings={result.get('processed_recordings')} chunks={result.get('chunks')} " \
               f"indexed={status.get('chunks_indexed')}/{status.get('chunks_total')}"

    def t_vector_semantic(self):
        data = self._json(self.client.get("/api/vector/search", params={"q": "摘要 两周 交付节点", "mode": "semantic", "limit": 8}))
        results = data.get("results") or []
        expect(data.get("mode") == "semantic", f"mode 回显异常：{data.get('mode')}")
        expect(results, f"语义搜索无结果：{data}")
        expect(all(float(hit.get("score") or 0) > 0 for hit in results), "语义结果 score 异常")
        return f"semantic 命中 {len(results)} 条（top={results[0].get('score')} backend={self.expect_vector_backend} fallback={data.get('fallback_reason')}）"

    def t_vector_hybrid(self):
        data = self._json(self.client.get("/api/vector/search", params={"q": "Graph500", "mode": "hybrid", "limit": 8}))
        results = data.get("results") or []
        expect(data.get("mode") == "hybrid", f"mode 回显异常：{data.get('mode')}")
        expect(results, f"混合搜索无结果：{data}")
        return f"hybrid 命中 {len(results)} 条"

    # ================================================================== 4
    def section_live(self) -> None:
        self.section("4. 实时转写 GA（WS ready/ping/片段）")
        self.check("v0310.live.ready-fields", self.t_live_ready_fields)
        self.check("v0310.live.ping-pong", self.t_live_ping_pong)
        self.check("v0310.live.stop-segments", self.t_live_stop_segments)

    def _new_live_session(self) -> dict:
        return self._json(self.client.post("/api/live/sessions", json={"merge_mode": "replace"}), expected=201)

    def _ws_uri(self, ws_url: str) -> str:
        token = self.ctx["token"]
        if ws_url.startswith(("ws://", "wss://")):
            base = ws_url
        else:
            base = f"ws://127.0.0.1:{int(self.base_url.rsplit(':', 1)[1])}{ws_url}"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}token={token}"

    def t_live_ready_fields(self):
        session = self._new_live_session()
        self.ctx["live_session"] = session
        uri = self._ws_uri(session["ws_url"])
        ws = ws_connect(uri, open_timeout=20, close_timeout=10, max_size=None)
        try:
            ready = json.loads(ws.recv(timeout=20))
            expect(ready.get("type") == "ready", f"首帧应为 ready：{ready}")
            for key in ("resumed", "base_ms", "total_ms", "connection"):
                expect(key in ready, f"ready 事件缺少 {key}：{ready}")
            expect(ready.get("resumed") is False, f"首次连接 resumed 应为 false：{ready}")
            expect(int(ready.get("connection") or 0) == 1, f"首次 connection 应为 1：{ready}")
            expect(int(ready.get("base_ms") or 0) == 0, f"首次 base_ms 应为 0：{ready}")
            self.ctx["live_ws"] = ws
            self.ctx["live_ready"] = ready
            return f"ready 字段完整：{ {k: ready.get(k) for k in ('resumed','base_ms','total_ms','connection')} }"
        except Exception:
            ws.close()
            raise

    def t_live_ping_pong(self):
        ws = self.ctx.get("live_ws")
        expect(ws is not None, "缺少 live WS 连接")
        ws.send(json.dumps({"type": "start", "sample_rate": 16000, "channels": 1}))
        ws.send(json.dumps({"type": "ping", "ts": int(time.time() * 1000)}))
        pong = None
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                raw = ws.recv(timeout=5)
            except (TimeoutError, ConnectionClosed):
                break
            if isinstance(raw, bytes):
                continue
            event = json.loads(raw)
            if event.get("type") == "pong":
                pong = event
                break
        expect(pong is not None, "未收到 pong 事件")
        expect("ts" in pong, f"pong 缺少 ts：{pong}")
        return f"ping→pong 正常（ts={pong.get('ts')}）"

    def t_live_stop_segments(self):
        ws = self.ctx.get("live_ws")
        session = self.ctx.get("live_session") or {}
        expect(ws is not None and session.get("id"), "缺少 live 会话/连接")
        frame = bytes(640)  # 20ms PCM16LE 16k mono
        try:
            for _ in range(6 * 50):  # 6s audio
                ws.send(frame)
                time.sleep(0.003)
            ws.send(json.dumps({"type": "stop"}))
            done = None
            events = []
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    raw = ws.recv(timeout=10)
                except (TimeoutError, ConnectionClosed):
                    break
                if isinstance(raw, bytes):
                    continue
                event = json.loads(raw)
                events.append(event)
                if event.get("type") in ("done", "error"):
                    done = event
                    break
        finally:
            ws.close()
        expect(done is not None and done.get("type") == "done", f"未收到 done：{events[-3:] if events else '[]'}")
        detail = self._json(self.client.get(f"/api/live/sessions/{session['id']}"))
        expect(detail["session"]["status"] == "ended", f"会话状态应为 ended：{detail['session']['status']}")
        segments = detail.get("segments") or []
        expect(segments, "实时片段未持久化")
        expect(int(detail["session"].get("total_audio_ms") or 0) > 0, "total_audio_ms 应 > 0")
        return f"stop 后 ended，segments={len(segments)} total_ms={detail['session'].get('total_audio_ms')} done_total_ms={done.get('total_ms')}"

    # ================================================================== 5
    def section_description(self) -> None:
        self.section("5. 描述自动生成 / backfill CLI")
        self.check("v0310.description.autofill", self.t_description_autofill)
        self.check("v0310.description.user-preserved", self.t_description_user_preserved)
        self.check("v0310.description.demo-unchanged", self.t_description_demo_unchanged)
        if self.data_dir:
            self.check("v0310.description.backfill", self.t_description_backfill)
        else:
            self.skip("v0310.description.backfill", "未提供 --data-dir，跳过 backfill CLI")

    def t_description_autofill(self):
        rec = self._upload_wav(self.client, f"E2E V0310 描述自动生成 {self.stamp}", seconds=8)
        detail = self._poll_ready(self.client, rec["id"])
        description = (detail.get("description") or "").strip()
        expect(description, f"处理后描述为空：{detail.get('description')}")
        expect(len(description) <= DESCRIPTION_MAX, f"自动描述超过 {DESCRIPTION_MAX} 字：{len(description)}")
        expect("暂无描述" not in description, "描述不应为占位文案")
        self.ctx["desc_rid"] = rec["id"]
        self.ctx["desc_text"] = description
        return f"id={rec['id']} 自动描述({len(description)}字)={description[:50]}…"

    def t_description_user_preserved(self):
        user_desc = "用户自定义描述-哨兵-请勿覆盖"
        rec = self._upload_wav(self.client, f"E2E V0310 用户描述 {self.stamp}", seconds=8, description=user_desc)
        detail = self._poll_ready(self.client, rec["id"])
        expect((detail.get("description") or "").strip() == user_desc,
               f"用户描述被覆盖：{detail.get('description')!r}")
        self.ctx["user_desc_rid"] = rec["id"]
        return f"id={rec['id']} 用户描述保持不变"

    def t_description_demo_unchanged(self):
        data = self._json(self.client.get("/api/recordings", params={"query": "TraceOpt", "page_size": 5}))
        items = data.get("items") or []
        expect(items, "找不到演示录音")
        demo = items[0]
        before = (demo.get("description") or "").strip()
        expect(before, "演示录音本应有描述（seed 数据）")
        detail = self._json(self.client.get(f"/api/recordings/{demo['id']}"))
        expect((detail.get("description") or "").strip() == before, "演示录音描述被意外修改")
        return f"demo id={demo['id']} 描述保持不变（{len(before)} 字）"

    def t_description_backfill(self):
        rid = self.ctx.get("desc_rid")
        expect(rid, "缺少可用于 backfill 的录音")
        # 清空描述，模拟历史数据
        self._json(self.client.patch(f"/api/recordings/{rid}", json={"description": ""}))
        cleared = self._json(self.client.get(f"/api/recordings/{rid}"))
        expect(not (cleared.get("description") or "").strip(), f"清空描述失败：{cleared.get('description')!r}")
        env = {**os.environ, "ECHOIC_DATA_DIR": str(self.data_dir), "ECHOIC_SECRET_KEY": os.environ.get("ECHOIC_SECRET_KEY", "e2e-secret-v0310")}
        base_cmd = [sys.executable, "-m", "app.tools.backfill_descriptions", "--recording-id", str(rid)]
        dry = subprocess.run(base_cmd, cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=120)
        expect(dry.returncode == 0, f"backfill dry-run 失败：{dry.stderr[-400:]}")
        dry_payload = self._parse_cli_json(dry.stdout)
        expect(int(dry_payload.get("derivable") or 0) >= 1, f"dry-run derivable 异常：{dry_payload}")
        expect(int(dry_payload.get("filled") or 0) == 0, f"dry-run 不应写入：{dry_payload}")
        still_empty = self._json(self.client.get(f"/api/recordings/{rid}"))
        expect(not (still_empty.get("description") or "").strip(), "dry-run 后描述应仍为空")
        apply = subprocess.run(base_cmd + ["--apply"], cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=180)
        expect(apply.returncode == 0, f"backfill --apply 失败：{apply.stderr[-400:]}")
        apply_payload = self._parse_cli_json(apply.stdout)
        expect(int(apply_payload.get("filled") or 0) >= 1, f"--apply filled 异常：{apply_payload}")
        filled = self._json(self.client.get(f"/api/recordings/{rid}"))
        new_desc = (filled.get("description") or "").strip()
        expect(new_desc, "--apply 后描述仍为空")
        expect(len(new_desc) <= DESCRIPTION_MAX, f"--apply 描述超长：{len(new_desc)}")
        # 用户描述录音不应被覆盖
        user_rid = self.ctx.get("user_desc_rid")
        if user_rid:
            user_detail = self._json(self.client.get(f"/api/recordings/{user_rid}"))
            expect((user_detail.get("description") or "").strip() == "用户自定义描述-哨兵-请勿覆盖",
                   "backfill 覆盖了用户描述")
        return f"dry-run derivable={dry_payload.get('derivable')} filled=0；--apply filled={apply_payload.get('filled')} " \
               f"新描述({len(new_desc)}字)={new_desc[:40]}…"

    @staticmethod
    def _parse_cli_json(stdout: str) -> dict:
        text = stdout.strip()
        start = text.find("{")
        end = text.rfind("}")
        expect(start >= 0 and end > start, f"CLI 未输出 JSON：{text[-300:]}")
        try:
            return json.loads(text[start:end + 1])
        except ValueError as exc:
            raise AssertionError(f"CLI JSON 解析失败：{exc}；输出={text[-300:]}") from exc

    # ================================================================== runner
    def run_v0310(self) -> int:
        print(f"Echoic v0.3.10 E2E Smoke — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"目标服务: {self.base_url} | data dir: {self.data_dir} | 期望向量后端: {self.expect_vector_backend}")
        print(f"Python: {sys.version.split()[0]} | httpx: {httpx.__version__} | ffmpeg: {self.ffmpeg or '缺失'}")
        print("=" * 78)
        try:
            self.section_env()
            if self.ctx.get("token"):
                self.section_register()
                self.section_profile()
                self.section_vector()
                self.section_live()
                self.section_description()
        except Exception as exc:  # noqa: BLE001
            import traceback
            self.record("v0310.fatal", "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
        return self._summary()

    def _summary(self) -> int:
        total = len(self.results)
        passed = sum(1 for row in self.results if row["status"] == "PASS")
        failed = [row for row in self.results if row["status"] == "FAIL"]
        skipped = sum(1 for row in self.results if row["status"] == "SKIP")
        print("\n" + "=" * 78)
        print("\033[1mv0.3.10 PASS/FAIL 矩阵\033[0m")
        print("-" * 78)
        for row in self.results:
            color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(row["status"], "")
            first_line = row["detail"].splitlines()[0] if row["detail"] else ""
            print(f"{color}{row['status']:<4}\033[0m {row['name']:<46} {row['duration']:6.2f}s  {first_line}")
        print("-" * 78)
        print(f"总计 {total} 项：\033[1;32m{passed} 通过\033[0m，\033[1;31m{len(failed)} 失败\033[0m，{skipped} 跳过")
        if failed:
            print("\n失败详情：")
            for row in failed:
                print(f"\n\033[1;31m✗ {row['name']}\033[0m")
                print(row["detail"])
            print("\n\033[1;31mE2E v0.3.10 SMOKE: FAIL\033[0m")
            return 1
        print("\n\033[1;32mE2E v0.3.10 SMOKE: PASS ✔\033[0m")
        return 0

    def close(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        super().close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Echoic v0.3.10 端到端冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ECHOIC_E2E_BASE_URL") or os.environ.get("ECHOIC_BASE_URL") or "http://127.0.0.1:8100",
    )
    parser.add_argument("--username", default=os.environ.get("ECHOIC_E2E_ADMIN_USERNAME") or os.environ.get("ECHOIC_ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or os.environ.get("ECHOIC_ADMIN_PASSWORD") or "admin123")
    parser.add_argument("--process-timeout", type=float, default=float(os.environ.get("ECHOIC_E2E_PROCESS_TIMEOUT") or 180))
    parser.add_argument("--data-dir", default=os.environ.get("ECHOIC_E2E_DATA_DIR") or "")
    parser.add_argument("--expect-vector-backend", default=os.environ.get("ECHOIC_E2E_VECTOR_BACKEND") or "sqlite_vec")
    parser.add_argument("--log", default=os.environ.get("ECHOIC_E2E_LOG_V0310") or str(E2E_DIR / "last-run-v0310.log"))
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    from smoke import Tee

    stdout_orig, stderr_orig = sys.stdout, sys.stderr
    sys.stdout = Tee(stdout_orig, log_fh)
    sys.stderr = Tee(stderr_orig, log_fh)
    smoke = None
    try:
        data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
        smoke = V0310Smoke(args.base_url, args.username, args.password, args.process_timeout, data_dir, args.expect_vector_backend)
        code = smoke.run_v0310()
    finally:
        if smoke is not None:
            smoke.close()
        print(f"\n日志已保存：{log_path}")
        sys.stdout, sys.stderr = stdout_orig, stderr_orig
        log_fh.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
