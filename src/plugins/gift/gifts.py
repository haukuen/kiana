"""QQ 礼物清单（移植自 bl-chat-plugin 的 sendGiftTool）。"""

from __future__ import annotations

import random
from dataclasses import dataclass

from nonebot import logger


@dataclass(frozen=True, slots=True)
class Gift:
    """单个礼物的协议参数。"""

    key: str
    name: str
    coin_value: int
    tianquan_id: int
    display_value: int


# 顺序与上游 giftConfigs 一致，随机池以此为基准。
GIFTS: dict[str, Gift] = {
    "champagne": Gift("champagne", "香槟", 182, 2179, 199999),
    "hammer": Gift("hammer", "风暴战锤", 1388, 2025, 1388),
    "space": Gift("space", "遨游太空", 1888, 1682, 1888),
    "party": Gift("party", "蹦迪派对", 2999, 2026, 2999),
    "camping": Gift("camping", "露营", 388, 2023, 388),
    "dragon": Gift("dragon", "龙腾万里", 11888, 1633, 199999),
    "supercar": Gift("supercar", "超级跑车", 1314, 2027, 1314),
    "helicopter": Gift("helicopter", "直升机", 18880, 1000000110, 18888),
}

GIFT_KEYS: tuple[str, ...] = tuple(GIFTS)


def available_gifts(configured: list[str]) -> list[Gift]:
    """挑出当前可送的礼物。

    `configured` 为空表示不限制；含未知 key 时告警并忽略该 key，
    全部 key 都无效时返回空列表（而不是回退到全部礼物）。
    """
    if not configured:
        return list(GIFTS.values())

    unknown = [key for key in configured if key not in GIFTS]
    if unknown:
        logger.warning(f"[gift] 未知的礼物 key: {unknown}，可选值: {list(GIFT_KEYS)}")

    return [GIFTS[key] for key in dict.fromkeys(configured) if key in GIFTS]


def pick_gift(configured: list[str]) -> Gift | None:
    """从可送的礼物中随机挑一个，池为空时返回 None。"""
    pool = available_gifts(configured)
    if not pool:
        return None
    # 随机礼物只是娱乐用途，不涉及安全场景
    return random.choice(pool)  # noqa: S311
