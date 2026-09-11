# gift

随机礼物插件。发送 `随机礼物` 即可让机器人从可送的礼物里随机挑一个，送给发命令的人。

协议部分移植自 [bl-chat-plugin](https://github.com/Cat-bl/bl-chat-plugin) 的 `sendGiftTool`，字节布局与上游一致（见 `tests/test_gift.py` 中钉死的上游参考数据包）。

## 命令列表

| 命令 | 说明 | 权限 |
|------|------|------|
| `随机礼物` | 随机送出一个礼物给发命令的人 | 所有人 |

## 行为

**命令全程静默**：成功、冷却中、池为空、发送失败都不回消息，只记日志。礼物本身在群里可见，不需要机器人再补一句播报。

代价是**出错时用户看到的就是"什么都没发生"**。要排查就去日志里搜 `[gift]`：
- `[gift] 向 X(QQ) 送出 Y（N金币）` — 成功
- `[gift] ... 冷却中，剩余 N 秒` — 被冷却拦下（debug 级）
- `[gift] 没有可送的礼物，检查 gift_pool 配置` — 配置的 key 全无效
- `[gift] 送礼失败: ...` — 发包失败

## 礼物列表

| key | 名称 | 金币 |
|-----|------|------|
| `champagne` | 香槟 | 182 |
| `hammer` | 风暴战锤 | 1388 |
| `space` | 遨游太空 | 1888 |
| `party` | 蹦迪派对 | 2999 |
| `camping` | 露营 | 388 |
| `dragon` | 龙腾万里 | 11888 |
| `supercar` | 超级跑车 | 1314 |
| `helicopter` | 直升机 | 18880 |

## 配置

```dotenv
# 插件全局开关，默认 true
gift_plugin_enabled=true

# 分群配置，模式可选 all/whitelist/blacklist
gift_group_mode=blacklist
gift_group_whitelist=[]
gift_group_blacklist=["942033342"]

# 可送的礼物 key 列表，留空表示上表全部可选
gift_pool=["champagne","camping","supercar"]

# 同一用户送礼冷却时间（秒），默认 60
gift_cooldown_time=60
```

`gift_pool` 里出现未知 key 会记 warning 并忽略该 key；如果配置的 key 全部无效，命令会回退成回复「没有可送的礼物」而不是随手送一个。

## 实现说明

- 礼物通过 NapCat 的 `send_packet` 发送 `MessageSvc.PbSendMsg` 数据包（Protobuf 手工编码，见 `protocol.py`）。
- 若当前 OneBot 实现返回 `retcode 1404`（action 不存在），自动退回 LLBot 的 `send_pb`。其他失败一律不重试，避免重复投递礼物。
- 送礼消耗机器人账号的 QQ 金币，因此默认带 60 秒的按用户冷却；**失败不写冷却**，一次网络抖动不会把用户锁住。
- 礼物只在发送成功后才计入冷却。
- 私聊也会响应，此时数据包里的群号填 0。
