"""传输层：官方 SDK 客户端的创建、三种协议的调用与异常映射。

URL 拼接、认证头、请求序列化、响应反序列化和 HTTP 状态分类全部交给官方 SDK
（``openai`` / ``anthropic``）；这里只剩三类项目侧逻辑：

* 客户端装配：``build_http_client()``（``trust_env=False`` + 按次超时；测试把它
  monkeypatch 成 ``httpx2.MockTransport`` 注入点，让真实 SDK 完成编解码）；
  ``anthropic_base_url()`` 把项目「base_url 含 /v1」的约定折算成 SDK 约定。
* 各协议的参数分支：输出预算字段（Chat 的 max_completion_tokens/max_tokens、
  Responses 的 max_output_tokens、Messages 必填 max_tokens）、``store=False``、
  JSON mode 与严格输出的参数形状、temperature 发送策略（``omit`` 保证未启用的
  参数绝不以 null 形式出现在请求体里）。
* 业务闸门与异常映射：截断/拒绝/空正文不算成功（``errors.response``）；SDK 异常
  按 超时/鉴权/服务/响应 四类映射成 ``AIErrorTypes`` 注入的插件异常。

成功、异常和取消路径都通过 ``async with`` 关闭 SDK 客户端（SDK ``close()`` 会
一并关闭传入的自定义 http 客户端）。

协议差异备注（对应旧 hand-rolled 实现的行为）：

1. ``openai_chat``：正文在 ``choices[0].message.content``；输出预算字段按档案选
   ``max_completion_tokens``（老中转可配置回 ``max_tokens``）；默认可能留存内容，
   必须显式 ``store=False``。
2. ``openai_responses``：``instructions`` 放系统提示、``input`` 放对话；正文用 SDK
   的 ``output_text`` 便利属性；``status`` 不是 ``completed`` 一律失败。
3. ``anthropic_messages``：``system`` 是顶层参数；``max_tokens`` 必填；正文在
   ``content[]`` 的 text 块里；先看 ``stop_reason``，只有完整结束的文本才算成功。
   官方 SDK 已移除 ``temperature`` 参数，该协议一律不发送温度。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx2
import openai
import pydantic
from anthropic import AsyncAnthropic, omit as _anthropic_omit
from anthropic.types import Message, MessageParam
from openai import AsyncOpenAI, Omit, omit as _openai_omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
)
from openai.types.responses import Response, ResponseInputParam

from .config import ProtocolName, ProviderConfig, provider_sends_temperature
from .errors import DEFAULT_AI_ERRORS, AIErrorTypes, AIResponseError
from .types import ChatRequest

_JSON_INSTRUCTION = "只输出一个 JSON 对象，不要输出 Markdown 代码围栏或任何解释文字。"

#: Chat Completions 的非正常 finish_reason → 用户可读的失败原因。
_FINISH_REASON_ERRORS: dict[str, str] = {
    "length": "输出被 token 预算截断（finish_reason=length）",
    "content_filter": "输出被安全过滤拦截（finish_reason=content_filter）",
    "tool_calls": "模型请求工具调用而非正文（finish_reason=tool_calls）",
    "function_call": "模型请求函数调用而非正文（finish_reason=function_call）",
}

#: Messages 的非 end_turn stop_reason → 用户可读的失败原因。
_ANTHROPIC_STOP_REASON_ERRORS: dict[str, str] = {
    "max_tokens": "输出被 max_tokens 截断（stop_reason=max_tokens）",
    "model_context_window_exceeded": "上下文窗口超限（stop_reason=model_context_window_exceeded）",
    "refusal": "模型拒绝回答（stop_reason=refusal）",
    "tool_use": "模型请求工具调用而非正文（stop_reason=tool_use）",
    "pause_turn": "长轮次被服务端暂停（stop_reason=pause_turn）",
    "stop_sequence": "命中未配置的停止序列（stop_reason=stop_sequence）",
}


def _stop_reason_error(stop_reason: str | None) -> str:
    if stop_reason is None:
        return "响应缺少完整的 stop_reason"
    known = _ANTHROPIC_STOP_REASON_ERRORS.get(stop_reason)
    if known:
        return known
    return f"模型未正常结束（stop_reason={stop_reason}）"


def build_http_client(timeout_seconds: float) -> httpx2.AsyncClient:
    """按次创建 SDK 传输客户端；``close`` 由 SDK 客户端的 ``async with`` 代管。

    ``trust_env=False`` 是有意的：Bot 常运行在配了系统代理的宿主上，AI 请求不希望
    被环境变量里的代理劫持。测试 monkeypatch 本函数返回
    ``httpx2.AsyncClient(transport=httpx2.MockTransport(...), trust_env=False)``。
    """
    return httpx2.AsyncClient(timeout=timeout_seconds, trust_env=False)


def anthropic_base_url(base_url: str) -> str:
    """项目 base_url 约定含 ``/v1``（旧实现直接拼 ``/messages``）；AsyncAnthropic
    自己会追加 ``v1/messages``，去掉尾部 ``/v1`` 防止请求落到 ``/v1/v1/messages``。"""
    url = base_url.strip().rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _response_error_detail(error: Exception) -> str | None:
    """响应类失败的用户可读原因；不属于响应类错误返回 None。"""
    if isinstance(error, json.JSONDecodeError):
        return "AI 响应不是合法 JSON"
    if isinstance(error, (openai.APIResponseValidationError, anthropic.APIResponseValidationError)):
        return f"AI 响应无法解析：{error}"
    if isinstance(error, openai.LengthFinishReasonError):
        return _FINISH_REASON_ERRORS["length"]
    if isinstance(error, openai.ContentFilterFinishReasonError):
        return _FINISH_REASON_ERRORS["content_filter"]
    if isinstance(error, pydantic.ValidationError):
        # 三协议 parse 的 JSON 解析 / 字段校验 / validator 失败都从这里出。
        return "模型输出字段不完整或格式不正确"
    return None


def map_sdk_error(error: Exception, errors: AIErrorTypes) -> Exception:
    """把 SDK 异常映射成注入的插件异常；不是可识别的 SDK 异常则原样返回。"""
    detail = _response_error_detail(error)
    if detail is not None:
        return errors.response(detail)
    if isinstance(error, (openai.APITimeoutError, anthropic.APITimeoutError)):
        return errors.timeout("AI 请求超时")
    if isinstance(error, AttributeError):
        # 兼容中转省略标准字段时，SDK 宽松构造出的响应会缺属性可读。
        return errors.response("AI 响应缺少预期的字段")
    if isinstance(
        error,
        (
            openai.AuthenticationError,
            anthropic.AuthenticationError,
            openai.PermissionDeniedError,
            anthropic.PermissionDeniedError,
        ),
    ):
        return errors.auth(f"AI 鉴权失败（HTTP {error.status_code}）{_status_detail(error)}")
    if isinstance(error, (openai.APIStatusError, anthropic.APIStatusError)):
        return errors.service(f"AI 服务返回 HTTP {error.status_code}{_status_detail(error)}")
    if isinstance(error, (openai.APIConnectionError, anthropic.APIConnectionError)):
        # SDK 把底层传输异常包起来；诊断要保留原始异常类型名，便于定位 DNS/连接/代理问题。
        cause = error.__cause__
        detail = (
            f"{type(cause).__name__}: {cause}"
            if cause is not None
            else f"{type(error).__name__}: {error}"
        )
        return errors.service(f"AI 请求失败: {detail}")
    return error


def _status_detail(error: openai.APIStatusError | anthropic.APIStatusError) -> str:
    """SDK 已从错误体提取 ``error.message`` 并读取 request-id 响应头，直接归一。"""
    parts: list[str] = []
    message = (error.message or "").strip()
    if message:
        parts.append(message)
    if error.request_id:
        parts.append(f"request id: {error.request_id}")
    return f"：{'；'.join(parts)}" if parts else ""


def _ensure_json_instruction(system: str, messages: tuple[dict[str, str], ...]) -> str:
    """保证上下文里有 "JSON" 字样（OpenAI 系 json_object 的硬性要求），没有就追加。"""
    haystack = system + "".join(message.get("content", "") for message in messages)
    if "json" in haystack.lower():
        return system
    return f"{system}\n{_JSON_INSTRUCTION}" if system else _JSON_INSTRUCTION


def extract_json_text(content: str, *, errors: AIErrorTypes = DEFAULT_AI_ERRORS) -> str:
    """从模型输出里截出 JSON 对象，容忍 Markdown 代码围栏和前后解释文字。

    Anthropic 没有免 schema 的 json_object，结构化意图全靠提示词，这是该路径的
    容错解析兜底；走官方严格结构化输出（``response_model``）时用不到它。
    """
    stripped = content.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or start > end:
        raise errors.response("模型输出中没有 JSON 对象")
    return stripped[start : end + 1]


@dataclass(frozen=True, slots=True)
class _Outcome:
    """单次 SDK 调用的原始产物：正文、usage、以及结构化结果（如有）。"""

    text: str
    usage: dict[str, int] = field(default_factory=dict)
    parsed: Any = None


def _openai_messages(
    system: str, messages: tuple[dict[str, str], ...]
) -> list[ChatCompletionMessageParam]:
    result: list[ChatCompletionMessageParam] = []
    if system:
        system_message: ChatCompletionSystemMessageParam = {"role": "system", "content": system}
        result.append(system_message)
    for message in messages:
        if message["role"] == "user":
            result.append({"role": "user", "content": message["content"]})
        else:
            result.append({"role": "assistant", "content": message["content"]})
    return result


def _chat_budget(
    provider: ProviderConfig, request: ChatRequest
) -> tuple[int | Omit | None, int | Omit | None]:
    """按档案把预算放进 max_completion_tokens / max_tokens 之一，另一个 omit 掉。"""
    if provider.chat_token_field == "max_completion_tokens":  # noqa: S105 - 配置字面量，非口令
        return request.max_tokens, _openai_omit
    return _openai_omit, request.max_tokens


def _chat_usage(completion: ChatCompletion) -> dict[str, int]:
    usage = getattr(completion, "usage", None)
    if usage is None:
        return {}
    return {"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens}


def _chat_content_text(content: Any) -> str | None:
    """OpenAI 风格 content：字符串，或内容块数组里的 text 段（部分中转的兼容形状，
    SDK 类型只认 str，这里做最小文本块读取）。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ]
        if parts:
            return "".join(parts).strip()
    return None


def _chat_text(completion: ChatCompletion, errors: AIErrorTypes) -> str:
    """Chat Completions 的正文闸门：终止状态、拒绝、空正文一律不算成功。"""
    choices = completion.choices
    if not choices:
        raise errors.response("响应中缺少 choices")
    first = choices[0]
    reason = getattr(first, "finish_reason", None)
    if reason in _FINISH_REASON_ERRORS:
        raise errors.response(_FINISH_REASON_ERRORS[reason])
    message = getattr(first, "message", None)
    refusal = getattr(message, "refusal", None)
    if refusal and refusal.strip():
        raise errors.response(f"模型拒绝回答：{refusal.strip()}")
    content = _chat_content_text(getattr(message, "content", None))
    if content is None:
        raise errors.response("响应中缺少可解析的 content")
    if not content:
        raise errors.response("响应正文为空")
    return content


async def _call_chat(
    client: AsyncOpenAI,
    provider: ProviderConfig,
    model: str,
    request: ChatRequest,
    errors: AIErrorTypes,
) -> _Outcome:
    messages = _openai_messages(request.system, request.messages)
    temperature = request.temperature if request.temperature is not None else _openai_omit
    max_completion_tokens, max_tokens = _chat_budget(provider, request)
    if request.response_model is not None:
        completion = await client.chat.completions.parse(
            model=model,
            messages=messages,
            store=False,
            temperature=temperature,
            max_completion_tokens=max_completion_tokens,
            max_tokens=max_tokens,
            response_format=request.response_model,
        )
        text = _chat_text(completion, errors)
        parsed = completion.choices[0].message.parsed
        if parsed is None:  # 正文闸门已过，理论不可达；防御空解析
            raise errors.response("AI 未返回可解析的结构化结果")
        return _Outcome(text=text, usage=_chat_usage(completion), parsed=parsed)

    completion = await client.chat.completions.create(
        model=model,
        messages=messages,
        store=False,
        temperature=temperature,
        max_completion_tokens=max_completion_tokens,
        max_tokens=max_tokens,
        response_format={"type": "json_object"} if request.json_object else _openai_omit,
    )
    return _Outcome(text=_chat_text(completion, errors), usage=_chat_usage(completion))


def _responses_input(messages: tuple[dict[str, str], ...]) -> ResponseInputParam:
    input_items: ResponseInputParam = []
    for message in messages:
        if message["role"] == "user":
            input_items.append({"role": "user", "content": message["content"]})
        else:
            input_items.append({"role": "assistant", "content": message["content"]})
    return input_items


def _responses_output_text(response: Response) -> str:
    """SDK 的 output_text 便利属性；兼容中转缺 output 时按空正文处理。"""
    return str(getattr(response, "output_text", "") or "").strip()


def _responses_usage(response: Response) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


def _gate_response_status(response: Response, errors: AIErrorTypes) -> None:
    """Responses 的失败态闸门：failed/incomplete/未完成状态一律报错，不给正文兜底留后门。"""
    status = getattr(response, "status", None)
    if status == "failed":
        error = getattr(response, "error", None)
        detail = getattr(error, "message", None)
        raise errors.response(f"响应报告失败：{detail or 'status=failed'}")
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        known = {
            "max_output_tokens": "输出预算耗尽被截断（incomplete: max_output_tokens）",
            "content_filter": "输出被安全过滤拦截（incomplete: content_filter）",
        }
        if reason in known:
            raise errors.response(known[reason])
        raise errors.response(f"响应未完成（incomplete: {reason or '未知原因'}）")
    if status in {"queued", "in_progress"}:
        raise errors.response(f"响应尚未完成（status={status}）")


async def _call_responses(
    client: AsyncOpenAI, model: str, request: ChatRequest, errors: AIErrorTypes
) -> _Outcome:
    input_items = _responses_input(request.messages)
    instructions = request.system or _openai_omit
    budget = request.max_tokens if request.max_tokens is not None else _openai_omit
    temperature = request.temperature if request.temperature is not None else _openai_omit
    if request.response_model is not None:
        response = await client.responses.parse(
            model=model,
            input=input_items,
            instructions=instructions,
            store=False,
            max_output_tokens=budget,
            temperature=temperature,
            text_format=request.response_model,
        )
        _gate_response_status(response, errors)
        text = _responses_output_text(response)
        if not text:
            raise errors.response("响应中缺少可解析的 output")
        parsed = response.output_parsed
        if parsed is None:  # 正文闸门已过，理论不可达；防御空解析
            raise errors.response("AI 未返回可解析的结构化结果")
        return _Outcome(text=text, usage=_responses_usage(response), parsed=parsed)

    response = await client.responses.create(
        model=model,
        input=input_items,
        instructions=instructions,
        store=False,
        max_output_tokens=budget,
        temperature=temperature,
        text={"format": {"type": "json_object"}} if request.json_object else _openai_omit,
    )
    _gate_response_status(response, errors)
    text = _responses_output_text(response)
    if not text:
        raise errors.response("响应中缺少可解析的 output")
    return _Outcome(text=text, usage=_responses_usage(response))


def _anthropic_messages(messages: tuple[dict[str, str], ...]) -> list[MessageParam]:
    result: list[MessageParam] = []
    for message in messages:
        if message["role"] == "user":
            result.append({"role": "user", "content": message["content"]})
        else:
            result.append({"role": "assistant", "content": message["content"]})
    return result


def _anthropic_usage(message: Message) -> dict[str, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}


def _gate_anthropic_stop(message: Message, *, structured: bool, errors: AIErrorTypes) -> None:
    """严格结构化输出只接受完整 end_turn；普通文本容忍省略 stop_reason 的中转，
    但只要给了非 end_turn 的停止原因就一律失败。"""
    stop_reason = getattr(message, "stop_reason", None)
    if stop_reason != "end_turn" and (structured or stop_reason is not None):
        raise errors.response(_stop_reason_error(stop_reason))


def _anthropic_text(message: Message, errors: AIErrorTypes) -> str:
    content = getattr(message, "content", None)
    blocks = content if isinstance(content, list) else []
    text = "".join(
        block.text
        for block in blocks
        if getattr(block, "type", None) == "text" and isinstance(getattr(block, "text", None), str)
    ).strip()
    if not text:
        raise errors.response("响应中缺少可解析的 content")
    return text


async def _call_anthropic(
    client: AsyncAnthropic, model: str, request: ChatRequest, errors: AIErrorTypes
) -> _Outcome:
    if request.max_tokens is None:
        # Messages 协议 max_tokens 必填；业务请求由 service 传 provider 值，探测传 1。
        raise errors.config("anthropic_messages 协议必须设置输出预算（max_tokens）")
    messages = _anthropic_messages(request.messages)
    system = request.system or _anthropic_omit
    if request.response_model is not None:
        message = await client.messages.parse(
            model=model,
            max_tokens=request.max_tokens,
            messages=messages,
            system=system,
            output_format=request.response_model,
        )
        _gate_anthropic_stop(message, structured=True, errors=errors)
        text = _anthropic_text(message, errors)
        parsed = message.parsed_output
        if parsed is None:  # 正文闸门已过，理论不可达；防御空解析
            raise errors.response("AI 未返回可解析的结构化结果")
        return _Outcome(text=text, usage=_anthropic_usage(message), parsed=parsed)

    message = await client.messages.create(
        model=model,
        max_tokens=request.max_tokens,
        messages=messages,
        system=system,
    )
    _gate_anthropic_stop(message, structured=False, errors=errors)
    return _Outcome(text=_anthropic_text(message, errors), usage=_anthropic_usage(message))


def _build_openai_client(
    provider: ProviderConfig, protocol: ProtocolName, timeout_seconds: float
) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=provider.base_url_for(protocol),
        api_key=provider.api_key,
        timeout=timeout_seconds,
        max_retries=0,
        http_client=build_http_client(timeout_seconds),
    )


def _build_anthropic_client(provider: ProviderConfig, timeout_seconds: float) -> AsyncAnthropic:
    return AsyncAnthropic(
        base_url=anthropic_base_url(provider.base_url_for("anthropic_messages")),
        api_key=provider.api_key,
        timeout=timeout_seconds,
        max_retries=0,
        http_client=build_http_client(timeout_seconds),
    )


async def execute(
    *,
    protocol: ProtocolName,
    provider: ProviderConfig,
    model: str,
    request: ChatRequest,
    timeout_seconds: float,
    errors: AIErrorTypes = DEFAULT_AI_ERRORS,
) -> _Outcome:
    """按协议调用官方 SDK 完成一次请求；SDK 客户端按次创建并关闭。

    ``errors`` 注入调用方异常类型；映射不了的异常原样抛出，由上层兜底。
    """
    temperature = request.temperature if provider_sends_temperature(provider, protocol) else None
    system = request.system
    if request.json_object and request.response_model is None:
        # OpenAI 系 json_object 要求上下文含 "JSON" 字样；Anthropic 靠提示词约束。
        system = _ensure_json_instruction(system, request.messages)
    request = ChatRequest(
        system=system,
        messages=request.messages,
        json_object=request.json_object,
        response_model=request.response_model,
        temperature=temperature,
        max_tokens=request.max_tokens,
    )

    try:
        if protocol == "anthropic_messages":
            async with _build_anthropic_client(provider, timeout_seconds) as client:
                return await _call_anthropic(client, model, request, errors)
        async with _build_openai_client(provider, protocol, timeout_seconds) as client:
            if protocol == "openai_chat":
                return await _call_chat(client, provider, model, request, errors)
            return await _call_responses(client, model, request, errors)
    except Exception as error:
        raise map_sdk_error(error, errors) from error


async def probe_protocol(
    *,
    protocol: ProtocolName,
    provider: ProviderConfig,
    model: str,
    budget: int | None,
    timeout_seconds: float,
) -> None:
    """对候选协议发一次最小请求；SDK 调用未抛异常且响应类型形状有效即视为协议可用。

    预算耗尽但形状有效的探测响应不要求存在正文（官方 Responses 协议预算下限 16，
    推理模型可能耗尽预算并返回 incomplete）。异常以共享层默认类型抛出，供
    ``_probe_all`` 换下一个候选或映射给等待者。
    """
    probe = ChatRequest(
        messages=({"role": "user", "content": "Reply with OK only."},), max_tokens=budget
    )
    try:
        if protocol == "anthropic_messages":
            async with _build_anthropic_client(provider, timeout_seconds) as client:
                message = await client.messages.create(
                    model=model,
                    max_tokens=probe.max_tokens if probe.max_tokens is not None else 1,
                    messages=_anthropic_messages(probe.messages),
                )
                if not isinstance(getattr(message, "content", None), list):
                    raise AIResponseError("探测响应不符合 Anthropic Messages 形状")
        elif protocol == "openai_chat":
            async with _build_openai_client(provider, protocol, timeout_seconds) as client:
                completion = await client.chat.completions.create(
                    model=model, messages=_openai_messages("", probe.messages), store=False
                )
                if not isinstance(getattr(completion, "choices", None), list):
                    raise AIResponseError("探测响应不符合 Chat Completions 形状")
        else:
            async with _build_openai_client(provider, protocol, timeout_seconds) as client:
                response = await client.responses.create(
                    model=model,
                    input=_responses_input(probe.messages),
                    store=False,
                    max_output_tokens=probe.max_tokens
                    if probe.max_tokens is not None
                    else _openai_omit,
                )
                if not isinstance(getattr(response, "output", None), list):
                    raise AIResponseError("探测响应不符合 Responses 形状")
    except Exception as error:
        raise map_sdk_error(error, DEFAULT_AI_ERRORS) from error
