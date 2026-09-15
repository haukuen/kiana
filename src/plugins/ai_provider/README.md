# ai_provider

无用户命令的 AI 前置插件。负责供应商配置、模型路由、协议选择、官方 SDK 请求和通用错误映射；业务插件负责提示词、业务模型、权限、缓存与落库。

## 配置

在 NoneBot 当前环境的配置文件中设置，例如：

```dotenv
AI_PROVIDERS=[{"id":"relay","protocol":"openai_chat","base_url":"https://api.example.com/v1","api_key":"替换为实际密钥","models":["summary-model"],"max_tokens":4096,"temperature_policy":"auto"}]
AI_DEFAULT_MODEL=relay/summary-model
AI_PLUGIN_MODELS={"refine":"relay/summary-model"}
```

模型选择优先级：调用时显式 `model` > `ai_plugin_models[caller]` > `ai_default_model`。模型串格式为 `provider_id/model_name`；`models` 留空表示不限制模型名。

- `protocol` 支持 `openai_chat`、`openai_responses`、`anthropic_messages` 和 `auto`，默认 `openai_chat`。
- `base_url` 是端点前缀，不包含 `/chat/completions` 等请求路径。
- `auto` 按 Chat、Messages、Responses 顺序探测，成功协议按端点、模型和凭据缓存。探测会产生额外请求；可用 `anthropic_base_url`、`responses_base_url` 指定相应端点。
- `max_tokens` 默认 4096，最小 16；Chat 默认使用 `max_completion_tokens`，旧接口可设 `chat_token_field="max_tokens"`。
- `temperature_policy` 默认 `auto`，省略 temperature；`send` 在 OpenAI 协议下发送调用方温度，`drop` 省略。Anthropic SDK 不发送 temperature；当前共享参数校验仍会拒绝 Anthropic `send` 策略下超出 0–1 的值。

## 调用与响应检查

```python
from nonebot import require

ai = require("src.plugins.ai_provider")
result = await ai.complete(
    caller="my_plugin",
    system="请概括下面的文字。",
    messages=[{"role": "user", "content": "待概括的文字"}],
)
summary = result.text
```

纯文本调用返回 `result.text`。`response_model` 接收 Pydantic 模型类，通过 SDK 严格结构化输出并返回 `result.parsed`；`json_object=True` 使用 JSON mode，Anthropic 则以提示词约束，业务字段仍由消费者校验。两种结构化参数互斥。

共享层拒绝空正文、已识别的拒答和截断状态。Chat 检查已知异常 `finish_reason`；Responses 检查失败、未完成和进行中状态；Anthropic 严格结构化输出要求 `end_turn`，普通文本容忍中转省略 `stop_reason`。这些检查不等于校验业务内容正确性。

`errors=AIErrorTypes(...)` 可把超时、鉴权、服务、响应和配置错误映射为消费者自己的异常类型。前置插件不会导入消费者的异常类或业务模型。

## 从插件独立配置迁移

接入本插件的消费者不再读取自身的 `base_url / api_key / model` 字段；需把旧值迁入供应商档案，并为对应 `caller` 配置模型路由。超时和业务温度参数仍由消费者传入。

迁移会同时采用共享层的输出预算、temperature 策略和响应检查。严格结构化输出还要求上游支持相应 SDK 的 schema 请求；不支持严格 schema 的业务可使用 JSON mode 并自行校验字段。
