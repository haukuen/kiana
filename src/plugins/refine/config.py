from typing import Literal

from pydantic import BaseModel, Field


class Config(BaseModel):
    refine_plugin_enabled: bool = Field(
        default=False,
        description="是否启用炼化插件",
    )

    refine_group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    refine_group_whitelist: list[str] = Field(
        default=[],
        description="白名单群组(仅在 whitelist 模式生效)",
    )
    refine_group_blacklist: list[str] = Field(
        default=[],
        description="黑名单群组(仅在 blacklist 模式生效)",
    )

    # ── AI 接口（OpenAI 兼容） ─────────────────────────
    refine_ai_base_url: str = Field(
        default="",
        description="OpenAI 兼容接口的 Base URL（如 https://api.openai.com/v1）",
    )
    refine_ai_api_key: str = Field(
        default="",
        description="OpenAI 兼容接口的 API Key",
    )
    refine_ai_model: str = Field(
        default="",
        description="OpenAI 兼容接口的模型名称（如 gpt-4o-mini）",
    )
    refine_ai_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=120,
        description="AI 请求超时时间（秒）",
    )
    refine_ai_temperature: float = Field(
        default=0.3,
        ge=0,
        le=2,
        description="AI 采样温度",
    )

    # ── 结果保留 ──────────────────────────────────────
    refine_result_retention_days: int = Field(
        default=3,
        ge=1,
        le=90,
        description="炼化结果保留天数，超期自动清理",
    )

    # ── 调度 ──────────────────────────────────────────
    refine_schedule_cron_hour: int = Field(
        default=8,
        ge=0,
        le=23,
        description="每日定时提炼的小时（0-23）。默认每天 08:00 跑一次。",
    )
    refine_schedule_cron_minute: int = Field(
        default=0,
        ge=0,
        le=59,
        description="每日定时提炼的分钟（0-59）",
    )
    refine_lookback_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="每次提炼回看最近多少小时的发言（1-168，即最多 7 天）",
    )

    # ── Prompt 预算 ────────────────────────────────────
    refine_max_messages_per_target: int = Field(
        default=200,
        ge=1,
        le=2000,
        description="单次提炼每个目标最多采样的消息条数",
    )
    refine_max_prompt_chars: int = Field(
        default=12000,
        ge=1000,
        le=50000,
        description="单次提炼 prompt 中拼接原文的字符预算",
    )
    refine_min_messages_to_refine: int = Field(
        default=5,
        ge=1,
        le=500,
        description="目标在回看窗口内少于该消息数时跳过提炼（避免无意义调用 AI）",
    )
