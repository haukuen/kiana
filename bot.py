import sys

import nonebot
from nonebot import logger
from nonebot.adapters.onebot.v11 import Adapter as ONEBOT_V11Adapter
from nonebot.log import default_filter, default_format, logger_id
from src.storage import get_db

# 移除 NoneBot 默认的日志处理器
logger.remove(logger_id)

# 添加新的日志处理器
logger.add(
    sys.stdout,
    level=0,
    diagnose=True,
    format="<g>{time:MM-DD HH:mm:ss}</g> [<lvl>{level}</lvl>] <c><u>{name}</u></c> | {message}",
    filter=default_filter,
)

# 修改日志轮转配置
logger.add(
    "log/info.log",
    level="INFO",
    format=default_format,
    rotation="00:00",
    retention="30 days",
)
logger.add(
    "log/debug.log",
    level="DEBUG",
    format=default_format,
    rotation="00:00",
    retention="7 days",
)
logger.add(
    "log/error.log",
    level="ERROR",
    format=default_format,
    rotation="00:00",
    retention="30 days",
)

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(ONEBOT_V11Adapter)

nonebot.load_from_toml("pyproject.toml")


@driver.on_shutdown
async def _close_storage():
    await get_db().close()


if __name__ == "__main__":
    nonebot.run()
