"""AI 调用的错误类型层级与「异常注入」用的类型集合。

共享层只负责把失败分类（超时 / 鉴权 / 服务 / 响应格式），具体抛哪个类由调用方
决定：``AIErrorTypes`` 让每个插件把自己的异常类传进来，从而保留各自的用户提示
文案；不关心类型的调用方直接用 ``DEFAULT_AI_ERRORS``。
"""

from __future__ import annotations

from dataclasses import dataclass


class AIError(Exception):
    """AI 调用失败基类。"""


class AITimeoutError(AIError):
    """AI 请求超时。"""


class AIAuthError(AIError):
    """AI 鉴权失败。"""


class AIServiceError(AIError):
    """AI 服务异常（非 2xx 响应或网络错误）。"""


class AIResponseError(AIError):
    """AI 返回内容不符合预期（不是合法 JSON、缺少可解析的 content 等）。"""


class AIConfigError(AIError):
    """调用方要的 provider / model 解析不出来（配置问题，不是运行时故障）。

    消费者通常会用 ``resolve(...).missing`` 提前校验并给出友好提示，这里是
    绕过校验直接调用时的兜底。
    """


@dataclass(frozen=True, slots=True)
class AIErrorTypes:
    """调用方的 AI 异常集合，用于把共享层的失败翻译成插件自己的异常。

    ``config`` 是后加的注入位：共享入口的输入/参数校验（消息、schema、temperature
    冲突等）也按调用方注入的类型抛错，默认仍是共享的 ``AIConfigError``——只传
    旧四参数的调用方行为不变。
    """

    timeout: type[Exception]
    auth: type[Exception]
    service: type[Exception]
    response: type[Exception]
    config: type[Exception] = AIConfigError


DEFAULT_AI_ERRORS = AIErrorTypes(
    timeout=AITimeoutError,
    auth=AIAuthError,
    service=AIServiceError,
    response=AIResponseError,
)
