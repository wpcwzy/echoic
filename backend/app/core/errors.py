from __future__ import annotations

from fastapi import HTTPException


class ApiError(HTTPException):
    """HTTP error with a machine readable code, serialized as {"detail","code"}."""

    def __init__(self, status_code: int, message: str, code: str = "ERROR", headers: dict | None = None):
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code
        self.message = message


def bad_request(message: str, code: str = "VALIDATION_ERROR") -> ApiError:
    return ApiError(400, message, code)


def unauthorized(message: str = "请先登录") -> ApiError:
    return ApiError(401, message, "UNAUTHORIZED")


def forbidden(message: str = "没有权限执行该操作") -> ApiError:
    return ApiError(403, message, "FORBIDDEN")


def not_found(message: str = "资源不存在") -> ApiError:
    return ApiError(404, message, "NOT_FOUND")


def conflict(message: str, code: str = "CONFLICT") -> ApiError:
    return ApiError(409, message, code)


class ProviderError(Exception):
    """Raised by ASR/LLM provider implementations; message is user friendly (no secrets)."""

    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.status_code = status_code
