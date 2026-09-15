"""随机礼物插件配置。"""

from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    gift_plugin_enabled: bool = Field(default=True, description="是否启用随机礼物插件")

    gift_group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    gift_group_whitelist: list[str] = Field(
        default=[], description="白名单群组(仅在 whitelist 模式生效)"
    )
    gift_group_blacklist: list[str] = Field(
        default=[], description="黑名单群组(仅在 blacklist 模式生效)"
    )

    gift_pool: list[str] = Field(
        default=[],
        description="可送的礼物 key 列表，留空表示全部可选（champagne/hammer/space/party/camping/dragon/supercar/helicopter）",
    )
    gift_cooldown_time: int = Field(default=60, description="同一用户送礼冷却时间（秒）")
