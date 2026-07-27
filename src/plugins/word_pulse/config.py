# src/plugins/word_pulse/config.py
from pydantic import BaseModel, Field

from src.plugins.group_permission import GroupPermissionMixin

# ── 不可调常量（细枝末节，硬编码避免污染配置）──
CLASSIFY_TEMPERATURE: float = 0.0       # 字符集扩展 + 灰区分类
SUMMARY_TEMPERATURE: float = 0.3        # 最终热度总结
REQUEST_TIMEOUT_SECONDS: float = 60.0   # 单次 AI 请求超时
QUERY_COOLDOWN_SECONDS: int = 30        # 同群查询冷却
RESULT_CACHE_TTL_SECONDS: int = 300     # 结果缓存 TTL（5 分钟）
TODAY_BUCKET_FRESH_SECONDS: int = 300   # 今日桶 freshness 窗口
MAX_EXAMPLES_IN_SUMMARY: int = 5        # 总结里典型原文条数


class Config(GroupPermissionMixin, BaseModel):
    word_pulse_plugin_enabled: bool = Field(default=False, description="是否启用词频统计插件")
    word_pulse_base_url: str = Field(default="", description="OpenAI 兼容接口的 Base URL")
    word_pulse_api_key: str = Field(default="", description="OpenAI 兼容接口的 API Key")
    word_pulse_model: str = Field(default="", description="OpenAI 兼容接口的模型名称")
    word_pulse_bucket_retention_days: int = Field(default=30, ge=1, le=365, description="桶保留天数")
    word_pulse_max_messages_per_bucket: int = Field(default=1000, ge=100, le=10000, description="单桶消息上限")
    word_pulse_max_sample_per_cluster: int = Field(default=3, ge=1, le=10, description="每cluster抽样条数")
    word_pulse_max_window_days: int = Field(default=31, ge=1, le=365, description="查询窗口上限天")
