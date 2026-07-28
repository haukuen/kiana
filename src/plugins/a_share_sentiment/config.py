from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    a_share_sentiment_plugin_enabled: bool = Field(
        default=False,
        description="是否启用 A 股情绪指数插件",
    )
    a_share_sentiment_group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    a_share_sentiment_group_whitelist: list[str] = Field(
        default=[],
        description="白名单群组(仅在 whitelist 模式生效)",
    )
    a_share_sentiment_group_blacklist: list[str] = Field(
        default=[],
        description="黑名单群组(仅在 blacklist 模式生效)",
    )

    a_share_sentiment_base_url: str = Field(
        default="",
        description="OpenAI 兼容接口的 Base URL",
    )
    a_share_sentiment_api_key: str = Field(
        default="",
        description="OpenAI 兼容接口的 API Key",
        json_schema_extra={"secret": True},
    )
    a_share_sentiment_model: str = Field(
        default="",
        description="OpenAI 兼容接口的模型名称",
    )
    a_share_sentiment_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        description="AI 请求超时时间（秒）",
    )
    a_share_sentiment_temperature: float = Field(
        default=0.2,
        ge=0,
        le=2,
        description="AI 采样温度",
    )
    a_share_sentiment_history_days: int = Field(
        default=5,
        ge=1,
        le=30,
        description="用于对比的历史自然日数量",
    )
    a_share_sentiment_min_messages: int = Field(
        default=20,
        ge=1,
        le=500,
        description="低于该文本消息数时标记低置信度",
    )
    a_share_sentiment_max_today_messages: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="今日样本最多保留的消息条数",
    )
    a_share_sentiment_max_history_messages_per_day: int = Field(
        default=40,
        ge=1,
        le=500,
        description="历史每天样本最多保留的消息条数",
    )
    a_share_sentiment_max_prompt_chars_today: int = Field(
        default=12000,
        ge=1000,
        le=50000,
        description="今日样本文本字符预算",
    )
    a_share_sentiment_max_prompt_chars_history_day: int = Field(
        default=4000,
        ge=500,
        le=20000,
        description="历史每天样本文本字符预算",
    )
    a_share_sentiment_cooldown_seconds: int = Field(
        default=300,
        ge=0,
        le=3600,
        description="同一群查询冷却时间（秒）",
    )
    a_share_sentiment_cache_ttl_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="同一群同一天结果缓存时间（分钟）",
    )
