from pydantic import BaseModel


class Config(BaseModel):
    max_nickname_length: int = 15  # 最大昵称长度限制
