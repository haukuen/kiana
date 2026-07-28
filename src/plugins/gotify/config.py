from pydantic import BaseModel, Field


class GotifyAppRule(BaseModel):
    app_id: str = Field(..., description="Gotify 应用 ID")
    forward_users: list[str] = Field(default=[], description="转发目标 QQ 用户 ID 列表")
    forward_groups: list[str] = Field(default=[], description="转发目标 QQ 群号列表")


class Config(BaseModel):
    gotify_plugin_enabled: bool = Field(default=False, description="是否启用 Gotify 转发插件")
    gotify_url: str = Field(default="", description="Gotify 服务器地址")
    gotify_client_token: str = Field(default="", description="Gotify Client Token", json_schema_extra={"secret": True})
    gotify_forward_users: list[str] = Field(default=[], description="默认转发目标 QQ 用户 ID 列表")
    gotify_forward_groups: list[str] = Field(default=[], description="默认转发目标 QQ 群号列表")
    gotify_app_rules: list[GotifyAppRule] = Field(default=[], description="按 appid 配置的转发规则")
    gotify_reconnect_interval: int = Field(default=5, description="重连间隔（秒）")
