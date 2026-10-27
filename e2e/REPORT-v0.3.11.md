# Echoic v0.3.11 发布验证与「合并说话人」轻量验收

- **验收日期**：2026-10-28 01:25 — 01:45（Asia/Shanghai）
- **验收人**：devops-qa（QA）
- **发布提交 / tag**：`5f3b955`（`feat(v0.3.11): merge mis-diarized speakers into one`）/ annotated tag `v0.3.11`（tag 对象 `60ab28b` → commit `5f3b955`）
- **范围**：按 Lead 要求做**轻量发布验证**（非全量 QA）：CI / Docker Publish / GHCR 匿名多架构校验 + 新「合并说话人」端点与 UI 抽查。
- **总体结论**：**PASS** ✅ —— 发布流水线与多架构镜像正常，说话人合并功能行为符合契约，无应用缺陷；仅有 1 条 CI 卫生观察（`sha-5f3b955` 标签因 main/tag 两次构建并发而被 main 构建覆盖，标签 `version=main`；正式语义化标签均指向 v0.3.11 正确镜像，不影响发布）。

---

## 1. GitHub Actions（全部 success）

| 工作流 | 事件 / 分支 | 运行 | 耗时 | 结果 |
|---|---|---|---|---|
| CI | push main | [40550294248](https://github.com/wpcwzy/echoic/actions/runs/40550294248) | 2m47s | ✅ Backend tests (pytest) + Frontend build (tsc + vite) |
| Docker Publish (GHCR) | push main | [40550293959](https://github.com/wpcwzy/echoic/actions/runs/40550293959) | 5m56s | ✅ Build & push multi-arch image |
| Docker Publish (GHCR) | push tag `v0.3.11` | [40550304789](https://github.com/wpcwzy/echoic/actions/runs/40550304789) | 5m36s | ✅ Build & push multi-arch image |

---

## 2. GHCR 匿名多架构校验

无登录（`Accept: application/vnd.oci.image.index.v1+json`）访问 `ghcr.io/wpcwzy/echoic`：

| tag | HTTP | manifest 类型 | index digest |
|---|---|---|---|
| `v0.3.11` / `0.3.11` / `0.3` / `0` / `latest` | 200 | OCI image index | **`sha256:dc584e407c08e42b00d1a9f94d4c25c985950ff7907a702289f4e205d8a6e7c5`**（5 个 tag 指向同一 release 镜像） |
| `sha-5f3b955` | 200 | OCI image index | `sha256:96d80f8bcf1b804b81ee1f5bb98d7e9326a5957409db79bde114944d0d11d9a0`（见 §5 观察项） |

- **平台**：`linux/amd64` + `linux/arm64` + 2 个 attestation manifest（provenance / SBOM，`unknown/unknown`）。
- **release 镜像（`v0.3.11` 等 canonical tags）**：
  - amd64 子 manifest：`sha256:f320e45a81eadafc3fc9b02de424a73f6b9e4da7ca94900f5ec97c1bcdd5b659`
  - amd64 config：`sha256:59310629d720fc18c3e4d16a628fdf1d1d29c9a16915c03279088d23df7c2138`，构建时间 `2026-10-27T17:25:35Z`
  - OCI labels：`title=Echoic`、**`version=v0.3.11`**、**`revision=5f3b95591991af0950fbd6936bcac3e4e239535c`**、`licenses=MIT`、`source=https://github.com/wpcwzy/echoic`
- `latest` / `0` / `0.3` / `0.3.11` 与 `v0.3.11` 指向同一 index，`docker pull ghcr.io/wpcwzy/echoic:v0.3.11` 在 amd64/arm64 可匿名拉取。

---

## 3. 「合并说话人」功能抽查（API + UI）

在全新数据目录、demo seed、mock 管线的临时实例（:8100，`frontend/dist` 为 v0.3.11 构建 `index-CVvJG7x0.js` / gzip 621.46 KB）上验证：

### 3.1 API（`POST /api/recordings/{id}/speakers/merge`）16/16 检查 PASS

以 demo 录音 TraceOpt（原 4 位说话人 / 73 段转录）合并 2 位说话人（源 `说话人2` 16 段 → 目标 `说话人1`）：

- 返回 200，`merged_segments=16`、`deleted_speaker_ids=[2]`；
- 说话人 **4 → 3**，源说话人被删除、目标保留并可按 `target_name` 重命名（合并后显示「合并后的说话人」）；
- 转录 **73 段数量不变**，所有片段的 `idx/start_ms/end_ms/text` 完全一致（文字与时间戳未被修改）；
- 源说话人的 16 段全部改为目标说话人（目标合并后共 50 段），无片段仍引用已删除说话人；
- 参数校验：空 `source_ids` → 422；已删除/不属于该录音的来源 → 400；非法目标 → 404。

### 3.2 UI（转写页）

- 详情页 → 转写页存在「合并说话人」按钮且可见；
- 弹窗文案正确（目标说话人（合并后保留）/ 要合并到目标的说话人（可多选）），目标下拉预选合并后的目标说话人，来源多选为空；弹窗说明「转录文字与时间戳不变；如需按新说话人重算 AI 分析，可重新生成」；
- 合并后转写页说话人数显示 3 位发言人（与 API 一致）；
- 录音页**无任何 Beta 文案**（v0.3.10 GA 状态未回退）。
- 证据截图（本地临时，未入库）：`.cache/v0311-merge-dialog.png`。

---

## 4. 观察项 / 风险

| 编号 | 级别 | 描述 | 建议 |
|---|---|---|---|
| OBS-v0311-01 | P3（CI/镜像卫生，非应用缺陷） | main push 与 tag push 两次 Docker Publish 同时为提交 `5f3b955` 构建并都推送 `sha-5f3b955`；main 运行结束更晚（17:30:24 vs 17:30:08），因此该 sha tag 被 main 构建覆盖：其镜像 `version` 标签为 `main`（而非 `v0.3.11`），digest 与语义化 tag 不同。**正式标签 `v0.3.11/0.3.11/0.3/0/latest` 均一致指向 v0.3.11 release 镜像，不受影响。** | 可选：`type=sha` 仅在 main push 或仅在 tag push 启用其一；或 metadata 的 `version` 标签改用 semver（避免 main 构建写 `version=main`）。不影响发布 |
| R1 | 本次为轻量验证 | 未重跑全量 pytest/回归冒烟（按 Lead 要求）；功能自测基线为后端 459 passed、UI 4→3 说话人 | 发布后如需可补跑 `scripts/verify.sh` |
| R2 | Docker 本地不可用 | 镜像运行时（含 sqlite-vec wheel 加载）仍以 CI 构建为准 | 在 Docker 主机做一次 `docker run` 冒烟（可选） |
| R3 | 真实 DashScope | 本次未涉及；沿用 v0.3.10 残余风险（真实实时/文件转写未联网实测） | 目标机配置真实 Key 后验证 |

---

## 5. 复现命令

```bash
# 发布流水线
gh run view 40550294248   # CI
gh run view 40550293959   # Docker Publish main
gh run view 40550304789   # Docker Publish tag v0.3.11

# GHCR 匿名多架构
TOKEN=$(curl -s 'https://ghcr.io/token?scope=repository:wpcwzy/echoic:pull&service=ghcr.io' | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  -H 'Accept: application/vnd.oci.image.index.v1+json' \
  https://ghcr.io/v2/wpcwzy/echoic/manifests/v0.3.11 | jq '.manifests[].platform'

# 说话人合并抽查（临时实例）
cd frontend && npm run build
ECHOIC_DATA_DIR=/tmp/echoic-v0311 ECHOIC_DEMO_SEED=true ECHOIC_EMBEDDED_WORKER=true \
ECHOIC_SECRET_KEY=e2e-secret-v0311 ECHOIC_STATIC_DIR=$PWD/frontend/dist \
ECHOIC_VECTOR_BACKEND=auto PORT=8100 ./scripts/run-local.sh --no-build

# admin 登录后：
# GET  /api/recordings?query=TraceOpt            -> 取 demo 录音 id
# GET  /api/recordings/{id}/transcript           -> 4 speakers / 73 segments
# POST /api/recordings/{id}/speakers/merge
#      {"source_ids":[2],"target_id":1,"target_name":"合并后的说话人"}
# GET  /api/recordings/{id}/transcript           -> 3 speakers / 73 segments（文字与时间戳不变）
```
