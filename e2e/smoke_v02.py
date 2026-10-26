#!/usr/bin/env python3
"""Echoic v0.2 端到端冒烟测试：多用户/角色、分享链接、consume 导入 + v0.1 回归。

在独立数据目录 + mock AI 管线的生产式服务上运行（见 task-10 / 部署文档）：
    ECHOIC_DATA_DIR=/tmp/echoic-e2e-v02 \
    ECHOIC_CONSUME_DIR=$PWD/.cache/e2e-v02-consume \
    backend/.venv/bin/python e2e/smoke_v02.py --base-url http://127.0.0.1:8100

覆盖：
  0) 健康检查 / admin 登录 / 演示数据
  1) 多用户与角色：用户 CRUD、重复用户名、停用账号登录、开放注册开关、last admin 保护、
     member 无权访问 admin 接口（users/settings/system/tags 写/consume scan）
  2) 权限矩阵：member 仅见自己录音；admin 上传→转移归属→member 可访问（detail/media/transcript/
     insights/chat）；member 访问他人录音 404；member 上传归自己；member 不可转归属；search/dashboard 按用户隔离
  3) 分享链接：公开元数据/view_count、transcript/insights/media Range、download 门禁、
     include 开关 403、密码解锁（错误 401/正确 200/Cookie 路径）、解锁限速 429、过期 404、撤销 404、
     member 分享权限、member2 不可见
  4) consume 目录导入：扫描端点、合法音频导入→就绪、重复内容去重、非法媒体→failed、不支持扩展名跳过、
     system.info consume 状态、member 无权扫描
  5) v0.1 回归：演示问答 SSE、admin 全量搜索、member 搜索隔离、dashboard 隔离

退出码：0 = 全部通过（允许 SKIP）；1 = 存在 FAIL。
日志默认写入 e2e/last-run-v02.log。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

E2E_DIR = Path(__file__).resolve().parent
REPO_ROOT = E2E_DIR.parent
sys.path.insert(0, str(E2E_DIR))

from smoke import Smoke, expect  # noqa: E402  （复用 v0.1 冒烟脚本的工具类）


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class V02Smoke(Smoke):
    def __init__(self, base_url: str, username: str, password: str, process_timeout: float, consume_dir: Path):
        super().__init__(base_url, username, password, process_timeout)
        self.consume_dir = consume_dir
        self.consume_dir.mkdir(parents=True, exist_ok=True)
        (self.consume_dir / "failed").mkdir(parents=True, exist_ok=True)
        self.clients: list[httpx.Client] = []
        self.stamp = time.strftime("%H%M%S")
        self.users: dict[str, dict] = {}

    # ------------------------------------------------------------------ clients
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

    def _new_user(self, key: str, role: str = "member", password: str | None = None) -> dict:
        username = f"e2e_{key}_{self.stamp}"
        payload = {
            "username": username,
            "password": password or "E2ePass123",
            "display_name": f"E2E {key}",
            "email": f"{username}@example.com",
            "role": role,
        }
        data = self._json(self.client.post("/api/users", json=payload), expected=201)
        expect(data.get("role") == role, f"创建用户角色不符：{data}")
        record = {**data, "_password": payload["password"]}
        self.users[key] = record
        return record

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

    def _upload_wav(self, client: httpx.Client, title: str, seconds: int = 12) -> dict:
        audio = self.make_audio(seconds=seconds)
        with audio.open("rb") as fh:
            resp = client.post(
                "/api/recordings/upload",
                files={"file": (audio.name, fh, "audio/wav")},
                data={"title": title, "description": "smoke_v02 自动生成音频", "language": "zh"},
                timeout=180.0,
            )
        rec = self._json(resp, expected=201)
        expect(rec.get("id"), f"上传响应缺少 id：{rec}")
        return rec

    def anon(self) -> httpx.Client:
        return self._client()

    def _scan(self) -> dict:
        return self._json(self.client.post("/api/system/consume/scan"))

    def _member_list_total(self, client: httpx.Client) -> int:
        data = self._json(client.get("/api/recordings", params={"page": 1, "page_size": 100}))
        return int(data.get("total") or 0)

    # ================================================================== sections
    def section_roles(self) -> None:
        self.section("1. 多用户与角色（R-18）")
        self.check("v02.users.list-admin", self.t_users_list)
        self.check("v02.users.create-member", self.t_users_create)
        self.check("v02.users.duplicate-409", self.t_users_duplicate)
        self.check("v02.users.disabled-login-403", self.t_disabled_login)
        self.check("v02.auth.register-gated", self.t_register_gated)
        self.check("v02.users.last-admin-protection", self.t_last_admin)
        self.check("v02.member.admin-api-403", self.t_member_admin_api)

    def t_users_list(self):
        data = self._json(self.client.get("/api/users", params={"page": 1, "page_size": 50}))
        items = data.get("items") or []
        expect(items, f"用户列表为空：{data}")
        for key in ("id", "username", "role", "disabled", "recording_count", "created_at"):
            expect(key in items[0], f"用户字段缺少 {key}：{items[0]}")
        admin_row = next((u for u in items if u["username"] == self.username), None)
        expect(admin_row and admin_row["role"] == "admin", f"管理员用户缺失/角色错误：{admin_row}")
        expect(any(u["username"] == (self.users.get("member1") or {}).get("username") for u in items),
               "用户列表未包含新建 member1") if self.users.get("member1") else None
        return f"用户 {data.get('total')} 个，admin 角色正常"

    def t_users_create(self):
        member1 = self._new_user("member1", "member")
        member2 = self._new_user("member2", "member")
        expect(member1["id"] != member2["id"], "两个用户 id 相同")
        c1 = self._login_client(member1["username"], member1["_password"])
        c2 = self._login_client(member2["username"], member2["_password"])
        self.ctx["member1"] = member1
        self.ctx["member2"] = member2
        self.ctx["client1"] = c1
        self.ctx["client2"] = c2
        me1 = self._json(c1.get("/api/auth/me"))["user"]
        expect(me1.get("role") == "member" and me1.get("email"), f"member1 /auth/me 异常：{me1}")
        return f"创建并登录 member1/member2（id={member1['id']}/{member2['id']}，role=member）"

    def t_users_duplicate(self):
        m1 = self.users["member1"]
        resp = self.client.post("/api/users", json={"username": m1["username"], "password": "E2ePass123"})
        expect(resp.status_code == 409, f"重复用户名期望 409，实际 {resp.status_code}：{resp.text[:200]}")
        resp2 = self.client.post("/api/users", json={"username": f"e2e_short_{self.stamp}", "password": "short"})
        expect(resp2.status_code == 422, f"短密码期望 422，实际 {resp2.status_code}")
        return "重复用户名 409；密码 <8 位 422"

    def t_disabled_login(self):
        m2 = self.users["member2"]
        self._json(self.client.patch(f"/api/users/{m2['id']}", json={"disabled": True}))
        resp = self._new_client_raw().post(
            "/api/auth/login", json={"username": m2["username"], "password": m2["_password"]}
        )
        expect(resp.status_code == 403, f"停用账号登录期望 403，实际 {resp.status_code}：{resp.text[:200]}")
        self._json(self.client.patch(f"/api/users/{m2['id']}", json={"disabled": False}))
        c2 = self._login_client(m2["username"], m2["_password"])
        self.ctx["client2"] = c2
        return "停用后登录 403，恢复后可登录"

    def _new_client_raw(self) -> httpx.Client:
        return self._client()

    def t_register_gated(self):
        payload = {"username": f"e2e_reg_{self.stamp}", "password": "RegPass123"}
        resp = self._new_client_raw().post("/api/auth/register", json=payload)
        expect(resp.status_code == 403, f"allow_registration=false 时注册期望 403，实际 {resp.status_code}")
        settings = self._json(self.client.get("/api/settings"))
        allow = (settings.get("system") or {}).get("allow_registration")
        expect(allow is False, f"默认 allow_registration 应为 false：{allow}")
        self._json(self.client.put("/api/settings", json={"system": {"allow_registration": True}}))
        created = self._json(self._new_client_raw().post("/api/auth/register", json=payload), expected=201)
        expect(created.get("role") == "member", f"注册用户角色应为 member：{created}")
        login = self._login_client(payload["username"], payload["password"])
        me = self._json(login.get("/api/auth/me"))["user"]
        expect(me.get("username") == payload["username"], "注册用户登录失败")
        self._json(self.client.put("/api/settings", json={"system": {"allow_registration": False}}))
        return "注册开关：关闭时 403；开启后 201(member) 可登录；已恢复关闭"

    def t_last_admin(self):
        admin_id = self._json(self.client.get("/api/auth/me"))["user"]["id"]
        demote = self.client.patch(f"/api/users/{admin_id}", json={"role": "member"})
        expect(demote.status_code in (400, 409), f"降级最后一个管理员期望 4xx，实际 {demote.status_code}")
        delete_self = self.client.delete(f"/api/users/{admin_id}")
        expect(delete_self.status_code in (400, 403, 409), f"删除自己期望 4xx，实际 {delete_self.status_code}")
        return "不能降级/删除最后一个管理员（已拦截）"

    def t_member_admin_api(self):
        c1 = self.ctx["client1"]
        cases = [
            ("GET", "/api/users", None, 403),
            ("POST", "/api/users", {"username": f"e2e_x_{self.stamp}", "password": "Xxxxxxxx1"}, 403),
            ("GET", "/api/settings", None, 403),
            ("PUT", "/api/settings", {"system": {"allow_registration": True}}, 403),
            ("GET", "/api/system/info", None, 403),
            ("POST", "/api/system/seed-demo", None, 403),
            ("POST", "/api/system/consume/scan", None, 403),
            ("POST", "/api/tags", {"name": f"e2e_tag_{self.stamp}", "color": "#1677ff"}, 403),
        ]
        for method, path, body, expected in cases:
            resp = c1.request(method, path, json=body) if body is not None else c1.request(method, path)
            expect(resp.status_code == expected,
                   f"{method} {path} 期望 {expected}，实际 {resp.status_code}：{resp.text[:200]}")
        tags = self._json(c1.get("/api/tags"))
        expect("items" in tags, f"member 读取标签失败：{tags}")
        return f"member 访问 {len(cases)} 个 admin 接口均 403；GET /api/tags 200"

    # ---------------------------------------------------------------- permissions
    def section_permissions(self) -> None:
        self.section("2. 权限矩阵与录音归属")
        self.check("v02.perm.admin-upload", self.t_admin_upload)
        self.check("v02.perm.reassign-owner", self.t_reassign_owner)
        self.check("v02.perm.member-upload", self.t_member_upload)
        self.check("v02.perm.member-isolation", self.t_member_isolation)
        self.check("v02.perm.member-own-access", self.t_member_own_access)
        self.check("v02.perm.member-owner-protection", self.t_member_owner_protection)
        self.check("v02.perm.search-dashboard-scoped", self.t_search_dashboard_scoped)

    def t_admin_upload(self):
        rec = self._upload_wav(self.client, f"E2E Admin Upload {self.stamp}", seconds=12)
        self.ctx["admin_rid"] = rec["id"]
        ready = self._poll_ready(self.client, rec["id"])
        owner = ready.get("owner") or {}
        expect(str((owner.get("username") or "")) == self.username, f"admin 上传 owner 异常：{owner}")
        return f"admin 上传 id={rec['id']} → ready，owner={owner.get('username')}"

    def t_reassign_owner(self):
        m1 = self.users["member1"]
        rid = self.ctx["admin_rid"]
        data = self._json(self.client.patch(f"/api/recordings/{rid}", json={"owner_id": m1["id"]}))
        expect(data.get("owner_id") == m1["id"], f"转移归属未生效：owner_id={data.get('owner_id')}")
        detail = self._json(self.client.get(f"/api/recordings/{rid}"))
        expect((detail.get("owner") or {}).get("username") == m1["username"], f"owner 详情不符：{detail.get('owner')}")
        return f"录音 {rid} 归属 → member1（owner_id={m1['id']}）"

    def t_member_upload(self):
        c1 = self.ctx["client1"]
        rec = self._upload_wav(c1, f"E2E Member Upload {self.stamp}", seconds=12)
        self.ctx["member_rid"] = rec["id"]
        ready = self._poll_ready(c1, rec["id"])
        m1 = self.users["member1"]
        expect(ready.get("owner_id") == m1["id"], f"member 上传 owner_id 异常：{ready.get('owner_id')}")
        return f"member1 上传 id={rec['id']} → ready，owner_id={m1['id']}"

    def t_member_isolation(self):
        c1 = self.ctx["client1"]
        demo_id = self.ctx["demo_id"]
        resp = c1.get(f"/api/recordings/{demo_id}")
        expect(resp.status_code == 404, f"member 访问他人录音期望 404，实际 {resp.status_code}")
        for path in (
            f"/api/recordings/{demo_id}/transcript",
            f"/api/recordings/{demo_id}/insights",
            f"/api/recordings/{demo_id}/media",
            f"/api/recordings/{demo_id}/chat",
            f"/api/recordings/{demo_id}/shares",
        ):
            resp = c1.get(path)
            expect(resp.status_code == 404, f"member 访问 {path} 期望 404，实际 {resp.status_code}")
        listing = self._json(c1.get("/api/recordings", params={"page": 1, "page_size": 100}))
        ids = [item["id"] for item in listing.get("items") or []]
        expect(demo_id not in ids, f"member 列表出现他人录音 {demo_id}")
        expect(set(ids) >= {self.ctx["admin_rid"], self.ctx["member_rid"]},
               f"member 列表缺少自己的录音：{ids}")
        return f"member 列表仅 {len(ids)} 条（demo 不可见，越权访问全部 404）"

    def t_member_own_access(self):
        c1 = self.ctx["client1"]
        rid = self.ctx["admin_rid"]
        detail = self._json(c1.get(f"/api/recordings/{rid}"))
        expect(detail.get("status") == "ready", f"转移后 member 读取详情失败：{detail.get('status')}")
        transcript = self._json(c1.get(f"/api/recordings/{rid}/transcript"))
        expect(transcript.get("segments"), "member 读取转录为空")
        insights = self._json(c1.get(f"/api/recordings/{rid}/insights"))
        expect(insights.get("summary"), "member 读取 AI 分析为空")
        media = c1.get(f"/api/recordings/{rid}/media", headers={"Range": "bytes=0-1023"})
        expect(media.status_code == 206, f"member Range 期望 206，实际 {media.status_code}")
        chat = self._json(c1.get(f"/api/recordings/{rid}/chat"))
        expect("messages" in chat, f"member 读取问答历史异常：{chat}")
        edit = self._json(c1.patch(f"/api/recordings/{rid}", json={"title": f"E2E Renamed {self.stamp}"}))
        expect(edit.get("title") == f"E2E Renamed {self.stamp}", f"member 修改自己录音标题失败：{edit.get('title')}")
        return "member 可读自己录音：detail/transcript/insights/media(Range206)/chat，可改标题"

    def t_member_owner_protection(self):
        c1 = self.ctx["client1"]
        rid = self.ctx["member_rid"]
        m2 = self.users["member2"]
        resp = c1.patch(f"/api/recordings/{rid}", json={"owner_id": m2["id"]})
        expect(resp.status_code == 403, f"member 转归属期望 403，实际 {resp.status_code}")
        c2 = self.ctx["client2"]
        resp2 = c2.get(f"/api/recordings/{self.ctx['admin_rid']}")
        expect(resp2.status_code == 404, f"member2 访问 member1 录音期望 404，实际 {resp2.status_code}")
        return "member 不能转移归属(403)；member2 访问 member1 录音 404"

    def t_search_dashboard_scoped(self):
        c1 = self.ctx["client1"]
        admin_total = int(self._json(self.client.get("/api/recordings", params={"page": 1, "page_size": 1}))["total"])
        member_total = self._member_list_total(c1)
        expect(member_total < admin_total, f"member 录音数应少于 admin（member={member_total}, admin={admin_total}）")
        demo_hits = self._json(c1.get("/api/search", params={"q": "TraceOpt", "limit": 10}))
        expect(not (demo_hits.get("recordings") or []), f"member 搜索不应命中 admin 录音：{demo_hits.get('recordings')}")
        own_hits = self._json(c1.get("/api/search", params={"q": f"E2E Renamed {self.stamp}", "limit": 10}))
        expect(own_hits.get("recordings"), "member 搜索自己录音无命中")
        member_dash = self._json(c1.get("/api/dashboard"))
        expect(int(member_dash["stats"]["total"]) == member_total,
               f"member dashboard total={member_dash['stats']['total']} != 列表 total={member_total}")
        admin_dash = self._json(self.client.get("/api/dashboard"))
        expect(int(admin_dash["stats"]["total"]) == admin_total, "admin dashboard total 与列表不一致")
        return f"admin total={admin_total}；member total={member_total}；搜索/dashboard 均按用户隔离"

    # ------------------------------------------------------------------ shares
    def section_shares(self) -> None:
        self.section("3. 分享链接（R-17）")
        self.check("v02.share.create-open", self.t_share_create_open)
        self.check("v02.share.public-metadata", self.t_share_public_metadata)
        self.check("v02.share.public-content", self.t_share_public_content)
        self.check("v02.share.download-gate", self.t_share_download_gate)
        self.check("v02.share.include-flags", self.t_share_include_flags)
        self.check("v02.share.password-unlock", self.t_share_password)
        self.check("v02.share.unlock-rate-limit", self.t_share_rate_limit)
        self.check("v02.share.expiry", self.t_share_expiry)
        self.check("v02.share.revoke", self.t_share_revoke)
        self.check("v02.share.member-scope", self.t_share_member_scope)

    def t_share_create_open(self):
        rid = self.ctx["admin_rid"]
        share = self._json(
            self.client.post(f"/api/recordings/{rid}/shares", json={"include_transcript": True, "include_insights": True}),
            expected=201,
        )
        expect(share.get("token") and share.get("url_path", "").startswith("/s/"),
               f"分享响应缺少 token/url_path：{share}")
        expect(share.get("has_password") is False, f"无密码分享 has_password 应为 False：{share}")
        self.ctx["share_open"] = share
        listing = self._json(self.client.get(f"/api/recordings/{rid}/shares"))
        expect(any(item["id"] == share["id"] for item in listing.get("items") or []),
               "录音分享列表未包含新建分享")
        return f"创建公开分享 id={share['id']} token={share['token'][:10]}…"

    def t_share_public_metadata(self):
        share = self.ctx["share_open"]
        anon = self.anon()
        first = self._json(anon.get(f"/api/public/shares/{share['token']}"))
        expect(first.get("recording", {}).get("title"), f"公开元数据缺少录音标题：{first}")
        expect((first.get("share") or {}).get("has_password") is False, "公开元数据 has_password 异常")
        expect("owner" not in json.dumps(first), "公开元数据泄露 owner 信息")
        second = self._json(anon.get(f"/api/public/shares/{share['token']}"))
        v1, v2 = int(first["share"]["view_count"]), int(second["share"]["view_count"])
        expect(v2 >= v1 + 1, f"view_count 未递增：{v1} -> {v2}")
        return f"匿名元数据 200；view_count {v1}→{v2}，无 owner 信息"

    def t_share_public_content(self):
        share = self.ctx["share_open"]
        anon = self.anon()
        transcript = self._json(anon.get(f"/api/public/shares/{share['token']}/transcript"))
        expect(transcript.get("segments"), "公开转录为空")
        insights = self._json(anon.get(f"/api/public/shares/{share['token']}/insights"))
        expect(insights.get("summary"), "公开 AI 分析为空")
        media = anon.get(f"/api/public/shares/{share['token']}/media", headers={"Range": "bytes=0-1023"})
        expect(media.status_code == 206, f"公开媒体 Range 期望 206，实际 {media.status_code}")
        return f"公开读取：transcript={len(transcript['segments'])} 段、insights OK、media Range 206"

    def t_share_download_gate(self):
        share = self.ctx["share_open"]
        anon = self.anon()
        resp = anon.get(f"/api/public/shares/{share['token']}/media", params={"download": 1})
        expect(resp.status_code == 403, f"allow_download=false 下载期望 403，实际 {resp.status_code}")
        resp2 = anon.get(f"/api/public/shares/{share['token']}/download")
        expect(resp2.status_code == 403, f"/download 期望 403，实际 {resp2.status_code}")
        # 允许下载的分享
        allowed = self._json(
            self.client.post(
                f"/api/recordings/{self.ctx['admin_rid']}/shares",
                json={"include_transcript": True, "include_insights": True, "allow_download": True},
            ),
            expected=201,
        )
        anon2 = self.anon()
        ok = anon2.get(f"/api/public/shares/{allowed['token']}/download")
        expect(ok.status_code == 200, f"allow_download=true 下载期望 200，实际 {ok.status_code}")
        cd = ok.headers.get("content-disposition", "")
        expect("attachment" in cd.lower(), f"下载缺少 attachment：{cd!r}")
        return "allow_download=false → 403（?download 与 /download）；=true → 200 attachment"

    def t_share_include_flags(self):
        rid = self.ctx["admin_rid"]
        share = self._json(
            self.client.post(
                f"/api/recordings/{rid}/shares",
                json={"include_transcript": False, "include_insights": False},
            ),
            expected=201,
        )
        anon = self.anon()
        t_resp = anon.get(f"/api/public/shares/{share['token']}/transcript")
        expect(t_resp.status_code == 403, f"include_transcript=false 期望 403，实际 {t_resp.status_code}")
        i_resp = anon.get(f"/api/public/shares/{share['token']}/insights")
        expect(i_resp.status_code == 403, f"include_insights=false 期望 403，实际 {i_resp.status_code}")
        meta = self._json(anon.get(f"/api/public/shares/{share['token']}"))
        expect(meta["share"]["include_transcript"] is False and meta["share"]["include_insights"] is False,
               f"公开元数据 include 标记未透出：{meta['share']}")
        return "include_transcript/insights=false → 公开接口 403，元数据标记正确"

    def t_share_password(self):
        rid = self.ctx["admin_rid"]
        share = self._json(
            self.client.post(f"/api/recordings/{rid}/shares", json={"password": "s3cret!pass"}),
            expected=201,
        )
        token = share["token"]
        anon = self.anon()
        meta = self._json(anon.get(f"/api/public/shares/{token}"))
        expect(meta["share"]["has_password"] is True, "密码分享 has_password 应为 true")
        locked = anon.get(f"/api/public/shares/{token}/transcript")
        expect(locked.status_code == 401, f"未解锁读取期望 401，实际 {locked.status_code}")
        wrong = anon.post(f"/api/public/shares/{token}/unlock", json={"password": "wrong-pass"})
        expect(wrong.status_code == 401, f"错误密码期望 401，实际 {wrong.status_code}")
        unlock = anon.post(f"/api/public/shares/{token}/unlock", json={"password": "s3cret!pass"})
        data = self._json(unlock)
        expect(data.get("ok") is True, f"解锁响应异常：{data}")
        cookie_headers = ", ".join(unlock.headers.get_list("set-cookie"))
        lowered = cookie_headers.lower()
        expect("echoic_share" in lowered, f"解锁未设置 share Cookie：{cookie_headers!r}")
        for flag in ("httponly", "samesite=lax", f"path=/api/public/shares/{token.lower()}", "max-age=7200"):
            expect(flag in lowered, f"share Cookie 缺少 {flag}：{cookie_headers!r}")
        transcript = self._json(anon.get(f"/api/public/shares/{token}/transcript"))
        expect(transcript.get("segments"), "解锁后读取转录失败")
        media = anon.get(f"/api/public/shares/{token}/media", headers={"Range": "bytes=0-511"})
        expect(media.status_code == 206, f"解锁后媒体 Range 期望 206，实际 {media.status_code}")
        no_pw = self._json(
            self.client.post(f"/api/recordings/{rid}/shares", json={"include_transcript": True}), expected=201
        )
        unlock2 = self.anon().post(f"/api/public/shares/{no_pw['token']}/unlock", json={"password": "x"})
        expect(unlock2.status_code == 200, f"无密码分享 unlock 应直接 200，实际 {unlock2.status_code}")
        return "密码分享：未解锁 401；错误密码 401；正确解锁 200 + 路径级 HttpOnly Cookie；解锁后内容可读"

    def t_share_rate_limit(self):
        rid = self.ctx["admin_rid"]
        share = self._json(
            self.client.post(f"/api/recordings/{rid}/shares", json={"password": "limit!pass"}),
            expected=201,
        )
        token = share["token"]
        statuses = []
        for _ in range(6):
            resp = self.anon().post(f"/api/public/shares/{token}/unlock", json={"password": "bad-guess"})
            statuses.append(resp.status_code)
        expect(429 in statuses, f"连续 6 次错误密码应出现 429，实际 {statuses}")
        expect(statuses.count(401) >= 4, f"限速前应允许若干次 401，实际 {statuses}")
        return f"解锁限速生效：{statuses}"

    def t_share_expiry(self):
        rid = self.ctx["admin_rid"]
        past = iso_utc(datetime.now(timezone.utc) - timedelta(minutes=5))
        resp = self.client.post(f"/api/recordings/{rid}/shares", json={"expires_at": past})
        expect(resp.status_code == 400, f"过去时间创建分享期望 400，实际 {resp.status_code}：{resp.text[:200]}")
        soon = iso_utc(datetime.now(timezone.utc) + timedelta(seconds=2))
        share = self._json(
            self.client.post(f"/api/recordings/{rid}/shares", json={"expires_at": soon}), expected=201
        )
        expect(share.get("expired") is False, "新建分享不应标记过期")
        time.sleep(3.2)
        anon = self.anon()
        gone = anon.get(f"/api/public/shares/{share['token']}")
        expect(gone.status_code == 404, f"过期分享期望 404，实际 {gone.status_code}")
        return "过去时间创建被 400 拒绝；有效期 2s 的分享过期后公开访问 404"

    def t_share_revoke(self):
        rid = self.ctx["admin_rid"]
        share = self._json(self.client.post(f"/api/recordings/{rid}/shares", json={}), expected=201)
        self._json(self.client.delete(f"/api/shares/{share['id']}"))
        anon = self.anon()
        resp = anon.get(f"/api/public/shares/{share['token']}")
        expect(resp.status_code == 404, f"撤销后公开访问期望 404，实际 {resp.status_code}")
        # member2 不能撤销 member1 的分享
        m1_share = self._json(
            self.ctx["client1"].post(f"/api/recordings/{self.ctx['admin_rid']}/shares", json={}), expected=201
        )
        c2 = self.ctx["client2"]
        revoke = c2.delete(f"/api/shares/{m1_share['id']}")
        expect(revoke.status_code in (403, 404), f"member2 撤销他人分享期望 403/404，实际 {revoke.status_code}")
        return "撤销后公开 404；member2 不能撤销他人分享"

    def t_share_member_scope(self):
        c1 = self.ctx["client1"]
        c2 = self.ctx["client2"]
        own = self._json(c1.post(f"/api/recordings/{self.ctx['member_rid']}/shares", json={}), expected=201)
        expect(own.get("token"), "member 分享自己录音失败")
        resp = c2.post(f"/api/recordings/{self.ctx['member_rid']}/shares", json={})
        expect(resp.status_code == 404, f"member2 分享 member1 录音期望 404，实际 {resp.status_code}")
        demo_resp = c1.post(f"/api/recordings/{self.ctx['demo_id']}/shares", json={})
        expect(demo_resp.status_code == 404, f"member1 分享 admin 录音期望 404，实际 {demo_resp.status_code}")
        return "member 可分享自己录音；不可分享他人录音（404）"

    # ------------------------------------------------------------------ consume
    def section_consume(self) -> None:
        self.section("4. consume 目录自动导入（R-22）")
        self._cleanup_consume_dir()
        self.check("v02.consume.status", self.t_consume_status)
        self.check("v02.consume.import-ready", self.t_consume_import)
        self.check("v02.consume.duplicate", self.t_consume_duplicate)
        self.check("v02.consume.unsupported-skip", self.t_consume_unsupported)
        self.check("v02.consume.invalid-failed", self.t_consume_invalid)

    def t_consume_status(self):
        info = self._json(self.client.get("/api/system/info"))
        consume = info.get("consume") or {}
        for key in ("enabled", "dir", "interval_sec", "pending_files", "failed_files", "last_scan"):
            expect(key in consume, f"/api/system/info consume 缺少 {key}：{consume}")
        expect(consume.get("enabled") is True, f"consume 未启用：{consume}")
        expect(str(self.consume_dir) in str(consume.get("dir")), f"consume dir 不匹配：{consume.get('dir')}")
        self.ctx["recordings_before_consume"] = int(info.get("recordings") or 0)
        return f"consume 状态：enabled=True dir={consume.get('dir')} interval={consume.get('interval_sec')}s"

    def _unique_audio(self, seconds: int = 10) -> Path:
        """生成内容唯一（音调不同）的 wav，避免 consume 的 sha256 跨轮次误判重复。"""
        expect(self.ffmpeg, "未找到 ffmpeg，无法生成测试音频")
        freq = 200 + (int(time.time_ns()) % 500)
        out = self.tmp / f"e2e-consume-{self.stamp}-{freq}.wav"
        cmd = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(out),
        ]
        import subprocess

        proc = subprocess.run(cmd, capture_output=True, text=True)
        expect(proc.returncode == 0, f"ffmpeg 生成唯一音频失败：{proc.stderr[-400:]}")
        return out

    def _copy_audio_to_consume(self, name: str) -> Path:
        src = self._unique_audio(seconds=10)
        dst = self.consume_dir / name
        shutil.copyfile(src, dst)
        return dst

    def _cleanup_consume_dir(self) -> None:
        for pattern_dir in (self.consume_dir, self.consume_dir / "failed"):
            if not pattern_dir.is_dir():
                continue
            for entry in pattern_dir.glob("e2e-*"):
                try:
                    entry.unlink()
                except OSError:
                    pass

    def _wait_consume_recording(self, title: str, timeout: float = 90) -> dict:
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            listing = self._json(self.client.get("/api/recordings", params={"query": title, "page_size": 20}))
            items = listing.get("items") or []
            if items:
                last = items[0]
                detail = self._json(self.client.get(f"/api/recordings/{last['id']}"))
                if detail.get("status") in ("ready", "failed"):
                    return detail
            time.sleep(1.5)
        raise AssertionError(f"consume 导入的录音未在 {timeout:.0f}s 内出现/就绪：{last}")

    def t_consume_import(self):
        name = f"e2e-consume-{self.stamp}.wav"
        path = self._copy_audio_to_consume(name)
        scanned = self._scan()
        expect(scanned.get("ok") is True, f"scan 响应异常：{scanned}")
        result = scanned.get("result") or {}
        for key in ("scanned", "imported", "duplicates", "failed"):
            expect(key in result, f"scan result 缺少 {key}：{result}")
        ready = self._wait_consume_recording(Path(name).stem)
        m1 = self.users["member1"]
        expect(ready.get("source") == "consume", f"consume 导入 source 应为 consume：{ready.get('source')}")
        expect(ready.get("status") == "ready", f"consume 导入未就绪：{ready.get('status')} {ready.get('error_message')}")
        expect(str(ready.get("original_filename")) == name, f"original_filename 不符：{ready.get('original_filename')}")
        owner = ready.get("owner") or {}
        expect(owner.get("username") in (self.username, None), f"consume 归属应为管理员：{owner}")
        self.ctx["consume_rid"] = ready["id"]
        self.ctx["consume_name"] = name
        expect(not path.exists(), "导入成功后源文件应被移出 consume 目录")
        return f"scan={result}；导入 id={ready['id']} → ready（source=consume, 文件已移出）"

    def t_consume_duplicate(self):
        name = f"e2e-dup-{self.stamp}.wav"
        src = self._unique_audio(seconds=8)
        dst = self.consume_dir / name
        shutil.copyfile(src, dst)
        first = self._scan()["result"]
        total_first = self._member_list_total(self.client)
        # 再复制同一内容 → 去重
        shutil.copyfile(src, self.consume_dir / name)
        second = self._scan()["result"]
        total_second = self._member_list_total(self.client)
        expect(int(first.get("imported") or 0) >= 1, f"首次扫描应导入至少 1 个：{first}")
        expect(int(second.get("duplicates") or 0) >= 1, f"重复文件应计入 duplicates：{second}")
        expect(total_second == total_first, f"重复文件不应新增录音：{total_first} -> {total_second}")
        return f"首次 {first}；重复 {second}；录音总数 {total_first} 不变"

    def t_consume_unsupported(self):
        before = self._json(self.client.get("/api/system/info"))
        junk = self.consume_dir / f"e2e-not-media-{self.stamp}.txt"
        junk.write_bytes(b"plain text, not audio")
        result = self._scan()["result"]
        after = self._json(self.client.get("/api/system/info"))
        expect(int(after.get("recordings") or 0) == int(before.get("recordings") or 0),
               "不支持扩展名不应创建录音")
        expect(junk.exists(), "不支持的扩展名应被跳过（文件保留原位）")
        return f"不支持扩展名跳过（scan={result}，录音数不变）"

    def t_consume_invalid(self):
        bad = self.consume_dir / f"e2e-bad-{self.stamp}.wav"
        bad.write_bytes(b"this is definitely not a valid audio file")
        result = self._scan()["result"]
        info = self._json(self.client.get("/api/system/info"))
        consume = info.get("consume") or {}
        expect(int(result.get("failed") or 0) >= 1, f"非法媒体应计入 failed：{result}")
        expect(int(consume.get("failed_files") or 0) >= 1, f"consume.failed_files 应 >=1：{consume}")
        expect(not bad.exists(), "非法媒体应被移入 consume/failed/")
        failed_dir = self.consume_dir / "failed"
        moved = list(failed_dir.glob(f"e2e-bad-{self.stamp}*"))
        expect(moved, f"consume/failed/ 下未找到失败文件：{[p.name for p in failed_dir.iterdir()]}")
        return f"非法媒体→failed（scan.failed={result.get('failed')}，已移入 consume/failed/）"

    # ----------------------------------------------------------------- regression
    def section_regression(self) -> None:
        self.section("5. v0.1 回归（按 v0.2 权限模型）")
        self.check("v02.regression.demo-chat-sse", self.t_chat_sse)

    # ================================================================== runner
    def run_v02(self) -> int:
        print(f"Echoic v0.2 E2E Smoke — {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"目标服务: {self.base_url} | consume dir: {self.consume_dir}")
        print(f"Python: {sys.version.split()[0]} | httpx: {httpx.__version__} | ffmpeg: {self.ffmpeg or '缺失'}")
        print("=" * 78)

        self.section("0. 环境与管理员登录")
        if not self.check("v02.health", self.t_health):
            print("\n\033[1;31m服务不可用，终止执行。\033[0m")
            return 1
        self.check("v02.auth.login-admin", self.t_login)
        self.check("v02.system.info", self.t_system_info)
        self.check("v02.library.demo", self.t_library_and_demo)

        self.section_roles()
        if self.ctx.get("client1"):
            self.section_permissions()
            self.section_shares()
            self.section_consume()
            self.section_regression()
        else:
            self.record("v02.permissions", "SKIP", "用户创建失败，跳过后续依赖用例")
            self.record("v02.shares", "SKIP", "用户创建失败")
            self.record("v02.consume", "SKIP", "用户创建失败")
            self.record("v02.regression", "SKIP", "用户创建失败")

        return self._summary()

    def _summary(self) -> int:
        total = len(self.results)
        passed = sum(1 for row in self.results if row["status"] == "PASS")
        failed = [row for row in self.results if row["status"] == "FAIL"]
        skipped = sum(1 for row in self.results if row["status"] == "SKIP")
        print("\n" + "=" * 78)
        print("\033[1mv0.2 PASS/FAIL 矩阵\033[0m")
        print("-" * 78)
        for row in self.results:
            color = {"PASS": "\033[1;32m", "FAIL": "\033[1;31m", "SKIP": "\033[1;33m"}.get(row["status"], "")
            first_line = row["detail"].splitlines()[0] if row["detail"] else ""
            print(f"{color}{row['status']:<4}\033[0m {row['name']:<40} {row['duration']:6.2f}s  {first_line}")
        print("-" * 78)
        print(f"总计 {total} 项：\033[1;32m{passed} 通过\033[0m，\033[1;31m{len(failed)} 失败\033[0m，{skipped} 跳过")
        if failed:
            print("\n失败详情：")
            for row in failed:
                print(f"\n\033[1;31m✗ {row['name']}\033[0m")
                print(row["detail"])
            print("\n\033[1;31mE2E v0.2 SMOKE: FAIL\033[0m")
            return 1
        print("\n\033[1;32mE2E v0.2 SMOKE: PASS ✔\033[0m")
        return 0

    def close(self):
        for client in self.clients:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        super().close()


def main() -> int:
    default_consume = os.environ.get("ECHOIC_E2E_CONSUME_DIR") or str(REPO_ROOT / ".cache" / "e2e-v02-consume")
    parser = argparse.ArgumentParser(description="Echoic v0.2 端到端冒烟测试")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ECHOIC_E2E_BASE_URL") or os.environ.get("ECHOIC_BASE_URL") or "http://127.0.0.1:8100",
    )
    parser.add_argument("--username", default=os.environ.get("ECHOIC_E2E_ADMIN_USERNAME") or os.environ.get("ECHOIC_ADMIN_USERNAME") or "admin")
    parser.add_argument("--password", default=os.environ.get("ECHOIC_E2E_ADMIN_PASSWORD") or os.environ.get("ECHOIC_ADMIN_PASSWORD") or "admin123")
    parser.add_argument("--process-timeout", type=float, default=float(os.environ.get("ECHOIC_E2E_PROCESS_TIMEOUT") or 180))
    parser.add_argument("--consume-dir", default=default_consume)
    parser.add_argument("--log", default=os.environ.get("ECHOIC_E2E_LOG_V02") or str(E2E_DIR / "last-run-v02.log"))
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
        smoke = V02Smoke(args.base_url, args.username, args.password, args.process_timeout, Path(args.consume_dir))
        code = smoke.run_v02()
    finally:
        if smoke is not None:
            smoke.close()
        print(f"\n日志已保存：{log_path}")
        sys.stdout, sys.stderr = stdout_orig, stderr_orig
        log_fh.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
