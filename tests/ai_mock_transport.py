"""官方 SDK 传输层的测试注入点。

ai_provider 的请求经官方 SDK（``openai``/``anthropic``）发出，
SDK 内部使用 httpx2 客户端，``patch("httpx.AsyncClient.post")`` 不再能拦截请求。
这里的 ``mock_ai_http`` 把 ``build_http_client`` 工厂换成 ``httpx2.MockTransport``，
让**真实 SDK** 完成序列化、反序列化与异常构造，测试只描述响应形状并断言请求。
"""

from __future__ import annotations

import contextlib
import inspect
import json
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import patch

import httpx2

Handler = Callable[[httpx2.Request], httpx2.Response]
"""同步或异步 handler 均可：返回协程时 transport 会自动 await。"""

AI_HTTP_TARGET = "src.plugins.ai_provider.client.build_http_client"


class AIHttpMock:
    """记录请求并按 handler 应答的传输工厂；``urls``/``bodies`` 供断言。"""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx2.Request] = []
        self.trust_env_flags: list[bool] = []

    def __call__(self, timeout_seconds: float) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
            transport=httpx2.MockTransport(self._handle),
        )
        self.trust_env_flags.append(bool(client.trust_env))
        return client

    async def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        result = self.handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@contextlib.contextmanager
def mock_ai_http(
    handler: Handler, target: str = AI_HTTP_TARGET
) -> Iterator[AIHttpMock]:
    """在 with 块内把 SDK 传输指向 mock；yield 出的 recorder 记录了全部请求。"""
    mock = AIHttpMock(handler)
    with patch(target, new=mock):
        yield mock


def chat_ok(content: str = "总结内容", finish_reason: str = "stop") -> Handler:
    """Chat Completions 的标准 200 响应。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "id": "chat-1",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-test",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": content},
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            },
        )

    return handler


def status_error(status: int, *, body: Any = None, headers: dict[str, str] | None = None) -> Handler:
    """非 2xx 响应；SDK 自己把它变成对应的异常类型。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body
        if headers:
            kwargs["headers"] = headers
        return httpx2.Response(status, **kwargs)

    return handler


def transport_failure(error: Exception) -> Handler:
    """在传输层抛出异常（超时、连接失败等），SDK 会包装成对应异常。"""

    def handler(request: httpx2.Request) -> httpx2.Response:
        raise error

    return handler
