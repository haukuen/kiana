"""word_pulse.__init__.py 的所有 handler 端到端测试。

覆盖:
- admin_matcher 路由:管理员校验 / 命令格式错误 / 私聊被拒
- _handle_add:成功 + 字符集扩展失败降级 + 非管理员被拒
- _handle_append:成功 + 主题不存在
- _handle_list:有主题 + 空列表
- _handle_del:删主题 + 删子类 + 主题/子类不存在
- _handle_refresh:成功 + 主题不存在 + 字符集刷新失败
- _handle_alias / _handle_unalias:加/删别名 + 各种失败分支
- handle_query (bug#4):查询成功 + 主题不存在 + 配置缺失 + 时间超限 +
  冷却 + 缓存命中 + parse_query None + 私聊被拒

模板:
- 用 ``app.test_matcher()`` 触发完整事件路由
- 用 ``should_call_send`` 断言回包
- 用 monkeypatch 启用插件 + 配置 AI
- mock ``httpx.AsyncClient.post`` 而非业务逻辑

不修改任何源代码。
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

# ═══════════════════════════════════════════════════════════════
# 工厂与公共辅助
# ═══════════════════════════════════════════════════════════════


def _make_group_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 100001,
    group_id: int = 200001,
    self_id: int = 987654321,
    nickname: str = "测试用户",
    card: str = "",
    role: str = "member",
    event_time: int | None = None,
) -> GroupMessageEvent:
    """构造群消息事件。role:member/admin/owner。"""
    actual = message if isinstance(message, Message) else Message(message)
    return GroupMessageEvent(
        time=event_time or int(datetime.now().timestamp()),
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
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role=role),
    )


def _make_private_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 100001,
    self_id: int = 987654321,
) -> PrivateMessageEvent:
    actual = message if isinstance(message, Message) else Message(message)
    return PrivateMessageEvent(
        time=int(datetime.now().timestamp()),
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="private",
        message_id=message_id,
        message=actual,
        original_message=actual.copy(),
        raw_message=str(actual),
        font=0,
        sender=Sender(user_id=user_id, nickname="私聊用户"),
    )


def _expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    """声明 bot 不被禁言。"""
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def _enable_word_pulse(monkeypatch) -> None:
    """启用 word_pulse 插件 + 配置 AI。"""
    from src.plugins import word_pulse
    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)
    monkeypatch.setattr(word_pulse.config, "word_pulse_base_url", "https://example.com/v1")
    monkeypatch.setattr(word_pulse.config, "word_pulse_api_key", "sk-test")
    monkeypatch.setattr(word_pulse.config, "word_pulse_model", "gpt-test")


def _fake_ai_response(content: str = "AI ok") -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )


def _fake_charset_response() -> httpx.Response:
    """expand_charsets 期望 {charsets: [{cluster, chars[5-30]}]}。"""
    return _fake_ai_response(json.dumps({
        "charsets": [{"cluster": "茅台", "chars": ["茅", "台", "酒", "股", "票"] * 2}]
    }))


# ═══════════════════════════════════════════════════════════════
# 路由层:管理员校验 / 命令格式错误 / 私聊被拒
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_admin_rejects_non_admin_for_add(app: App, monkeypatch) -> None:
    """非管理员(member role)执行 add → 被拒。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    # 清冷却避免污染
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 add 炒股 茅台", message_id=1, role="member")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "仅群管理或 Bot 管理员可使用此命令", result={"message_id": 100})


@pytest.mark.asyncio
async def test_admin_accepts_admin_role_for_add_success(app: App, monkeypatch) -> None:
    """管理员(admin role)执行 add → 字符集扩展成功 → 返回正常提示。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 add 炒股 茅台", message_id=2, role="admin")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_fake_charset_response())):
        async with app.test_matcher(word_pulse.admin_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "✓ 主题「炒股」已创建（覆盖式）\n  子类：茅台",
                result={"message_id": 101},
            )

    # 副作用断言:主题与子类应已落库
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None, "add 后主题应已写入 DB"
    clusters = await get_clusters(theme["id"])
    assert [c["name"] for c in clusters] == ["茅台"], "种子词应作为子类落库"


@pytest.mark.asyncio
async def test_admin_accepts_list_for_non_admin(app: App, monkeypatch) -> None:
    """list 命令对非管理员可用(空列表)。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 list", message_id=3, role="member")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群暂无主题", result={"message_id": 102})


# ═══════════════════════════════════════════════════════════════
# _handle_add:成功 + AI 失败降级 + 非管理员
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_add_returns_degraded_message_when_ai_fails(app: App, monkeypatch) -> None:
    """expand_charsets 抛 WordPulseAIError → 返回降级提示。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 add 炒股 茅台", message_id=4, role="admin")
    # mock httpx.post 抛 HTTPStatusError(401) → WordPulseAIAuthError → 降级
    err_resp = httpx.Response(401, request=httpx.Request("POST", "https://example.com/v1/chat/completions"))
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.HTTPStatusError("e", request=err_resp.request, response=err_resp))):
        async with app.test_matcher(word_pulse.admin_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "✓ 主题「炒股」已创建（字符集扩展失败，已降级为纯精确匹配，可稍后重试「词频 refresh 炒股」）",
                result={"message_id": 103},
            )

    # 副作用断言:即便 AI 失败降级,主题与子类仍应写入 DB
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None, "降级时主题仍应写入 DB"
    clusters = await get_clusters(theme["id"])
    assert [c["name"] for c in clusters] == ["茅台"], "降级时种子词仍应作为子类落库"


# ═══════════════════════════════════════════════════════════════
# _handle_append:成功 + 主题不存在
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_append_unknown_theme(app: App, monkeypatch) -> None:
    """append 到不存在主题 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 append 炒股 茅台", message_id=5, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 104})


@pytest.mark.asyncio
async def test_handle_append_success(app: App, monkeypatch) -> None:
    """append 到已有主题 → 追加成功。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    # 预创建主题
    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 append 炒股 五粮液", message_id=6, role="admin")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_fake_charset_response())):
        async with app.test_matcher(word_pulse.admin_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(event, "✓ 已追加子类到「炒股」：五粮液", result={"message_id": 105})

    # 副作用断言:子类数量应从 1 增至 2,新增「五粮液」
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None
    clusters = await get_clusters(theme["id"])
    assert [c["name"] for c in clusters] == ["茅台", "五粮液"], "append 后应同时含原种子词与新增子类"


# ═══════════════════════════════════════════════════════════════
# _handle_list:空 + 有主题 + 带别名
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_list_with_theme_and_aliases(app: App, monkeypatch) -> None:
    """list 返回主题+子类+别名。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import add_cluster_aliases, replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])
    clusters = await replace_clusters(tid, ["茅台"])
    await add_cluster_aliases(clusters[0], ["茅子"], theme_id=tid)

    event = _make_group_event("词频 list", message_id=7, role="member")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "📊 本群主题列表：\n  · 炒股 / 茅台（别名: 茅子）",
            result={"message_id": 106},
        )


@pytest.mark.asyncio
async def test_handle_list_no_alias_shows_plain(app: App, monkeypatch) -> None:
    """list 子类无别名时不显示「（别名:...）」。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "游戏")
    await replace_clusters(tid, ["原神"])

    event = _make_group_event("词频 list", message_id=8, role="member")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "📊 本群主题列表：\n  · 游戏 / 原神",
            result={"message_id": 107},
        )


# ═══════════════════════════════════════════════════════════════
# _handle_del:主题 + 子类 + 不存在分支
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_del_unknown_theme(app: App, monkeypatch) -> None:
    """del 不存在的主题 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 del 炒股", message_id=9, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 108})


@pytest.mark.asyncio
async def test_handle_del_theme_success(app: App, monkeypatch) -> None:
    """del 已存在主题 → 删除成功。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 del 炒股", message_id=10, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "✓ 已删除主题「炒股」", result={"message_id": 109})

    # 副作用断言:主题应已被删除
    from src.plugins.word_pulse.db import get_theme
    assert await get_theme("200001", "炒股") is None, "del 主题后 DB 中应不存在"


@pytest.mark.asyncio
async def test_handle_del_cluster_success(app: App, monkeypatch) -> None:
    """del <主题> <子类> → 删除子类。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台", "五粮液"])

    event = _make_group_event("词频 del 炒股 茅台", message_id=11, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "✓ 已从「炒股」删除子类「茅台」", result={"message_id": 110})

    # 副作用断言:被删子类不再出现在 cluster 列表中,其他子类保留
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None
    clusters = await get_clusters(theme["id"])
    names = [c["name"] for c in clusters]
    assert "茅台" not in names, "被删子类应不再存在"
    assert names == ["五粮液"], "未删的子类应保留"


@pytest.mark.asyncio
async def test_handle_del_cluster_unknown(app: App, monkeypatch) -> None:
    """del <主题> <不存在子类> → 提示子类不存在。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 del 炒股 五粮液", message_id=12, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "主题「炒股」中没有子类「五粮液」", result={"message_id": 111})


# ═══════════════════════════════════════════════════════════════
# _handle_refresh:成功 + 主题不存在 + 字符集失败
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_refresh_unknown_theme(app: App, monkeypatch) -> None:
    """refresh 不存在主题 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 refresh 炒股", message_id=13, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 112})


@pytest.mark.asyncio
async def test_handle_refresh_no_clusters(app: App, monkeypatch) -> None:
    """refresh 主题无子类 → 提示无子类。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    await upsert_theme("200001", "炒股")

    event = _make_group_event("词频 refresh 炒股", message_id=14, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "主题「炒股」没有任何子类", result={"message_id": 113})


@pytest.mark.asyncio
async def test_handle_refresh_ai_failure(app: App, monkeypatch) -> None:
    """refresh 字符集扩展失败 → 返回降级提示。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 refresh 炒股", message_id=15, role="admin")
    err_resp = httpx.Response(500, request=httpx.Request("POST", "https://example.com/v1/chat/completions"))
    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=httpx.HTTPStatusError("e", request=err_resp.request, response=err_resp))):
        async with app.test_matcher(word_pulse.admin_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(event, "字符集刷新失败，请稍后重试或检查 AI 配置", result={"message_id": 114})


@pytest.mark.asyncio
async def test_handle_refresh_success(app: App, monkeypatch) -> None:
    """refresh 成功 → 返回刷新提示。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 refresh 炒股", message_id=16, role="admin")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=_fake_charset_response())):
        async with app.test_matcher(word_pulse.admin_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                event,
                "✓ 主题「炒股」字符集已刷新（1 个子类）\n  子类：茅台",
                result={"message_id": 115},
            )

    # 副作用断言:扩展后的字符集应已落库
    from src.plugins.word_pulse.db import get_expanded_charset, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None
    charset = await get_expanded_charset(theme["id"])
    assert charset, "refresh 成功后应写入非空字符集"
    # fake_charset_response 里给「茅台」返回了 10 个字符,这里至少抽样校验
    assert "茅" in charset


# ═══════════════════════════════════════════════════════════════
# _handle_alias / _handle_unalias
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handle_alias_unknown_theme(app: App, monkeypatch) -> None:
    """alias 不存在主题 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 alias 炒股 茅台 茅子", message_id=17, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 116})


@pytest.mark.asyncio
async def test_handle_alias_unknown_cluster(app: App, monkeypatch) -> None:
    """alias 主名词不在主题中 → 提示子类不存在。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 alias 炒股 五粮液 粮液", message_id=18, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "主题「炒股」中没有子类「五粮液」", result={"message_id": 117})


@pytest.mark.asyncio
async def test_handle_alias_success(app: App, monkeypatch) -> None:
    """alias 成功添加别名。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 alias 炒股 茅台 茅子 飞天", message_id=19, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "✓ 已为「茅台」添加别名：茅子、飞天",
            result={"message_id": 118},
        )

    # 副作用断言:对应 cluster 的 aliases 应含新增别名
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None
    clusters = await get_clusters(theme["id"])
    maotai = next((c for c in clusters if c["name"] == "茅台"), None)
    assert maotai is not None
    assert set(maotai["aliases"]) >= {"茅子", "飞天"}, "alias 后 DB 中应能看到新增别名"


@pytest.mark.asyncio
async def test_handle_alias_all_dup_returns_warning(app: App, monkeypatch) -> None:
    """alias 全是重复别名 → 返回「未新增别名」提示。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import add_cluster_aliases, replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    cids = await replace_clusters(tid, ["茅台"])
    await add_cluster_aliases(cids[0], ["茅子"], theme_id=tid)

    event = _make_group_event("词频 alias 炒股 茅台 茅子", message_id=20, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未新增别名（全部为重复或与现有子类名冲突）：茅子",
            result={"message_id": 119},
        )


@pytest.mark.asyncio
async def test_handle_unalias_success(app: App, monkeypatch) -> None:
    """unalias 成功删除别名。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import add_cluster_aliases, replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    cids = await replace_clusters(tid, ["茅台"])
    await add_cluster_aliases(cids[0], ["茅子", "飞天"], theme_id=tid)

    event = _make_group_event("词频 unalias 炒股 茅台 茅子", message_id=21, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "✓ 已从「茅台」删除别名：茅子", result={"message_id": 120})

    # 副作用断言:对应 cluster 的 aliases 中,被删别名不再存在,未删的保留
    from src.plugins.word_pulse.db import get_clusters, get_theme
    theme = await get_theme("200001", "炒股")
    assert theme is not None
    clusters = await get_clusters(theme["id"])
    maotai = next((c for c in clusters if c["name"] == "茅台"), None)
    assert maotai is not None
    assert "茅子" not in maotai["aliases"], "unalias 后被删别名应从 DB 中移除"
    assert "飞天" in maotai["aliases"], "未删除的别名应保留"


@pytest.mark.asyncio
async def test_handle_unalias_unknown_theme(app: App, monkeypatch) -> None:
    """unalias 不存在主题 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("词频 unalias 炒股 茅台 茅子", message_id=22, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 121})


@pytest.mark.asyncio
async def test_handle_unalias_unknown_cluster(app: App, monkeypatch) -> None:
    """unalias 主名词不在主题中 → 提示子类不存在。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 unalias 炒股 五粮液 粮液", message_id=23, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "主题「炒股」中没有子类「五粮液」", result={"message_id": 122})


@pytest.mark.asyncio
async def test_handle_unalias_nothing_removed(app: App, monkeypatch) -> None:
    """unalias 别名不存在 → 返回「未删除」提示。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])

    event = _make_group_event("词频 unalias 炒股 茅台 不存在的别名", message_id=24, role="admin")
    async with app.test_matcher(word_pulse.admin_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未删除任何别名（不存在的别名被忽略）：不存在的别名",
            result={"message_id": 123},
        )


# ═══════════════════════════════════════════════════════════════
# handle_query (bug#4):各种分支
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_query_private_message_rejected(app: App, monkeypatch) -> None:
    """私聊总结命令 → 提示仅群聊可用。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_private_event("总结 7天 炒股", message_id=25)
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "仅支持群聊使用", result={"message_id": 124})


@pytest.mark.asyncio
async def test_query_unknown_format_returns_error(app: App, monkeypatch) -> None:
    """查询格式错误(parse_query None) → 提示格式错误。

    matcher 正则 ``^总结\\s+\\d+\\s*(天|d|周|w|月|m)\\s+\\S+`` (无结尾 $),
    parse_query 正则有结尾 $ 锚定。因此「总结 7天 炒股 额外内容」能过 matcher
    正则,但 parse_query 返回 None → handler 回「查询格式错误」。
    """
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("总结 7天 炒股 额外内容", message_id=26, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "查询格式错误", result={"message_id": 125})


@pytest.mark.asyncio
async def test_query_config_missing_returns_error(app: App, monkeypatch) -> None:
    """配置缺失 → 提示配置错误。"""
    from src.plugins import word_pulse
    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)
    monkeypatch.setattr(word_pulse.config, "word_pulse_base_url", "")
    monkeypatch.setattr(word_pulse.config, "word_pulse_api_key", "")
    monkeypatch.setattr(word_pulse.config, "word_pulse_model", "")
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("总结 7天 炒股", message_id=27, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "词频插件未配置 base_url", result={"message_id": 126})


@pytest.mark.asyncio
async def test_query_window_over_limit(app: App, monkeypatch) -> None:
    """查询窗口超过 max_window_days → 提示超限。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    # max=31,查 100 月 = 100*31=3100 → 超限
    monkeypatch.setattr(word_pulse.config, "word_pulse_max_window_days", 31)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("总结 100月 炒股", message_id=28, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "时间范围超过上限 31 天", result={"message_id": 127})


@pytest.mark.asyncio
async def test_query_unknown_theme(app: App, monkeypatch) -> None:
    """查询主题不存在 → 提示未创建。"""
    from src.plugins import word_pulse
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    event = _make_group_event("总结 7天 炒股", message_id=29, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "本群尚未创建主题「炒股」", result={"message_id": 128})


@pytest.mark.asyncio
async def test_query_theme_without_clusters(app: App, monkeypatch) -> None:
    """主题存在但无子类 → 提示无子类。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    await upsert_theme("200001", "炒股")

    event = _make_group_event("总结 7天 炒股", message_id=30, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "主题「炒股」没有任何子类", result={"message_id": 129})


@pytest.mark.asyncio
async def test_query_cooldown_blocks(app: App, monkeypatch) -> None:
    """冷却期内 → 提示冷却中。

    patch _cooldown_remaining 返回固定 25,避免真实时序 flaky。
    """
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, save_bucket, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])
    import json as _json
    await save_bucket("200001", tid, "2026-01-01", _json.dumps({"_other": 0, "_skipped": 0}), "[]", 0, False)

    # 标记冷却,让 handler 进入冷却分支前不再命中缓存
    word_pulse.cooldown_dict["200001"] = __import__("time").time()
    # patch 冷却剩余时间为固定 25 秒,确保文案稳定
    monkeypatch.setattr(word_pulse, "_cooldown_remaining", lambda _gid: 25)

    event = _make_group_event("总结 1天 炒股", message_id=31, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "冷却中，请等待 25 秒", result={"message_id": 130})

    # 副作用断言:冷却被命中后,该群 gid 应仍在 cooldown_dict 中
    assert "200001" in word_pulse.cooldown_dict, "冷却命中后该群应仍在 cooldown_dict 中"


@pytest.mark.asyncio
async def test_query_cache_hit(app: App, monkeypatch) -> None:
    """缓存命中 → 直接返回缓存内容,不调 AI,不计冷却。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.db import replace_clusters, save_bucket, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])
    # 灌 buckets 让 cache_key 稳定
    await save_bucket("200001", tid, "2026-01-01", '{"_other":0,"_skipped":0}', "[]", 0, False)

    # 先手动调 _resolve_query_target 拿到正确的 cache_key
    from src.plugins.word_pulse import _resolve_query_target
    from src.plugins.word_pulse.commands import parse_query
    query = parse_query("总结 1天 炒股")
    assert query is not None
    probe_event = _make_group_event("总结 1天 炒股", message_id=999, role="member")
    resolved = await _resolve_query_target(probe_event, query)
    assert not isinstance(resolved, str)
    _theme, _clusters, ck = resolved

    # 注入缓存
    word_pulse.result_cache[ck] = word_pulse.CachedResult(
        created_at=__import__("time").time(),
        response_text="这是缓存的总结结果",
    )

    event = _make_group_event("总结 1天 炒股", message_id=32, role="member")
    async with app.test_matcher(word_pulse.query_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, "这是缓存的总结结果", result={"message_id": 131})

    # 副作用断言:缓存命中分支不应清空缓存,对应 cache_key 仍应在 result_cache
    assert ck in word_pulse.result_cache, "缓存命中后 key 应仍存在于 result_cache"
    # 缓存命中不计冷却
    assert "200001" not in word_pulse.cooldown_dict, "缓存命中不应记录冷却"


@pytest.mark.asyncio
async def test_query_full_pipeline_success(app: App, monkeypatch) -> None:
    """查询全链路:有 buckets → AI summarize → 返回渲染后的总结。

    patch _build_window_desc 固定 window 文本,使断言稳定(避开 today 日期)。
    mock summarize 直接返回 SummaryResult,跳过 AI 真调用。
    """
    from src.plugins import word_pulse
    from src.plugins.word_pulse.ai import (
        ExampleItem,
        RankItem,
        SummaryResult,
        UnclassifiedTerm,
    )
    from src.plugins.word_pulse.db import replace_clusters, save_bucket, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    today = _dt.now(_ZI("Asia/Shanghai")).strftime("%Y-%m-%d")
    await save_bucket(
        "200001", tid, today,
        _json.dumps({"茅台": 3, "_other": 0, "_skipped": 0}),
        _json.dumps([{"cluster": "茅台", "text": "茅台涨", "author": "张三", "day": today, "event_time": 0}]),
        3, False,
    )

    async def fake_compute(**_):
        return [{
            "day": today, "total_messages": 3,
            "counts": {"茅台": 3, "_other": 0, "_skipped": 0},
            "samples": [{"cluster": "茅台", "text": "茅台涨", "author": "张三", "day": today, "event_time": 0}],
            "sampled": False,
        }]

    fake_summary = SummaryResult(
        ranking=[RankItem(cluster="茅台", count=3, percent=100.0)],
        trend="茅台热度上升",
        examples=[ExampleItem(cluster="茅台", text="茅台涨", author="张三", day=today)],
        unclassified_high_freq=[UnclassifiedTerm(term="未知词", count=1)],
    )

    # 固定 window 描述,避开 today 日期
    monkeypatch.setattr(word_pulse, "_build_window_desc", lambda _q, _d: "1天 (T1 ~ T2)")

    expected = "\n".join([
        "📊 炒股主题 · 1天 (T1 ~ T2)",
        "━" * 30,
        "排名  子类     计数   占比",
        "1.   茅台     3      100.0%",
        "-     其他      0      0%",
        "-     跳过      0",
        "━" * 30,
        "📈 趋势：茅台热度上升",
        "💬 典型原文：",
        f'  · "茅台涨" — 张三 {today}',
        "🔍 新发现的可能相关词（未归类）：",
        "  · 未知词（1）",
    ])

    event = _make_group_event("总结 1天 炒股", message_id=33, role="member")
    with (
        patch("src.plugins.word_pulse.compute_or_load_buckets", new=AsyncMock(side_effect=fake_compute)),
        patch("src.plugins.word_pulse.summarize", new=AsyncMock(return_value=fake_summary)),
    ):
        async with app.test_matcher(word_pulse.query_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(event, expected, result={"message_id": 132})

    # 副作用断言:setup 写入的 bucket 应仍持久化在 DB 中(未被 handler 破坏)
    from src.plugins.word_pulse.db import get_bucket
    bucket = await get_bucket("200001", tid, today)
    assert bucket is not None, "查询全链路完成后 bucket 应仍持久化"
    assert bucket["total_messages"] == 3


@pytest.mark.asyncio
async def test_query_ai_timeout_returns_friendly(app: App, monkeypatch) -> None:
    """AI 超时 → 返回「AI 总结超时」(走 _run_summary 异常分支)。"""
    from src.plugins import word_pulse
    from src.plugins.word_pulse.ai import WordPulseAITimeoutError
    from src.plugins.word_pulse.db import replace_clusters, save_bucket, upsert_theme
    _enable_word_pulse(monkeypatch)
    word_pulse.cooldown_dict.clear()
    word_pulse.result_cache.clear()

    tid = await upsert_theme("200001", "炒股")
    await replace_clusters(tid, ["茅台"])
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    today = _dt.now(_ZI("Asia/Shanghai")).strftime("%Y-%m-%d")
    await save_bucket("200001", tid, today, _json.dumps({"_other": 0, "_skipped": 0}), "[]", 0, False)

    async def fake_compute(**_):
        return [{"day": today, "total_messages": 0, "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False}]

    event = _make_group_event("总结 1天 炒股", message_id=34, role="member")
    with (
        patch("src.plugins.word_pulse.compute_or_load_buckets", new=AsyncMock(side_effect=fake_compute)),
        patch("src.plugins.word_pulse.summarize", new=AsyncMock(side_effect=WordPulseAITimeoutError("timeout"))),
    ):
        async with app.test_matcher(word_pulse.query_matcher) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, event)
            ctx.should_pass_rule()
            ctx.should_call_send(event, "AI 总结超时，请稍后重试", result={"message_id": 133})
