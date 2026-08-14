from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    qqq_plugin_enabled: bool = Field(default=True, description="是否启用 QQQ 行情查询插件")
    qqq_enable_price_query: bool = Field(default=True, description="是否启用 QQQ 价格查询功能")

    qqq_group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    qqq_group_whitelist: list[str] = Field(
        default=[], description="白名单群组(仅在 whitelist 模式生效)"
    )
    qqq_group_blacklist: list[str] = Field(
        default=[], description="黑名单群组(仅在 blacklist 模式生效)"
    )

    qqq_cooldown_time: int = Field(default=10, description="群聊查询冷却时间（秒）")
