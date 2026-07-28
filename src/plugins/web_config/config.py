"""web_config 插件自身配置。

定义 Web GUI 的访问口令,供 routes / auth 读取。
"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    web_config_token: str = Field(
        default="",
        description="Web GUI 访问口令;空时仅允许 localhost",
    )
