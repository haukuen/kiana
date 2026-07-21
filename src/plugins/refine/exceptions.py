"""炼化插件错误类型层级。

参考 ``a_share_sentiment.ai`` 与 ``trawler.exceptions`` 的设计：基类
``RefineError`` 统一所有可恢复错误；commands 层用 ``friendly_error`` 把
各子类映射为用户可见的中文提示。
"""


class RefineError(Exception):
    """炼化功能可恢复错误基类。"""


class RefineAIError(RefineError):
    """AI 分析失败基类。"""


class RefineAITimeoutError(RefineAIError):
    """AI 请求超时。"""


class RefineAIAuthError(RefineAIError):
    """AI 鉴权失败。"""


class RefineAIServiceError(RefineAIError):
    """AI 服务异常（HTTP 5xx / 网络错误）。"""


class RefineAIResponseError(RefineAIError):
    """AI 返回格式异常（空响应 / 不是合法 JSON / 缺字段）。"""


class RefineConfigError(RefineError):
    """配置缺失或不合法（缺 api_key / base_url / model 等）。"""


class RefineSubscriptionNotFoundError(RefineError):
    """订阅不存在。"""


class RefineCollectionNotFoundError(RefineError):
    """引用的 un_nickname 集合不存在。"""
