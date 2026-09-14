"""严格结构化消费者的 user content 边界。"""

import json
from unittest.mock import patch

import httpx2
import pytest

from tests.ai_mock_transport import AI_HTTP_TARGET, AIHttpMock


@pytest.mark.asyncio
async def test_sentiment_sends_serialized_payload_directly_as_user_content() -> None:
    from src.plugins.a_share_sentiment.ai import request_sentiment_analysis

    prompt_payload = json.dumps(
        {
            "market": "A股",
            "today": {"date": "2026-03-24", "sample_messages": ["10:00 张三: 看多"]},
            "history": [{"date": "2026-03-23", "sample_messages": []}],
        },
        ensure_ascii=False,
    )
    response_payload = {
        "score": 61,
        "label": "偏乐观",
        "confidence": 0.66,
        "summary": "群里整体偏乐观。",
        "reasons": ["讨论集中在反弹", "看多措辞明显"],
        "compare_to_history": "比输入中的前一日更积极。",
    }
    response = httpx2.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(response_payload, ensure_ascii=False),
                    },
                }
            ]
        },
    )
    http = AIHttpMock(lambda request: response)

    with patch(AI_HTTP_TARGET, new=http):
        result = await request_sentiment_analysis(
            timeout_seconds=30,
            temperature=0.2,
            prompt_payload=prompt_payload,
        )

    assert result.score == 61
    body = http.bodies()[0]
    assert body["messages"][1] == {"role": "user", "content": prompt_payload}
