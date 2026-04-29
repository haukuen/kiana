"""帮助命令插件

触发「猫猫帮助」展示所有可用命令总览图
触发「猫猫帮助 <插件名>」展示指定插件的详细命令图
"""

from pathlib import Path

from nonebot import get_loaded_plugins, logger, on_regex
from nonebot.adapters.onebot.v11 import MessageEvent, MessageSegment
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata
from nonebot_plugin_htmlkit import template_to_pic

from ..group_permission import check_plugin_visibility
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助命令插件",
    usage=(
        "发送关键词查看帮助:\n"
        "- 猫猫帮助: 查看所有可用命令\n"
        "- 猫猫帮助 <命令名>: 查看指定命令的详细用法"
    ),
    config=Config,
)

TEMPLATE_DIR = Path(__file__).parent / "template"

help_matcher = on_regex(r"^猫猫帮助\s*(\S+)?$", priority=1, block=True)


async def _collect_visible_plugins(event: MessageEvent) -> list[dict]:
    """收集当前事件下可见的插件信息"""
    plugins = get_loaded_plugins()
    result = []
    for plugin in plugins:
        if plugin.metadata is None:
            continue
        if plugin.name == "help":
            continue
        if not await check_plugin_visibility(plugin.name, event):
            continue
        result.append({
            "name": plugin.metadata.name,
            "desc": plugin.metadata.description or "",
        })
    return result


def _find_plugin(name: str):
    """按名称查找已加载插件（不区分大小写）"""
    for plugin in get_loaded_plugins():
        if plugin.metadata and plugin.metadata.name.lower() == name.lower():
            return plugin
    return None


@help_matcher.handle()
async def handle_help(
    event: MessageEvent,
    regex_groups: tuple = RegexGroup(),
):
    query = regex_groups[0]

    if query:
        # 详情模式
        plugin = _find_plugin(query)
        if not plugin:
            await help_matcher.finish(
                f"未找到「{query}」命令，发送「猫猫帮助」查看所有命令"
            )

        if not await check_plugin_visibility(plugin.name, event):  # type: ignore[union-attr]
            await help_matcher.finish(
                f"未找到「{query}」命令，发送「猫猫帮助」查看所有命令"
            )

        assert plugin.metadata is not None
        usage = plugin.metadata.usage or plugin.metadata.description or "暂无详细说明"
        template_data = {
            "mode": "detail",
            "name": plugin.metadata.name,
            "usage": usage,
        }
    else:
        # 总览模式
        commands = await _collect_visible_plugins(event)
        if not commands:
            await help_matcher.finish("暂无可用命令")

        template_data = {
            "mode": "overview",
            "commands": commands,
        }

    try:
        image_bytes = await template_to_pic(
            template_path=str(TEMPLATE_DIR),
            template_name="help.html",
            templates=template_data,
            max_width=720,
        )
        await help_matcher.send(MessageSegment.image(image_bytes))
    except Exception as e:
        logger.error(f"帮助页面渲染失败: {e}")
        # fallback 到纯文本
        if query:
            await help_matcher.finish(f"【{plugin.metadata.name}】\n{usage}")  # type: ignore[possibly-undefined]
        else:
            text_lines = [f"• {c['name']}: {c['desc']}" for c in commands]  # type: ignore[possibly-undefined]
            await help_matcher.finish("可用命令:\n" + "\n".join(text_lines))
