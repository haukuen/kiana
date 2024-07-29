import sys
import nonebot
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot import logger
from nonebot.log import logger_id, default_format, default_filter

# 移除 NoneBot 默认的日志处理器
logger.remove(logger_id)

# 添加新的日志处理器
logger.add(
    sys.stdout,
    level=0,
    diagnose=True,
    format="<g>{time:MM-DD HH:mm:ss}</g> [<lvl>{level}</lvl>] <c><u>{name}</u></c> | {message}",
    filter=default_filter
)

logger.add("log/info.log", level="INFO", format=default_format, rotation="1 day")
logger.add("log/debug.log", level="DEBUG", format=default_format, rotation="1 week")
logger.add("log/error.log", level="ERROR", format=default_format, rotation="1 week")

nonebot.init()

driver = nonebot.get_driver()
driver.register_adapter(QQAdapter)

# nonebot.load_builtin_plugins('echo')


nonebot.load_from_toml("pyproject.toml")

if __name__ == "__main__":
    nonebot.run()
