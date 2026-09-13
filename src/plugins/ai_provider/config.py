"""供应商档案、模型路由与端点解析。

形状参考 LiteLLM 的 ``model_name`` + ``litellm_params``：调用方写
``provider_id/model_name``，provider 档案里放协议、端点、密钥与模型清单。
provider 一律**显式声明**（``protocol=auto`` 只是让本进程探测一次协议，不会猜
provider 本身）。

本模块必须**自包含**且**导入无副作用**，才能被 ``web_config`` 的扫描器按文件路径单独
加载（见 ``web_config/scanner.py:_load_config_module``）：它用一个不在 ``sys.modules``
里的合成模块名 exec 本文件，因此

* 模块级相对 import 会直接 ModuleNotFoundError；
* 模块级 ``get_plugin_config(Config)`` 会因为嵌套模型 ``ProviderConfig`` 在该命名空间
  里解析不出来而抛 PydanticUserError（纯标量字段的 Config 侥幸不受影响）。

两种情况都会让扫描器静默跳过本插件，配置项在管理界面里消失，所以配置实例改成
惰性获取（``load_config()`` / 模块级 ``__getattr__``）。

另外本文件**不能加 ``from __future__ import annotations``**：那会把注解变成字符串，
而 ``dataclasses`` 解析 ``Target`` 的字符串注解时要查 ``sys.modules[cls.__module__]``，
在我们这种不在 sys.modules 里的合成模块中直接 AttributeError。
"""

from dataclasses import dataclass
from typing import Any, Literal

from nonebot import get_plugin_config
from pydantic import BaseModel, ConfigDict, Field, model_validator

ProtocolName = Literal["openai_chat", "anthropic_messages", "openai_responses"]
ProtocolChoice = Literal["openai_chat", "anthropic_messages", "openai_responses", "auto"]

#: auto 探测的候选顺序：先试最通用的 Chat Completions，再试原生协议。
PROTOCOLS: tuple[ProtocolName, ...] = (
    "openai_chat",
    "anthropic_messages",
    "openai_responses",
)


class ProviderConfig(BaseModel):
    """一个供应商档案。"""

    id: str = Field(
        description="引用名，模型串里的前半段（如 anthropic、relay、deepseek）",
    )
    protocol: ProtocolChoice = Field(
        default="openai_chat",
        description=(
            "接口协议：openai_chat / anthropic_messages / openai_responses；"
            "auto 表示按候选顺序探测一次并缓存结果"
        ),
    )
    base_url: str = Field(
        default="",
        description="端点前缀，不含 /chat/completions 这类路径（如 https://api.openai.com/v1）",
    )
    anthropic_base_url: str = Field(
        default="",
        description="仅 protocol=auto 时的 Anthropic 端点，留空即用 base_url",
    )
    responses_base_url: str = Field(
        default="",
        description="仅 protocol=auto 时的 Responses 端点，留空即用 base_url",
    )
    api_key: str = Field(
        default="",
        description="接口密钥",
        json_schema_extra={"secret": True},
    )
    models: list[str] = Field(
        default=[],
        description="该 provider 可用的模型名；留空表示不校验",
    )
    max_tokens: int = Field(
        default=4096,
        ge=16,
        description=(
            "输出上限，最小 16（openai_responses 的协议下限）。anthropic_messages 必填字段；"
            "openai_responses 用作 max_output_tokens；openai_chat 按 chat_token_field 发送"
        ),
    )
    chat_token_field: Literal["max_completion_tokens", "max_tokens"] = Field(
        default="max_completion_tokens",
        description=(
            "openai_chat 的输出预算字段：较新的 OpenAI 模型（o 系/gpt-5 系）只认"
            " max_completion_tokens；老中转只支持旧的 max_tokens 时改选它。其他协议忽略"
        ),
    )
    temperature_policy: Literal["auto", "send", "drop"] = Field(
        default="auto",
        description=(
            "temperature 发送策略：auto=对 OpenAI 系协议都省略（推理模型收到 temperature 会直接 400），"
            "send=总是发送（仅 OpenAI 系协议生效）、drop=从不发送。anthropic_messages 一律不发送："
            "官方 SDK 已移除 temperature 参数"
        ),
    )

    def base_url_for(self, protocol: ProtocolName) -> str:
        """取该协议要用的端点。"""
        override = {
            "anthropic_messages": self.anthropic_base_url,
            "openai_responses": self.responses_base_url,
        }.get(protocol, "")
        return (override or self.base_url).strip()


class Config(BaseModel):
    # 运行期赋值（测试用 monkeypatch / setattr、以及热改配置）也要走校验，
    # 否则 ai_providers 被塞进裸 dict 时，resolve() 会在 .id 上炸掉。
    model_config = ConfigDict(validate_assignment=True)

    ai_providers: list[ProviderConfig] = Field(
        default=[],
        description="供应商档案列表，每项形如 {id: relay, protocol: openai_chat, base_url: ..., api_key: ...}",
    )
    ai_default_model: str = Field(
        default="",
        description="未按插件指定时使用的模型，格式 provider_id/model_name",
    )
    ai_plugin_models: dict[str, str] = Field(
        default={},
        description='按插件分配模型，如 {"refine": "anthropic/claude-opus-5"}',
    )

    @model_validator(mode="after")
    def validate_provider_ids(self) -> "Config":
        """provider id 是路由与 auto 缓存键，必须非空且唯一。"""
        provider_ids = [provider.id.strip() for provider in self.ai_providers]
        if any(not provider_id for provider_id in provider_ids):
            raise ValueError("ai_providers 中的 id 不能为空")
        duplicates = sorted(
            provider_id for provider_id in set(provider_ids) if provider_ids.count(provider_id) > 1
        )
        if duplicates:
            raise ValueError(f"ai_providers 中存在重复 id：{'、'.join(duplicates)}")
        return self


_config: Config | None = None


def load_config() -> Config:
    """取插件配置实例（惰性，只实例化一次）。"""
    global _config  # noqa: PLW0603 - 进程内单例，供 __getattr__ 惰性暴露
    if _config is None:
        _config = get_plugin_config(Config)
    return _config


def __getattr__(name: str) -> Any:
    """让 ``from .config import config`` 也能惰性取到实例。"""
    if name == "config":
        return load_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True, slots=True)
class Target:
    """一次调用的目标：哪个 provider、哪个模型、以及配置是否齐全。"""

    provider: ProviderConfig | None
    model: str
    missing: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing


def resolve(caller: str, model: str | None = None) -> Target:
    """把「调用方 + 可选显式模型」解析成调用目标。

    优先级：显式 ``model`` > ``ai_plugin_models[caller]`` > ``ai_default_model``。
    ``missing`` 里是给用户看的整句提示（不是字段名），调用方直接展示第一条即可。
    """
    settings = load_config()
    if not settings.ai_providers:
        return Target(provider=None, model="", missing=("ai_providers 未配置",))

    spec = (
        model or settings.ai_plugin_models.get(caller) or settings.ai_default_model or ""
    ).strip()
    if not spec:
        return Target(
            provider=None,
            model="",
            missing=(f"未指定模型（ai_plugin_models[{caller}] 与 ai_default_model 都为空）",),
        )

    provider_id, separator, model_name = spec.partition("/")
    if not separator or not model_name:
        return Target(
            provider=None,
            model="",
            missing=(f"模型「{spec}」要写成 provider_id/model_name",),
        )

    provider = next((item for item in settings.ai_providers if item.id == provider_id), None)
    if provider is None:
        known = "、".join(item.id for item in settings.ai_providers)
        return Target(
            provider=None,
            model=model_name,
            missing=(f"provider「{provider_id}」不在 ai_providers 里（现有：{known}）",),
        )

    problems: list[str] = []
    if not usable_urls(provider):
        problems.append(f"provider「{provider_id}」缺少 base_url")
    if not provider.api_key.strip():
        problems.append(f"provider「{provider_id}」缺少 api_key")
    if provider.models and model_name not in provider.models:
        problems.append(f"provider「{provider_id}」的 models 清单里没有「{model_name}」")
    return Target(provider=provider, model=model_name, missing=tuple(problems))


def usable_urls(provider: ProviderConfig) -> dict[ProtocolName, str]:
    """该档案在当前协议设置下真正可用的端点。

    ``protocol=auto`` 时列出三种协议里配了端点的那些（没配的跳过，探测时自然忽略）。
    """
    protocols = PROTOCOLS if provider.protocol == "auto" else (provider.protocol,)
    return {item: url for item in protocols if (url := provider.base_url_for(item))}


def provider_sends_temperature(provider: ProviderConfig, protocol: ProtocolName) -> bool:
    """该 provider 在这个协议下是否应该带 temperature。

    ``auto`` 对三种协议都保守省略：较新的推理模型（Claude Opus 5 / Sonnet 5、
    OpenAI o 系/gpt-5 系）收到 temperature 会直接 400 或忽略；确要发送就显式
    ``temperature_policy="send"``（Anthropic 侧随后校验 0-1）。
    """
    del protocol  # 策略当前与协议无关，保留参数以稳定调用形状
    return provider.temperature_policy == "send"


__all__ = [
    "PROTOCOLS",
    "Config",
    "ProtocolChoice",
    "ProtocolName",
    "ProviderConfig",
    "Target",
    "load_config",
    "provider_sends_temperature",
    "resolve",
    "usable_urls",
]
