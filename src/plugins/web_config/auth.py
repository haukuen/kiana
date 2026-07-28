"""Web GUI 访问鉴权。

规则:
- 未配置 `web_config_token`: 仅 localhost (127.0.0.1 / ::1 / localhost) 允许。
- 配置了 token: 必须 `Authorization: Bearer <token>`。
"""

from __future__ import annotations

from fastapi import Header, HTTPException, Request


def verify_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> bool:
    """FastAPI 依赖项:校验访问权限。"""
    # 函数内 import 避免循环导入
    from . import config  # noqa: PLC0415

    token = config.web_config_token

    if not token:
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "localhost"):
            raise HTTPException(403, "Remote access requires web_config_token")
        return True

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != token:
        raise HTTPException(403, "Invalid token")
    return True
