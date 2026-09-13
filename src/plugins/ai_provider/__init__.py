"""前置插件：向其他插件提供多供应商的 LLM 调用能力。

本插件**没有任何功能**：不注册 matcher、定时任务，也不建表，只提供模块级 API。
传输、序列化与基础异常分类全部交给官方 SDK（``openai`` / ``anthropic``），本插件
只保留供应商路由、业务策略和必要的兼容处理。消费者在模块顶部声明依赖后调用：

```python
from nonebot import require

_ai = require("src.plugins.ai_provider")

result = await _ai.complete(
    caller="refine",                      # 命中 ai_plugin_models 里的模型
    system="你是一个总结助手",
    messages=[{"role": "user", "content": payload}],
)
print(result.text)

# 需要严格结构化输出时传 Pydantic 模型（与 json_object 互斥），
# SDK 官方 parse 接口负责 schema 生成、发送与解析，直接拿到模型实例：
result = await _ai.complete(
    caller="a_share_sentiment",
    system="你是情绪分析助手",
    messages=[{"role": "user", "content": payload}],
    response_model=MyResult,
)
print(result.parsed.summary)
```

想指定模型就写 ``provider_id/model_name``（``model="anthropic/claude-opus-5"``）；
provider 档案（协议、端点、密钥、模型清单）在插件配置的 ``ai_providers`` 里。
"""

from nonebot.plugin import PluginMetadata

from .client import build_http_client, extract_json_text, map_sdk_error
from .config import (
    PROTOCOLS,
    Config,
    ProtocolChoice,
    ProtocolName,
    ProviderConfig,
    Target,
    config,
    load_config,
    provider_sends_temperature,
    resolve,
    usable_urls,
)
from .errors import (
    DEFAULT_AI_ERRORS,
    AIAuthError,
    AIConfigError,
    AIError,
    AIErrorTypes,
    AIResponseError,
    AIServiceError,
    AITimeoutError,
)
from .service import auto_cached_protocol, complete, reset_auto_cache, resolve_protocol
from .types import ChatRequest, ChatResult

__plugin_meta__ = PluginMetadata(
    name="ai_provider",
    description="多供应商 LLM 前置插件：统一三种协议、端点与模型路由，自身不含功能",
    usage="本插件没有用户命令；由 refine、a_share_sentiment 等插件声明依赖后使用",
    config=Config,
)

__all__ = [
    "DEFAULT_AI_ERRORS",
    "PROTOCOLS",
    "AIAuthError",
    "AIConfigError",
    "AIError",
    "AIErrorTypes",
    "AIResponseError",
    "AIServiceError",
    "AITimeoutError",
    "ChatRequest",
    "ChatResult",
    "Config",
    "ProtocolChoice",
    "ProtocolName",
    "ProviderConfig",
    "Target",
    "auto_cached_protocol",
    "build_http_client",
    "complete",
    "config",
    "extract_json_text",
    "load_config",
    "map_sdk_error",
    "provider_sends_temperature",
    "reset_auto_cache",
    "resolve",
    "resolve_protocol",
    "usable_urls",
]
