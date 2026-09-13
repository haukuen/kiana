"""对外入口：解析目标 → 选协议 → 发请求 → 归一化结果。

``protocol="auto"`` 的探测借鉴 sub2api 的「自适应 API 协议」：同一个端点按候选顺序
试一次，第一个成功的协议写进进程内缓存。缓存键是「协议端点 + 模型 + 鉴权档案」的
稳定指纹而不是 provider id——热改配置（换端点/换模型/换密钥）后旧协议不该被沿用。

并发首次探测用**每个指纹一个共享任务**合并：同批调用共享成功或失败结果，而不是
拿互斥锁把失败串行重试一遍。探测任务以共享层默认异常运行，每个等待者拿到结果或
异常后各自映射成自己注入的异常类型；单个等待者的超时/取消不影响共享任务。探测
是有代价的（多一次最小请求），所以默认仍是显式声明协议。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable
from typing import Any, overload

from nonebot import logger
from pydantic import BaseModel

from .client import execute, probe_protocol
from .config import (
    PROTOCOLS,
    ProtocolName,
    ProviderConfig,
    Target,
    provider_sends_temperature,
    resolve,
    usable_urls,
)
from .errors import (
    DEFAULT_AI_ERRORS,
    AIAuthError,
    AIConfigError,
    AIErrorTypes,
    AIResponseError,
    AIServiceError,
    AITimeoutError,
)
from .types import ChatRequest, ChatResult

#: 指纹 → 探测出来的协议。只活在进程内；配置变化会算出不同指纹自然失效，
#: 全量清理用 reset_auto_cache()。失败不缓存，后续调用可重试。
_auto_cache: dict[str, ProtocolName] = {}

#: 指纹 → 进行中的探测任务。同指纹的并发调用共享同一个任务的成功或失败；
#: 任务结束后由回调清出登记并领取异常，避免后台未处理异常。
_probes: dict[str, asyncio.Task[ProtocolName]] = {}

#: 各协议探测请求的输出预算。Chat 探测不发送预算字段（省一个兼容性变量）；
#: Anthropic 官方示例确认 max_tokens=1 合法；Responses 有协议下限 16。
_PROBE_BUDGETS: dict[ProtocolName, int | None] = {
    "openai_chat": None,
    "anthropic_messages": 1,
    "openai_responses": 16,
}
_PROBE_TIMEOUT_SECONDS = 15.0

#: 业务输入范围：本项目只需要非流式文本消息，system 走独立参数。
_ALLOWED_ROLES = frozenset({"user", "assistant"})


def reset_auto_cache() -> None:
    """清空自适应协议的探测缓存并取消进行中的探测任务（测试/热重置用）。

    被取消的旧任务不会再把结果回写缓存（写缓存前会检查取消状态）。
    """
    _auto_cache.clear()
    for task in _probes.values():
        task.cancel()
    _probes.clear()


def auto_cached_protocol(provider: ProviderConfig, model: str = "") -> ProtocolName | None:
    """查询 auto 探测缓存；未探测过或非 auto 协议返回 None。"""
    return _auto_cache.get(_auto_fingerprint(provider, model))


def _auto_fingerprint(provider: ProviderConfig, model: str) -> str:
    """auto 探测缓存的稳定指纹：候选协议端点 + 模型 + 鉴权档案（不透明哈希）。

    指纹只影响缓存命中，日志与异常里都不会出现原文。
    """
    endpoints = sorted(usable_urls(provider).items())
    material = json.dumps(
        {"endpoints": endpoints, "model": model, "api_key": provider.api_key},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _probe_done(fingerprint: str) -> Callable[[asyncio.Task[ProtocolName]], None]:
    def callback(task: asyncio.Task[ProtocolName]) -> None:
        if _probes.get(fingerprint) is task:
            _probes.pop(fingerprint)
        if not task.cancelled():
            # 领取异常：等待者可能已按自己的超时退出，没人 await 过这个任务。
            task.exception()

    return callback


def _remap_probe_error(error: Exception, errors: AIErrorTypes) -> Exception:
    """探测任务以共享层默认异常运行；等待者按自己注入的类型重新抛出。"""
    if isinstance(error, AITimeoutError):
        return errors.timeout(str(error))
    if isinstance(error, AIAuthError):
        return errors.auth(str(error))
    if isinstance(error, AIServiceError):
        return errors.service(str(error))
    if isinstance(error, AIResponseError):
        return errors.response(str(error))
    if isinstance(error, AIConfigError):
        return errors.config(str(error))
    return error


async def _probe_all(provider: ProviderConfig, model: str, fingerprint: str) -> ProtocolName:
    """按候选顺序探测；全部失败抛第一个错误（共享层默认异常类型）。"""
    urls = usable_urls(provider)
    candidates: list[ProtocolName] = [item for item in PROTOCOLS if item in urls]
    if not candidates:
        raise AIConfigError(f"provider「{provider.id}」没有配置任何可用端点")

    first_error: Exception | None = None
    for protocol in candidates:
        try:
            await probe_protocol(
                protocol=protocol,
                provider=provider,
                model=model,
                budget=_PROBE_BUDGETS[protocol],
                timeout_seconds=_PROBE_TIMEOUT_SECONDS,
            )
        except Exception as error:  # 探测就是逐个试，失败换下一个
            if first_error is None:
                first_error = error
            continue
        if (current := asyncio.current_task()) is not None and current.cancelling():
            # reset_auto_cache 已请求取消：不再把旧任务的探测结果回写缓存。
            raise asyncio.CancelledError
        _auto_cache[fingerprint] = protocol
        logger.info(f"[AI] provider「{provider.id}」自适应协议探测结果：{protocol}")
        return protocol

    if first_error is not None:
        raise first_error
    raise AIConfigError(f"provider「{provider.id}」没有任何可用协议")


def _validate_messages(
    messages: Iterable[dict[str, str]], errors: AIErrorTypes
) -> tuple[dict[str, str], ...]:
    """校验并归一化消息列表；本项目只收 role + 字符串 content 的文本消息。"""
    normalized: list[dict[str, str]] = []
    for index, message in enumerate(messages, start=1):
        if not isinstance(message, dict):
            raise errors.config(f"第 {index} 条消息必须是 dict")
        role = message.get("role")
        content = message.get("content")
        if role not in _ALLOWED_ROLES:
            raise errors.config(
                f"第 {index} 条消息的 role 必须是 user/assistant（system 请用 system 参数），得到 {role!r}"
            )
        if not isinstance(content, str):
            raise errors.config(
                f"第 {index} 条消息的 content 必须是字符串，得到 {type(content).__name__}"
            )
        if not content.strip():
            raise errors.config(f"第 {index} 条消息的 content 不能为空")
        normalized.append({"role": role, "content": content})
    if not normalized:
        raise errors.config("messages 不能为空")
    return tuple(normalized)


def _validate_response_model(json_object: bool, response_model: Any, errors: AIErrorTypes) -> None:
    """校验严格结构化输出参数：必须是 Pydantic 模型类，且与 json_object 互斥。"""
    if response_model is None:
        return
    if json_object:
        raise errors.config(
            "json_object 与 response_model 互斥：response_model 走官方严格结构化输出"
        )
    if not (isinstance(response_model, type) and issubclass(response_model, BaseModel)):
        raise errors.config("response_model 必须是 Pydantic BaseModel 的子类")


def _validate_temperature(temperature: float | None, errors: AIErrorTypes) -> None:
    if temperature is None:
        return
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        raise errors.config("temperature 必须是数字")
    if not 0 <= temperature <= 2:
        raise errors.config("temperature 必须在 0 到 2 之间")


@overload
async def complete(
    *,
    caller: str,
    system: str = "",
    messages: Iterable[dict[str, str]] = (),
    model: str | None = None,
    json_object: bool = False,
    response_model: None = None,
    temperature: float | None = None,
    timeout_seconds: float = 60.0,
    errors: AIErrorTypes = ...,
) -> ChatResult[None]: ...


@overload
async def complete[TModel: BaseModel](
    *,
    caller: str,
    system: str = "",
    messages: Iterable[dict[str, str]] = (),
    model: str | None = None,
    json_object: bool = False,
    response_model: type[TModel],
    temperature: float | None = None,
    timeout_seconds: float = 60.0,
    errors: AIErrorTypes = ...,
) -> ChatResult[TModel]: ...


async def complete(
    *,
    caller: str,
    system: str = "",
    messages: Iterable[dict[str, str]] = (),
    model: str | None = None,
    json_object: bool = False,
    response_model: type[BaseModel] | None = None,
    temperature: float | None = None,
    timeout_seconds: float = 60.0,
    errors: AIErrorTypes = DEFAULT_AI_ERRORS,
) -> ChatResult[Any]:
    """按 ``caller``（或显式 ``model``）解析目标并完成一次调用。

    消费者只需要给 ``system`` 与 ``messages``，不需要知道对面是哪种协议。
    ``json_object=True`` 走各协议 JSON mode（Anthropic 用提示词兜底）；
    ``response_model`` 传 Pydantic 模型走官方严格结构化输出，结果 ``parsed``
    直接是该模型实例，两者互斥。输入/参数校验失败抛调用方注入的
    ``errors.config`` 类型。
    """
    if not isinstance(system, str):
        raise errors.config("system 必须是字符串")
    normalized_messages = _validate_messages(messages, errors)
    _validate_response_model(json_object, response_model, errors)
    _validate_temperature(temperature, errors)

    target = resolve(caller, model)
    if not target.complete or target.provider is None:
        raise errors.config(target.missing[0])

    provider = target.provider
    protocol = await resolve_protocol(
        provider,
        model=target.model,
        timeout_seconds=timeout_seconds,
        errors=errors,
    )
    if (
        temperature is not None
        and provider_sends_temperature(provider, protocol)
        and protocol == "anthropic_messages"
        and not 0 <= temperature <= 1
    ):
        raise errors.config("anthropic_messages 协议下 temperature 必须在 0 到 1 之间")

    request = ChatRequest(
        system=system,
        messages=normalized_messages,
        json_object=json_object,
        response_model=response_model,
        temperature=temperature,
        max_tokens=provider.max_tokens,
    )
    outcome = await execute(
        protocol=protocol,
        provider=provider,
        model=target.model,
        request=request,
        timeout_seconds=timeout_seconds,
        errors=errors,
    )
    return ChatResult(
        text=outcome.text,
        model=target.model,
        provider_id=provider.id,
        protocol=protocol,
        usage=outcome.usage,
        parsed=outcome.parsed,
    )


async def resolve_protocol(
    provider: ProviderConfig,
    *,
    model: str,
    timeout_seconds: float,
    errors: AIErrorTypes,
) -> ProtocolName:
    """得到这次调用真正要用的协议；``auto`` 时共享探测一次并缓存。

    探测只确认端点接受对应协议（SDK 响应类型形状有效即可），不要求正文；
    每个候选受探测超时上限约束，而等待整轮的时间用调用方预算——前一个候选
    耗时 9 秒失败、后一个 9 秒成功时，配置 30 秒超时的首次调用可以继续。

    同指纹的并发调用共享同一个探测任务（含失败结果）；每个等待者按自己的超时
    预算等待，取消或超时都不影响共享任务，异常映射成调用方注入的类型。
    """
    if provider.protocol != "auto":
        return provider.protocol

    fingerprint = _auto_fingerprint(provider, model)
    if (cached := _auto_cache.get(fingerprint)) is not None:
        return cached

    task = _probes.get(fingerprint)
    if task is None:
        task = asyncio.create_task(_probe_all(provider, model, fingerprint))
        task.add_done_callback(_probe_done(fingerprint))
        _probes[fingerprint] = task

    try:
        # shield：等待者超时/被取消时只撤掉等待本身，共享任务继续跑给其他等待者。
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
    except TimeoutError as error:  # asyncio.TimeoutError 在 3.11+ 就是内建 TimeoutError
        raise errors.timeout(f"AI 协议探测等待超时（{timeout_seconds:g}s）") from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise _remap_probe_error(error, errors) from error


__all__ = [
    "Target",
    "auto_cached_protocol",
    "complete",
    "reset_auto_cache",
    "resolve",
    "resolve_protocol",
]
