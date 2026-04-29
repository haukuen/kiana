from collections.abc import Awaitable, Callable
from typing import Any, Literal

from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent
from pydantic import BaseModel, Field

_VISIBILITY_REGISTRY: dict[str, Callable[[Event], Awaitable[bool]]] = {}


async def check_plugin_visibility(plugin_name: str, event: Event) -> bool:
    """检查插件在当前事件下是否可见

    对于未注册的插件（不使用 group_permission 的插件），默认返回 True。
    """
    checker = _VISIBILITY_REGISTRY.get(plugin_name)
    return await checker(event) if checker else True


class GroupPermissionMixin(BaseModel):
    """分群权限配置 Mixin

    可混入到插件的 Config 类中，提供标准的分群配置字段
    """

    group_mode: Literal["all", "whitelist", "blacklist"] = Field(
        default="all",
        description="群组控制模式: all(全部群启用) | whitelist(仅白名单群) | blacklist(黑名单外的群)",
    )
    group_whitelist: list[str] = Field(
        default=[], description="白名单群组(仅在 whitelist 模式生效)"
    )
    group_blacklist: list[str] = Field(
        default=[], description="黑名单群组(仅在 blacklist 模式生效)"
    )


def check_group_permission(
    event: Event,
    enabled: bool,
    group_mode: str,
    group_whitelist: list[str],
    group_blacklist: list[str],
) -> bool:
    """检查插件是否在当前群启用

    Args:
        event: 事件对象
        enabled: 插件全局开关
        group_mode: 群组控制模式 (all/whitelist/blacklist)
        group_whitelist: 白名单群组列表
        group_blacklist: 黑名单群组列表

    Returns:
        bool: 是否启用
    """
    # 检查全局开关
    if not enabled:
        return False

    # 私聊消息始终启用
    if not isinstance(event, GroupMessageEvent):
        return True

    # 获取群号
    group_id = str(event.group_id)

    # 根据模式判断
    if group_mode == "all":
        return True
    if group_mode == "whitelist":
        return group_id in group_whitelist
    if group_mode == "blacklist":
        return group_id not in group_blacklist
    # 未知模式，默认启用
    return True


def create_group_rule(
    config_getter: Callable[[], Any],
    plugin_enabled_attr: str,
    prefix: str = "",
) -> Callable[[Event], Any]:
    """创建群组规则检查函数

    Args:
        config_getter: 获取配置对象的函数
        plugin_enabled_attr: 插件启用开关的属性名（如 "fund_plugin_enabled"）
        prefix: 配置前缀（如 "fund_"），如果为空则使用 group_mode/group_whitelist/group_blacklist

    Returns:
        群组规则检查函数

    Example:
        >>> config = get_plugin_config(Config)
        >>> fund_rule = create_group_rule(lambda: config, "fund_plugin_enabled", "fund_")
        >>> # 在 on_regex 中使用
        >>> fund_query = on_regex(pattern, rule=fund_rule)
    """

    async def group_rule(event: Event) -> bool:
        config = config_getter()
        enabled = getattr(config, plugin_enabled_attr, True)

        # 构建配置属性名
        mode_attr = f"{prefix}group_mode" if prefix else "group_mode"
        whitelist_attr = f"{prefix}group_whitelist" if prefix else "group_whitelist"
        blacklist_attr = f"{prefix}group_blacklist" if prefix else "group_blacklist"

        group_mode = getattr(config, mode_attr, "all")
        group_whitelist = getattr(config, whitelist_attr, [])
        group_blacklist = getattr(config, blacklist_attr, [])

        return check_group_permission(event, enabled, group_mode, group_whitelist, group_blacklist)

    # 注册可见性检查器
    plugin_name = prefix.rstrip("_") if prefix else plugin_enabled_attr.replace("_plugin_enabled", "")
    _VISIBILITY_REGISTRY[plugin_name] = group_rule

    return group_rule


def create_sub_feature_rule(
    config_getter: Callable[[], Any],
    plugin_enabled_attr: str,
    feature_enabled_attr: str,
    prefix: str = "",
) -> Callable[[Event], Any]:
    """创建带子功能开关的群组规则检查函数

    用于有多个子功能的插件（如 Gold 插件有价格查询和走势图两个功能）

    Args:
        config_getter: 获取配置对象的函数
        plugin_enabled_attr: 插件启用开关的属性名
        feature_enabled_attr: 子功能启用开关的属性名
        prefix: 配置前缀

    Returns:
        群组规则检查函数

    Example:
        >>> config = get_plugin_config(Config)
        >>> price_rule = create_sub_feature_rule(
        ...     lambda: config,
        ...     "gold_plugin_enabled",
        ...     "gold_enable_price_query",
        ...     "gold_"
        ... )
    """

    async def sub_feature_rule(event: Event) -> bool:
        config = config_getter()

        # 先检查子功能是否启用
        feature_enabled = getattr(config, feature_enabled_attr, True)
        if not feature_enabled:
            return False

        # 再检查群权限
        plugin_enabled = getattr(config, plugin_enabled_attr, True)

        mode_attr = f"{prefix}group_mode" if prefix else "group_mode"
        whitelist_attr = f"{prefix}group_whitelist" if prefix else "group_whitelist"
        blacklist_attr = f"{prefix}group_blacklist" if prefix else "group_blacklist"

        group_mode = getattr(config, mode_attr, "all")
        group_whitelist = getattr(config, whitelist_attr, [])
        group_blacklist = getattr(config, blacklist_attr, [])

        return check_group_permission(
            event, plugin_enabled, group_mode, group_whitelist, group_blacklist
        )

    # 注册可见性检查器（子功能插件共享同一个 plugin_name）
    plugin_name = prefix.rstrip("_") if prefix else plugin_enabled_attr.replace("_plugin_enabled", "")
    if plugin_name not in _VISIBILITY_REGISTRY:
        _VISIBILITY_REGISTRY[plugin_name] = sub_feature_rule

    return sub_feature_rule


def create_platform_rule(
    config_getter: Callable[[], Any],
    platform: str,
) -> Callable[[Event], Any]:
    """为多平台插件创建平台特定的群组规则检查函数

    Args:
        config_getter: 获取配置对象的函数
        platform: 平台名称 (如 "bilibili", "douyin", "xiaohongshu")

    Returns:
        群组规则检查函数

    Example:
        >>> config = get_plugin_config(Config)
        >>> bilibili_rule = create_platform_rule(lambda: config, "bilibili")
    """

    async def platform_rule(event: Event) -> bool:
        config = config_getter()

        # 检查平台开关
        enabled_attr = f"enable_{platform}"
        enabled = getattr(config, enabled_attr, True)

        # 构建分群配置属性名
        mode_attr = f"{platform}_group_mode"
        whitelist_attr = f"{platform}_group_whitelist"
        blacklist_attr = f"{platform}_group_blacklist"

        group_mode = getattr(config, mode_attr, "all")
        group_whitelist = getattr(config, whitelist_attr, [])
        group_blacklist = getattr(config, blacklist_attr, [])

        return check_group_permission(event, enabled, group_mode, group_whitelist, group_blacklist)

    # 注册平台可见性检查器
    _VISIBILITY_REGISTRY[platform] = platform_rule

    return platform_rule
