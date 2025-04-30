from pydantic import BaseModel


class Config(BaseModel):
    COOLDOWN_TIME: int = 1  # 冷却时间（秒）
    API_URL: str = "https://mbmodule-openapi.paas.cmbchina.com/product/v1/func/market-center"
    API_HEADERS: dict = {
        "Host": "mbmodule-openapi.paas.cmbchina.com",
        "Connection": "keep-alive",
        "sec-ch-ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Android WebView";v="128"',
        "Accept": "application/json, text/plain, */*",
        "sec-ch-ua-platform": "Android",
        "sec-ch-ua-mobile": "?1",
        "User-Agent": "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:34.0) Gecko/20100101 Firefox/34.0",
        "Origin": "https://mbmodulecdn.cmbimg.com",
        "X-Requested-With": "cmb.pb",
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Referer": "https://mbmodulecdn.cmbimg.com/",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    API_PAYLOAD: str = 'params=[{"prdType":"H","prdCode":""}]'
