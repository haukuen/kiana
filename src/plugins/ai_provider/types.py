"""ai_provider 的公共数据形状：中性请求、中性结果、以及「一次调用打到哪里」。

协议名与 Config 放在 ``config.py``（它必须自包含，见该模块的说明），这里只引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from .config import ProtocolName


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """协议无关的一次请求。

    调用方只给 ``system`` 和 ``messages``（role/content 都是字符串）；各协议的
    参数形状（预算字段、JSON mode、严格输出字段）由 ``client.py`` 按协议表达。

    ``json_object=True`` 是「提示词兜底 + 各协议 JSON mode」（Anthropic 没有原生
    等价物，靠提示词约束 + 调用方围栏容错）；``response_model`` 是严格结构化输出
    的 Pydantic 模型，由 SDK 官方 parse 接口生成 schema 并解析回模型实例，两者
    互斥。``max_tokens`` 为 None 时省略预算字段（探测请求用；业务请求由 service
    传 provider 值）。
    """

    system: str = ""
    messages: tuple[dict[str, str], ...] = ()
    json_object: bool = False
    response_model: type[BaseModel] | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ChatResult[TModel]:
    """一次调用的结果。``text`` 已归一化，调用方不必知道响应是 choices 还是内容块。

    ``parsed`` 只在 ``response_model`` 传入时非空（``ChatResult[模型类型]``）：
    SDK 已按模型类型解析完成，消费者直接拿到业务实例，无需再 ``json.loads`` +
    ``model_validate``。
    """

    text: str
    model: str
    provider_id: str
    protocol: ProtocolName
    usage: dict[str, int] = field(default_factory=dict)
    parsed: TModel | None = None
