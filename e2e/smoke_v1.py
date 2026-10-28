#!/usr/bin/env python3
"""Echoic v1.0 端到端冒烟：视频分镜 / 审计日志 / Qdrant 向量后端 / 注册资料回归 / 分享。

在独立数据目录 + mock AI 管线的生产式服务上运行（task-30）：
    ECHOIC_DATA_DIR=$PWD/.cache/e2e-v1-data \\
    ECHOIC_VECTOR_BACKEND=qdrant ECHOIC_QDRANT_URL=http://127.0.0.1:16333 \\
    ECHOIC_QDRANT_API_KEY=... ECHOIC_ALLOW_REGISTRATION=true \\
    backend/.venv/bin/python e2e/smoke_v1.py --base-url http://127.0.0.1:8101 \\
        --data-dir $PWD/.cache/e2e-v1-data --expect-vector-backend qdrant \\
        --qdrant-url http://127.0.0.1:16333 --qdrant-api-key ...

覆盖（v1.0 新能力 + 关键回归）：
  0) health/features/static：health ok、version、vector/live/storage/trash 能力块、前端 index/manifest/sw
  1) 向量后端：配置 mock embeddings → status（auto 优先级/qdrant 后端）→ reindex →
     语义/混合搜索 →（若真实 Qdrant 可用）直连 Qdrant REST 校验集合与 points_count
  2) 视频分镜：ffmpeg 生成 3 段硬切视频 → 上传处理 → scenes 列表（有序/时间戳/数量）→
     分镜 JPEG（content-type/魔数/Cache-Control）→ 未登录 401 / 成员越权 404 / 不存在 404
  3) 描述自动生成 + 回收站回归：空描述上传 → ready 后自动生成描述；trash → 回收站可见 → restore
  4) 标签/设置审计埋点：tag.create、settings.update 产生审计记录
  5) 分享/公开页回归：视频分享（无密码、允许下载）→ 公开元数据（标题/描述/media_type/权限）→
     public media Range 206 → transcript/insights/download → view_count 递增
  6) 注册 / 个人资料回归：allow_registration、注册 201、重复 409、登录、PATCH /auth/me、
     改密（旧密码错误/过短/成功）→ 旧密码失效、新密码可登录
  7) 审计日志：列表/过滤（action/target_type/q）/分页/排序、成员 403、未登录 401、
     关键动作齐全（login/upload/register/profile_update/change_password/trash/restore/tag.create/
     settings.update/share.create）、detail 不含密码/Key 明文、target_id 正确
  8) 汇总输出 PASS/FAIL/SKIP；上下文 JSON 供截图脚本复用（视频/分享 token 等）

退出码：0 = 全部通过（允许 SKIP）；1 = 存在 FAIL。日志默认 e2e/last-run-v1.log。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

import httpx

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(E2E_DIR))

from smoke import Smoke, expect  # noqa: E402
from smoke_v0310 import V0310Smoke  # noqa: E402  （复用 v0.3.10 的 HTTP/上传辅助方法）

JPEG_MAGIC = b"\xff\xd8\xff"
_SK_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{8,}")


class V1Smoke(V0310Smoke):
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        process_timeout: float,
        data_dir: Path | None,
        expected_vector_backend: str,
        qdrant_url: str,
        qdrant_api_key: str,
        context_out: Path,
    ) -> None:
        super().__init__(
            base_url,
            username,
            password,
            process_timeout,
            data_dir,
            expected_vector_backend or "auto",
        )
        self.expected_vector_backend = (expected_vector_backend or "").strip().lower()
        self.qdrant_url = qdrant_url.rstrip("/")
        self.qdrant_api_key = qdrant_api_key
        self.context_out = context_out
        self.stamp = time.strftime("%H%M%S")
        self.member: dict = {}
        self.member_password = f"V1Member{self.stamp}!"
        self.member_client: httpx.Client | None = None

    # ------------------------------------------------------------------ helpers
    def _anon(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        )

    def _set_embeddings(self, provider: str) -> dict:
        return self._json(self.client.put("/api/settings", json={"ai": {"embeddings": {"provider": provider}}}))

    def _new_member(self, key: str = "member") -> dict:
        username = f"e2e_v1_{key}_{self.stamp}"
        data = self._json(
            self.client.post(
                "/api/users",
                json={
                    "username": username,
                    "password": self.member_password,
                    "display_name": f"v1 {key}",
                    "role": "member",
                },
            ),
            expected=201,
        )
        data["_password"] = self.member_password
        self.member = data
        return data

    def _qdrant_get(self, path: str) -> dict:
        headers = {"api-key": self.qdrant_api_key} if self.qdrant_api_key else {}
        with httpx.Client(timeout=10.0, trust_env=False) as client:
            resp = client.get(f"{self.qdrant_url}{path}", headers=headers)
            resp.raise_for_status()
            return resp.json()

    def _make_cuts_video(self, path: Path, *, segment_seconds: int = 2, with_audio: bool = True) -> Path:
        """3 hard scene cuts (testsrc -> smptebars -> rgbtestsrc), optional AAC audio."""
        expect(self.ffmpeg, "未找到 ffmpeg，无法生成视频分镜测试素材")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = "320x180"
        args: list[str] = [
            self.ffmpeg or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=25:duration={segment_seconds}",
            "-f", "lavfi", "-i", f"smptebars=size={size}:rate=25:duration={segment_seconds}",
            "-f", "lavfi", "-i", f"rgbtestsrc=size={size}:rate=25:duration={segment_seconds}",
        ]
        filters = "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]"
        maps = ["-map", "[v]"]
        if with_audio:
            args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={segment_seconds * 3}"]
            maps += ["-map", "3:a", "-shortest"]
        args += ["-filter_complex", filters, *maps, "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p"]
        if with_audio:
            args += ["-c:a", "aac"]
        args.append(str(path))
        subprocess.run(args, check=True, capture_output=True)
        return path

    def _upload_video(self, client: httpx.Client, title: str, seconds: int = 2) -> dict:
        path = self._make_cuts_video(self.tmp / f"e2e-v1-cuts-{self.stamp}.mp4",
                                     segment_seconds=seconds)
        data = {"title": title, "language": "zh"}
        with path.open("rb") as fh:
            resp = client.post(
                "/api/recordings/upload",
                files={"file": (path.name, fh, "video/mp4")},
                data=data,
                timeout=300.0,
            )
        return self._json(resp, expected=201)

    def _audit_page(self, client: httpx.Client, **params) -> dict:
        return self._json(client.get("/api/system/audit-logs", params=params))

    # ================================================================== 0. env
    def section_env(self) -> None:
        self.section("0. health / features / 静态资源（v1.0）")
        if not self.check("v1.health", self.t_health):
            raise RuntimeError("服务不可用")
        self.check("v1.auth.login-admin", self.t_login)
        self.check("v1.features", self.t_features)
        self.check("v1.static", self.t_static)

    def t_health(self):
        data = self._wait_health(timeout=60)
        expect(data.get("status") == "ok", f"health 应为 ok：{data}")
        version = str(data.get("version") or "")
        expect(version, f"health 缺少 version：{data}")
        self.ctx["version"] = version
        expect(data.get("db") == "ok", f"db 状态异常：{data}")
        expect(data.get("media") == "ok", f"media 状态异常：{data}")
        # allow_registration 由注册/资料回归小节显式开启（verify.sh 默认数据目录未必预置）
        return f"version={version} db={data.get('db')} media={data.get('media')} " \
               f"ffmpeg={data.get('ffmpeg')} worker={data.get('worker')} " \
               f"allow_registration={data.get('allow_registration')}"

    def t_features(self):
        features = self._json(self.client.get("/api/system/features"))
        self.ctx["features"] = features
        vector = features.get("vector") or {}
        expect(vector.get("search_enabled") is True, f"features.vector 缺少 search_enabled：{features}")
        expect(features.get("live", {}).get("enabled") is True, f"live 应启用：{features.get('live')}")
        expect(features.get("storage", {}).get("backend") == "local", f"storage 应为 local：{features}")
        tid = features.get("trash", {}).get("retention_days")
        expect(int(tid or 0) >= 1, f"trash.retention_days 异常：{features.get('trash')}")
        return f"vector.backend={vector.get('backend')} live={features['live'].get('provider')} " \
               f"storage={features['storage'].get('backend')} trash={tid}d"

    def t_static(self):
        index = self.client.get("/")
        expect(index.status_code == 200, f"GET / 期望 200，实际 {index.status_code}")
        expect("<div id=\"root\"" in index.text or "id=root" in index.text, "前端 index 缺少 #root 挂载点")
        expect("/assets/" in index.text, "前端 index 未引用构建产物 /assets/")
        manifest = self.client.get("/manifest.webmanifest")
        expect(manifest.status_code == 200, f"manifest 期望 200，实际 {manifest.status_code}")
        mdata = manifest.json()
        expect(mdata.get("name") or mdata.get("short_name"), f"manifest 缺少 name：{str(mdata)[:200]}")
        expect(mdata.get("icons"), "manifest 缺少 icons（PWA 安装要求）")
        sw = self.client.get("/sw.js")
        expect(sw.status_code == 200, f"sw.js 期望 200，实际 {sw.status_code}")
        expect("addEventListener" in sw.text or "self." in sw.text, "sw.js 内容异常")
        return f"index/manifest({mdata.get('short_name')})/sw 均可访问"

    # ================================================================== 1. vector
    def section_vector(self) -> None:
        self.section("1. 向量后端（auto 优先级 / Qdrant / reindex / 语义搜索）")
        self.check("v1.vector.configure-embeddings", self.t_vector_setup)
        if self.ctx.get("vector_ready"):
            self.check("v1.vector.reindex", self.t_vector_reindex)
            self.check("v1.vector.status", self.t_vector_status)
            if self.ctx.get("vector_backend") == "qdrant":
                self.check("v1.vector.qdrant-server-collection", self.t_qdrant_server)
            else:
                self.skip(
                    "v1.vector.qdrant-server-collection",
                    f"当前服务 vector backend={self.ctx.get('vector_backend')}（非 qdrant），"
                    "真实 Qdrant 服务校验跳过；Qdrant 场景见 v1.0 QA 主环境",
                )
            self.check("v1.vector.semantic-search", self.t_vector_semantic)
            self.check("v1.vector.hybrid-search", self.t_vector_hybrid)
        else:
            for name in (
                "v1.vector.reindex",
                "v1.vector.status",
                "v1.vector.qdrant-server-collection",
                "v1.vector.semantic-search",
                "v1.vector.hybrid-search",
            ):
                self.skip(name, "embedding 配置失败，向量链路跳过")

    def t_vector_setup(self):
        self._set_embeddings("mock")
        status = self._json(self.client.get("/api/vector/status"))
        expect(status.get("embeddings_configured") is True, f"mock embeddings 未生效：{status}")
        expect(status.get("available") is True, f"向量后端不可用（fallback={status.get('fallback_reason')}）")
        backend = str(status.get("backend") or "")
        self.ctx["vector_backend"] = backend
        expected = self.expected_vector_backend
        if expected and expected not in ("auto",):
            expect(backend == expected,
                   f"向量后端应为 {expected}（ECHOIC_VECTOR_BACKEND），实际 {backend}：{status}")
        self.ctx["vector_ready"] = True
        return f"embeddings=mock backend={backend} chunks_total={status.get('chunks_total')} " \
               f"expected={expected or 'auto'}"

    def t_vector_reindex(self):
        result = self._json(self.client.post("/api/vector/reindex", json={"force": True}))
        expect(result.get("ok") is True, f"reindex 失败：{result}")
        expect(int(result.get("chunks") or 0) > 0, f"reindex 未写入 chunks：{result}")
        expect(not result.get("errors"), f"reindex 出现错误：{result.get('errors')}")
        return f"recordings={result.get('processed_recordings')} chunks={result.get('chunks')} " \
               f"skipped={result.get('skipped')}"

    def t_vector_status(self):
        status = self._json(self.client.get("/api/vector/status"))
        self.ctx["vector_status"] = status
        expect(int(status.get("chunks_indexed") or 0) > 0, f"reindex 后 chunks_indexed=0：{status}")
        expect(int(status.get("stale_chunks") or 0) == 0, f"reindex 后仍有 stale chunks：{status}")
        expect(status.get("embedding_model"), f"缺少 embedding_model：{status}")
        expect(int(status.get("dim") or 0) > 0, f"缺少 dim：{status}")
        return f"backend={status.get('backend')} indexed={status.get('chunks_indexed')} " \
               f"model={status.get('embedding_model')} dim={status.get('dim')}"

    def t_qdrant_server(self):
        expect(self.qdrant_url, "未提供 --qdrant-url，无法校验真实 Qdrant 服务")
        status = self.ctx.get("vector_status") or {}
        dim = int(status.get("dim") or 0)
        expect(dim > 0, f"无法从 status 获取维度：{status}")
        prefix = str(os.environ.get("ECHOIC_E2E_QDRANT_COLLECTION") or "echoic_chunks")
        collection = f"{prefix}_{dim}"
        detail = self._qdrant_get(f"/collections/{collection}")
        result = detail.get("result") or {}
        points = int(result.get("points_count") or 0)
        expect(points > 0, f"Qdrant 集合 {collection} points_count={points}：{str(detail)[:300]}")
        self.ctx["qdrant_collection"] = collection
        self.ctx["qdrant_points"] = points
        return f"Qdrant {self.qdrant_url} 集合 {collection} points_count={points} " \
               f"status={result.get('status')}"

    def t_vector_semantic(self):
        q = "摘要 两周 交付节点"
        data = self._json(self.client.get("/api/vector/search",
                                          params={"q": q, "mode": "semantic", "limit": 8}))
        expect(data.get("mode") == "semantic", f"搜索 mode 回显异常：{data.get('mode')}")
        results = data.get("results") or []
        expect(results, f"语义搜索无结果（q={q}，backend={self.ctx.get('vector_backend')}）：{data}")
        expect(any(hit.get("recording_title") for hit in results), "语义结果缺少 recording_title")
        expect(all(float(hit.get("score") or 0) > 0 for hit in results), "语义结果 score 异常")
        self.ctx["semantic_results"] = results
        return f"semantic 命中 {len(results)} 条（top={results[0].get('score')} " \
               f"fallback={data.get('fallback_reason')}）"

    def t_vector_hybrid(self):
        data = self._json(self.client.get("/api/vector/search",
                                          params={"q": "Graph500", "mode": "hybrid", "limit": 8}))
        results = data.get("results") or []
        expect(results, f"混合搜索无结果：{data}")
        return f"hybrid 命中 {len(results)} 条（fallback={data.get('fallback_reason')}）"

    # ================================================================== 2. scenes
    def section_scenes(self) -> None:
        self.section("2. 视频分镜（ffmpeg scene detection / API / 权限）")
        video_ready = self.check("v1.scenes.upload-video", self.t_upload_video)
        if not video_ready:
            for name in ("v1.scenes.pipeline-ready", "v1.scenes.list", "v1.scenes.image",
                         "v1.scenes.auth", "v1.scenes.description"):
                self.skip(name, "视频上传失败")
            return
        self.check("v1.scenes.pipeline-ready", self.t_video_ready)
        self.check("v1.scenes.list", self.t_scenes_list)
        self.check("v1.scenes.image", self.t_scenes_image)
        self.check("v1.scenes.auth", self.t_scenes_auth)
        self.check("v1.scenes.description", self.t_video_description)

    def t_upload_video(self):
        title = f"E2E v1.0 视频分镜 {self.stamp}"
        recording = self._upload_video(self.client, title, seconds=2)
        expect(recording.get("id"), f"视频上传响应缺少 id：{recording}")
        expect(recording.get("media_type") == "video",
               f"media_type 应为 video：{recording.get('media_type')}")
        self.ctx["video_id"] = int(recording["id"])
        self.ctx["video_title"] = title
        return f"recording_id={recording['id']} media_type={recording.get('media_type')} " \
               f"status={recording.get('status')}"

    def t_video_ready(self):
        detail, stages = self._poll_recording(self.ctx["video_id"], self.process_timeout)
        expect(detail.get("status") == "ready",
               f"视频处理状态 {detail.get('status')}：{detail.get('error_message')}")
        self.ctx["video_detail"] = detail
        return f"status=ready stages={stages} duration_ms={detail.get('duration_ms')}"

    def t_scenes_list(self):
        data = self._json(self.client.get(f"/api/recordings/{self.ctx['video_id']}/scenes"))
        items = data.get("items") or []
        expect(len(items) >= 2, f"3 段硬切视频应至少抽出 2 个分镜，实际 {len(items)}：{items}")
        indexes = [int(item.get("idx")) for item in items]
        times = [int(item.get("time_ms") or 0) for item in items]
        expect(indexes == sorted(indexes), f"分镜 idx 未升序：{indexes}")
        expect(times == sorted(times), f"分镜 time_ms 未升序：{times}")
        expect(all(item.get("image_filename") for item in items), "分镜缺少 image_filename")
        expect(all(item.get("width") and item.get("height") for item in items), "分镜缺少宽高")
        self.ctx["scene_items"] = items
        return f"分镜数={len(items)} times_ms={times} size={items[0].get('width')}x{items[0].get('height')}"

    def t_scenes_image(self):
        items = self.ctx.get("scene_items") or []
        expect(items, "无分镜可校验图片")
        image = self.client.get(
            f"/api/recordings/{self.ctx['video_id']}/scenes/{items[0]['id']}/image"
        )
        expect(image.status_code == 200, f"分镜图片期望 200，实际 {image.status_code}")
        content_type = image.headers.get("content-type", "")
        expect("image/jpeg" in content_type, f"分镜图片 content-type 异常：{content_type!r}")
        expect(image.content.startswith(JPEG_MAGIC), f"分镜图片不是 JPEG（头部 {image.content[:8]!r}）")
        expect(len(image.content) > 500, f"分镜图片过小：{len(image.content)} bytes")
        cache_control = image.headers.get("cache-control", "")
        expect("private" in cache_control, f"分镜图片应允许私有缓存，实际 {cache_control!r}")
        return f"JPEG {len(image.content)} bytes type={content_type} cache={cache_control}"

    def t_scenes_auth(self):
        video_id = self.ctx["video_id"]
        scene_id = (self.ctx.get("scene_items") or [{}])[0].get("id")
        with self._anon() as anon:
            unauth = anon.get(f"/api/recordings/{video_id}/scenes")
            expect(unauth.status_code == 401, f"未登录访问分镜期望 401，实际 {unauth.status_code}")
        member = self._new_member("scenes")
        self.member = member
        self.member_client = self._login_client(member["username"], self.member_password)
        foreign = self.member_client.get(f"/api/recordings/{video_id}/scenes")
        expect(foreign.status_code == 404,
               f"成员访问他人分镜期望 404（不泄露存在性），实际 {foreign.status_code}")
        missing = self.client.get(f"/api/recordings/{video_id}/scenes/999999/image")
        expect(missing.status_code == 404, f"不存在的分镜期望 404，实际 {missing.status_code}")
        return "未登录 401 / 成员越权 404 / 不存在 404"

    def t_video_description(self):
        detail = self._json(self.client.get(f"/api/recordings/{self.ctx['video_id']}"))
        description = str(detail.get("description") or "").strip()
        expect(description, "视频录音描述为空（mock 管线应自动生成描述）")
        self.ctx["video_description"] = description
        return f"自动描述（{len(description)} 字）：{description[:80]}"

    # ======================================================= 3. description/trash
    def section_description_trash(self) -> None:
        self.section("3. 描述自动生成 + 回收站回归")
        created = self.check("v1.desc.upload-empty", self.t_upload_audio)
        if created:
            self.check("v1.desc.auto-generated", self.t_audio_description)
            self.check("v1.trash.soft-delete", self.t_trash)
            self.check("v1.trash.restore", self.t_restore)
        else:
            for name in ("v1.desc.auto-generated", "v1.trash.soft-delete", "v1.trash.restore"):
                self.skip(name, "音频上传失败")

    def t_upload_audio(self):
        rec = self._upload_wav(self.client, f"E2E v1.0 描述回归 {self.stamp}", seconds=8,
                               description="")
        self.ctx["audio_id"] = int(rec["id"])
        return f"recording_id={rec['id']} status={rec.get('status')}"

    def t_audio_description(self):
        self._poll_ready(self.client, self.ctx["audio_id"])
        detail = self._json(self.client.get(f"/api/recordings/{self.ctx['audio_id']}"))
        description = str(detail.get("description") or "").strip()
        expect(description, "空描述录音 ready 后仍未自动生成描述")
        self.ctx["audio_description"] = description
        return f"自动描述：{description[:80]}"

    def t_trash(self):
        rid = self.ctx["audio_id"]
        data = self._json(self.client.delete(f"/api/recordings/{rid}"))
        expect(data.get("ok") is True, f"软删除响应异常：{data}")
        expect(data.get("restore_before"), f"软删除缺少 restore_before：{data}")
        trashed = self._json(self.client.get("/api/recordings", params={"trashed": True, "page_size": 100}))
        expect(any(int(item["id"]) == rid for item in trashed.get("items") or []),
               "回收站列表未包含刚删除的录音")
        missing = self.client.get(f"/api/recordings/{rid}/scenes")
        expect(missing.status_code == 404, f"回收站录音分镜期望 404，实际 {missing.status_code}")
        return f"trash ok restore_before={data.get('restore_before')}，回收站可见且详情不可访问"

    def t_restore(self):
        rid = self.ctx["audio_id"]
        data = self._json(self.client.post(f"/api/recordings/{rid}/restore"))
        ok_flag = data.get("ok") is True if "ok" in data else int(data.get("id") or 0) == int(rid)
        expect(ok_flag, f"恢复响应异常：{str(data)[:300]}")
        detail = self._json(self.client.get(f"/api/recordings/{rid}"))
        expect(detail.get("status") == "ready", f"恢复后状态异常：{detail.get('status')}")
        expect(detail.get("deleted_at") is None, f"恢复后 deleted_at 非空：{detail.get('deleted_at')}")
        return "restore ok，status=ready 且 deleted_at=null"

    # =========================================================== 4. tag/settings
    def section_tag_settings(self) -> None:
        self.section("4. 标签 / 设置（为审计埋点）")
        self.check("v1.tag.create", self.t_tag_create)
        self.check("v1.settings.update", self.t_settings_update)

    def t_tag_create(self):
        name = f"E2E v1.0 标签 {self.stamp}"
        created = self._json(self.client.post("/api/tags", json={"name": name, "color": "#1677ff"}),
                             expected=201)
        expect(created.get("id"), f"创建标签响应缺少 id：{created}")
        self.ctx["tag_id"] = created["id"]
        return f"tag_id={created['id']} name={name}"

    def t_settings_update(self):
        original = self._json(self.client.get("/api/settings"))
        original_temp = float(original["ai"]["llm"]["temperature"])
        updated = self._json(self.client.put("/api/settings", json={"ai": {"llm": {"temperature": 0.25}}}))
        expect(abs(float(updated["ai"]["llm"]["temperature"]) - 0.25) < 1e-6,
               f"设置更新失败：{updated['ai']['llm']}")
        self._json(self.client.put("/api/settings",
                                   json={"ai": {"llm": {"temperature": original_temp}}}))
        return f"temperature {original_temp} -> 0.25 -> {original_temp}（已恢复）"

    # ================================================================== 5. share
    def section_share(self) -> None:
        self.section("5. 分享 / 公开页回归（视频 + Range + 权限）")
        if not self.ctx.get("video_id"):
            self.skip("v1.share.create", "缺少视频录音")
            return
        self.check("v1.share.create", self.t_share_create)
        if self.ctx.get("share_token"):
            self.check("v1.share.public-metadata", self.t_share_metadata)
            self.check("v1.share.public-media-range", self.t_share_media_range)
            self.check("v1.share.public-transcript", self.t_share_transcript)
            self.check("v1.share.public-insights", self.t_share_insights)
            self.check("v1.share.public-download", self.t_share_download)
            self.check("v1.share.view-count", self.t_share_view_count)

    def t_share_create(self):
        data = self._json(
            self.client.post(
                f"/api/recordings/{self.ctx['video_id']}/shares",
                json={
                    "include_transcript": True,
                    "include_insights": True,
                    "allow_download": True,
                },
            ),
            expected=201,
        )
        expect(data.get("token"), f"分享创建响应缺少 token：{data}")
        expect(data.get("url_path", "").startswith("/s/"), f"url_path 异常：{data.get('url_path')}")
        expect(data.get("has_password") is False, f"未设密码却 has_password=true：{data}")
        self.ctx["share_token"] = data["token"]
        self.ctx["share"] = data
        return f"share_id={data.get('id')} token={data['token'][:10]}… url={data.get('url_path')}"

    def t_share_metadata(self):
        with self._anon() as anon:
            data = self._json(anon.get(f"/api/public/shares/{self.ctx['share_token']}"))
        share = data.get("share") or {}
        recording = data.get("recording") or {}
        expect(share.get("has_password") is False, f"公开分享 has_password 异常：{share}")
        expect(share.get("include_transcript") is True and share.get("include_insights") is True,
               f"公开分享 include 标记异常：{share}")
        expect(share.get("allow_download") is True, f"公开分享 allow_download 异常：{share}")
        expect(recording.get("title") == self.ctx.get("video_title"),
               f"公开分享标题不匹配：{recording.get('title')} != {self.ctx.get('video_title')}")
        expect(recording.get("description"), "公开分享描述为空（视频描述应自动生成）")
        expect(recording.get("media_type") == "video",
               f"公开分享 media_type 应为 video：{recording.get('media_type')}")
        expect(recording.get("speakers") or recording.get("tags") is not None,
               f"公开分享缺少 speakers/tags 字段：{recording}")
        self.ctx["public_share"] = data
        return f"title={recording.get('title')} media={recording.get('media_type')} " \
               f"desc={str(recording.get('description'))[:40]}…"

    def t_share_media_range(self):
        with self._anon() as anon:
            resp = anon.get(
                f"/api/public/shares/{self.ctx['share_token']}/media",
                headers={"Range": "bytes=0-1023"},
            )
        expect(resp.status_code == 206,
               f"公开媒体 Range 期望 206，实际 {resp.status_code}：{resp.text[:200]}")
        content_range = resp.headers.get("content-range", "")
        expect(content_range.startswith("bytes 0-1023/"), f"Content-Range 异常：{content_range!r}")
        expect(len(resp.content) == 1024, f"Range 返回 {len(resp.content)} 字节 != 1024")
        content_type = resp.headers.get("content-type", "")
        expect(content_type.startswith("video/") or "mp4" in content_type,
               f"公开媒体 content-type 异常：{content_type!r}")
        return f"206 {content_range} type={content_type}"

    def t_share_transcript(self):
        with self._anon() as anon:
            data = self._json(anon.get(f"/api/public/shares/{self.ctx['share_token']}/transcript"))
        segments = data.get("segments") or []
        expect(segments, f"公开转录为空：{str(data)[:200]}")
        self.ctx["public_segments"] = segments
        return f"public transcript segments={len(segments)}"

    def t_share_insights(self):
        with self._anon() as anon:
            data = self._json(anon.get(f"/api/public/shares/{self.ctx['share_token']}/insights"))
        expect(isinstance(data, dict) and data, f"公开分析为空：{str(data)[:200]}")
        expect("missing" in data or data.get("summary") or data.get("chapters"),
               f"公开分析结构异常：{str(data)[:200]}")
        return f"public insights keys={sorted(data)[:8]}"

    def t_share_download(self):
        with self._anon() as anon:
            resp = anon.get(f"/api/public/shares/{self.ctx['share_token']}/download")
        expect(resp.status_code == 200, f"公开下载期望 200，实际 {resp.status_code}")
        disposition = resp.headers.get("content-disposition", "")
        expect("attachment" in disposition.lower(), f"公开下载缺少 attachment：{disposition!r}")
        expect(len(resp.content) > 1000, f"公开下载内容过小：{len(resp.content)}")
        return f"download 200 {len(resp.content)} bytes"

    def t_share_view_count(self):
        with self._anon() as anon:
            data = self._json(anon.get(f"/api/public/shares/{self.ctx['share_token']}"))
        view_count = int((data.get("share") or {}).get("view_count") or 0)
        expect(view_count >= 1, f"公开访问后 view_count 未递增：{data.get('share')}")
        return f"view_count={view_count}"

    # ================================================================== 6. auth
    def section_auth(self) -> None:
        self.section("6. 注册 / 个人资料 / 改密回归（v0.3.10 能力）")
        allow = self.check("v1.auth.allow-registration", self.t_allow_registration)
        if not allow:
            for name in ("v1.auth.register", "v1.auth.register-duplicate", "v1.auth.profile-update",
                         "v1.auth.change-password", "v1.auth.new-password-login"):
                self.skip(name, "allow_registration 未开启")
            return
        self.check("v1.auth.register", self.t_register)
        self.check("v1.auth.register-duplicate", self.t_register_duplicate)
        self.check("v1.auth.profile-update", self.t_profile_update)
        self.check("v1.auth.change-password", self.t_change_password)
        self.check("v1.auth.new-password-login", self.t_new_password_login)

    def t_allow_registration(self):
        health = self._json(self.client.get("/api/system/health"))
        if health.get("allow_registration") is not True:
            self._json(self.client.put("/api/settings", json={"system": {"allow_registration": True}}))
            health = self._json(self.client.get("/api/system/health"))
        expect(health.get("allow_registration") is True,
               f"allow_registration 未开启：{health.get('allow_registration')}")
        return "health.allow_registration=true"

    def t_register(self):
        username = f"e2e_v1_reg_{self.stamp}"
        password = f"V1Reg{self.stamp}!"
        data = self._json(
            self.client.post(
                "/api/auth/register",
                json={
                    "username": username,
                    "password": password,
                    "display_name": f"v1 注册 {self.stamp}",
                    "email": f"{username}@example.com",
                },
            ),
            expected=201,
        )
        expect(data.get("username") == username, f"注册响应用户异常：{data}")
        expect(data.get("role") == "member", f"新注册用户角色应为 member：{data.get('role')}")
        self.ctx["reg_user"] = {"username": username, "password": password}
        return f"registered {username} role={data.get('role')}"

    def t_register_duplicate(self):
        user = self.ctx["reg_user"]
        resp = self.client.post(
            "/api/auth/register",
            json={"username": user["username"], "password": user["password"]},
        )
        expect(resp.status_code == 409, f"重复注册期望 409，实际 {resp.status_code}")
        return "重复用户名 409"

    def t_profile_update(self):
        user = self.ctx["reg_user"]
        client = self._login_client(user["username"], user["password"])
        self.ctx["reg_client"] = client
        token = client.cookies.get("echoic_token")
        expect(token, "注册用户登录未种下 Cookie")
        updated = self._json(
            client.patch("/api/auth/me", json={"display_name": "v1 资料更新", "email": "v1-updated@example.com"})
        )
        body = updated.get("user") if "user" in updated else updated
        expect(body.get("display_name") == "v1 资料更新", f"资料更新失败：{updated}")
        expect(body.get("email") == "v1-updated@example.com", f"邮箱更新失败：{updated}")
        me = self._json(client.get("/api/auth/me"))
        me_body = me.get("user") if "user" in me else me
        expect(me_body.get("display_name") == "v1 资料更新", f"GET /auth/me 不一致：{me}")
        return f"profile updated: {me_body.get('display_name')} <{me_body.get('email')}>"

    def t_change_password(self):
        client = self.ctx.get("reg_client")
        user = self.ctx["reg_user"]
        wrong = client.post(
            "/api/auth/change-password",
            json={"old_password": "wrong-password", "new_password": "AnotherPass123"},
        )
        expect(wrong.status_code in (400, 401),
               f"旧密码错误期望 400/401，实际 {wrong.status_code}")
        short = client.post(
            "/api/auth/change-password",
            json={"old_password": user["password"], "new_password": "short"},
        )
        expect(short.status_code in (400, 422),
               f"过短新密码期望 400/422，实际 {short.status_code}")
        new_password = f"V1New{self.stamp}!"
        ok = self._json(
            client.post(
                "/api/auth/change-password",
                json={"old_password": user["password"], "new_password": new_password},
            )
        )
        expect(ok.get("ok") is True, f"改密失败：{ok}")
        user["new_password"] = new_password
        return "旧密码错误 400/401；新密码过短 422；改密成功"

    def t_new_password_login(self):
        user = self.ctx["reg_user"]
        old_client = self._anon()
        try:
            old_resp = old_client.post(
                "/api/auth/login",
                json={"username": user["username"], "password": user["password"]},
            )
            expect(old_resp.status_code == 401, f"旧密码应失效，实际 {old_resp.status_code}")
        finally:
            old_client.close()
        new_client = self._login_client(user["username"], user["new_password"])
        self.ctx["reg_client"] = new_client
        me = self._json(new_client.get("/api/auth/me"))
        me_body = me.get("user") if "user" in me else me
        expect(me_body.get("username") == user["username"], "新密码登录用户不一致")
        return "旧密码 401，新密码登录成功"

    # ================================================================== 7. audit
    def section_audit(self) -> None:
        self.section("7. 审计日志（admin 浏览器 / 过滤 / 权限 / 脱敏）")
        self.check("v1.audit.list", self.t_audit_list)
        self.check("v1.audit.expected-actions", self.t_audit_actions)
        self.check("v1.audit.filters", self.t_audit_filters)
        self.check("v1.audit.pagination", self.t_audit_pagination)
        self.check("v1.audit.member-forbidden", self.t_audit_member)
        self.check("v1.audit.unauthorized", self.t_audit_unauth)
        self.check("v1.audit.no-secrets", self.t_audit_no_secrets)

    def t_audit_list(self):
        page = self._audit_page(self.client, page=1, page_size=50)
        items = page.get("items") or []
        total = int(page.get("total") or 0)
        expect(total > 0, f"审计日志总数为 0：{page}")
        expect(items, "审计日志 items 为空")
        expect(len(items) <= 50, f"page_size 未生效：{len(items)}")
        ids = [int(item.get("id") or 0) for item in items]
        expect(ids == sorted(ids, reverse=True), f"审计日志未按 id 倒序：{ids[:10]}")
        self.ctx["audit_items"] = items
        self.ctx["audit_total"] = total
        return f"total={total} page_items={len(items)} latest={items[0].get('action')}"

    def t_audit_actions(self):
        items = self.ctx.get("audit_items") or []
        actions = {item.get("action") for item in items}
        required = {
            "auth.login",
            "recording.upload",
            "recording.trash",
            "recording.restore",
            "tag.create",
            "settings.update",
            "share.create",
            "auth.register",
            "auth.profile_update",
            "auth.change_password",
        }
        missing = sorted(required - actions)
        # 审计是 best-effort；个别动作可能因实现缺失而缺失 —— 每个缺失都单独记录，便于定位
        expect(not missing, f"缺少审计动作：{missing}；实际动作（前 30）：{sorted(actions)[:30]}")
        upload_entries = [i for i in items if i.get("action") == "recording.upload"]
        expect(upload_entries, "缺少 recording.upload 审计记录")
        expect(all(item.get("target_type") == "recording" for item in upload_entries[:3]),
               f"upload 审计 target_type 异常：{upload_entries[:3]}")
        return f"关键动作齐全（{len(required)} 类），upload entries={len(upload_entries)}"

    def t_audit_filters(self):
        upload = self._audit_page(self.client, action="recording.upload", page_size=20)
        expect(int(upload.get("total") or 0) >= 1, f"action 过滤无结果：{upload}")
        expect(all(item.get("action") == "recording.upload" for item in upload.get("items") or []),
               "action 过滤结果包含其他动作")
        target = self._audit_page(self.client, target_type="recording", page_size=20)
        expect(int(target.get("total") or 0) >= 1, f"target_type 过滤无结果：{target}")
        expect(all(item.get("target_type") == "recording" for item in target.get("items") or []),
               "target_type 过滤结果异常")
        q = self._audit_page(self.client, q="recording.", page_size=20)
        expect(int(q.get("total") or 0) >= 1, f"q 模糊搜索无结果：{q}")
        share_filter = self._audit_page(self.client, action="share.create", page_size=20)
        share_entries = share_filter.get("items") or []
        video_id = int(self.ctx.get("video_id") or -1)
        expect(
            any(int((item.get("detail") or {}).get("recording_id") or 0) == video_id
                for item in share_entries),
            f"share.create 未在 detail.recording_id 记录视频：{share_entries[:3]}",
        )
        return f"action/target_type/q/share.detail 过滤均正常（upload total={upload.get('total')}）"

    def t_audit_pagination(self):
        page1 = self._audit_page(self.client, page=1, page_size=2)
        page2 = self._audit_page(self.client, page=2, page_size=2)
        items1 = page1.get("items") or []
        items2 = page2.get("items") or []
        expect(len(items1) == 2, f"page_size=2 返回 {len(items1)} 条")
        expect(len(items2) == 2, f"page=2 返回 {len(items2)} 条")
        expect({i["id"] for i in items1}.isdisjoint({i["id"] for i in items2}), "分页出现重复记录")
        expect(page1.get("total") == page2.get("total"), "分页 total 不一致")
        expect(int(page1["total"]) >= 4, f"总记录不足以验证分页：{page1.get('total')}")
        return f"page_size=2 稳定，total={page1.get('total')}"

    def t_audit_member(self):
        client = self.member_client
        expect(client is not None, "缺少成员客户端（前置分镜/注册用例未执行）")
        resp = client.get("/api/system/audit-logs")
        expect(resp.status_code == 403, f"成员访问审计日志期望 403，实际 {resp.status_code}")
        return "member 403"

    def t_audit_unauth(self):
        with self._anon() as anon:
            resp = anon.get("/api/system/audit-logs")
        expect(resp.status_code == 401, f"未登录访问审计日志期望 401，实际 {resp.status_code}")
        return "unauth 401"

    def t_audit_no_secrets(self):
        items = self.ctx.get("audit_items") or []
        expect(items, "审计日志为空，无法校验脱敏")
        admin_password = os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or self.password
        secret_key = os.environ.get("ECHOIC_SECRET_KEY") or ""
        leaks: list[str] = []
        for item in items:
            blob = json.dumps(item, ensure_ascii=False)
            if admin_password and admin_password in blob:
                leaks.append(f"admin password in {item.get('action')}")
            if secret_key and secret_key in blob:
                leaks.append(f"secret_key in {item.get('action')}")
            if _SK_PATTERN.search(blob):
                leaks.append(f"api key pattern in {item.get('action')}")
            detail = item.get("detail") or {}
            for key in ("password", "new_password", "old_password", "api_key", "secret_key", "token"):
                if isinstance(detail, dict) and key in detail and detail[key] not in (None, "", "***"):
                    leaks.append(f"detail.{key} plaintext in {item.get('action')}")
        expect(not leaks, f"审计日志疑似泄露敏感信息：{leaks[:5]}")
        actions_with_detail = sum(1 for item in items if item.get("detail"))
        return f"{len(items)} 条记录无密码/Key 明文（{actions_with_detail} 条含 detail）"

    # ================================================================== 8. cleanup
    def section_cleanup(self) -> None:
        self.section("8. 登出（保留数据供 UI 截图）")
        self.check("v1.auth.logout", self.t_logout_admin)

    def t_logout_admin(self):
        data = self._json(self.client.post("/api/auth/logout"))
        expect(data.get("ok") is True, f"注销响应异常：{data}")
        return "admin logout ok（录音/分享/审计数据保留，供截图脚本使用）"

    # ================================================================== runner
    def _write_context(self) -> None:
        context = {
            "base_url": self.base_url,
            "version": self.ctx.get("version"),
            "vector_backend": self.ctx.get("vector_backend"),
            "video_id": self.ctx.get("video_id"),
            "video_title": self.ctx.get("video_title"),
            "video_description": self.ctx.get("video_description"),
            "scene_count": len(self.ctx.get("scene_items") or []),
            "share_token": self.ctx.get("share_token"),
            "share_url_path": (self.ctx.get("share") or {}).get("url_path"),
            "audio_id": self.ctx.get("audio_id"),
            "member_username": (self.member or {}).get("username"),
            "member_password": self.member_password,
            "tag_id": self.ctx.get("tag_id"),
            "qdrant_collection": self.ctx.get("qdrant_collection"),
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            self.context_out.parent.mkdir(parents=True, exist_ok=True)
            self.context_out.write_text(json.dumps(context, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        except OSError as exc:
            print(f"[warn] 上下文写入失败：{exc}")

    def _summary(self) -> int:
        total = len(self.results)
        passed = sum(1 for r in self.results if r["status"] == "PASS")
        skipped = sum(1 for r in self.results if r["status"] == "SKIP")
        failed = [r for r in self.results if r["status"] == "FAIL"]
        print("\n" + "=" * 78)
        print("\033[1mPASS/FAIL 矩阵\033[0m")
        print("-" * 78)
        for row in self.results:
            color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(row["status"], "")
            detail = row["detail"].splitlines()[0] if row["detail"] else ""
            print(f"{color}{row['status']:<4}\033[0m {row['name']:<46} {row['duration']:6.2f}s  {detail}")
        print("-" * 78)
        print(f"总计 {total} 项：\033[1;32m{passed} 通过\033[0m，"
              f"\033[1;31m{len(failed)} 失败\033[0m，{skipped} 跳过")
        if failed:
            print("\n失败详情：")
            for row in failed:
                print(f"\n\033[1;31m✗ {row['name']}\033[0m")
                print(row["detail"])
            print("\n\033[1;31mE2E v1.0 SMOKE: FAIL\033[0m")
            return 1
        print("\n\033[1;32mE2E v1.0 SMOKE: PASS ✔\033[0m")
        return 0

    def run_v1(self) -> int:
        print(f"Echoic v1.0 E2E Smoke — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"目标服务: {self.base_url} | data dir: {self.data_dir}")
        print(f"Python: {sys.version.split()[0]} | httpx: {httpx.__version__} | "
              f"ffmpeg: {self.ffmpeg or '缺失'}")
        print(f"期望向量后端: {self.expected_vector_backend or 'auto'}")
        print("=" * 78)
        try:
            self.section_env()
            self.section_vector()
            self.section_scenes()
            self.section_description_trash()
            self.section_tag_settings()
            self.section_share()
            self.section_auth()
            self.section_audit()
            self.section_cleanup()
        except Exception as exc:  # noqa: BLE001 - 基础环节失败也要输出矩阵
            self.record("v1.fatal", "FAIL", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=4)}")
        finally:
            self._write_context()
        return self._summary()

    def close(self) -> None:
        for client in (self.member_client, self.ctx.get("reg_client")):
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
        super().close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Echoic v1.0 端到端冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ECHOIC_E2E_BASE_URL") or os.environ.get("ECHOIC_BASE_URL") or "http://127.0.0.1:8100",
    )
    parser.add_argument("--username", default=os.environ.get("ECHOIC_E2E_ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or "admin123")
    parser.add_argument("--process-timeout", type=float,
                        default=float(os.environ.get("ECHOIC_E2E_PROCESS_TIMEOUT") or 240))
    parser.add_argument("--data-dir", default=os.environ.get("ECHOIC_E2E_DATA_DIR") or "")
    parser.add_argument("--expect-vector-backend",
                        default=os.environ.get("ECHOIC_E2E_VECTOR_BACKEND") or "")
    parser.add_argument("--qdrant-url", default=os.environ.get("ECHOIC_E2E_QDRANT_URL") or "")
    parser.add_argument("--qdrant-api-key", default=os.environ.get("ECHOIC_E2E_QDRANT_API_KEY") or "")
    parser.add_argument(
        "--context-out",
        default=os.environ.get("ECHOIC_E2E_CONTEXT_OUT") or str(REPO_ROOT / ".cache" / "e2e-v1-context.json"),
    )
    parser.add_argument("--log", default=os.environ.get("ECHOIC_E2E_LOG_V1") or str(E2E_DIR / "last-run-v1.log"))
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
        smoke = V1Smoke(
            args.base_url,
            args.username,
            args.password,
            args.process_timeout,
            data_dir,
            args.expect_vector_backend,
            args.qdrant_url,
            args.qdrant_api_key,
            Path(args.context_out),
        )
        code = smoke.run_v1()
    finally:
        if smoke is not None:
            smoke.close()
        print(f"\n日志已保存：{log_path}")
        sys.stdout, sys.stderr = stdout_orig, stderr_orig
        log_fh.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
