# qqq

QQQ（纳斯达克 100 ETF）实时行情查询插件。发送 `qqq` 即可返回最新价格和涨跌幅。

## 命令列表

| 命令 | 说明 |
|------|------|
| `qqq` | 查询 QQQ 最新行情（忽略大小写） |

## 使用示例

```
用户: qqq
Bot: 732.07（+1.16%）
```

## 配置项

在 `.env` 或 `.env.prod` 中配置：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `qqq_plugin_enabled` | bool | true | 是否启用 QQQ 行情查询插件 |
| `qqq_enable_price_query` | bool | true | 是否启用价格查询功能 |
| `qqq_group_mode` | str | "all" | 群组控制模式：all/whitelist/blacklist |
| `qqq_group_whitelist` | list | [] | 白名单群组 |
| `qqq_group_blacklist` | list | [] | 黑名单群组 |
| `qqq_cooldown_time` | int | 10 | 群聊查询冷却时间（秒），私聊无冷却 |
