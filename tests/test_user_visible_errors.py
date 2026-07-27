def test_anime_trace_failure_messages_are_sanitized() -> None:
    from src.plugins.anime_trace import get_anime_trace_failure_message
    from src.plugins.anime_trace.exceptions import (
        APIRequestError,
        APIResponseError,
        ImageDownloadError,
    )

    assert get_anime_trace_failure_message(ImageDownloadError("boom")) == "下载图片失败，请稍后重试"
    assert get_anime_trace_failure_message(APIRequestError("boom")) == "搜番请求失败，请稍后重试"
    assert get_anime_trace_failure_message(APIResponseError("boom")) == "搜番结果解析失败，请稍后重试"
    assert get_anime_trace_failure_message(RuntimeError("boom")) == "处理图片失败，请稍后重试"


def test_doubili_failure_messages_are_sanitized() -> None:
    from src.plugins.doubili import get_doubili_failure_message
    from src.plugins.doubili.exceptions import (
        APIError,
        ParseError,
        VideoDurationExceededError,
        VideoSizeExceededError,
    )

    assert (
        get_doubili_failure_message("Bilibili", VideoSizeExceededError(12.3, 10.0))
        == "视频大小超过 10.0MB，无法发送"
    )
    assert (
        get_doubili_failure_message("抖音", VideoDurationExceededError(120, 90))
        == "视频时长超过 1.5 分钟，无法发送"
    )
    assert get_doubili_failure_message("小红书", ParseError("boom")) == "小红书内容解析失败，请稍后重试"
    assert get_doubili_failure_message("Bilibili", APIError("boom")) == "Bilibili内容获取失败，请稍后重试"
