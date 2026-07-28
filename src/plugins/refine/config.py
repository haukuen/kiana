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
        json_schema_extra={"secret": True},
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

    # ── 缓存新鲜度与冷却 ──────────────────────────────
    refine_result_fresh_seconds: int = Field(
        default=86400,
        ge=60,
        le=604800,
        description=(
            "炼化结果新鲜期（秒）。在该时间内用 `炼化 <标签>` 命令查询，"
            "直接返回缓存，不调 AI；超过该时间才触发重炼。默认 86400（24h）。"
        ),
    )
    refine_query_cooldown_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description=(
            "同一订阅两次重炼之间的冷却时间（秒）。冷却内的 `炼化` 命令直接返回旧缓存，"
            "防止高频查询撑爆 AI 账单。`强制炼化` 命令忽略冷却。默认 60。"
        ),
    )

    # ── 采集窗口与 Prompt 预算 ─────────────────────────
    refine_lookback_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="每次提炼回看最近多少小时的发言（1-168，即最多 7 天）",
    )
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
