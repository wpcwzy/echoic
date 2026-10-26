#!/usr/bin/env python3
"""Echoic v0.3 端到端冒烟测试（回收站/批量、特性开关、实时转写 Mock、向量检索）。

在独立数据目录 + mock AI 管线的生产式服务上运行（见 task-18）：
    ECHOIC_DATA_DIR=$PWD/.cache/e2e-v03-data \
    ECHOIC_CONSUME_DIR=$PWD/.cache/e2e-v03-consume \
    ECHOIC_LIVE_ENABLED=true ECHOIC_LIVE_PROVIDER=mock \
    backend/.venv/bin/python e2e/smoke_v03.py --base-url http://127.0.0.1:8100 \
        --data-dir $PWD/.cache/e2e-v03-data

覆盖：
  0) 健康 / 管理员登录 / 演示数据 / features 能力探测
  1) 回收站（R-23）：软删除响应、默认查询不可见、回收站列表、详情/媒体/转录/分析/问答/分享/公开分享 404、
     恢复后内容完整、分享保持撤销、彻底删除（DB+媒体目录）、成员范围
  2) 批量操作：部分成功、上限 400、标签增删、重跑、purge 仅回收站、成员权限
  3) 实时转写 Beta（R-19）：创建会话 → WS（ready/partial/final/done）→ 结束/片段持久化 →
     上传音频绑定 live_session_id → ready → 录音实时稿可查 → replace 保留离线说话人；promote 单说话人；
     重复生产者 4409、stop 幂等、重复绑定 409
  4) 向量 / 混合检索（R-20）：mock embedding 配置 → 状态 → reindex → semantic/hybrid/keyword 搜索 →
     成员 ACL 隔离 → embeddings 关闭时降级 fallback_reason 且恢复
  5) storage local / media Range 回归 / 演示问答 SSE 回归

退出码：0 = 全部通过（允许 SKIP）；1 = 存在 FAIL。日志默认写入 e2e/last-run-v03.log。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect as ws_connect

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(E2E_DIR))

from smoke import Smoke, expect  # noqa: E402  （复用 v0.1 冒烟脚本工具类）

DEFAULT_BATCH_LIMIT = 100


class V03Smoke(Smoke):
    def __init__(self, base_url: str, username: str, password: str, process_timeout: float, data_dir: Path | None):
        super().__init__(base_url, username, password, process_timeout)
        self.data_dir = data_dir
        self.stamp = time.strftime("%H%M%S")
        self.clients: list[httpx.Client] = []

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

    def _new_user(self, key: str, role: str = "member", password: str = "V03Pass123") -> dict:
        username = f"e2e_v03_{key}_{self.stamp}"
        data = self._json(
            self.client.post(
                "/api/users",
                json={"username": username, "password": password, "display_name": f"v03 {key}", "role": role},
            ),
            expected=201,
        )
        record = {**data, "_password": password}
        self.ctx.setdefault("users", {})[key] = record
        return record

    def _anon(self) -> httpx.Client:
        return self._client()

    def _upload_wav(self, client: httpx.Client, title: str, seconds: int = 8, live_session_id: int | None = None) -> dict:
        audio = self.make_audio(seconds=seconds)
        data = {"title": title, "description": "smoke_v03 自动生成音频", "language": "zh"}
        if live_session_id is not None:
            data["live_session_id"] = str(live_session_id)
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
        expect(
            detail.get("status") == "ready",
            f"录音 {recording_id} 未就绪：status={detail.get('status')} stage={detail.get('stage')} "
            f"error={detail.get('error_message')}",
        )
        return detail

    def _trash(self, recording_id: int) -> dict:
        return self._json(self.client.delete(f"/api/recordings/{recording_id}"))

    def _create_tag(self, name: str) -> dict:
        return self._json(self.client.post("/api/tags", json={"name": name, "color": "#1677ff"}), expected=201)

    def _set_embeddings(self, provider: str) -> dict:
        return self._json(self.client.put("/api/settings", json={"ai": {"embeddings": {"provider": provider}}}))

    def _media_dir(self, uuid: str) -> Path | None:
        if not self.data_dir:
            return None
        return Path(self.data_dir) / "media" / uuid

    # ================================================================== 0. env
    def section_env(self) -> None:
        self.section("0. 环境 / features / 演示数据")
        if not self.check("v03.health", self.t_health):
            raise RuntimeError("服务不可用")
        self.check("v03.auth.login-admin", self.t_login)
        self.check("v03.system.info", self.t_system_info)
        self.check("v03.library.demo", self.t_library_and_demo)
        self.check("v03.features", self.t_features)
        self.check("v03.health.storage", self.t_health_storage)

    def t_features(self):
        data = self._json(self.client.get("/api/system/features"))
        for key in ("live", "vector", "storage", "trash"):
            expect(key in data, f"/api/system/features 缺少 {key}：{data}")
        live = data["live"]
        expect(live.get("enabled") is True, f"LIVE_ENABLED 应为 true（QA 环境）：{live}")
        expect(live.get("provider") == "mock", f"LIVE_PROVIDER 应为 mock：{live}")
        expect(int(live.get("max_minutes") or 0) > 0, f"live.max_minutes 异常：{live}")
        expect(live.get("merge_mode") in ("replace", "promote"), f"live.merge_mode 异常：{live}")
        vector = data["vector"]
        expect(vector.get("backend") in ("numpy", "pgvector"), f"vector.backend 异常：{vector}")
        expect(vector.get("search_enabled") is True, f"vector.search_enabled 应为 true：{vector}")
        storage = data["storage"]
        expect(storage.get("backend") == "local", f"storage.backend 应为 local：{storage}")
        trash = data["trash"]
        expect(int(trash.get("retention_days") or 0) >= 1, f"trash.retention_days 异常：{trash}")
        self.ctx["features"] = data
        return f"live={live} vector={vector} storage={storage} trash={trash}"

    def t_health_storage(self):
        data = self._json(self.client.get("/api/system/health"))
        expect(data.get("status") == "ok", f"health 应为 ok：{data}")
        storage = data.get("storage")
        if storage is None:
            return "health 无 storage 字段（本地存储模式；features.storage.backend=local 已校验）"
        status = storage.get("status") if isinstance(storage, dict) else storage
        expect(status in ("ok", None), f"health.storage 异常：{storage}")
        return f"health.storage={storage}"

    # ================================================================== 1. trash
    def section_trash(self) -> None:
        self.section("1. 回收站（R-23）")
        self.check("v03.trash.soft-delete", self.t_trash_soft_delete)
        self.check("v03.trash.invisible-everywhere", self.t_trash_invisible)
        self.check("v03.trash.list", self.t_trash_list)
        self.check("v03.trash.restore", self.t_trash_restore)
        self.check("v03.trash.share-revoked", self.t_trash_share_revoked)
        self.check("v03.trash.purge", self.t_trash_purge)

    def t_trash_soft_delete(self):
        rec = self._upload_wav(self.client, f"E2E V03 Trash {self.stamp}", seconds=6)
        self._poll_ready(self.client, rec["id"])
        detail = self._json(self.client.get(f"/api/recordings/{rec['id']}"))
        self.ctx["trash_rec"] = detail
        self.ctx["trash_share"] = self._json(
            self.client.post(f"/api/recordings/{rec['id']}/shares", json={"include_transcript": True}), expected=201
        )
        before_total = int(self._json(self.client.get("/api/recordings", params={"page_size": 1}))["total"])
        deleted = self._json(self.client.delete(f"/api/recordings/{rec['id']}"))
        expect(deleted.get("ok") is True and deleted.get("id") == rec["id"], f"软删除响应异常：{deleted}")
        expect(deleted.get("deleted_at"), f"软删除缺少 deleted_at：{deleted}")
        expect(deleted.get("restore_before"), f"软删除缺少 restore_before：{deleted}")
        after_total = int(self._json(self.client.get("/api/recordings", params={"page_size": 1}))["total"])
        expect(after_total == before_total - 1, f"软删除后列表总数未减少：{before_total} -> {after_total}")
        return f"id={rec['id']} deleted_at={deleted['deleted_at']} restore_before={deleted['restore_before']}"

    def t_trash_invisible(self):
        rec = self.ctx["trash_rec"]
        rid, title = rec["id"], rec["title"]
        cases = [
            ("GET", f"/api/recordings/{rid}", None),
            ("GET", f"/api/recordings/{rid}/media", None),
            ("GET", f"/api/recordings/{rid}/transcript", None),
            ("GET", f"/api/recordings/{rid}/insights", None),
            ("GET", f"/api/recordings/{rid}/chat", None),
            ("GET", f"/api/recordings/{rid}/shares", None),
            ("PATCH", f"/api/recordings/{rid}", {"title": "should fail"}),
        ]
        for method, path, body in cases:
            resp = self.client.request(method, path, json=body) if body is not None else self.client.request(method, path)
            expect(resp.status_code == 404, f"已删除录音 {method} {path} 期望 404，实际 {resp.status_code}")
        search = self._json(self.client.get("/api/search", params={"q": title, "limit": 10}))
        expect(not (search.get("recordings") or []), f"搜索仍命中已删除录音：{search.get('recordings')}")
        expect(not (search.get("segments") or []), "分段搜索仍命中已删除录音片段")
        dashboard = self._json(self.client.get("/api/dashboard"))
        trashed_ids = [item["id"] for item in self._json(self.client.get("/api/dashboard")).get("recent") or []]
        expect(rid not in trashed_ids, f"仪表盘 recent 仍包含已删除录音 {rid}")
        share = self.ctx.get("trash_share") or {}
        if share.get("token"):
            public = self._anon().get(f"/api/public/shares/{share['token']}")
            expect(public.status_code == 404, f"已删除录音的公开分享期望 404，实际 {public.status_code}")
        expect(dashboard.get("stats") is not None, "dashboard stats 异常")
        return "detail/media/transcript/insights/chat/shares/patch/search/dashboard/公开分享 均不可见"

    def t_trash_list(self):
        data = self._json(self.client.get("/api/recordings", params={"trashed": "true", "page_size": 50}))
        items = data.get("items") or []
        target = next((item for item in items if item["id"] == self.ctx["trash_rec"]["id"]), None)
        expect(target is not None, f"回收站列表未包含录音 {self.ctx['trash_rec']['id']}：{[i['id'] for i in items]}")
        expect(target.get("deleted_at"), f"回收站条目缺少 deleted_at：{target}")
        expect("stats" in data, "回收站列表缺少 stats")
        return f"回收站 {data.get('total')} 条，deleted_at={target.get('deleted_at')}"

    def t_trash_restore(self):
        rid = self.ctx["trash_rec"]["id"]
        restored = self._json(self.client.post(f"/api/recordings/{rid}/restore"))
        expect(restored.get("id") == rid, f"恢复响应异常：{restored}")
        expect(restored.get("status") == "ready", f"恢复后状态异常：{restored.get('status')}")
        transcript = self._json(self.client.get(f"/api/recordings/{rid}/transcript"))
        expect(transcript.get("segments"), "恢复后转录为空")
        insights = self._json(self.client.get(f"/api/recordings/{rid}/insights"))
        expect(insights.get("summary"), "恢复后 AI 分析 summary 为空")
        visible = self._json(self.client.get("/api/recordings", params={"page_size": 100}))
        expect(any(item["id"] == rid for item in visible.get("items") or []), "恢复后列表仍不可见")
        return f"id={rid} 恢复后 transcript={len(transcript['segments'])} 段，insights/列表恢复正常"

    def t_trash_share_revoked(self):
        rid = self.ctx["trash_rec"]["id"]
        share = self.ctx.get("trash_share") or {}
        expect(share.get("token"), "缺少删除前创建的分享")
        public = self._anon().get(f"/api/public/shares/{share['token']}")
        expect(public.status_code == 404, f"恢复后旧分享仍应失效（404），实际 {public.status_code}")
        shares = self._json(self.client.get(f"/api/recordings/{rid}/shares"))
        item = next((s for s in shares.get("items") or [] if s["id"] == share["id"]), None)
        expect(item is not None, "分享列表异常")
        expect(item.get("revoked_at"), f"删除录音时分享应自动软撤销：{item}")
        return "删除录音时分享自动撤销；恢复后旧链接保持失效（404）"

    def t_trash_purge(self):
        rid = self.ctx["trash_rec"]["id"]
        uuid = self.ctx["trash_rec"]["uuid"]
        media_dir = self._media_dir(uuid)
        if media_dir is not None:
            expect(media_dir.exists(), f"purge 前媒体目录不存在：{media_dir}")
        self._trash(rid)
        purged = self._json(self.client.delete(f"/api/recordings/{rid}/purge"))
        expect(purged.get("ok") is True and purged.get("purged") is True, f"purge 响应异常：{purged}")
        resp = self.client.get(f"/api/recordings/{rid}")
        expect(resp.status_code == 404, f"purge 后详情期望 404，实际 {resp.status_code}")
        trash = self._json(self.client.get("/api/recordings", params={"trashed": "true", "page_size": 100}))
        expect(not any(item["id"] == rid for item in trash.get("items") or []), "purge 后回收站仍包含该录音")
        if media_dir is not None:
            expect(not media_dir.exists(), f"purge 后媒体目录未删除：{media_dir}")
            return f"id={rid} 已彻底删除（DB + 媒体目录 {media_dir.name} 已清理）"
        return f"id={rid} 已彻底删除（DB 行清理；未提供 data-dir 未校验媒体目录）"

    # ================================================================== 2. batch
    def section_batch(self) -> None:
        self.section("2. 批量操作（R-23）")
        self.check("v03.batch.setup", self.t_batch_setup)
        self.check("v03.batch.partial-trash", self.t_batch_partial_trash)
        self.check("v03.batch.partial-restore", self.t_batch_partial_restore)
        self.check("v03.batch.tags", self.t_batch_tags)
        self.check("v03.batch.reprocess", self.t_batch_reprocess)
        self.check("v03.batch.purge-only-trashed", self.t_batch_purge_only_trashed)
        self.check("v03.batch.limit-400", self.t_batch_limit)
        self.check("v03.batch.member-scope", self.t_batch_member_scope)

    def t_batch_setup(self):
        ids = []
        for idx in range(3):
            rec = self._upload_wav(self.client, f"E2E V03 Batch {self.stamp}-{idx}", seconds=5)
            self._poll_ready(self.client, rec["id"])
            ids.append(rec["id"])
        self.ctx["batch_ids"] = ids
        self.ctx["batch_tag"] = self._create_tag(f"v03tag_{self.stamp}")
        return f"创建 3 条录音 {ids} + 标签 id={self.ctx['batch_tag']['id']}"

    def t_batch_partial_trash(self):
        ids = self.ctx["batch_ids"]
        result = self._json(
            self.client.post("/api/recordings/batch", json={"action": "trash", "recording_ids": ids + [99999999]})
        )
        expect(result.get("succeeded") == 3 and result.get("failed") == 1,
               f"批量移入回收站期望 3 成功 1 失败：{result}")
        failed = [item for item in result.get("results") or [] if not item["ok"]]
        expect(failed and failed[0].get("error"), f"失败项缺少 error：{failed}")
        data = self._json(self.client.get("/api/recordings", params={"trashed": "true", "page_size": 100}))
        trashed = {item["id"] for item in data.get("items") or []}
        expect(set(ids) <= trashed, f"批量移入回收站后未能在回收站找到：{ids} vs {trashed}")
        return f"批量 trash：succeeded={result['succeeded']} failed={result['failed']}（无效 id 99999999）"

    def t_batch_partial_restore(self):
        ids = self.ctx["batch_ids"]
        result = self._json(
            self.client.post("/api/recordings/batch", json={"action": "restore", "recording_ids": ids + [99999999]})
        )
        expect(result.get("succeeded") == 3 and result.get("failed") == 1,
               f"批量恢复期望 3 成功 1 失败：{result}")
        for rid in ids:
            detail = self._json(self.client.get(f"/api/recordings/{rid}"))
            expect(detail.get("status") == "ready", f"恢复后录音 {rid} 状态异常：{detail.get('status')}")
        return f"批量 restore：succeeded={result['succeeded']} failed={result['failed']}"

    def t_batch_tags(self):
        ids = self.ctx["batch_ids"][:2]
        tag_id = self.ctx["batch_tag"]["id"]
        added = self._json(
            self.client.post(
                "/api/recordings/batch", json={"action": "tag_add", "recording_ids": ids, "tag_ids": [tag_id]}
            )
        )
        expect(added.get("succeeded") == 2, f"批量打标签失败：{added}")
        for rid in ids:
            detail = self._json(self.client.get(f"/api/recordings/{rid}"))
            expect(any(tag["id"] == tag_id for tag in detail.get("tags") or []), f"录音 {rid} 未打上标签")
        removed = self._json(
            self.client.post(
                "/api/recordings/batch", json={"action": "tag_remove", "recording_ids": ids, "tag_ids": [tag_id]}
            )
        )
        expect(removed.get("succeeded") == 2, f"批量移除标签失败：{removed}")
        return "批量 tag_add / tag_remove 各 2 条成功，详情标签同步"

    def t_batch_reprocess(self):
        rid = self.ctx["batch_ids"][0]
        result = self._json(
            self.client.post(
                "/api/recordings/batch",
                json={"action": "reprocess", "recording_ids": [rid], "from": "insights"},
            )
        )
        expect(result.get("succeeded") == 1, f"批量重跑失败：{result}")
        self._poll_ready(self.client, rid, timeout=120)
        insights = self._json(self.client.get(f"/api/recordings/{rid}/insights"))
        expect(insights.get("summary"), "重跑后 insights.summary 为空")
        return f"批量 reprocess(from=insights) 成功并恢复 ready（id={rid}）"

    def t_batch_purge_only_trashed(self):
        rid = self.ctx["batch_ids"][1]
        # 未删除时 purge 应失败（逐条结果，不整体 400）
        result = self._json(
            self.client.post("/api/recordings/batch", json={"action": "purge", "recording_ids": [rid]})
        )
        expect(result.get("failed") == 1 and result.get("succeeded") == 0,
               f"批量 purge 未删除录音应失败：{result}")
        self._trash(rid)
        purged = self._json(
            self.client.post("/api/recordings/batch", json={"action": "purge", "recording_ids": [rid]})
        )
        expect(purged.get("succeeded") == 1, f"批量 purge 回收站录音失败：{purged}")
        expect(self.client.get(f"/api/recordings/{rid}").status_code == 404, "批量 purge 后详情仍可访问")
        self.ctx["batch_ids"] = [value for value in self.ctx["batch_ids"] if value != rid]
        return "批量 purge：active → 逐条失败；回收站 → 成功并彻底删除"

    def t_batch_limit(self):
        limit = int(os.environ.get("ECHOIC_E2E_BATCH_LIMIT") or DEFAULT_BATCH_LIMIT)
        ids = list(range(1, limit + 2))  # > limit
        resp = self.client.post("/api/recordings/batch", json={"action": "trash", "recording_ids": ids})
        expect(resp.status_code == 400, f"超过批量上限期望 400，实际 {resp.status_code}")
        resp2 = self.client.post("/api/recordings/batch", json={"action": "trash", "recording_ids": []})
        expect(resp2.status_code == 422, f"空 recording_ids 期望 422，实际 {resp2.status_code}")
        return f"批量上限 {limit}：{len(ids)} 条 → 400；空列表 → 422"

    def t_batch_member_scope(self):
        member = self._new_user("batch_member")
        client = self._login_client(member["username"], member["_password"])
        own = self._upload_wav(client, f"E2E V03 Member Batch {self.stamp}", seconds=5)
        self._poll_ready(client, own["id"])
        # 成员可批量移入自己录音的回收站
        result = self._json(
            client.post("/api/recordings/batch", json={"action": "trash", "recording_ids": [own["id"]]})
        )
        expect(result.get("succeeded") == 1, f"成员批量移入自己录音回收站失败：{result}")
        # 成员标签批量操作 → 403（admin-only）
        tag = self.ctx.get("batch_tag") or {}
        resp = client.post(
            "/api/recordings/batch", json={"action": "tag_add", "recording_ids": [own["id"]], "tag_ids": [tag.get("id")]}
        )
        expect(resp.status_code == 403, f"成员批量打标签期望 403，实际 {resp.status_code}")
        # 成员 + 管理员录音混合 → 部分成功
        admin_rid = self.ctx["batch_ids"][0] if self.ctx.get("batch_ids") else 1
        self._json(client.post(f"/api/recordings/{own['id']}/restore"))
        mixed = self._json(
            client.post("/api/recordings/batch", json={"action": "trash", "recording_ids": [own["id"], admin_rid]})
        )
        expect(mixed.get("succeeded") == 1 and mixed.get("failed") == 1,
               f"成员混合批量期望 1 成功 1 失败：{mixed}")
        self.ctx["batch_member_client"] = client
        return "成员：批量移入自己录音成功；tag 批量 403；混合越权 1 成功 1 失败"

    # ================================================================== 3. live
    def section_live(self) -> None:
        self.section("3. 实时转写 Beta（R-19，mock provider）")
        self.check("v03.live.replace", self.t_live_replace)
        self.check("v03.live.promote", self.t_live_promote)
        self.check("v03.live.stop-idempotent", self.t_live_stop_idempotent)
        self.check("v03.live.duplicate-producer", self.t_live_duplicate_producer)
        self.check("v03.live.rebind-conflict", self.t_live_rebind_conflict)

    def _ws_uri(self, ws_url: str) -> str:
        token = self.ctx["token"]
        if ws_url.startswith(("ws://", "wss://")):
            base = ws_url
        else:
            base = f"ws://127.0.0.1:{int(self.base_url.rsplit(':', 1)[1])}{ws_url}"
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}token={token}"

    def _run_live_stream(self, session: dict, seconds: int) -> list[dict]:
        uri = self._ws_uri(session["ws_url"])
        events: list[dict] = []
        frame = bytes(640)  # 20 ms PCM16LE 16k mono
        frames = seconds * 50
        with ws_connect(uri, open_timeout=20, close_timeout=10, max_size=None) as ws:
            ready = json.loads(ws.recv(timeout=20))
            events.append(ready)
            ws.send(json.dumps({"type": "start", "sample_rate": 16000, "channels": 1}))
            for _ in range(frames):
                ws.send(frame)
                time.sleep(0.003)
            ws.send(json.dumps({"type": "stop"}))
            deadline = time.time() + 40
            while time.time() < deadline:
                try:
                    raw = ws.recv(timeout=10)
                except (TimeoutError, ConnectionClosed):
                    break
                if isinstance(raw, bytes):
                    continue
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                events.append(event)
                if event.get("type") in ("done", "error"):
                    break
        return events

    def _create_live_session(self, merge_mode: str | None = None) -> dict:
        payload: dict = {}
        if merge_mode:
            payload["merge_mode"] = merge_mode
        return self._json(self.client.post("/api/live/sessions", json=payload), expected=201)

    def t_live_replace(self):
        session = self._create_live_session("replace")
        expect(session.get("status") == "active", f"新建实时会话状态异常：{session}")
        expect(session.get("provider") == "mock", f"mock provider 期望 mock，实际 {session.get('provider')}")
        expect(session.get("ws_url"), f"缺少 ws_url：{session}")
        events = self._run_live_stream(session, seconds=12)
        types = [event.get("type") for event in events]
        for expected_type in ("ready", "partial", "final", "done"):
            expect(expected_type in types, f"实时流缺少 {expected_type} 事件，收到：{types[:15]}")
        finals = [event for event in events if event.get("type") == "final"]
        expect(finals, "实时流没有 final 片段")
        total_ms = int((next(e for e in reversed(events) if e.get("type") == "done") or {}).get("total_ms") or 0)
        expect(total_ms >= 6000, f"实时会话 total_ms 偏小：{total_ms}")
        detail = self._json(self.client.get(f"/api/live/sessions/{session['id']}"))
        expect(detail["session"]["status"] in ("ended", "merged"), f"WS 结束后会话状态异常：{detail['session']['status']}")
        expect(detail.get("segments"), "实时片段未持久化")
        # 上传音频并绑定 live_session_id → replace 模式保留离线说话人
        wav = self._upload_wav(self.client, f"E2E V03 Live Replace {self.stamp}", seconds=12, live_session_id=session["id"])
        self._poll_ready(self.client, wav["id"], timeout=180)
        live = self._json(self.client.get(f"/api/recordings/{wav['id']}/live"))
        expect(live.get("session", {}).get("recording_id") == wav["id"], f"录音未绑定实时会话：{live.get('session')}")
        expect(live.get("segments"), "录音实时稿片段为空")
        transcript = self._json(self.client.get(f"/api/recordings/{wav['id']}/transcript"))
        expect(transcript.get("speakers") and len(transcript["speakers"]) >= 1, "replace 模式离线转录缺少说话人")
        self.ctx["live_replace"] = {"session": session, "recording": wav, "events": len(events), "finals": len(finals)}
        status = live.get("session", {}).get("status")
        return f"session={session['id']} events={len(events)} finals={len(finals)} total_ms={total_ms} " \
               f"recording={wav['id']} merge_status={status} speakers={len(transcript['speakers'])}"

    def t_live_promote(self):
        session = self._create_live_session("promote")
        expect(session.get("merge_mode") == "promote", f"promote 会话 merge_mode 异常：{session.get('merge_mode')}")
        events = self._run_live_stream(session, seconds=8)
        finals = [event for event in events if event.get("type") == "final"]
        expect(finals, "promote 实时流没有 final 片段")
        wav = self._upload_wav(self.client, f"E2E V03 Live Promote {self.stamp}", seconds=8, live_session_id=session["id"])
        self._poll_ready(self.client, wav["id"], timeout=180)
        transcript = self._json(self.client.get(f"/api/recordings/{wav['id']}/transcript"))
        expect(transcript.get("segments"), "promote 后转录为空")
        speakers = transcript.get("speakers") or []
        expect(len(speakers) == 1, f"promote 后应为单说话人，实际 {len(speakers)}：{speakers}")
        live_text = "".join(segment.get("text", "") for segment in transcript.get("segments") or [])
        expect(live_text.strip(), "promote 后转录文本为空")
        self.ctx["live_promote"] = {"session": session, "recording": wav, "speakers": len(speakers)}
        return f"session={session['id']} recording={wav['id']} 转录单说话人，finals={len(finals)}"

    def t_live_stop_idempotent(self):
        session = self._create_live_session()
        first = self._json(self.client.post(f"/api/live/sessions/{session['id']}/stop"))
        second = self._json(self.client.post(f"/api/live/sessions/{session['id']}/stop"))
        expect(first.get("ok") is True and second.get("ok") is True, f"stop 幂等失败：{first} / {second}")
        return f"stop 幂等：status={first.get('status')}"

    def t_live_duplicate_producer(self):
        session = self._create_live_session()
        uri = self._ws_uri(session["ws_url"])
        first = ws_connect(uri, open_timeout=20, close_timeout=10, max_size=None)
        try:
            ready = json.loads(first.recv(timeout=20))
            expect(ready.get("type") == "ready", f"首个生产者未就绪：{ready}")
            second = ws_connect(uri, open_timeout=20, close_timeout=10, max_size=None)
            try:
                try:
                    second.recv(timeout=10)
                    raise AssertionError("第二个生产者未被拒绝（期望 close 4409）")
                except ConnectionClosed as exc:
                    code = getattr(exc, "code", None) or getattr(exc.rcvd, "code", None)
                    expect(code == 4409, f"第二个生产者 close code 期望 4409，实际 {code}")
            finally:
                second.close()
        finally:
            first.close()
            self.client.post(f"/api/live/sessions/{session['id']}/cancel")
        return "同一 active 会话的第二个 WS 生产者被 close 4409 拒绝"

    def t_live_rebind_conflict(self):
        session = self._create_live_session()
        events = self._run_live_stream(session, seconds=3)
        expect(any(e.get("type") == "done" for e in events), "3s 实时流未收到 done")
        wav = self._upload_wav(self.client, f"E2E V03 Live Bind {self.stamp}", seconds=4, live_session_id=session["id"])
        self._poll_ready(self.client, wav["id"], timeout=120)
        audio = self.make_audio(seconds=3)
        with audio.open("rb") as fh:
            resp = self.client.post(
                "/api/recordings/upload",
                files={"file": (audio.name, fh, "audio/wav")},
                data={"title": f"E2E V03 Rebind {self.stamp}", "live_session_id": str(session["id"])},
            )
        expect(resp.status_code == 409, f"重复绑定 live_session_id 期望 409，实际 {resp.status_code}")
        return "同一实时会话重复绑定录音 → 409 CONFLICT"

    # ================================================================== 4. vector
    def section_vector(self) -> None:
        self.section("4. 向量 / 混合检索（R-20，mock embeddings）")
        self.check("v03.vector.setup-embeddings", self.t_vector_setup)
        self.check("v03.vector.reindex", self.t_vector_reindex)
        self.check("v03.vector.semantic-search", self.t_vector_semantic)
        self.check("v03.vector.hybrid-search", self.t_vector_hybrid)
        self.check("v03.vector.member-acl", self.t_vector_acl)
        self.check("v03.vector.fallback-none", self.t_vector_fallback)
        self.check("v03.vector.restore-mock", self.t_vector_restore)

    def t_vector_setup(self):
        self._set_embeddings("mock")
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("embeddings_configured") is True, f"mock embeddings 未生效：{status}")
        expect(status.get("backend") in ("numpy", "pgvector"), f"vector backend 异常：{status}")
        expect(status.get("available") is True, f"vector 后端不可用：{status}")
        self.ctx["vector_status"] = status
        return f"embeddings=mock backend={status.get('backend')} chunks_total={status.get('chunks_total')} " \
               f"stale={status.get('stale_chunks')}"

    def t_vector_reindex(self):
        result = self._json(self.client.post("/api/vector/reindex", json={"force": True}))
        expect(result.get("ok") is True, f"reindex 失败：{result}")
        expect(int(result.get("chunks") or 0) > 0, f"reindex 未写入 chunks：{result}")
        status = self._json(self.client.get("/api/vector/status"))
        expect(int(status.get("chunks_indexed") or 0) > 0, f"reindex 后 chunks_indexed=0：{status}")
        expect(int(status.get("stale_chunks") or 0) == 0, f"reindex 后仍有 stale chunks：{status}")
        self.ctx["vector_status"] = status
        return f"reindex: recordings={result.get('processed_recordings')} chunks={result.get('chunks')} " \
               f"skipped={result.get('skipped')} indexed={status.get('chunks_indexed')}"

    def t_vector_semantic(self):
        q = "摘要 两周 交付节点"
        data = self._json(self.client.get("/api/vector/search", params={"q": q, "mode": "semantic", "limit": 8}))
        expect(data.get("mode") == "semantic", f"搜索 mode 回显异常：{data.get('mode')}")
        results = data.get("results") or []
        expect(results, f"语义搜索无结果（q={q}）：{data}")
        expect(any(hit.get("recording_title") for hit in results), "语义结果缺少 recording_title")
        expect(all(float(hit.get("score") or 0) > 0 for hit in results), "语义结果 score 异常")
        self.ctx["semantic_results"] = results
        return f"semantic 命中 {len(results)} 条（top score={results[0].get('score')} fallback={data.get('fallback_reason')}）"

    def t_vector_hybrid(self):
        data = self._json(self.client.get("/api/vector/search", params={"q": "Graph500", "mode": "hybrid", "limit": 8}))
        expect(data.get("mode") == "hybrid", f"hybrid mode 回显异常：{data.get('mode')}")
        results = data.get("results") or []
        expect(results, f"混合搜索无结果：{data}")
        return f"hybrid 命中 {len(results)} 条（fallback={data.get('fallback_reason')}）"

    def t_vector_acl(self):
        member = self._new_user("vector_member")
        client = self._login_client(member["username"], member["_password"])
        own = self._upload_wav(client, f"E2E V03 Vector Member {self.stamp}", seconds=6)
        self._poll_ready(client, own["id"])
        # 用成员自己转录中的真实短语做语义查询（mock embeddings 为字符 n-gram，自查询可稳定命中）
        transcript = self._json(client.get(f"/api/recordings/{own['id']}/transcript"))
        segments = transcript.get("segments") or []
        expect(segments, "成员录音转录为空，无法构造语义查询")
        # mock embeddings 为字符 n-gram 余弦：用成员自己前两段完整文本做查询可稳定高于 min_score(0.2)
        query = "，".join(str(seg.get("text") or "").strip() for seg in segments[:2]) or "项目进展"
        expect(query, "成员语义查询为空")
        # admin reindex 把成员录音也纳入索引
        self._json(self.client.post("/api/vector/reindex", json={"force": True}))
        member_hits = self._json(
            client.get("/api/vector/search", params={"q": query, "mode": "semantic", "limit": 10})
        ).get("results") or []
        member_ids = {item["id"] for item in self._json(client.get("/api/recordings", params={"page_size": 100}))["items"]}
        expect(member_hits, f"成员语义搜索无结果（query={query!r}；自有录音应命中）")
        leaked = [hit for hit in member_hits if int(hit.get("recording_id")) not in member_ids]
        expect(not leaked, f"成员语义搜索泄露他人录音：{[(h['recording_id'], h.get('recording_title')) for h in leaked]}")
        admin_hits = self._json(
            self.client.get("/api/vector/search", params={"q": query, "mode": "semantic", "limit": 10})
        ).get("results") or []
        expect(len(admin_hits) >= len(member_hits), "admin 语义结果不应少于 member")
        self.ctx["member_vector"] = {"user": member, "client": client, "recording_id": own["id"]}
        return f"member 结果 {len(member_hits)} 条且仅含自有录音；admin {len(admin_hits)} 条（query={query!r}）"

    def t_vector_fallback(self):
        self._set_embeddings("none")
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("embeddings_configured") is False, f"关闭 embedding 后状态异常：{status}")
        data = self._json(self.client.get("/api/vector/search", params={"q": "Graph500", "mode": "semantic"}))
        expect(data.get("fallback_reason"), f"未配置 embeddings 时语义搜索应返回 fallback_reason：{data}")
        resp = self.client.post("/api/vector/reindex", json={"force": True})
        expect(resp.status_code == 400, f"未配置 embeddings 时 reindex 期望 400，实际 {resp.status_code}")
        return f"embeddings=none → semantic fallback_reason={data.get('fallback_reason')!r}，reindex 400"

    def t_vector_restore(self):
        self._set_embeddings("mock")
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("embeddings_configured") is True, f"恢复 mock embeddings 失败：{status}")
        data = self._json(self.client.get("/api/vector/search", params={"q": "摘要", "mode": "hybrid"}))
        expect(not data.get("fallback_reason"), f"恢复后 hybrid 不应 fallback：{data}")
        return "已恢复 embeddings=mock，hybrid 搜索无 fallback"

    # ================================================================== 5. storage/regression
    def section_storage_regression(self) -> None:
        self.section("5. storage local / media Range / 问答回归")
        self.check("v03.storage.local-range", self.t_storage_local_range)
        self.check("v03.regression.demo-chat-sse", self.t_chat_sse)

    def t_storage_local_range(self):
        rec = self._upload_wav(self.client, f"E2E V03 Storage {self.stamp}", seconds=6)
        ready = self._poll_ready(self.client, rec["id"])
        media = self.client.get(f"/api/recordings/{rec['id']}/media", headers={"Range": "bytes=0-1023"})
        expect(media.status_code == 206, f"local 存储媒体 Range 期望 206，实际 {media.status_code}")
        expect(media.headers.get("content-range", "").startswith("bytes 0-1023/"), "Content-Range 异常")
        features = self.ctx.get("features") or self._json(self.client.get("/api/system/features"))
        expect(features["storage"]["backend"] == "local", "storage backend 应为 local")
        self.ctx["cleanup_ids"] = [rec["id"]]
        return f"local 存储 Range 206；source={ready.get('source')}"

    # ================================================================== runner
    def run_v03(self) -> int:
        print(f"Echoic v0.3 E2E Smoke — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"目标服务: {self.base_url} | data dir: {self.data_dir}")
        print(f"Python: {sys.version.split()[0]} | httpx: {httpx.__version__} | ffmpeg: {self.ffmpeg or '缺失'}")
        print("=" * 78)
        try:
            self.section_env()
            if self.ctx.get("token"):
                self.section_trash()
                self.section_batch()
                self.section_live()
                self.section_vector()
                self.section_storage_regression()
        except Exception as exc:  # noqa: BLE001 - 基础环节失败也要输出矩阵
            self.record("v03.fatal", "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
        return self._summary()

    def _summary(self) -> int:
        total = len(self.results)
        passed = sum(1 for row in self.results if row["status"] == "PASS")
        failed = [row for row in self.results if row["status"] == "FAIL"]
        skipped = sum(1 for row in self.results if row["status"] == "SKIP")
        print("\n" + "=" * 78)
        print("\033[1mv0.3 PASS/FAIL 矩阵\033[0m")
        print("-" * 78)
        for row in self.results:
            color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(row["status"], "")
            first_line = row["detail"].splitlines()[0] if row["detail"] else ""
            print(f"{color}{row['status']:<4}\033[0m {row['name']:<44} {row['duration']:6.2f}s  {first_line}")
        print("-" * 78)
        print(f"总计 {total} 项：\033[1;32m{passed} 通过\033[0m，\033[1;31m{len(failed)} 失败\033[0m，{skipped} 跳过")
        if failed:
            print("\n失败详情：")
            for row in failed:
                print(f"\n\033[1;31m✗ {row['name']}\033[0m")
                print(row["detail"])
            print("\n\033[1;31mE2E v0.3 SMOKE: FAIL\033[0m")
            return 1
        print("\n\033[1;32mE2E v0.3 SMOKE: PASS ✔\033[0m")
        return 0

    def close(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        super().close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Echoic v0.3 端到端冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ECHOIC_E2E_BASE_URL") or os.environ.get("ECHOIC_BASE_URL") or "http://127.0.0.1:8100",
    )
    parser.add_argument("--username", default=os.environ.get("ECHOIC_E2E_ADMIN_USERNAME") or os.environ.get("ECHOIC_ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or os.environ.get("ECHOIC_ADMIN_PASSWORD") or "admin123")
    parser.add_argument("--process-timeout", type=float, default=float(os.environ.get("ECHOIC_E2E_PROCESS_TIMEOUT") or 180))
    parser.add_argument("--data-dir", default=os.environ.get("ECHOIC_E2E_DATA_DIR") or "")
    parser.add_argument("--log", default=os.environ.get("ECHOIC_E2E_LOG_V03") or str(E2E_DIR / "last-run-v03.log"))
    args = parser.parse_args()

    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("w", encoding="utf-8")
    from smoke import Tee  # 延迟导入避免循环

    stdout_orig, stderr_orig = sys.stdout, sys.stderr
    sys.stdout = Tee(stdout_orig, log_fh)
    sys.stderr = Tee(stderr_orig, log_fh)
    smoke = None
    try:
        data_dir = Path(args.data_dir).expanduser() if args.data_dir else None
        smoke = V03Smoke(args.base_url, args.username, args.password, args.process_timeout, data_dir)
        code = smoke.run_v03()
    finally:
        if smoke is not None:
            smoke.close()
        print(f"\n日志已保存：{log_path}")
        sys.stdout, sys.stderr = stdout_orig, stderr_orig
        log_fh.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
