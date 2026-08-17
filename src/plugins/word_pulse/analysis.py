from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from src.plugins.message_archive.db import ArchivedMessage, fetch_group_messages_by_time_range
from src.plugins.word_pulse.ai import classify_batch
from src.plugins.word_pulse.db import delete_buckets_by_theme, get_bucket, save_bucket
from src.storage import get_db

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
db = get_db()


def classify_message(
    msg_text: str, char_pool: set[str], cluster_terms: dict[str, set[str]],
) -> list[str] | Literal["GREY"] | None:
    if not msg_text:
        return None
    msg_chars = set(msg_text)
    if not (msg_chars & char_pool):
        return None
    hits = [name for name, terms in cluster_terms.items() if any(t in msg_text for t in terms)]
    if hits:
        return hits
    return "GREY"


def uniform_sample(messages: list[ArchivedMessage], max_count: int) -> list[ArchivedMessage]:
    if max_count <= 0:
        return []
    if len(messages) <= max_count:
        return messages
    if max_count == 1:
        # 单条时取中间那条，避免下方循环 ZeroDivisionError
        return [messages[len(messages) // 2]]
    result: list[ArchivedMessage] = []
    seen: set[int] = set()
    last = len(messages) - 1
    for i in range(max_count):
        idx = round(i * last / (max_count - 1))
        m = messages[idx]
        if m.id not in seen:
            seen.add(m.id)
            result.append(m)
    return sorted(result, key=lambda x: x.event_time)


def _pick_evenly(items: list, count: int) -> list:
    """从列表中均匀选取 count 个元素。"""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return items
    result: list = []
    last = len(items) - 1
    for i in range(count):
        idx = round(i * last / (count - 1))
        result.append(items[idx])
    return result


def merge_results(
    direct: list[tuple[int, list[str]]],
    grey_classified: list[tuple[int, str | None]],
    all_messages: dict[int, ArchivedMessage],
    day_label: str,
    skipped_count: int, max_sample: int = 3,
) -> tuple[dict[str, int], list[dict]]:
    """合并弱预筛直接归类 + LLM 灰区归类结果，按 cluster 抽样原文。

    samples schema: [{"cluster": str, "text": str, "author": str, "day": str, "event_time": int}]
    """
    counts: dict[str, int] = {"_other": 0, "_skipped": skipped_count}
    cluster_samples: dict[str, list[tuple[int, int, str, str]]] = {}  # cluster -> [(event_time, msg_id, text, author)]

    def _record(cluster_name: str, msg_id: int) -> None:
        counts[cluster_name] = counts.get(cluster_name, 0) + 1
        msg = all_messages.get(msg_id)
        if msg is None:
            return
        cs = cluster_samples.setdefault(cluster_name, [])
        # 收集 2x 候选以便后续按 event_time 均匀抽样
        if len(cs) < max_sample * 2:
            cs.append((msg.event_time, msg.id, msg.plain_text, msg.sender_name))

    for msg_id, clusters in direct:
        for cl in clusters:
            _record(cl, msg_id)

    for msg_id, cluster_name in grey_classified:
        if cluster_name is not None:
            _record(cluster_name, msg_id)
        else:
            counts["_other"] += 1

    # Build samples: per cluster pick max_sample items evenly distributed by event_time
    samples: list[dict] = []
    for cl_name, items in cluster_samples.items():
        items.sort(key=lambda x: x[0])  # sort by event_time
        selected = _pick_evenly(items, min(max_sample, len(items)))
        for et, _mid, text, author in selected:
            samples.append({
                "cluster": cl_name,
                "text": text,
                "author": author,
                "day": day_label,
                "event_time": et,
            })

    return counts, samples


def build_summary_prompt(
    theme_name: str, clusters: list[dict], buckets: list[dict], window_meta: str, max_examples: int = 5,
) -> str:
    cluster_defs = "\n".join(f"- {c['name']}" for c in clusters)
    day_lines: list[str] = []
    samples: list[dict] = []
    for b in buckets:
        day_lines.append(f"  [{b['day']}] 总{b['total_messages']}条 | " + ", ".join(f"{k}:{v}" for k, v in sorted(b.get("counts", {}).items()) if k != "_skipped"))
        samples.extend(b.get("samples", []))
    parts = [f"主题：{theme_name}", f"时间范围：{window_meta}", f"子类：\n{cluster_defs}", "", "日桶统计：", *day_lines]
    if samples:
        parts.append("")
        parts.append("典型原文：")
        for s in samples[:max_examples]:
            parts.append(f"  [{s['day']}] {s['author']}: {s['text']} (→ {s['cluster']})")
    return "\n".join(parts)


def _bucket_from_cache(cached: dict) -> dict:
    return {
        "day": cached["day"],
        "total_messages": cached["total_messages"],
        "counts": json.loads(cached["counts_json"]),
        "samples": json.loads(cached["samples_json"]),
        "sampled": bool(cached["sampled"]),
    }


async def _compute_day_bucket(
    *, group_id: str, theme_id: int, theme_name: str, day: str, day_dt: datetime,
    clusters: list[dict], char_pool: set[str], cluster_terms: dict[str, set[str]],
    max_messages_per_bucket: int, max_sample_per_cluster: int,
    base_url: str, api_key: str, model: str, temperature: float, timeout: float,
) -> dict:
    day_start = int(datetime.combine(day_dt.date(), time.min, SHANGHAI_TZ).timestamp())
    messages = await fetch_group_messages_by_time_range(
        group_id=group_id, start_time=day_start, end_time=day_start + 86400,
    )
    if not messages:
        empty = json.dumps({"_other": 0, "_skipped": 0}, ensure_ascii=False)
        await save_bucket(group_id, theme_id, day, empty, "[]", 0, False)
        return {"day": day, "total_messages": 0, "counts": {"_other": 0, "_skipped": 0}, "samples": [], "sampled": False}

    sampled_flag = False
    if len(messages) > max_messages_per_bucket:
        messages = uniform_sample(messages, max_messages_per_bucket)
        sampled_flag = True

    direct: list[tuple[int, list[str]]] = []
    grey_msgs: list[tuple[int, str]] = []
    skipped = 0
    all_msg_map: dict[int, ArchivedMessage] = {}
    for msg in messages:
        all_msg_map[msg.id] = msg
        r = classify_message(msg.plain_text, char_pool, cluster_terms)
        if r is None:
            skipped += 1
        elif r == "GREY":
            grey_msgs.append((msg.id, msg.plain_text))
        else:
            direct.append((msg.id, r))

    grey_classified: list[tuple[int, str | None]] = []
    if grey_msgs:
        grey_classified = await classify_batch(
            base_url=base_url, api_key=api_key, model=model, messages=grey_msgs,
            clusters=clusters, theme_name=theme_name, temperature=temperature, timeout=timeout,
        )

    counts, samples = merge_results(direct, grey_classified, all_msg_map, day, skipped, max_sample_per_cluster)
    await save_bucket(
        group_id, theme_id, day,
        json.dumps(counts, ensure_ascii=False), json.dumps(samples, ensure_ascii=False),
        len(messages), sampled_flag,
    )
    return {"day": day, "total_messages": len(messages), "counts": counts, "samples": samples, "sampled": sampled_flag}


async def compute_or_load_buckets(
    *, group_id: str, theme_id: int, theme_name: str,
    clusters: list[dict], char_pool: set[str], cluster_terms: dict[str, set[str]],
    day_range: int, base_url: str, api_key: str, model: str,
    max_messages_per_bucket: int, max_sample_per_cluster: int,
    temperature: float, timeout: float,
    today_bucket_fresh_seconds: int = 300,
) -> list[dict]:
    now_dt = datetime.now(SHANGHAI_TZ)
    today = now_dt.strftime("%Y-%m-%d")
    now_ts = int(now_dt.timestamp())
    results: list[dict] = []
    for offset in range(day_range):
        day_dt = now_dt - timedelta(days=offset)
        day = day_dt.strftime("%Y-%m-%d")
        cached = await get_bucket(group_id, theme_id, day)
        if cached:
            computed_at = cached["computed_at"]
            # History days: indefinitely cached. Today: fresh within window.
            if day < today:
                results.append(_bucket_from_cache(cached))
                continue
            if day == today and (now_ts - computed_at) < today_bucket_fresh_seconds:
                results.append(_bucket_from_cache(cached))
                continue
        bucket = await _compute_day_bucket(
            group_id=group_id, theme_id=theme_id, theme_name=theme_name, day=day, day_dt=day_dt,
            clusters=clusters, char_pool=char_pool, cluster_terms=cluster_terms,
            max_messages_per_bucket=max_messages_per_bucket, max_sample_per_cluster=max_sample_per_cluster,
            base_url=base_url, api_key=api_key, model=model, temperature=temperature, timeout=timeout,
        )
        results.append(bucket)
    results.reverse()
    return results


# Re-export delete_buckets_by_theme for callers that invalidate on cluster change.
__all__ = [
    "SHANGHAI_TZ",
    "build_summary_prompt",
    "classify_message",
    "compute_or_load_buckets",
    "delete_buckets_by_theme",
    "merge_results",
    "uniform_sample",
]
