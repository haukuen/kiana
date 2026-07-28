"""Web Config REST 路由。

所有接口前缀 `/web_config/api`,均依赖 verify_token 鉴权。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import verify_token
from .env_store import read_env, write_fields
from .scanner import scan_all_plugins

logger = logging.getLogger("web_config.routes")

router = APIRouter(prefix="/web_config/api", tags=["web_config"], dependencies=[Depends(verify_token)])

# 合法环境变量名:`^[A-Za-z_][A-Za-z0-9_]*$`。防止 key 含 `\n` 等字符
# 在 .env 中造成注入(例如 `"a\nEVIL=injected"` 会被 python-dotenv 当成新变量)。
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _plugins_dir() -> Path:
    """从插件 __init__ 拿到 PLUGINS_DIR。函数内 import 避免循环。"""
    from . import PLUGINS_DIR  # noqa: PLC0415

    return PLUGINS_DIR


def _env_file() -> Path:
    from . import ENV_FILE  # noqa: PLC0415

    return ENV_FILE


@router.get("/schema")
def get_schema() -> list[dict[str, Any]]:
    """返回所有插件的 form schema。"""
    plugins = scan_all_plugins(_plugins_dir())
    return [_plugin_to_dict(p) for p in plugins]


@router.get("/config")
def get_config() -> dict[str, str]:
    """返回 .env.prod 当前所有 KV。"""
    return read_env(_env_file())


@router.put("/config")
def put_config(body: dict[str, Any]) -> dict[str, bool]:
    """部分更新 .env.prod。"""
    bad_keys = [k for k in body if not ENV_KEY_RE.match(k)]
    if bad_keys:
        raise HTTPException(400, f"非法的配置项名: {bad_keys[:3]}")
    try:
        write_fields(_env_file(), body)
    except (OSError, PermissionError) as e:
        logger.exception("写入 .env.prod 失败")
        raise HTTPException(500, f"写入 .env.prod 失败: {e}. 检查 docker volume 是否为 rw") from e
    return {"ok": True}


# ── 序列化辅助 ────────────────────────────────────────────────
# dataclasses.asdict 会递归转 dict,且对 None / list 处理符合 JSON 期望。


def _field_to_dict(f: Any) -> dict[str, Any]:
    return {
        "key": f.key,
        "label": f.label,
        "type": f.type,
        "default": f.default,
        "secret": f.secret,
        "description": f.description,
        "min": f.min,
        "max": f.max,
        "options": f.options,
        "subfields": [_field_to_dict(s) for s in f.subfields] if f.subfields else None,
    }


def _plugin_to_dict(p: Any) -> dict[str, Any]:
    return {
        "plugin_name": p.plugin_name,
        "display_name": p.display_name,
        "description": p.description,
        "fields": [_field_to_dict(f) for f in p.fields],
    }
