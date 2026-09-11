# gift

随机礼物插件。在群里发一条命令，机器人就从可送的礼物里随机挑一个送出去。

协议部分移植自 [bl-chat-plugin](https://github.com/Cat-bl/bl-chat-plugin) 的 `sendGiftTool`，字节布局与上游一致（见 `tests/test_gift.py` 中钉死的上游参考数据包）。

## 命令列表

| 命令 | 收礼人 | 权限 |
|------|--------|------|
| `随机礼物` | 发命令的人自己 | 所有人 |
| `送礼物` | 同上（不 @ 时） | 所有人 |
| `送礼物@某人` | 被 @ 的人 | 所有人 |
| `随机礼物@某人` | 被 @ 的人 | 所有人 |

`@` 支持带不带空格（`送礼物@某人` 和 `送礼物 @某人` 都可以）。`@全体成员` 不算收礼人，会退回发给自己。

## 署名

礼物包里的 `senduin` / `sendnickname` 填的是**发命令的人**，不是机器人自己 —— 所以群里显示的是「张三 送给 李四」，而不是「Amadeus 送给 李四」。

> ⚠️ 这一点**未经验证**：服务端有可能忽略包里的发送者字段、一律按会话身份（也就是 bot）来记。上游的 `sendGiftTool` 提供了 `senderQQ` / `senderNickname` 参数，说明作者认为这是可控的，但没写验证结果。实测一次就知道；如果不生效，改回 `sender_qq=int(bot.self_id)` + 取 `get_login_info` 的昵称即可。

## 行为

**命令全程静默**：成功、冷却中、池为空、发送失败都不回消息，只记日志。礼物本身在群里可见，不需要机器人再补一句播报。

代价是**出错时用户看到的就是"什么都没发生"**。要排查就去日志里搜 `[gift]`：

- `[gift] A(QQ) 向 B(QQ) 送出 Y（N金币）` — 成功
- `[gift] ... 冷却中，剩余 N 秒` — 被冷却拦下（debug 级）
- `[gift] 没有可送的礼物，检查 gift_pool 配置` — 配置的 key 全无效
- `[gift] 取成员 QQ 昵称失败: ...` — 取被 @ 的人的昵称失败，退回用 QQ 号当名字
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

`gift_pool` 里出现未知 key 会记 warning 并忽略该 key；如果配置的 key 全部无效，命令安静地什么都不做，**不会**回退成"全部礼物可选" —— 配错时乱送是要花真金币的。

## 实现说明

- 礼物通过 NapCat 的 `send_packet` 发送 `MessageSvc.PbSendMsg` 数据包（Protobuf 手工编码，见 `protocol.py`）。
- 若当前 OneBot 实现返回 `retcode 1404`（action 不存在），自动退回 LLBot 的 `send_pb`。其他失败一律不重试，避免重复投递礼物。
- 送礼消耗机器人账号的 QQ 金币，因此默认带 60 秒冷却；**冷却按发命令的人算，不按收礼人**，否则同一个人换着 @ 别人就能绕开。**失败不写冷却**，一次网络抖动不会把用户锁住。
- 命令解析用 `on_alconna`（`nonebot-plugin-alconna`），参数是 `Args["target?", At | AtAll]`。**没用 `on_fullmatch`**：at 段不计入纯文本，`送礼物 @某人` 的纯文本是带尾空格的 `"送礼物 "`，而 `FullmatchRule` 是精确相等匹配，匹配不上。Alconna 直接吃 `At`/`AtAll` 组件，空格与否、`@全体成员` 的区分都由它处理。
  - 注意：nonebug 的假 adapter 只实现了 `text`，at 段会降级成 `Other`，所以 `At` 参数在测试里默认匹配不上。`tests/test_gift.py` 里有个夹具把 uniseg 的 builder/exporter 换成真实的 OneBot11 实现 —— 细节和坑写在 `AGENTS.md`。
- 被 @ 的人的昵称要单独查 `get_group_member_info`（群名片 > 昵称 > QQ 号）。查失败只降级成 QQ 号，不会让整次送礼泡汤。
- **只响应群聊**。分群规则（`group_permission.check_group_permission`）本身对私聊一律放行，所以插件在规则层单独拦了一道：私聊里 @ 不了人、送礼也只能送给自己，没有意义。要放开的话去掉 `_gift_rule` 里的 `isinstance` 判断即可。
