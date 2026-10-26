"""Settings store tests (read-only assertions over Lead-owned settings_store).

Covers Fernet encryption at rest (no plaintext in DB), masking, partial
updates (keep / __clear__), prompt overrides + reset, and default bootstrap.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.core.errors import ApiError
from app.core.crypto import decrypt_secret
from app.db import Base, SessionLocal, init_db
from app.models import AppSetting
from app.services.prompts import DEFAULT_PROMPTS
from app.services.settings_store import (
    ensure_default_settings,
    load_ai_config,
    masked_settings,
    reset_prompts,
    update_settings,
)

SECRET = "sk-super-secret-1234567890"


@pytest.fixture()
def database(db):
    init_db()
    with SessionLocal() as session:
        for table in reversed(Base.metadata.sorted_tables):
            session.execute(table.delete())
        session.commit()
    ensure_default_settings(db)
    yield db


def _raw_settings(db) -> dict[str, str]:
    return {row.key: row.value_json for row in db.scalars(select(AppSetting)).all()}


def test_defaults_bootstrap(database):
    cfg = load_ai_config(database)
    assert cfg.asr.provider == "mock"
    assert cfg.llm.provider == "mock"
    assert cfg.embeddings.provider == "none"
    assert cfg.demo_mode is True
    assert set(cfg.prompts) == set(DEFAULT_PROMPTS)
    assert cfg.prompts["summary"] == DEFAULT_PROMPTS["summary"]
    settings_out = masked_settings(database)
    assert settings_out["system"]["demo_mode"] is True
    assert settings_out["ai"]["asr"]["provider"] == "mock"
    assert settings_out["ai"]["asr"]["configured"] is True
    assert settings_out["ai"]["embeddings"]["provider"] == "none"
    assert settings_out["prompt_overrides"] == []
    worker = settings_out["system"]["worker"]
    assert isinstance(worker["running"], bool)
    assert isinstance(worker["queue_depth"], int)


def test_api_key_is_encrypted_at_rest_and_masked(database):
    update_settings(
        database,
        {
            "ai": {
                "llm": {
                    "provider": "openai",
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "model": "qwen-plus",
                    "api_key": SECRET,
                }
            }
        },
    )
    raw = _raw_settings(database)
    assert all(SECRET not in value for value in raw.values()), "API key must not be stored in plaintext"
    row = database.get(AppSetting, "ai.llm")
    stored = json.loads(row.value_json)
    assert stored["api_key_enc"] and stored["api_key_enc"] != SECRET
    assert decrypt_secret(stored["api_key_enc"]) == SECRET

    cfg = load_ai_config(database)
    assert cfg.llm.api_key == SECRET
    assert cfg.llm.provider == "openai"
    assert cfg.llm.model == "qwen-plus"

    settings_out = masked_settings(database)
    llm_out = settings_out["ai"]["llm"]
    assert llm_out["has_api_key"] is True
    assert llm_out["configured"] is True
    assert SECRET not in json.dumps(settings_out, ensure_ascii=False)
    assert "****" in llm_out["api_key_masked"]
    assert llm_out["api_key_masked"].endswith("7890")


def test_update_settings_keeps_key_when_blank_and_clears_on_marker(database):
    update_settings(
        database,
        {"ai": {"llm": {"provider": "openai", "api_key": SECRET, "model": "qwen-plus"}}},
    )
    # Empty/omitted api_key keeps the previous value.
    update_settings(database, {"ai": {"llm": {"model": "qwen-max"}}})
    update_settings(database, {"ai": {"llm": {"api_key": ""}}})
    assert load_ai_config(database).llm.api_key == SECRET
    assert load_ai_config(database).llm.model == "qwen-max"

    # __clear__ wipes the encrypted value.
    update_settings(database, {"ai": {"llm": {"api_key": "__clear__"}}})
    assert load_ai_config(database).llm.api_key == ""
    assert masked_settings(database)["ai"]["llm"]["has_api_key"] is False


def test_invalid_provider_and_base_url_rejected(database):
    with pytest.raises(ApiError) as excinfo:
        update_settings(database, {"ai": {"llm": {"provider": "not-a-provider"}}})
    assert excinfo.value.status_code == 400
    with pytest.raises(ApiError):
        update_settings(database, {"ai": {"asr": {"provider": "mock", "base_url": "ftp://example.com"}}})


def test_prompt_override_and_reset(database):
    custom = "TASK=minutes\n自定义纪要提示词\n{transcript}"
    update_settings(database, {"prompts": {"summary": custom}})
    cfg = load_ai_config(database)
    assert cfg.prompts["summary"] == custom
    assert cfg.prompts["chapters"] == DEFAULT_PROMPTS["chapters"]
    assert masked_settings(database)["prompt_overrides"] == ["summary"]

    reset_prompts(database)
    cfg = load_ai_config(database)
    assert cfg.prompts["summary"] == DEFAULT_PROMPTS["summary"]
    assert masked_settings(database)["prompt_overrides"] == []
