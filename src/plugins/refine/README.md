# refine 炼化插件

订阅某个群聊发言人或 `un_nickname` 集合，**懒触发**地通过 AI 提炼目标近期发言，生成简要总结。结果与订阅一一对应，新结果通过 `INSERT OR REPLACE` 自动覆盖旧记录，无需清理任务。**不主动推送** — 用户必须用 `炼化 <标签>` 命令拉取结果。

- 数据源：复用 `message_archive` 归档表（不重复存储原始消息）
- 订阅对象：单用户（`user:<qq>` 或 `@某人`）或 `un_nickname` 集合（`collection:<名>` / `集合 <名>`）
- AI 接口：统一调用 `ai_provider`，支持 OpenAI Chat、Responses 和 Anthropic Messages（与情绪、词频共用前置插件）
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
| `REFINE_AI_TIMEOUT_SECONDS` | `30.0` | AI 请求超时（秒） |
| `REFINE_AI_TEMPERATURE` | `0.3` | AI 采样温度 |
| `REFINE_RESULT_FRESH_SECONDS` | `86400` | 结果新鲜期（秒）。新鲜期内 `炼化` 命令直接返回缓存不调 AI（60-604800，默认 24 小时） |
| `REFINE_QUERY_COOLDOWN_SECONDS` | `60` | 同一订阅两次重炼之间的冷却时间（秒）。冷却内 `炼化` 命令返回旧缓存，防止高频查询撑爆 AI 账单；`强制炼化` 忽略冷却（0-3600） |
| `REFINE_LOOKBACK_HOURS` | `24` | 每次提炼回看小时数（1-168） |
| `REFINE_MAX_MESSAGES_PER_TARGET` | `200` | 单目标每次采样上限 |
| `REFINE_MAX_PROMPT_CHARS` | `12000` | prompt 中原文总字符预算 |
| `REFINE_MIN_MESSAGES_TO_REFINE` | `5` | 目标在窗口内消息数不足此值时跳过（节省 AI 调用） |

> AI 的端点与模型不在本插件配置里，由前置插件 `ai_provider` 统一持有
> （`ai_providers` / `ai_default_model` / `ai_plugin_models`）。
>
> 炼化是纯文本调用（不传 JSON Schema），自动获得 `ai_provider` 的完整协议行为：
> 三种协议（openai_chat / anthropic_messages / openai_responses）按前置插件的响应检查提取正文，
> 拒绝空正文及已识别的截断／失败状态。具体检查边界见[架构说明](../ai_provider/README.md#调用与响应检查)。
> `temperature_policy=auto` 时 OpenAI 系协议不发送 temperature，确要发送显式改 `send`
> （Anthropic 一律不发送：官方 SDK 已移除该参数）；openai_chat 的输出预算默认走
> `max_completion_tokens`，老中转可在档案里改 `chat_token_field="max_tokens"`。
> 配置/参数错误（缺端点、参数校验失败等）统一抛 `RefineConfigError`，走 commands
> 层既有的中文提示路径。
> 配置示例见[AI 前置插件说明](../ai_provider/README.md#配置)。

### 启用步骤

```dotenv
REFINE_PLUGIN_ENABLED=true
# AI 端点与模型由 ai_provider 前置插件持有，本插件不再配置 base_url/api_key/model。
# 想让炼化用哪个模型，在 ai_plugin_models 里按插件名指定：
AI_PLUGIN_MODELS={"refine":"relay/gpt-4o-mini"}
```

---

## 前置依赖

依赖三个内置插件：

- **`ai_provider`** — 提供 AI 端点与模型（前置插件，本身无命令）。未配置 `ai_providers` 时提炼会提示「AI 配置缺失」。
- **`message_archive`** — 提供发言原文（按时间窗 + 群号）。未启用时本插件采集结果为空。
- **`un_nickname`** — 提供 `集合` 数据（订阅类型 `collection` 时）。不使用集合订阅时可缺失。

均在 `pyproject.toml` 的 `plugin_dirs` 下，无需额外安装。

---

## 命令列表

| 命令 | 参数 | 说明 |
|------|------|------|
| `炼化订阅` | `<标签> <目标>` | 新增订阅。目标写法见下 |
| `炼化订阅列表` | - | 列出本群所有订阅 |
| `炼化取消订阅` | `<标签>` | 删除订阅（同时级联删除其结果） |
| `炼化` | `<标签>` | 缓存新鲜（在 `REFINE_RESULT_FRESH_SECONDS` 内）直接返回；否则实时提炼并落库。冷却内重复查询静默返回旧缓存 |
| `强制炼化` | `<标签>` | 跳过新鲜检查与冷却，每次都重炼 |
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

**查看 / 触发提炼**：

```
炼化 张三
```

首次查询会触发实时提炼（如果缓存过期或不存在）；后续在新鲜期内的查询直接返回缓存：

```
🧪 炼化结果：[张三]
目标：用户=123456
采样窗口：2026-07-20 08:00 ~ 07-21 08:00
采样消息：42 条
模型：gpt-4o-mini

张三在过去 24 小时主要讨论了...（AI 生成）
```

**强制重炼**（跳过新鲜检查与冷却）：

```
强制炼化 张三
```

---

## 工作机制

1. **采集**（`collector.py`）：按订阅的 `lookback_hours` 窗口从 `message_archive` 拉群消息，按 `user_id` 过滤；订阅为 `collection` 时先从 `nickname_collections` 拿成员列表再过滤。
2. **Prompt 构建**（`collector.build_prompt_payload`）：每条消息渲染为 `[MM-DD HH:MM] <昵称>: <文本>`，整体截断到 `max_prompt_chars`。
3. **AI 调用**（`ai.py`）：`complete(caller="refine")` 由前置插件选择供应商、模型及协议；system prompt 指示生成 300 字内中文总结。
4. **落库**（`db.save_result`）：结果与订阅 id、时间窗、消息数、模型名绑定。`refine_result` 表与订阅 1:1，新结果通过 `INSERT OR REPLACE` 自动覆盖旧记录，无需清理任务。
5. **懒触发**（`commands._lazy`）：用户调用 `炼化 <标签>` 时，若结果在新鲜期内（`refine_result_fresh_seconds`）则直接返回缓存；否则实时提炼。冷却期内（`refine_query_cooldown_seconds`）的重复查询静默返回旧缓存（用户合理重复查询，不警告）。
6. **强制重炼**（`commands._force`）：用户调用 `强制炼化 <标签>` 时跳过新鲜检查与冷却，每次都重炼。

### 跳过 AI 调用的条件

为节省 token，下列情况不调用 AI：

- `炼化` 命令命中新鲜缓存（`created_at` 在 `refine_result_fresh_seconds` 内）
- `炼化` 命令在冷却期内（`refine_query_cooldown_seconds`），且有旧结果可返回
- 目标在窗口内消息数 < `refine_min_messages_to_refine`（默认 5）
- 订阅为 `collection` 但 un_nickname 集合已不存在/为空
- `message_archive` 在该窗口内对该群没有任何归档

### AI 失败的回退

- `炼化` / `强制炼化` 调 AI 失败时，如果存在旧结果，会带 `⚠️ AI 调用失败，显示上次结果` 警告返回旧缓存；无旧结果则返回 `❌ 提炼失败：{error}`。

---

## 故障排查

| 现象 | 可能原因 | 解决 |
|------|---------|------|
| 启动日志 `配置缺失: ai_providers 未配置` | ai_provider 里没有可用档案 | 配置 `.env` 的 `ai_providers`（见 ai_provider 说明） |
| `❌ AI 配置缺失` | `炼化` / `强制炼化` 命令再次校验失败 | 同上 |
| `❌ 提炼失败：AI 请求超时` | 模型/网络慢 | 调大 `REFINE_AI_TIMEOUT_SECONDS` |
| `❌ 提炼失败：AI 鉴权失败` | api_key 错误 | 检查 ai_provider 里该档案的 `api_key` |
| `⚠️ AI 调用失败，显示上次结果` | AI 调用失败但有旧缓存可回退 | 检查 `ai_providers` 的 base_url / 网络；稍后重试 |
| `⚠️ 目标近期发言不足` | 目标在窗口内发言太少 | 调小 `REFINE_MIN_MESSAGES_TO_REFINE`、调大 `REFINE_LOOKBACK_HOURS`，或等目标多说话 |
| `⚠️ 目标近期发言不足，显示上次结果` | 同上但有旧缓存可回退 | 同上 |
| `集合「xxx」不存在或无成员` | 订阅时引用了未创建的集合 | 先用 `集合 xxx @人` 创建 |
