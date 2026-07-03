"""测试 un_nickname 插件的 at 昵称替换功能。

重点覆盖 AT_NICKNAME_PATTERN 与 is_replacing_nickname 的一致性，
以及此前中文标点/贪婪吞字等回归场景。
"""

from datetime import datetime

import pytest
from nonebot.adapters.onebot.v11 import Bot as OneBotV11Bot, GroupMessageEvent, Message
from nonebug import App


def _make_event(text: str) -> GroupMessageEvent:
    """构造一条仅含纯文本的群消息事件"""
    return GroupMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=987654321,
        post_type="message",
        sub_type="normal",
        user_id=111,
        message_type="group",
        group_id=222,
        message_id=1,
        message=Message(text),
        original_message=Message(text),
        raw_message=text,
        font=0,
        sender={"user_id": 111, "nickname": "u", "card": "", "role": "member"},  # type: ignore[arg-type]
    )


# ============== AT_NICKNAME_PATTERN 单元测试 ==============


@pytest.mark.parametrize(
    "text,expected_names",
    [
        # 基本场景
        ("at老张", ["老张"]),
        ("at 老张", ["老张"]),
        ("at老张 吃饭", ["老张"]),
        # 中文标点后跟（B1 回归）：以前 (?=\s|$) 全部失败
        ("at老张，吃饭吗", ["老张"]),
        ("at老张。", ["老张"]),
        ("at老张！", ["老张"]),
        ("at老张？", ["老张"]),
        # 句中、句尾
        ("喂 at老张 出来", ["老张"]),
        ("喂 at老张", ["老张"]),
        # 多个昵称
        ("at老张 at小李", ["老张", "小李"]),
        # 拉丁/数字昵称
        ("at abc 吃饭", ["abc"]),
        ("at dev1 出事了", ["dev1"]),
    ],
)
def test_at_nickname_pattern_matches(text: str, expected_names: list[str]) -> None:
    """正则应正确捕获名字，且不吞掉后续中文

    注意：纯字符正则无法做中文分词，'at老张吃饭' 这种无边界写法无法区分
    昵称与后续正文，需用空格或标点分隔。这是固有设计契约。
    """
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    matches = AT_NICKNAME_PATTERN.findall(text)
    assert matches == expected_names, f"文本 {text!r} 期望 {expected_names}，实际 {matches}"


@pytest.mark.parametrize(
    "text",
    [
        "that老张",  # 前面是字母，不构成 \b
        "battle today",
        "what is that",
        "look at",  # at 后没有名字字符
        "at，",  # at 后直接是中文标点，没有名字
        "AT老张",  # 大小写敏感，不匹配
        "At老张",
    ],
)
def test_at_nickname_pattern_no_match(text: str) -> None:
    """非 at 昵称语法不应被匹配"""
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    assert AT_NICKNAME_PATTERN.search(text) is None, f"文本 {text!r} 不应被匹配"


def test_at_nickname_pattern_consecutive_latin() -> None:
    """连写拉丁昵称（B3 回归）：'at dev at test' 在带空格时应分别命中"""
    from src.plugins.un_nickname.utils import AT_NICKNAME_PATTERN

    # 空格分隔的连写：第一段 "at dev" 命中 dev，剩余 " at test" 命中 test
    matches = AT_NICKNAME_PATTERN.findall("at dev at test 出事了")
    assert matches == ["dev", "test"]


# ============== is_replacing_nickname rule 一致性 ==============


@pytest.mark.parametrize(
    "text,should_trigger",
    [
        # 与正则一致：命中（昵称后有明确边界）
        ("at老张", True),
        ("at老张，吃饭吗", True),
        ("at老张 吃饭", True),
        # 与正则一致：不命中
        ("that老张", False),
        ("battle", False),
        ("what is that", False),
        ("look at", False),
    ],
)
def test_is_replacing_nickname_consistency(text: str, should_trigger: bool) -> None:
    """rule 必须与 AT_NICKNAME_PATTERN 判定一致，避免假阳性空跑 handler"""
    from src.plugins.un_nickname.handlers import is_replacing_nickname

    assert is_replacing_nickname(_make_event(text)) is should_trigger


# ============== handler 行为冒烟 ==============


@pytest.mark.asyncio
async def test_replace_handler_no_match_skips_send(app: App) -> None:
    """无候选名时 handler 应早返回，不查询缓存、不发送消息"""
    from src.plugins.un_nickname import handlers
    from src.plugins.un_nickname.cache import _nickname_cache

    async with app.test_api() as ctx:
        bot = ctx.create_bot(base=OneBotV11Bot, self_id="987654321")
        event = _make_event("that老张 battle today")

        # 无 at 昵称命中，handler 应在收集候选阶段就 return，
        # 不应触发任何缓存查询（不写 _nickname_cache）或调用 send API
        _nickname_cache.clear()
        await handlers.handle_replace_nickname(bot, event)
        # 缓存未被写入，证明未走到 get_cached_nickname_map 分支
        assert "222" not in _nickname_cache
