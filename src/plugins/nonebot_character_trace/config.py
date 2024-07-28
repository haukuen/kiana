from pydantic import BaseModel


class Config(BaseModel):
    """Plugin Config Here"""
    api: str = 'https://aiapiv2.animedb.cn/ai/api/detect?&is_multi=0&ai_detect=2'
    high_anime1: str = "anime_model_lovelive"
    high_anime2: str = "pre_stable"
    normal_anime: str = "anime"
    normal_gal: str = "game"
    high_gal: str = "game_model_kirakira"