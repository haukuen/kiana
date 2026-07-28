"""Web 配置 GUI 后端。

挂载:
- `/web_config/api/*`: REST 接口 (routes.py)
- `/web_config/static/*`: 静态文件 (static/, 另一 agent 负责)
所有路径前缀均用 `/web_config/`,避免与 OneBot `/onebot/v11/` 冲突。
"""

from __future__ import annotations

import logging
from pathlib import Path

import nonebot
from nonebot import get_driver, get_plugin_config
from nonebot.plugin import PluginMetadata

from .config import Config

logger = logging.getLogger("web_config")

__plugin_meta__ = PluginMetadata(
    name="web_config",
    description="Web 配置 GUI",
    usage="浏览器访问 /web_config/ 编辑 .env 配置",
    type="application",
    config=Config,
)

# 路径解析(模块级常量,供 routes / auth 通过 `from . import PLUGINS_DIR` 读到)
_PLUGIN_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _PLUGIN_DIR.parents[2]  # src/plugins/web_config -> 项目根
PLUGINS_DIR = _PROJECT_ROOT / "src" / "plugins"

# 读插件自身配置;NoneBot 未初始化时(如直接 import 子模块做扫描)
# 使用默认值,保证包始终可被导入。
try:
    config: Config = get_plugin_config(Config)
except Exception:
    config = Config()


def _resolve_env_file() -> Path:
    """目标 env 文件路径(写死 .env.prod,相对项目根解析)。"""
    return _PROJECT_ROOT / ".env.prod"


# 模块级常量:GUI 读写唯一的目标文件
ENV_FILE: Path = _resolve_env_file()

# ── 注册 FastAPI 路由 + 静态文件 ──────────────────────────────
# 模块级 `nonebot.get_app()` 仅在 NoneBot 已初始化时生效;否则跳过,
# 不影响 `from .scanner import ...` 等纯工具导入。


def _mount_web_endpoints() -> None:
    from fastapi.responses import RedirectResponse  # noqa: PLC0415
    from fastapi.staticfiles import StaticFiles  # noqa: PLC0415

    from . import routes  # noqa: PLC0415

    app = nonebot.get_app()
    app.include_router(routes.router)

    # 访问入口:GET /web_config -> 重定向到 static 下的 index.html
    @app.get("/web_config", include_in_schema=False)
    async def _redirect_to_gui() -> RedirectResponse:
        return RedirectResponse(url="/web_config/static/")

    static_dir = _PLUGIN_DIR / "static"
    if static_dir.is_dir():
        app.mount(
            "/web_config/static",
            StaticFiles(directory=str(static_dir), html=True),
            name="web_config_static",
        )
    else:
        nonebot.logger.warning(
            f"web_config: 静态目录 {static_dir} 不存在,跳过挂载 /web_config/static"
        )


try:
    _mount_web_endpoints()
except Exception:
    # NoneBot 未初始化(直接导入子模块场景,如扫描测试),静默跳过挂载
    logger.debug("web_config 跳过 FastAPI 挂载(NoneBot 可能未初始化)", exc_info=True)
