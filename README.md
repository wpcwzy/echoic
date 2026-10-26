# Echoic

**English** | [简体中文](README.zh-CN.md)

[![CI](https://github.com/wpcwzy/echoic/actions/workflows/ci.yml/badge.svg)](https://github.com/wpcwzy/echoic/actions/workflows/ci.yml)
[![Docker Publish (GHCR)](https://github.com/wpcwzy/echoic/actions/workflows/docker-publish.yml/badge.svg)](https://github.com/wpcwzy/echoic/actions/workflows/docker-publish.yml)
[![GHCR](https://img.shields.io/badge/ghcr.io-wpcwzy%2Fechoic-2496ED?logo=docker&logoColor=white)](https://github.com/wpcwzy/echoic/pkgs/container/echoic)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> A self-hosted private knowledge base for **meetings, calls, voice recordings and videos**.
> Upload a file or record from the browser, and get a **transcript, AI minutes, viewpoint tree & mind map, chapters, key insights, action items and AI Q&A** — with all data staying on your own server.

- Positioning: what paperless-ngx is to paper documents, **Echoic is to recordings**
- Version: **v0.2.0** — P1 batch 1 (multi-user & roles, share links, consume-directory import, CI/CD + multi-arch GHCR image)
- License: MIT

## 📸 Screenshots

> Captured from the built-in demo data. More in [`docs/screenshots/`](docs/screenshots/).

| Library | Recording detail · viewpoint tree |
|---|---|
| ![Library](docs/screenshots/02-library.png) | ![Recording detail](docs/screenshots/03-detail-viewpoint.png) |

| AI mind map | Settings · AI providers |
|---|---|
| ![Mind map](docs/screenshots/05-mindmap.png) | ![Settings](docs/screenshots/06-settings.png) |

**v0.2 collaboration & sharing**

| User & role management | Create a share link |
|---|---|
| ![Users](docs/screenshots/v0.2/01-users.png) | ![Share dialog](docs/screenshots/v0.2/02-share-dialog.png) |

| Public share page (no login) | Password-protected share |
|---|---|
| ![Public share](docs/screenshots/v0.2/03-public-share.png) | ![Password gate](docs/screenshots/v0.2/04-public-password.png) |

---

## ✨ Features

| Capability | Details |
|---|---|
| 🎙 Browser recording + upload | Record directly from the microphone (timer / pause / level meter); supports mp3/wav/m4a/aac/ogg/webm/mp4/mov/mkv up to 2 GB per file |
| 📝 Speech-to-text | Plug-and-play providers: Alibaba Cloud DashScope / OpenAI-compatible Whisper / Mock; transcript with speaker labels and timestamps, renameable and editable |
| 🧠 AI minutes | Markdown summary + key conclusions + decisions + action items; regenerable |
| 🌳 Viewpoint tree & mind map | Structured viewpoint tree rendered as an interactive mind map; click any node to seek in the recording |
| 📑 Smart chapters | Automatic chapter segmentation (title / time range / summary / bullets); click to jump |
| 💬 AI Q&A | RAG over transcripts + minutes, streamed over SSE, answers cite `[12:05]` timestamps that seek the player |
| 📚 Library | Search / status filters / tags / sorting / stats; global search jumps to the matching transcript segment |
| 🎧 Player | Drag-to-seek, ±15 s, playback speed, chapter ticks, active-line highlighting, HTTP Range streaming |
| 💾 Data ownership | SQLite (optional PostgreSQL) + local media directory; backup = archive one data volume |
| 🔐 Security | HttpOnly cookie JWT, bcrypt passwords, Fernet-encrypted API keys (masked in responses), upload validation, login rate limiting |
| 🎭 Demo mode | Full workflow with zero API keys and zero internet access (bundled demo recordings + complete AI results) |
| 👥 Multi-user & roles | `admin` / `member`; admin user management (create / edit / disable / reset password / delete), registration switch; recordings are owner-scoped and admins can transfer ownership; disabled accounts lose sessions immediately |
| 🔗 Share links | One-click sharing from the detail page: optional password, expiry (1 h – never), include transcript/minutes, allow download; public read-only page `/s/{token}` with revoke and view counters |
| 📥 Consume directory | Drop audio/video into `data/consume/` and it is imported and processed automatically (paperless-style); sha256 dedupe, failures quarantined, status and manual scan in Settings |
| 📤 Export | Transcript export as TXT / SRT / Markdown / JSON |
| 🐳 One-command deploy | Prebuilt multi-arch **GHCR image** (amd64/arm64, published by CI) + image-first docker compose: `pull && up -d`; source builds supported; optional standalone worker / PostgreSQL |

---

## 🏗 Architecture

```
┌──────────────────────── Browser (React 18 + TS + Vite + AntD 5) ────────────────────────┐
│  Library · Record & upload · Detail (transcript | minutes | viewpoint tree | chapters)  │
│  AI Q&A panel · Mind map · Settings · Users · Public share page                         │
└───────────────────────────────────────┬─────────────────────────────────────────────────┘
                                        │ same-origin HTTPS · HttpOnly cookie JWT · SSE
┌───────────────────────────────────────▼─────────────────────────────────────────────────┐
│ FastAPI (single process, uvicorn)                                                       │
│  /api/auth /api/recordings /transcript /insights /chat(SSE) /search /tags /settings …   │
│  /api/users /api/shares /api/public/shares /api/system/consume …                        │
│  ├─ core: config · security · crypto · storage · media (ffmpeg)                         │
│  ├─ providers: pluggable ASR / LLM / Embedding (mock / DashScope / OpenAI-compatible)   │
│  ├─ services: pipeline · job queue · chat RAG · analysis · consume · demo               │
│  └─ embedded worker thread (or standalone process); serves the built frontend           │
└──────┬──────────────────────────────────────────────┬───────────────────────────────────┘
       │ SQLAlchemy 2.0                               │ local filesystem (mounted volume /data)
┌──────▼──────────────┐                    ┌──────────▼───────────────────────────────────┐
│ SQLite (default,WAL)│                    │ echoic.db · secret.key · media/{uuid}/…      │
│ PostgreSQL (opt.)   │                    │ original.* · playback.m4a · asr.wav          │
└─────────────────────┘                    └──────────────────────────────────────────────┘
       │
       ▼ outbound HTTPS only (self-hosted / intranet-compatible endpoints also work)
  Alibaba Cloud DashScope (paraformer ASR / qwen LLM / embeddings)
  OpenAI-compatible LLM (DeepSeek / Ollama / vLLM / LM Studio…)
  Whisper-compatible ASR (OpenAI / faster-whisper-server / FunASR…)
```

Processing pipeline:

```
upload/record → store + ffprobe → [prepare] normalize 16k wav + playback.m4a
             → [transcribe] ASR (speaker diarization)
             → [analyze] LLM minutes/chapters/viewpoint tree/insights/action items
             → [embed] chunks + vectors (optional) → [finalize] status=ready
failure → status=failed + human-readable message, retryable; progress/stage exposed for polling
```

More design docs: [ARCHITECTURE.md](docs/ARCHITECTURE.md) · [PIPELINE.md](docs/PIPELINE.md) · [API.md](docs/API.md) · [FRONTEND.md](docs/FRONTEND.md)

---

## 🚀 Quick start (prebuilt Docker image, recommended)

No local build required — pull the multi-arch image published by CI (linux/amd64 + linux/arm64).

```bash
# 1. Prepare environment variables (at minimum change the admin password)
cp .env.example .env
vi .env                    # set ECHOIC_ADMIN_PASSWORD (default admin123 is for first-run only)

# 2. Pull the prebuilt image and start
docker compose pull
docker compose up -d

# 3. Open http://localhost:8000 and sign in with admin / your password
```

- Image: `ghcr.io/wpcwzy/echoic`, tags `latest` (newest main), semver tags such as `v0.2.0`, and `sha-xxxxxxx`;
  choose one via `ECHOIC_IMAGE_TAG` in `.env` (pin a version in production, e.g. `v0.2.0`);
- The GHCR package is **public** — `docker compose pull` works anonymously;
  if you ever get a 403, run `docker login ghcr.io` or set the package visibility to Public
  (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) §3.2);
- **Mock demo mode** is enabled by default: no API keys required, example recordings and complete AI results ship with the app;
- To use real models, open **Settings → AI providers**, enter your DashScope or OpenAI-compatible endpoint/key/model and click **Test connection**;
- Data, media and secrets live in the `echoic-data` volume — back it up to migrate.

### Build from source (optional)

```bash
# For development, customization or offline environments; builds a local tag echoic:local
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

Full deployment guide (reverse proxy, backup/restore, upgrades, PostgreSQL, standalone worker, troubleshooting):
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

---

## 🐳 Container images & CI/CD

| Item | Details |
|---|---|
| Image | `ghcr.io/wpcwzy/echoic` (GitHub Container Registry) |
| Architectures | `linux/amd64` + `linux/arm64` manifest (x86 servers, Apple Silicon and ARM cloud hosts) |
| Tags | `latest` (each main push), `vX.Y.Z` / `X.Y` / `X` (git tags), `sha-xxxxxxx` (each build, for reproducibility/rollback) |
| Publish pipeline | [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml): QEMU + Buildx multi-arch build → GHCR; BuildKit GHA cache + provenance |
| CI | [`.github/workflows/ci.yml`](.github/workflows/ci.yml): backend pytest (with ffmpeg) + frontend tsc/vite build + PR image build (amd64, no push) |
| Dependency updates | [`.github/dependabot.yml`](.github/dependabot.yml): weekly pip / npm / GitHub Actions / Docker base image checks |

**Releasing a new version** (maintainers):

```bash
git tag -a v0.2.0 -m "Echoic v0.2.0" && git push origin v0.2.0   # builds v0.2.0 / 0.2 / 0 image tags
docker pull ghcr.io/wpcwzy/echoic:v0.2.0                          # verify after publish
```

**GHCR package visibility (once per package)**: new GitHub packages are private by default, so anonymous
`docker pull` returns 403. The publish workflow attempts to switch the package to public via `GITHUB_TOKEN`
(`continue-on-error`; this often lacks permission). If it did not work, go to
<https://github.com/users/wpcwzy/packages/container/echoic/settings> → Danger Zone → Change visibility → **Public**,
or use a PAT with `write:packages`:

```bash
echo "<PAT>" | docker login ghcr.io -u wpcwzy --password-stdin
docker pull ghcr.io/wpcwzy/echoic:latest
```

---

## ✅ Verification (v0.2.0)

| Item | Result | Evidence |
|---|---|---|
| GitHub Actions CI | **Passing**: backend pytest + frontend tsc/vite build; PR also validates the amd64 image build | [CI runs](https://github.com/wpcwzy/echoic/actions/workflows/ci.yml) |
| Multi-arch image publish | **Succeeded**: linux/amd64 + linux/arm64 pushed to GHCR (`latest` / `sha-*`; semver tags on releases) | [Publish runs](https://github.com/wpcwzy/echoic/actions/workflows/docker-publish.yml) · [GHCR package](https://github.com/wpcwzy/echoic/pkgs/container/echoic) |
| Backend tests | **179 passed** (v0.1 baseline 129 + v0.2 users/permissions/shares/consume/migrations 50) | `cd backend && .venv/bin/pytest -q` |
| Frontend build | **Passing** (`tsc --noEmit` clean; ~599 KB JS + 18.5 KB CSS gzipped) | `cd frontend && npm run build` |
| End-to-end smoke (mock, fresh database) | v0.1 baseline **30/30** + v0.2 **34/34 PASS**: permission matrix, user CRUD, share create/password/expiry/revoke/public access, consume import/dedupe/quarantine, registration switch, regression suite | [`e2e/last-run.log`](e2e/last-run.log) · [`e2e/last-run-v02.log`](e2e/last-run-v02.log) · [`e2e/REPORT-v0.2.md`](e2e/REPORT-v0.2.md) |
| UI screenshot audit | v0.1 6/6 + v0.2 7/7, no overflow, no uncaught exceptions, no 5xx | [`docs/screenshots/`](docs/screenshots/) · [`docs/screenshots/v0.2/`](docs/screenshots/v0.2/) |

---

## 🛠 Development

Requirements: Python 3.11+ (3.12 recommended), Node.js 20+ (22 recommended), ffmpeg.

```bash
# Backend + frontend (hot reload): backend http://127.0.0.1:8000, frontend http://localhost:5173
./scripts/dev.sh

# Production-like local run: build the frontend, serve it from FastAPI on :8100
./scripts/run-local.sh

# One-shot verification: pytest + frontend typecheck/build + health + e2e smoke
./scripts/verify.sh
```

Manual setup:

```bash
# Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8000

# Frontend (Vite dev server proxies /api → :8000)
cd frontend
npm ci        # or npm install
npm run dev
```

Repository layout:

```
backend/app/{api,core,providers,services}   FastAPI app + pluggable AI providers
backend/app/{main.py,config.py,models.py,…} app wiring / config / ORM / DTOs
frontend/src/{pages,components,layouts,api} React frontend
docker/{entrypoint.sh}                      container entrypoint
scripts/{dev.sh,run-local.sh,verify.sh}     local dev / verification scripts
docs/                                       architecture, pipeline, API, frontend, deployment
e2e/                                        end-to-end smoke scripts and acceptance reports
```

---

## ⚙️ Configuration

Everything is configurable through `ECHOIC_*` environment variables or `.env` (see [.env.example](.env.example) for the full annotated list). Common options:

| Variable | Default | Description |
|---|---|---|
| `ECHOIC_ADMIN_USERNAME` / `ECHOIC_ADMIN_PASSWORD` | `admin` / `admin123` | Admin account created on first start (change it afterwards in Settings) |
| `ECHOIC_SECRET_KEY` | auto-generated | JWT / API-key encryption secret; if empty, generated into the data directory |
| `ECHOIC_DATA_DIR` | `./data` (`/data` in Docker) | SQLite, media and secrets directory |
| `ECHOIC_DATABASE_URL` | empty (SQLite) | e.g. `postgresql+psycopg://…` for PostgreSQL |
| `ECHOIC_DEMO_SEED` | `true` | Seed bundled demo recordings into an empty library |
| `ECHOIC_EMBEDDED_WORKER` | `true` | In-process worker thread; set `false` for a standalone worker |
| `ECHOIC_MAX_UPLOAD_MB` | `2048` | Per-file upload limit |
| `ECHOIC_IMAGE_TAG` | `latest` | **Deployment**: GHCR image tag (`latest` / `v0.2.0` / `sha-xxxxxxx`) |
| `ECHOIC_ALLOW_REGISTRATION` | `false` | v0.2: allow self-service registration (default: admins create accounts) |
| `ECHOIC_SHARE_COOKIE_HOURS` | `2` | v0.2: public-share unlock cookie lifetime (hours) |
| `ECHOIC_CONSUME_ENABLED` | `true` | v0.2: watch the consume directory and import recordings automatically |
| `ECHOIC_CONSUME_DIR` | empty (`<data>/consume`) | v0.2: consume directory (`/data/consume` in Docker, persisted with the data volume) |
| `ECHOIC_CONSUME_INTERVAL_SEC` | `15` | v0.2: consume scan interval (seconds) |
| `ECHOIC_CONSUME_OWNER_USERNAME` | empty (first admin) | v0.2: owner of imported recordings |
| `ECHOIC_ASR_PROVIDER` / `ECHOIC_LLM_PROVIDER` | `mock` / `mock` | AI providers (`mock` / `dashscope_realtime` / `openai` …) |
| `ECHOIC_ASR_API_KEY` / `ECHOIC_LLM_API_KEY` | empty | Provider keys (seeded into the database on first start; settings UI takes precedence) |

> Note: `ECHOIC_*AI*` values from `.env` are written into the database **only on first start**. Change them later in **Settings** (effective immediately, keys encrypted at rest).

---

## 🗺 Roadmap

### v0.2.0 (P1 batch 1, shipped)

- [x] Multi-arch container image + automated GHCR publishing + GitHub Actions CI/CD (image-first compose)
- [x] Multi-user / roles (admin / member) + user management + registration switch
- [x] Public share links (password / expiry / permission flags / read-only page / revoke / view counts)
- [x] Consume directory auto-import (watch folder, auto-create recordings, dedupe, failure quarantine)
- [x] Database migration mechanism with v0.1 upgrade compatibility

### v0.2.1 / v0.3 (planned, see docs/09)

- [ ] Dark theme and trash / batch operations (v0.2.1)
- [ ] Object storage (S3/MinIO) abstraction, extension cleanup (v0.2.1 / v0.3)
- [ ] Live transcription and live captions (v0.3)
- [ ] Vector search (sqlite-vec / pgvector) and hybrid retrieval (v0.3)
- [ ] Self-service registration page, member profile / password page
- [ ] Video slide/scene extraction, mobile app / browser extension, OAuth/LDAP, audit logs

---

## 📄 License

[MIT License](LICENSE) © 2026 Echoic contributors.

---

## 🔗 Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Goals/non-goals, architecture, data model, security design |
| [docs/PIPELINE.md](docs/PIPELINE.md) | Processing pipeline, job queue, provider interfaces, mock behavior |
| [docs/API.md](docs/API.md) | REST + SSE contract (single source of truth) |
| [docs/FRONTEND.md](docs/FRONTEND.md) | Pages, routes, components, visual guidelines |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Deployment guide: Docker, backups, upgrades, troubleshooting (Chinese) |
| [docs/08-P1-协作与分享设计.md](docs/08-P1-协作与分享设计.md) | v0.2 contract: roles/permissions, share links, consume, migrations |
| [docs/09-P1-后续能力技术方案.md](docs/09-P1-后续能力技术方案.md) | Remaining P1 technical plan: live transcription, vector search, object storage, trash, dark theme |
| [e2e/REPORT.md](e2e/REPORT.md) · [e2e/REPORT-v0.2.md](e2e/REPORT-v0.2.md) | End-to-end acceptance reports (v0.1 / v0.2) |
| [CHANGELOG.md](CHANGELOG.md) | Release notes and known limitations |
