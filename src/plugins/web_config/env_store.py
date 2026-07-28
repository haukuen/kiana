"""python-dotenv 读写封装。

- `read_env`: 读取单个 env 文件全部 KV。
- `write_fields`: 部分更新,自动序列化 list/dict/bool。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, set_key


def read_env(path: Path) -> dict[str, str]:
    """返回 .env 文件所有 KV,值为字符串原样。

    文件不存在时返回空 dict。
    """
    if not path.is_file():
        return {}
    # dotenv_values 已经会忽略注释行和空行,且返回值类型为 Optional[str]
    raw = dotenv_values(str(path))
    return {k: (v if v is not None else "") for k, v in raw.items()}


def write_fields(path: Path, updates: dict[str, Any]) -> None:
    """对 .env 做部分更新。list/dict 用 json.dumps,bool 用 lowercase str。

    文件不存在时 set_key 会自动创建。
    """
    for key, value in updates.items():
        value_str = _serialize(value)
        # quote_mode="always":值里有 = # " 等特殊字符更安全
        set_key(str(path), key, value_str, quote_mode="always")


def _serialize(value: Any) -> str:
    if isinstance(value, bool):
        # 注意:必须在 int 之前判断(bool 是 int 子类)
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)
