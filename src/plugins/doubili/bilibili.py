import html
import json
import re
from urllib.parse import unquote

from httpx import AsyncClient
from nonebot import get_plugin_config, logger

from .config import Config

config = get_plugin_config(Config)


async def get_redirect_url(url: str, headers: dict) -> str:
    """获取重定向后的URL"""
    async with AsyncClient(follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
        return str(response.url)


# BV 号编解码常量（新版算法 2024+）
_BV_TABLE = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_BV_TR = {_BV_TABLE[i]: i for i in range(58)}
_BV_S = [0, 1, 2, 9, 7, 5, 6, 4, 8, 3, 10, 11]  # 12位全编码映射
_BV_XOR = 23442827791579
_BV_MASK = 2251799813685247  # (1 << 51) - 1
_BV_MAX = 2251799813685248  # 1 << 51

# JSON 小程序消息正则（限制长度防止 ReDoS 攻击）
_JSON_CQ_PATTERN = re.compile(r"\[CQ:json,data=([^\]]{1,10000})\]")


def normalize_video_id(text: str) -> str:
    """标准化视频ID的大小写

    将文本中的 BV/AV 号统一为标准格式：
    - BV号：前缀大写 BV + 10位 Base58 字符（保持原样）
    - AV号：前缀小写 av + 数字

    注意：Base58 字符本身严格区分大小写，不应修改。
    例如：'c' 和 'C' 在 Base58 编码中是不同的字符。

    Args:
        text: 包含视频ID的文本

    Returns:
        标准化后的文本

    Examples:
        >>> normalize_video_id("bv17hCTBxE6L")
        "BV17hCTBxE6L"
        >>> normalize_video_id("AV170001")
        "av170001"
        >>> normalize_video_id("Bv17hCTBxE6L")
        "BV17hCTBxE6L"
    """
    # 标准化 BV 号前缀为大写（保持 Base58 字符原样）
    # 使用 (?i) 标志进行大小写不敏感匹配前缀
    text = re.sub(
        r"\b[Bb][Vv]([FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf]{10})\b",
        r"BV\1",
        text,
    )

    # 标准化 AV 号前缀为小写，直接返回结果
    return re.sub(r"\b[Aa][Vv](\d{6,})\b", r"av\1", text)


def is_valid_bvid(bvid: str) -> bool:
    """验证 BV 号是否合法（新版算法）

    使用 2024 年新版 BV-AV 互转算法验证 BV 号的有效性。
    新版算法取消了固定位限制，使用 12 位全编码。

    Args:
        bvid: BV 号字符串（例如：BV17hCTBxE6L）

    Returns:
        bool: True 表示合法的 BV 号，False 表示非法

    Examples:
        >>> is_valid_bvid("BV17hCTBxE6L")  # 新版格式
        True
        >>> is_valid_bvid("BVinvalidbvid")  # 非法字符
        False
    """
    # 1. 长度检查
    if len(bvid) != 12:
        return False

    # 2. 前缀检查
    if not bvid.startswith("BV"):
        return False

    # 3. 字符集检查（所有字符必须在新版 Base58 表中）
    for char in bvid[2:]:  # 跳过 "BV" 前缀
        if char not in _BV_TR:
            return False

    # 4. 解码验证（尝试转换为 AV 号）
    try:
        r = 0
        for i in range(3, 12):
            r = r * 58 + _BV_TR[bvid[_BV_S[i]]]

        aid = (r & _BV_MASK) ^ _BV_XOR

        # AV 号必须是正整数
        return aid > 0
    except (KeyError, ValueError):
        return False


# Base58 字符集（新版）
_BASE58_CHARS = r"[FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf]"

# BV/AV 号核心正则模式（支持大小写不敏感匹配）
_BV_PATTERN_STR = rf"[Bb][Vv]{_BASE58_CHARS}{{10}}"
_AV_PATTERN_STR = r"[Aa][Vv](\d{6,})"

# 编译的正则对象（用于内部搜索）
_BV_REGEX = re.compile(_BV_PATTERN_STR)
_AV_REGEX = re.compile(_AV_PATTERN_STR)

# 匹配模式（基于 BV 号算法结构优化，复用核心模式）
PATTERNS = {
    # BV 号格式（新版）: BV + 10个Base58字符（无固定位限制）
    "BV": re.compile(rf"\b({_BV_PATTERN_STR})(?:\s)?(\d{{1,3}})?\b"),
    "av": re.compile(
        rf"\b{_AV_PATTERN_STR}(?:\s)?(\d{{1,3}})?\b"
    ),  # 添加单词边界，防止误匹配avatar等单词
    "b23": re.compile(r"https?://b23\.tv/[A-Za-z\d\._?%&+\-=/#]+"),
    "bili2233": re.compile(r"https?://bili2233\.cn/[A-Za-z\d\._?%&+\-=/#]+"),
    # Bilibili URL 精确匹配（复用 BV/AV 模式）
    "bilibili": re.compile(
        rf"https?://(?:(?:www|m)\.)?bilibili\.com/video/"
        rf"(?:{_BV_PATTERN_STR}|{_AV_PATTERN_STR})"
        rf"(?:[/?#].*)?"
    ),
}


async def _extract_from_json(text: str) -> tuple[str, str]:
    """从JSON小程序中提取视频ID"""
    try:
        # 增强的JSON解析，支持多种转义格式
        json_str = _JSON_CQ_PATTERN.search(text)
        if not json_str:
            return "", ""
        # 使用 html.unescape() 解码所有 HTML 实体（&#44;、&#91;、&amp; 等）
        decoded_data = html.unescape(unquote(json_str.group(1)))
        json_data = json.loads(decoded_data)
        detail = json_data["meta"]["detail_1"]
        if "qqdocurl" in detail:
            doc_url = detail["qqdocurl"]
            logger.debug(f"提取到 qqdocurl: {doc_url}")

            # 大小写标准化：统一 URL 中的 BV/AV 前缀
            doc_url = normalize_video_id(doc_url)

            if "b23.tv" in doc_url or "bili2233.cn" in doc_url:
                try:
                    url = await get_redirect_url(doc_url, config.API_HEADERS)
                    return await extract_video_id(url)
                except Exception as e:
                    logger.error(f"短链接重定向失败，尝试直接解析: {e}")
                    # 重定向失败，尝试直接从URL提取
            # 使用精确的 BV 号格式（Base58 + 固定位）
            bv_match = _BV_REGEX.search(doc_url)
            if bv_match:
                bvid = bv_match.group(0)
                if is_valid_bvid(bvid):
                    return "BV", bvid
                logger.debug(f"无效的 BV 号: {bvid}")
            av_match = _AV_REGEX.search(doc_url)
            if av_match:
                return "aid", av_match.group(1)
    except Exception as e:
        logger.error(f"解析小程序数据失败: {type(e).__name__}: {e}", exc_info=True)
    return "", ""


async def _extract_from_url(matched: re.Match, key: str) -> tuple[str, str]:
    """从URL中提取视频ID"""
    if key in ("b23", "bili2233"):
        url = await get_redirect_url(matched.group(0), headers=config.API_HEADERS)
        return await extract_video_id(url)
    if key == "BV":
        bvid = matched.group(1)
        if is_valid_bvid(bvid):
            return "BV", bvid
        logger.debug(f"无效的 BV 号: {bvid}")
        return "", ""
    if key == "av":
        return "aid", matched.group(1)
    if key == "bilibili":
        # 使用精确的 BV 号格式（Base58 + 固定位）
        bv_match = _BV_REGEX.search(matched.group(0))
        if bv_match:
            bvid = bv_match.group(0)
            if is_valid_bvid(bvid):
                return "BV", bvid
            logger.debug(f"无效的 BV 号: {bvid}")
        av_match = _AV_REGEX.search(matched.group(0))
        if av_match:
            return "aid", av_match.group(1)
    return "", ""


async def extract_video_id(text: str) -> tuple[str, str]:
    """从文本中提取视频ID"""
    if "CQ:json" in text:
        result = await _extract_from_json(text)
        if result != ("", ""):
            return result

    for key, pattern in PATTERNS.items():
        if matched := pattern.search(text):
            result = await _extract_from_url(matched, key)
            if result != ("", ""):
                return result

    return "", ""


async def get_video_info(bvid: str | None = None, aid: int | None = None):
    """获取 Bilibili 视频详细信息"""
    if not bvid and not aid:
        return "必须提供 bvid 或 aid 参数！"

    params = {"bvid": bvid, "aid": aid}

    async with AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            config.BILIBILI_VIEW_API_URL, headers=config.API_HEADERS, params=params
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            return f"获取视频信息失败：{data.get('message', '未知错误')}"

        if data["data"]["duration"] > config.MAX_VIDEO_DURATION:
            return f"视频时长超过{config.MAX_VIDEO_DURATION / 60:.1f}分钟，无法下载"

        return data["data"]
    return None


async def get_video_stream(bvid: str | None = None, aid: int | None = None) -> dict | str:
    """获取 Bilibili 视频流信息"""
    video_info = await get_video_info(bvid=bvid, aid=aid)
    if isinstance(video_info, str):  # 如果返回的是错误信息
        return video_info

    cid = video_info.get("cid")
    if not cid:
        return "未能获取视频的 cid！"

    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": config.VIDEO_QUALITY,
    }

    async with AsyncClient(follow_redirects=True) as client:
        response = await client.get(
            config.BILIBILI_API_URL, headers=config.API_HEADERS, params=params
        )
        response.raise_for_status()
        data = response.json()

        if data.get("code") != 0:
            return f"获取视频信息失败：{data.get('message', '未知错误')}"

        video_url = data["data"]["durl"][0]["url"]
        video_size = int(data["data"]["durl"][0]["size"])

        if video_size > config.MAX_VIDEO_SIZE:
            return f"视频大小超过{config.MAX_VIDEO_SIZE / 1024 / 1024:.1f}MB，无法下载"

        return {"url": video_url, "headers": config.API_HEADERS}
    return None
