"""词频插件命令解析测试。

覆盖：add / append / list / del / refresh / alias / unalias 七个子命令。

通过 nonebug `app` fixture 触发 NoneBot 初始化，避免 collect 阶段触发
插件包 __init__.py 链式导入时报 driver 未初始化错误。
"""

from __future__ import annotations

import pytest
from nonebug import App


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
