"""本次 4 个 bug 修复 + help 命令的集中端到端集成验证。

覆盖场景:
1. **bug#1**:`炼化这个功能怎么用`(无空格粘连)不应触发任何炼化命令 — force_whitespace=True
2. **bug#1 对照**:`炼化 <不存在标签>`(有空格)正常进入 handler 并回「未找到标签」
3. **bug#2**:集合订阅炼化时,所有成员的发言都被采到 prompt 里(per-member 配额)
4. **bug#3**:word_pulse 的 AI 调用走 `response_format: {type: "json_object"}`,
   不带 strict json_schema(通过 mock httpx 捕获请求体验证)
5. **help**:`词频 帮助` 与 `词频 help` 都能触发并返回完整帮助文案

复用 conftest.py 的 `App` fixture 与 autouse 的 `reset_*` 表清理 fixture。
辅助函数(`_make_group_event` / `_expect_bot_not_muted` / `_fake_ai_response` /
`_fake_dt`)从 test_refine_integration.py 拷贝过来,避免跨文件 import 测试辅助。

不修改任何源代码、不修改其他测试。
"""

from __future__ import annotations

import json as json_lib
from datetime import datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebug import App

# ── 本地辅助工厂(从 test_refine_integration.py 拷贝,保持一致) ──


def _make_group_event(
    message: Message | str,
    *,
    message_id: int = 1,
    user_id: int = 100001,
    group_id: int = 200001,
    self_id: int = 987654321,
    nickname: str = "测试用户",
    card: str = "",
    event_time: int | None = None,
) -> GroupMessageEvent:
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
        sender=Sender(user_id=user_id, nickname=nickname, card=card, role="member"),
    )


def _expect_bot_not_muted(
    ctx, group_id: int = 200001, self_id: int = 987654321
) -> None:
    """声明 bot 不被禁言 — should_call_send 之前必须调用。

    项目 ``check_bot_mute_status`` preprocessor 在群消息场景下会查 mute cache,
    触发 get_group_member_info API。未先声明会导致 nonebug 报意外 API 调用。
    """
    ctx.should_call_api(
        "get_group_member_info",
        {"group_id": group_id, "user_id": self_id, "no_cache": True},
        result={"shut_up_timestamp": 0},
    )


def _fake_ai_response(content: str = "AI 生成的总结") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"role": "assistant", "content": content}},
            ],
        },
        request=httpx.Request("POST", "https://example.com/v1/chat/completions"),
    )


def _fake_dt():
    """返回一个 strftime 始终输出 'T' 的假 datetime 实例。"""
    return type("FakeDS", (), {"strftime": lambda self, fmt: "T"})()


def _configure_refine_plugin_for_e2e(rp) -> None:
    """把 refine 的 config 全设成可用值。

    对齐 test_refine_integration.py 的 `_reset_refine_config` autouse fixture,
    但本文件不依赖那个 fixture(独立于 test_refine_integration.py 运行)。
    """
    rp.config.refine_plugin_enabled = True
    rp.config.refine_group_mode = "all"
    rp.config.refine_group_whitelist = []
    rp.config.refine_group_blacklist = []
    rp.config.refine_ai_base_url = "https://example.com/v1"
    rp.config.refine_ai_api_key = "sk-test"
    rp.config.refine_ai_model = "gpt-test"
    rp.config.refine_ai_timeout_seconds = 30.0
    rp.config.refine_ai_temperature = 0.3
    rp.config.refine_result_fresh_seconds = 86400
    rp.config.refine_query_cooldown_seconds = 60
    rp.config.refine_lookback_hours = 24
    rp.config.refine_max_messages_per_target = 200
    rp.config.refine_max_prompt_chars = 12000
    rp.config.refine_min_messages_to_refine = 2


# ═══════════════════════════════════════════════════════════════
# 场景 1: bug#1 — `炼化这个功能怎么用` 不触发炼化命令
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bug1_lazy_ignores_glued_text(app: App) -> None:
    """bug#1: `炼化这个功能怎么用`(无空格粘连)不应触发炼化命令。

    复现路径:历史 bug 下 COMMAND_START 含空字符串,会把 `炼化这个功能怎么用`
    错解析为 `炼化` 命令 + arg="这个功能怎么用",再回「未找到标签」。

    修复:commands.py 给所有 on_command 加 force_whitespace=True。
    本用例验证 rule 层就拦下,handler 完全不进入(无任何回包)。
    """
    import src.plugins.refine as rp  # noqa: PLC0415
    from src.plugins.refine import commands  # noqa: PLC0415

    _configure_refine_plugin_for_e2e(rp)

    event = _make_group_event("炼化这个功能怎么用", message_id=1)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        ctx.receive_event(bot, event)
        # should_not_pass_rule = rule 拦下,不进入 handler,不调 mute API,不回包
        ctx.should_not_pass_rule()


# ═══════════════════════════════════════════════════════════════
# 场景 2: bug#1 对照 — 有空格正常触发
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bug1_lazy_works_with_space(app: App) -> None:
    """bug#1 对照: `炼化 <不存在标签>`(有空格)正常进入 handler。

    虽然订阅不存在,但 handler 会回复「未找到标签为「...」的订阅」,
    证明命令字 + 空格 + 参数的路径正常走通,force_whitespace 没误伤。
    """
    import src.plugins.refine as rp  # noqa: PLC0415
    from src.plugins.refine import commands  # noqa: PLC0415

    _configure_refine_plugin_for_e2e(rp)

    event = _make_group_event("炼化 不存在的标签", message_id=2)
    async with app.test_matcher(commands.refine_lazy) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            event,
            "未找到标签为「不存在的标签」的订阅",
            result={"message_id": 100},
        )


# ═══════════════════════════════════════════════════════════════
# 场景 3: bug#2 — 集合炼化采到所有成员发言
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bug2_collection_refine_covers_all_members(app: App) -> None:
    """bug#2: 集合炼化时所有成员的发言都被采到 prompt。

    场景:集合 3 成员各发 5 条,共 15 条,走「订阅 → 炼化」完整命令链路。
    通过 mock httpx.AsyncClient.post 捕获 AI 请求体,反查 user prompt
    包含三个成员各自的发言。

    说明:15 条远小于 max_messages=200,即便旧实现(per-member 配额前的共享
    预算)也能采全 — 这是 oracle 指出的"现有测试测不出 bug"的原因。本用例
    专注于端到端验证修复后行为正常,而非回归(回归测试在 test_refine_
    integration.py 的 test_collect_distributes_quota_per_member_not_shared)。
    """
    import src.plugins.refine as rp  # noqa: PLC0415
    from src import plugins as global_plugins  # noqa: PLC0415
    from src.plugins.message_archive.db import archive_message_event  # noqa: PLC0415
    from src.plugins.refine import commands  # noqa: PLC0415
    from src.plugins.un_nickname.db import add_collection_members  # noqa: PLC0415

    _configure_refine_plugin_for_e2e(rp)

    fixed_now = 1_800_000_000

    # 1. 建集合 3 成员
    await add_collection_members("200001", "bug2集合", ["111", "222", "333"])

    # 2. 灌消息:每人 5 条(用 user 区分,nickname 用「成员<uid>」便于断言)
    # 时间必须都 < fixed_now(lookback 窗口 = [now-24h, now]),否则被过滤
    mid_base = 100
    for uid in (111, 222, 333):
        for i in range(5):
            # 每成员 5 条,顺序错开 50s,整体落在 fixed_now 前 100~10000s
            ts = fixed_now - 10000 + (uid % 100) * 200 + i * 50
            await archive_message_event(
                _make_group_event(
                    f"成员{uid}发言{i}",
                    user_id=uid,
                    group_id=200001,
                    message_id=mid_base,
                    event_time=ts,
                    nickname=f"成员{uid}",
                )
            )
            mid_base += 1

    # 3. 订阅命令 — collection:bug2集合
    subscribe_event = _make_group_event(
        "炼化订阅 bug2test collection:bug2集合", message_id=200
    )
    async with app.test_matcher(commands.refine_subscribe) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx)
        ctx.receive_event(bot, subscribe_event)
        ctx.should_pass_rule()
        ctx.should_call_send(
            subscribe_event,
            (
                "✅ 已订阅 [bug2test] (集合=bug2集合)\n"
                "发送 `炼化 bug2test` 查看结果（首次查询会触发提炼）"
            ),
            result={"message_id": 201},
        )

    # 4. 炼化命令 — patch httpx 捕获 prompt
    captured_prompt: list[str] = []

    async def capture_post(url, headers=None, json=None, **kwargs):
        if json and "messages" in json:
            # messages[1] 是 user prompt,内含采集到的原文
            captured_prompt.append(json["messages"][1]["content"])
        return _fake_ai_response("三人综合总结")

    lazy_event = _make_group_event(
        "炼化 bug2test", message_id=201, event_time=fixed_now
    )
    # mute cache 在第一个 matcher 后可能被填充,手动清掉保证第二个 matcher
    # 的 expect_bot_not_muted 被正确消费
    global_plugins._mute_cache.clear()

    fake_dt = _fake_dt()
    with (
        patch(
            "httpx.AsyncClient.post",
            new=AsyncMock(side_effect=capture_post),
        ),
        patch.object(commands, "datetime") as mock_dt,
        patch("time.time", return_value=float(fixed_now)),
    ):
        mock_dt.fromtimestamp.return_value = fake_dt
        async with app.test_matcher(commands.refine_lazy) as ctx:
            bot = ctx.create_bot(base=Bot, self_id="987654321")
            _expect_bot_not_muted(ctx)
            ctx.receive_event(bot, lazy_event)
            ctx.should_pass_rule()
            ctx.should_call_send(
                lazy_event,
                "⏳ 正在为 [bug2test] 提炼，请稍候...",
                result={"message_id": 202},
            )
            ctx.should_call_send(
                lazy_event,
                (
                    "🧪 炼化结果：[bug2test]\n"
                    "目标：集合=bug2集合\n"
                    "集合成员：3 人\n"
                    "采样窗口：T ~ T\n"
                    "采样消息：15 条\n"
                    "模型：gpt-test\n"
                    "\n"
                    "三人综合总结"
                ),
                result={"message_id": 203},
            )

    # 关键断言:prompt 里包含所有 3 个成员的发言
    assert len(captured_prompt) == 1, f"AI 应只被调用一次,实际 {len(captured_prompt)}"
    prompt_text = captured_prompt[0]
    assert "成员111" in prompt_text, f"成员111 缺失: {prompt_text}"
    assert "成员222" in prompt_text, f"成员222 缺失: {prompt_text}"
    assert "成员333" in prompt_text, f"成员333 缺失: {prompt_text}"


# ═══════════════════════════════════════════════════════════════
# 场景 4: bug#3 — word_pulse AI 用 json_object 而非 strict json_schema
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_bug3_word_pulse_uses_json_object_not_strict(app: App) -> None:
    """bug#3: word_pulse AI 调用直接用 response_format=json_object,不走 strict。

    修复背景:原 strict json_schema + json_object 两级降级对部分上游 OpenAI
    兼容网关不兼容(strict 首次即 400),修复为单一 json_object + pydantic 校验。

    验证:patch httpx.AsyncClient.post 捕获请求体,断言:
    - response_format == {"type": "json_object"}
    - 不带 strict 字段
    - response_format 内不含 json_schema 字段

    直接调用 expand_charsets 绕过 matcher,聚焦 AI 调用层。
    """
    from src.plugins.word_pulse.ai import expand_charsets  # noqa: PLC0415

    captured_body: dict = {}

    async def mock_post(url, headers=None, json=None, **kwargs):
        captured_body.update(json or {})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json_lib.dumps(
                                {
                                    "charsets": [
                                        {
                                            "cluster": "测试种子",
                                            "chars": ["字"] * 5,
                                        }
                                    ]
                                }
                            ),
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=mock_post)):
        result = await expand_charsets(
            base_url="https://example.com/v1",
            api_key="sk-test",
            model="gpt-test",
            seeds=["测试种子"],
            theme="测试主题",
        )

    # expand_charsets 返回 {cluster: chars} 字典
    assert "测试种子" in result

    # ── 关键断言:response_format 正确,不走 strict ──
    rf = captured_body.get("response_format")
    assert isinstance(rf, dict), f"response_format 必须是 dict,实际: {rf!r}"
    assert rf == {"type": "json_object"}, (
        f"response_format 必须是 {{'type': 'json_object'}},实际: {rf}"
    )
    # 顶层不能有 strict 痕迹
    assert "strict" not in captured_body, (
        f"请求体不应有 strict 字段,实际 keys: {list(captured_body.keys())}"
    )
    # response_format 内不能有 json_schema(那是 strict 模式专用的)
    assert "json_schema" not in rf, f"response_format 不应含 json_schema: {rf}"


# ═══════════════════════════════════════════════════════════════
# 场景 5: help — `词频 帮助` / `词频 help` 都触发并返回完整文案
# ═══════════════════════════════════════════════════════════════


# 与 word_pulse.handle_help 内联文案保持一致(独立常量,避免源码改动时文案
# 微调让本测试 flaky)。如果未来 handle_help 文案变化,这里同步更新即可。
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


@pytest.mark.asyncio
async def test_help_command_triggers_for_word_pulse_help_cn(
    app: App, monkeypatch
) -> None:
    """help: `词频 帮助` 触发并返回完整帮助文案。"""
    from src.plugins import word_pulse  # noqa: PLC0415

    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)

    event = _make_group_event("词频 帮助", message_id=500, group_id=300001)
    async with app.test_matcher(word_pulse.help_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx, group_id=300001)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, EXPECTED_HELP_TEXT, result={"message_id": 501})


@pytest.mark.asyncio
async def test_help_command_triggers_for_word_pulse_help_en(
    app: App, monkeypatch
) -> None:
    """help: `词频 help`(英文别名)同样触发并返回完整文案。"""
    from src.plugins import word_pulse  # noqa: PLC0415

    monkeypatch.setattr(word_pulse.config, "word_pulse_plugin_enabled", True)

    event = _make_group_event("词频 help", message_id=501, group_id=300001)
    async with app.test_matcher(word_pulse.help_matcher) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="987654321")
        _expect_bot_not_muted(ctx, group_id=300001)
        ctx.receive_event(bot, event)
        ctx.should_pass_rule()
        ctx.should_call_send(event, EXPECTED_HELP_TEXT, result={"message_id": 502})
