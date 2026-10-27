from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"
APP_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Runtime configuration, overridable with ECHOIC_* env vars or backend/.env."""

    model_config = SettingsConfigDict(
        env_prefix="ECHOIC_",
        env_file=(".env", str(REPO_ROOT / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Echoic"
    version: str = "0.3.6"
    debug: bool = False
    log_level: str = "INFO"

    # storage / db
    data_dir: Path = DEFAULT_DATA_DIR
    database_url: str = ""  # empty -> sqlite in data_dir

    # security
    secret_key: str = ""  # empty -> auto generated in data_dir/secret.key
    admin_username: str = "admin"
    admin_password: str = "admin123"
    admin_display_name: str = "管理员"
    session_days: int = 7

    # runtime
    embedded_worker: bool = True
    demo_seed: bool = True
    max_upload_mb: int = 2048
    share_cookie_hours: int = 2

    # multi-user / consume directory (P1 v0.2)
    allow_registration: bool = False
    consume_enabled: bool = True
    consume_dir: str = ""
    consume_interval_sec: int = 15
    consume_owner_username: str = ""

    # v0.3.0: trash / batch
    trash_retention_days: int = 30
    batch_max_items: int = 100

    # v0.3.0: object storage (local | s3)
    storage_backend: str = "local"
    s3_endpoint: str = ""
    s3_bucket: str = "echoic"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_path_style: bool = True
    s3_prefix: str = ""
    s3_presign_redirect: bool = False

    # v0.3.0: live transcription (beta, default off)
    live_enabled: bool = False
    live_provider: str = "auto"
    live_max_minutes: int = 60
    live_merge_mode: str = "replace"

    # v0.3.0: vector retrieval
    vector_backend: str = "auto"
    vector_top_k: int = 8
    vector_min_score: float = 0.2
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    static_dir: str = ""  # empty -> backend/app/static, fallback frontend/dist

    # provider defaults (only used to seed app_settings on first run)
    asr_provider: str = "mock"
    asr_api_key: str = ""
    asr_base_url: str = ""
    asr_model: str = ""
    asr_language: str = "zh"
    asr_diarization: bool = True
    # v0.3.5: independent realtime ASR configuration (fallback to the ASR key/base when empty)
    realtime_asr_provider: str = "auto"
    realtime_asr_api_key: str = ""
    realtime_asr_base_url: str = ""
    realtime_asr_model: str = ""
    realtime_asr_language: str = "zh"

    llm_provider: str = "mock"
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.3

    embeddings_provider: str = "none"
    embeddings_api_key: str = ""
    embeddings_base_url: str = ""
    embeddings_model: str = ""
    embeddings_dimensions: int = 1024

    @property
    def data_path(self) -> Path:
        p = Path(os.path.expanduser(str(self.data_dir)))
        if not p.is_absolute():
            p = (REPO_ROOT / p).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def media_path(self) -> Path:
        p = self.data_path / "media"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def consume_path(self) -> Path:
        if self.consume_dir:
            p = Path(os.path.expanduser(self.consume_dir))
            if not p.is_absolute():
                p = (REPO_ROOT / p).resolve()
        else:
            p = self.data_path / "consume"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{self.data_path / 'echoic.db'}"

    @property
    def resolved_static_dir(self) -> Path | None:
        if self.static_dir:
            p = Path(self.static_dir).expanduser()
            return p if p.is_absolute() else (REPO_ROOT / p).resolve()
        app_static = APP_DIR / "static"
        if (app_static / "index.html").exists():
            return app_static
        dist = REPO_ROOT / "frontend" / "dist"
        if (dist / "index.html").exists():
            return dist
        return None

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def get_secret_key(self) -> str:
        """Return the signing/encryption secret, generating and persisting one if needed."""
        if self.secret_key:
            return self.secret_key
        key_file = self.data_path / "secret.key"
        if key_file.exists():
            value = key_file.read_text(encoding="utf-8").strip()
            if value:
                self.secret_key = value
                return value
        value = secrets.token_urlsafe(48)
        key_file.write_text(value, encoding="utf-8")
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
        self.secret_key = value
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
