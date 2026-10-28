"""Settings & provider connection-test endpoints (docs/API.md §6).

v0.2: settings (including the masks of API keys) are admin-only.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from ..core.errors import ProviderError
from ..db import get_db
from ..deps import require_admin
from ..models import User
from ..schemas import (
    ProviderTestIn,
    ProviderTestOut,
    SettingsOut,
    SettingsUpdateIn,
)
from ..services import audit as audit_service
from ..services.settings_store import masked_settings, reset_prompts, update_settings

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


def _settings_payload(db: Session) -> SettingsOut:
    return SettingsOut.model_validate(masked_settings(db))


@router.get("", response_model=SettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return _settings_payload(db)


@router.put("", response_model=SettingsOut)
def put_settings(
    payload: SettingsUpdateIn,
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    patch = payload.model_dump(exclude_none=True)
    # Never audit settings values (they can carry API keys): section names only.
    sections = sorted(str(key) for key in patch.keys())
    update_settings(db, patch)
    result = _settings_payload(db)
    audit_service.audit_request(
        db,
        request,
        _admin,
        "settings.update",
        target_type="settings",
        detail={"sections": sections},
    )
    return result


@router.post("/test", response_model=ProviderTestOut)
async def test_provider(
    payload: ProviderTestIn,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    try:
        from ..providers import registry

        result = await registry.test_provider(db, payload.kind, payload.draft)
        return ProviderTestOut.model_validate(result)
    except ProviderError as exc:
        return ProviderTestOut(ok=False, message=exc.message)
    except NotImplementedError:  # pragma: no cover - transitional stub
        return ProviderTestOut(ok=False, message="连接测试尚未实现")
    except Exception as exc:  # contract: failures still answer HTTP 200 with ok=false
        logger.warning("provider test failed kind=%s: %s", payload.kind, exc)
        return ProviderTestOut(ok=False, message=f"连接测试失败：{exc}")


@router.post("/prompts/reset", response_model=SettingsOut)
def reset_prompt_settings(
    request: Request,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    reset_prompts(db)
    result = _settings_payload(db)
    audit_service.audit_request(
        db,
        request,
        _admin,
        "settings.prompts_reset",
        target_type="settings",
    )
    return result
