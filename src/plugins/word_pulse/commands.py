from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedCommand:
    action: str  # add / append / list / del / refresh / alias / unalias
    theme: str | None = None
    seeds: list[str] | None = None


@dataclass(slots=True)
class ParsedQuery:
    window_value: int
    window_unit: str
    theme: str


_PREFIX = "词频"
_QUERY_P = "总结"

_CMD_ADD = re.compile(rf"^{_PREFIX}\s+add\s+(\S+)\s+(.+)$")
_CMD_APPEND = re.compile(rf"^{_PREFIX}\s+append\s+(\S+)\s+(.+)$")
_CMD_LIST = re.compile(rf"^{_PREFIX}\s+list$")
_CMD_DEL_CLUSTER = re.compile(rf"^{_PREFIX}\s+del\s+(\S+)\s+(\S+)$")
_CMD_DEL_THEME = re.compile(rf"^{_PREFIX}\s+del\s+(\S+)$")
_CMD_REFRESH = re.compile(rf"^{_PREFIX}\s+refresh\s+(\S+)$")
# alias / unalias: 主题 主名词 别名1 [别名2 ...] —— 至少 3 个 token（含命令字）
_CMD_ALIAS = re.compile(rf"^{_PREFIX}\s+alias\s+(\S+)\s+(\S+)\s+(.+)$")
_CMD_UNALIAS = re.compile(rf"^{_PREFIX}\s+unalias\s+(\S+)\s+(\S+)\s+(.+)$")
_QUERY_RE = re.compile(rf"^{_QUERY_P}\s+(\d+)\s*(天|d|周|w|月|m)\s+(\S+)$")


def parse_command(text: str) -> ParsedCommand | None:  # noqa: PLR0911
    m = _CMD_DEL_CLUSTER.match(text)
    if m:
        return ParsedCommand(action="del", theme=m.group(1), seeds=[m.group(2)])
    m = _CMD_DEL_THEME.match(text)
    if m:
        return ParsedCommand(action="del", theme=m.group(1))
    m = _CMD_ALIAS.match(text)
    if m:
        # 主名词 + 至少一个别名
        aliases = [s for s in re.split(r"\s+", m.group(3).strip()) if s]
        return ParsedCommand(action="alias", theme=m.group(1), seeds=[m.group(2), *aliases])
    m = _CMD_UNALIAS.match(text)
    if m:
        aliases = [s for s in re.split(r"\s+", m.group(3).strip()) if s]
        return ParsedCommand(action="unalias", theme=m.group(1), seeds=[m.group(2), *aliases])
    m = _CMD_ADD.match(text)
    if m:
        seeds = [s for s in re.split(r"\s+", m.group(2).strip()) if s]
        return ParsedCommand(action="add", theme=m.group(1), seeds=seeds)
    m = _CMD_APPEND.match(text)
    if m:
        seeds = [s for s in re.split(r"\s+", m.group(2).strip()) if s]
        return ParsedCommand(action="append", theme=m.group(1), seeds=seeds)
    m = _CMD_LIST.match(text)
    if m:
        return ParsedCommand(action="list")
    m = _CMD_REFRESH.match(text)
    if m:
        return ParsedCommand(action="refresh", theme=m.group(1))
    return None


def parse_query(text: str) -> ParsedQuery | None:
    m = _QUERY_RE.match(text)
    if not m:
        return None
    return ParsedQuery(window_value=int(m.group(1)), window_unit=m.group(2), theme=m.group(3))


def resolve_window_days(value: int, unit: str, max_days: int) -> int | None:
    multiplier = {"天": 1, "d": 1, "周": 7, "w": 7, "月": 31, "m": 31}.get(unit)
    if multiplier is None:
        return None
    total = value * multiplier
    return None if total > max_days else total
