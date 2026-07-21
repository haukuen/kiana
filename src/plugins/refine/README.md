# refine 炼化插件

订阅某个群聊发言人或 `un_nickname` 集合，按 cron 周期性通过 AI 提炼目标近期发言，生成简要总结，落库保留 N 天（默认 3）。**不主动推送** — 用户必须用 `炼化查询` 命令拉取结果。

- 数据源：复用 `message_archive` 归档表（不重复存储原始消息）
- 订阅对象：单用户（`user:<qq>` 或 `@某人`）或 `un_nickname` 集合（`collection:<名>` / `集合 <名>`）
- AI 接口：OpenAI 兼容 `chat/completions`（同 `a_share_sentiment`）
- 命令权限：所有人可用；分群权限由 `refine_group_*` 控制

---

## 配置项

在 `.env`（或环境变量）配置：

| 环境变量 | 默认值 | 说明 |
|---------|-------|------|
| `REFINE_PLUGIN_ENABLED` | `false` | 是否启用炼化插件 |
| `REFINE_GROUP_MODE` | `all` | 群组控制模式：`all`/`whitelist`/`blacklist` |
| `REFINE_GROUP_WHITELIST` | `[]` | 白名单群（whitelist 模式生效） |
| `REFINE_GROUP_BLACKLIST` | `[]` | 黑名单群（blacklist 模式生效） |
| `REFINE_AI_BASE_URL` | `""` | OpenAI 兼容接口 Base URL（如 `https://api.openai.com/v1`） |
| `REFINE_AI_API_KEY` | `""` | API Key |
| `REFINE_AI_MODEL` | `""` | 模型名（如 `gpt-4o-mini`） |
| `REFINE_AI_TIMEOUT_SECONDS` | `30.0` | AI 请求超时（秒） |
| `REFINE_AI_TEMPERATURE` | `0.3` | AI 采样温度 |
| `REFINE_RESULT_RETENTION_DAYS` | `3` | 结果保留天数（超期自动清理） |
| `REFINE_SCHEDULE_CRON_HOUR` | `8` | 每日定时提炼的小时（0-23） |
| `REFINE_SCHEDULE_CRON_MINUTE` | `0` | 每日定时提炼的分钟（0-59） |
| `REFINE_LOOKBACK_HOURS` | `24` | 每次提炼回看小时数（1-168） |
| `REFINE_MAX_MESSAGES_PER_TARGET` | `200` | 单目标每次采样上限 |
| `REFINE_MAX_PROMPT_CHARS` | `12000` | prompt 中原文总字符预算 |
| `REFINE_MIN_MESSAGES_TO_REFINE` | `5` | 目标在窗口内消息数不足此值时跳过（节省 AI 调用） |

### 启用步骤

```dotenv
REFINE_PLUGIN_ENABLED=true
REFINE_AI_BASE_URL=https://api.openai.com/v1
REFINE_AI_API_KEY=sk-xxx
REFINE_AI_MODEL=gpt-4o-mini
```

---

## 前置依赖

依赖两个内置插件：

- **`message_archive`** — 提供发言原文（按时间窗 + 群号）。未启用时本插件采集结果为空。
- **`un_nickname`** — 提供 `集合` 数据（订阅类型 `collection` 时）。不使用集合订阅时可缺失。

两者均已在 `pyproject.toml` 的 `plugin_dirs` 下，无需额外安装。

---

## 命令列表

| 命令 | 参数 | 说明 |
|------|------|------|
| `炼化订阅` | `<标签> <目标>` | 新增订阅。目标写法见下 |
| `炼化订阅列表` | - | 列出本群所有订阅 |
| `炼化取消订阅` | `<标签>` | 删除订阅（同时级联删除其历史结果） |
| `炼化查询` | `[标签]` | 不带标签列出本群所有订阅最新摘要；带标签返回完整结果 |
| `炼化刷新` | `<标签>` | 立即重新提炼（不等 cron，覆盖最新结果） |
| `炼化帮助` | - | 帮助 |

### 订阅目标写法

| 写法 | 含义 |
|------|------|
| `user:123456` | 订阅 QQ 为 123456 的用户 |
| `123456` | 同上的简写（纯数字自动识别） |
| `collection:小组A` | 订阅名为 `小组A` 的 un_nickname 集合 |
| `集合 小组A` | 同上的中文别名 |
| 直接 `@某人` | 命令消息中 @ 该用户，等价于 `user:<其QQ>` |

### 标签规则

- 同一群内 `(target_type, target_value)` 唯一：一个目标不能被订阅两次
- 同一群内 `label` 唯一：标签是查询/取消的句柄
- 标签建议简短好记（如 `张三`、`打板群`、`核心团队`）

---

## 用法示例

**订阅单用户**（任意一种写法等价）：

```
炼化订阅 张三 user:123456
炼化订阅 张三 123456
炼化订阅 张三 @张三本人
```

**订阅集合**（先用 un_nickname 创建集合）：

```
集合 核心 @张三 @李四 @王五
炼化订阅 核心团队 collection:核心
炼化订阅 核心团队 集合 核心
```

**查询最新结果**：

```
炼化查询 张三
```

输出（懒触发，只有这里才推送给用户）：

```
🧪 炼化结果：[张三]
目标：用户=123456
采样窗口：2026-07-20 08:00 ~ 07-21 08:00
采样消息：42 条
模型：gpt-4o-mini

张三在过去 24 小时主要讨论了...（AI 生成）
```

**列出本群所有订阅的最新摘要**：

```
炼化查询
```

输出：

```
📋 本群 3 个订阅的最新结果:
  • [张三] (2026-07-21 08:01, 42条): 张三讨论了行情、操作和心态...
  • [核心团队] (2026-07-21 08:02, 87条): 团队成员集中关注 XX 板块...
  • [新手群] 暂无结果（等待定时提炼或炼化刷新）

使用 `炼化查询 <标签>` 查看完整结果
```

**立即重新提炼**：

```
炼化刷新 张三
```

---

## 工作机制

1. **采集**（`collector.py`）：按订阅的 `lookback_hours` 窗口从 `message_archive` 拉群消息，按 `user_id` 过滤；订阅为 `collection` 时先从 `nickname_collections` 拿成员列表再过滤。
2. **Prompt 构建**（`collector.build_prompt_payload`）：每条消息渲染为 `[MM-DD HH:MM] <昵称>: <文本>`，整体截断到 `max_prompt_chars`。
3. **AI 调用**（`ai.py`）：OpenAI 兼容 `chat/completions`，system prompt 指示生成 300 字内中文总结。
4. **落库**（`db.add_result`）：结果与订阅 id、时间窗、消息数、模型名绑定。
5. **懒触发**（`commands._query`）：用户查询时才把最新结果发到群里。
6. **清理**（`runner.run_purge`）：每日 04:30 清理创建超过 `retention_days` 的结果。

### 跳过 AI 调用的条件

为节省 token，下列情况不调用 AI，结果保持上次：

- 目标在窗口内消息数 < `refine_min_messages_to_refine`（默认 5）
- 订阅为 `collection` 但 un_nickname 集合已不存在/为空
- `message_archive` 在该窗口内对该群没有任何归档

---

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 启动日志 `配置缺失: refine_ai_*` | 未填 base_url/key/model | 补齐 `.env` 配置 |
| `❌ AI 配置缺失` | 刷新命令时再次校验失败 | 同上 |
| `⚠️ [...] 提炼未完成：消息不足` | 目标在窗口内发言太少 | 调小 `REFINE_MIN_MESSAGES_TO_REFINE` 或等目标多说话 |
| `⚠️ [...] 提炼未完成：AI 请求超时` | 模型/网络慢 | 调大 `REFINE_AI_TIMEOUT_SECONDS` |
| `⚠️ [...] 提炼未完成：AI 鉴权失败` | api_key 错误 | 检查 `REFINE_AI_API_KEY` |
| `集合「xxx」不存在或无成员` | 订阅时引用了未创建的集合 | 先用 `集合 xxx @人` 创建 |
| `炼化查询` 显示 `暂无结果` | 还没到下一次 cron；或上次提炼被跳过 | 用 `炼化刷新 <标签>` 立即触发 |
