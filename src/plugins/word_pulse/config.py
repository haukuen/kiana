# src/plugins/word_pulse/config.py
from pydantic import BaseModel, Field

from src.plugins.group_permission import GroupPermissionMixin


class Config(GroupPermissionMixin, BaseModel):
    word_pulse_plugin_enabled: bool = Field(default=False, description="是否启用词频统计插件")
    word_pulse_base_url: str = Field(default="", description="OpenAI 兼容接口的 Base URL")
    word_pulse_api_key: str = Field(default="", description="OpenAI 兼容接口的 API Key")
    word_pulse_model: str = Field(default="", description="OpenAI 兼容接口的模型名称")
    word_pulse_temperature: float = Field(default=0.0, ge=0, le=2, description="分类温度")
    word_pulse_summary_temperature: float = Field(default=0.3, ge=0, le=2, description="汇总温度")
    word_pulse_timeout_seconds: float = Field(default=60.0, gt=0, le=300, description="AI请求超时")
    word_pulse_cooldown_seconds: int = Field(default=30, ge=0, le=3600, description="查询冷却秒数")
    word_pulse_cache_ttl_minutes: int = Field(default=5, ge=1, le=1440, description="结果缓存分钟")
    word_pulse_bucket_retention_days: int = Field(default=30, ge=1, le=365, description="桶保留天数")
    word_pulse_max_messages_per_bucket: int = Field(default=1000, ge=100, le=10000, description="单桶消息上限")
    word_pulse_max_sample_per_cluster: int = Field(default=3, ge=1, le=10, description="每cluster抽样条数")
    word_pulse_max_examples_in_summary: int = Field(default=5, ge=1, le=20, description="总结中原文条数")
    word_pulse_max_window_days: int = Field(default=31, ge=1, le=365, description="查询窗口上限天")
    word_pulse_today_bucket_fresh_seconds: int = Field(default=300, ge=60, le=3600, description="今日桶 freshness 窗口秒数")
