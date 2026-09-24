from nonebot import get_driver
from nonebot.plugin import PluginMetadata

from .config import Config
from .db import ensure_schema, fetch_collection_members

__plugin_meta__ = PluginMetadata(
    name="un_nickname",
    description="存储和管理群成员昵称和集合",
    usage="@某人 昵称 xxx\n发送'at昵称'即可触发@\n删除昵称 @某人\n清空昵称 @某人\n集合 xxx @人 创建/添加成员\n集合 xxx 查看成员\n集合列表\n移除集合 xxx @人\n删除集合 xxx",
    config=Config,
)

driver = get_driver()


@driver.on_startup
async def init_nickname_schema():
    """在 NoneBot 启动时初始化数据库 schema"""
    ensure_schema()


# 导入 handlers 触发 matcher 注册
from . import handlers  # noqa: E402
