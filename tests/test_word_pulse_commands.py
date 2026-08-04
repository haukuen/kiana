"""词频插件命令解析测试。

覆盖：add / append / list / del / refresh / alias / unalias 七个子命令。

通过 nonebug `app` fixture 触发 NoneBot 初始化，避免 collect 阶段触发
插件包 __init__.py 链式导入时报 driver 未初始化错误。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App


def make_group_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 100001,
    group_id: int = 200001,
    self_id: int = 987654321,
    nickname: str = "测试用户",
    card: str = "",
) -> GroupMessageEvent:
    actual = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        group_id=group_id,
        message_id=message_id,
        message=actual,
        original_message=actual.copy(),
        raw_message=str(actual),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )


EXPECTED_HELP_TEXT = "\n".join(
    [
        "📊 词频插件帮助",
        "",
        "群聊主题词频统计 — 创建主题 → AI 日桶分类 → 热度总结。",
        "",
        "管理命令(需群管理员/Bot 管理员):",
        "  词频 add <主题> <种子词1> <种子词2> ...   创建主题",
        "  词频 append <主题> <种子词...>            追加子类",
        "  词频 list                                 列出本群主题",
        "  词频 del <主题>                           删除主题",
        "  词频 del <主题> <子类>                    删除子类",
        "  词频 refresh <主题>                       刷新字符集(AI 扩展)",
        "  词频 alias <主题> <主名词> <别名...>      给子类加别名",
        "  词频 unalias <主题> <主名词> <别名...>    删除子类别名",
        "",
        "查询命令(所有人可用):",
        "  总结 <N>天/周/月 <主题>                  查询热度统计(例: 总结 7天 炒股)",
        "",
        "时间单位: 天/d, 周/w, 月/m",
    ]
)


def expect_bot_not_muted(
    ctx, group_id: int = 300001, self_id: int = 987654321
) -> None:
    """should_call_send 之前必须先声明：bot 不被禁言。

    项目 ``check_bot_mute_status`` preprocessor 在群消息场景下会查 mute cache，
    触发 get_group_member_info API。未先声明会导致 nonebug 报意外 API 调用。
    """
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


@pytest.fixture
def parse_command(app: App):
    """延迟导入 parse_command，确保 NoneBot 已初始化。"""
    from src.plugins.word_pulse.commands import parse_command as _pc
    return _pc


def test_parse_add_basic(app: App, parse_command) -> None:
    cmd = parse_command("词频 add 炒股 茅台 五粮液 创业板")
    assert cmd is not None
    assert cmd.action == "add"
    assert cmd.theme == "炒股"
    assert cmd.seeds == ["茅台", "五粮液", "创业板"]


def test_parse_add_single_seed(app: App, parse_command) -> None:
    cmd = parse_command("词频 add 游戏 原神")
    assert cmd is not None
    assert cmd.action == "add"
    assert cmd.theme == "游戏"
    assert cmd.seeds == ["原神"]


def test_parse_append(app: App, parse_command) -> None:
    cmd = parse_command("词频 append 炒股 中信证券")
    assert cmd is not None
    assert cmd.action == "append"
    assert cmd.theme == "炒股"
    assert cmd.seeds == ["中信证券"]


def test_parse_list(app: App, parse_command) -> None:
    cmd = parse_command("词频 list")
    assert cmd is not None
    assert cmd.action == "list"
    assert cmd.theme is None
    assert cmd.seeds is None


def test_parse_del_theme(app: App, parse_command) -> None:
    cmd = parse_command("词频 del 炒股")
    assert cmd is not None
    assert cmd.action == "del"
    assert cmd.theme == "炒股"
    assert cmd.seeds is None


def test_parse_del_cluster(app: App, parse_command) -> None:
    cmd = parse_command("词频 del 炒股 茅台")
    assert cmd is not None
    assert cmd.action == "del"
    assert cmd.theme == "炒股"
    assert cmd.seeds == ["茅台"]


def test_parse_refresh(app: App, parse_command) -> None:
    cmd = parse_command("词频 refresh 炒股")
    assert cmd is not None
    assert cmd.action == "refresh"
    assert cmd.theme == "炒股"


def test_parse_alias_basic(app: App, parse_command) -> None:
    cmd = parse_command("词频 alias 炒股 茅台 茅子 飞天")
    assert cmd is not None
    assert cmd.action == "alias"
    assert cmd.theme == "炒股"
    # 第一个是主名词，其余是别名
    assert cmd.seeds == ["茅台", "茅子", "飞天"]


def test_parse_alias_multiple_aliases(app: App, parse_command) -> None:
    cmd = parse_command("词频 alias 炒股 茅台 茅子 飞天 茅茅子 茅神")
    assert cmd is not None
    assert cmd.action == "alias"
    assert cmd.theme == "炒股"
    assert cmd.seeds == ["茅台", "茅子", "飞天", "茅茅子", "茅神"]


def test_parse_alias_requires_at_least_two_args(app: App, parse_command) -> None:
    """alias 必须包含主名词 + 至少一个别名。"""
    assert parse_command("词频 alias 炒股 茅台") is None


def test_parse_unalias_basic(app: App, parse_command) -> None:
    cmd = parse_command("词频 unalias 炒股 茅台 茅子")
    assert cmd is not None
    assert cmd.action == "unalias"
    assert cmd.theme == "炒股"
    assert cmd.seeds == ["茅台", "茅子"]


def test_parse_unalias_requires_at_least_two_args(app: App, parse_command) -> None:
    assert parse_command("词频 unalias 炒股 茅台") is None


def test_parse_unknown_action_returns_none(app: App, parse_command) -> None:
    assert parse_command("词频 foobar 炒股 茅台") is None


def test_parse_no_prefix_returns_none(app: App, parse_command) -> None:
    assert parse_command("总结 7天 炒股") is None


def test_parse_empty_returns_none(app: App, parse_command) -> None:
    assert parse_command("") is None


# ── help_matcher 注册 smoke test ──────────────────────


def test_help_matcher_registered(app: App) -> None:
    """help_matcher 已注册且 handler 绑定成功，import 不报错。"""
    from src.plugins import word_pulse

    assert word_pulse.help_matcher is not None
    # 确认有 handler 绑定（nonebot Matcher 的 handlers 非空）
    assert word_pulse.handle_help is not None


@pytest.mark.asyncio
async def test_help_command_triggers_and_replies(app: App, monkeypatch) -> None:
    """「词频 帮助」触发 help_matcher，返回格式正确的帮助文案。

    config 默认 word_pulse_plugin_enabled=False 会让 rule 拒绝群消息，
    所以用 monkeypatch 临时打开开关。
    """
    from src.plugins import word_pulse

    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)

    event = make_group_event("词频 帮助", message_id=100, group_id=300001)
    async with app.test_matcher(word_pulse.help_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, EXPECTED_HELP_TEXT, result={"message_id": 1000})


@pytest.mark.asyncio
async def test_help_command_accepts_help_alias(app: App, monkeypatch) -> None:
    """「词频 help」(英文别名) 同样触发帮助，文案一致。"""
    from src.plugins import word_pulse

    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)

    event = make_group_event("词频 help", message_id=101, group_id=300001)
    async with app.test_matcher(word_pulse.help_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, EXPECTED_HELP_TEXT, result={"message_id": 1001})


@pytest.mark.asyncio
async def test_help_command_rejects_pasted_text(app: App, monkeypatch) -> None:
    """「词频帮助我」不应触发 help_matcher（正则锚定整条消息）。"""
    from src.plugins import word_pulse

    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)

    event = make_group_event("词频帮助我", message_id=102, group_id=300001)
    async with app.test_matcher(word_pulse.help_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        # rule 不通过 → matcher 不进入 handler，mute preprocessor 也不会跑，
        # 所以这里不声明 expect_bot_not_muted（否则声明不会被消费而报错）。
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule()
