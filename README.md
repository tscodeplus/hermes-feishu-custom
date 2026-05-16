# Hermes 自定义飞书

[English](README_EN.md)

基于飞书 **CardKit 2.0** 的 Hermes 自定义平台适配器，为飞书机器人提供真正的**流式交互卡片**回复体验。

相比 Hermes 内置的飞书渠道，本插件解决了消息格式化差、表格/代码块不支持、流式体验割裂等痛点。

## 与内置飞书渠道的对比

| 特性 | 内置飞书渠道 | 本自定义渠道 |
|------|-------------|-------------|
| 消息类型 | 普通文本 / 基础交互卡片 | CardKit 2.0 流式交互卡片 |
| 流式回复 | 多次编辑消息，体验割裂 | 单卡片内渐进更新，流畅自然 |
| Markdown 表格 | 不支持或渲染异常 | 原生支持 |
| 代码块 | 格式丢失 | 原生支持，语法高亮 |
| 消息合并 | 每次更新覆盖前一条 | 所有内容聚合在一张卡片中 |
| 底部信息栏 | 无 | 显示配置名称、耗时、模型/提供商 |
| 定时任务样式 | 无特殊处理 | 黄色标题卡片，自动收尾 |
| 工具调用展示 | 与回复混在一起 | 工具调用与回复分成独立卡片 |

### 效果对比

#### 普通消息回复

| 内置渠道 | 自定义渠道 |
|----------|-----------|
| ![内置渠道消息](docs/screenshots/message_builtin_feishu.png) | ![自定义渠道消息](docs/screenshots/message_custom_feishu.png) |

#### 表格渲染

| 内置渠道 | 自定义渠道 |
|----------|-----------|
| ![内置渠道表格](docs/screenshots/table_builtin_feishu.png) | ![自定义渠道表格](docs/screenshots/table_custom_feishu.png) |

内置渠道的表格使用纯文本拼凑，列对齐混乱、难以阅读。自定义渠道使用 CardKit 2.0 的原生 Markdown 渲染，表格清晰规整。

## 工作原理

```
Hermes AI Agent
    │
    ▼
run_conversation()  ← 模型追踪（monkey-patch）
    │
    ▼
FeishuCustomAdapter
    │
    ├── send()          → CardKit 2.0 创建卡片 → 流式更新内容
    ├── edit_message()  → 持续追加文本 → 或 finalize 收尾
    └── _cardkit_finalize() → 关闭流式模式 → 添加底部信息栏
    │
    ▼
飞书客户端（用户看到一张不断更新的卡片）
```

核心技术点：

- **CardKit 2.0 流式**：使用 `lark_oapi.api.cardkit` 的 `content_card_element` 接口，逐个 `sequence` 推送内容到卡片的同一个 `element_id`
- **单卡片复用**：同一轮对话复用一张卡片，`send()` 检测到内容延续时追加而非新建
- **段边界检测**：当 stream consumer 重置时（跨工具调用），自动完结旧卡片并创建新卡片
- **模型追踪**：通过 monkey-patch `AIAgent.run_conversation` 和 `_try_activate_fallback` 捕获实际使用的模型/提供商，在卡片底部展示

## 安装

### 1. 克隆仓库

```bash
cd ~/.hermes/plugins/platforms/
git clone https://github.com/tscodeplus/hermes-feishu-custom.git feishu_custom
```

### 2. 安装依赖

```bash
pip install 'lark-oapi>=1.5.3,<2'
```

> 注意：本插件**不**依赖 Hermes 内置的 `lark-oapi`（如果已安装则共用），但版本需要 ≥ 1.5.3 才能使用 CardKit 2.0 API。

### 3. 配置环境变量

在 Hermes 的 `.env` 或系统环境变量中添加：

```bash
# 必填 — 飞书应用凭证
FEISHU_CUSTOM_APP_ID=cli_xxxxxxxxxxxx
FEISHU_CUSTOM_APP_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxx

# 可选 — 用户访问控制
FEISHU_CUSTOM_ALLOW_ALL_USERS=true          # 允许所有用户使用（开发测试用）
# FEISHU_CUSTOM_ALLOWED_USERS=user_id_1,user_id_2  # 仅允许指定用户

# 可选 — 高级配置
FEISHU_CUSTOM_LARK_HOST=feishu              # feishu（飞书）或 lark（Lark海外版）
FEISHU_CUSTOM_CONNECTION_MODE=websocket     # websocket 或 webhook
FEISHU_CUSTOM_MAX_MESSAGE_LENGTH=15000      # 单条消息最大长度（字符）
```

### 4. 配置 Hermes

编辑 Hermes 的 `config.yaml`（通常位于 `~/.hermes/config.yaml`）：

```yaml
platforms:
  # 重要：禁用内置飞书渠道，避免与本插件冲突
  feishu:
    enabled: false

  # 启用自定义渠道
  feishu_custom:
    enabled: true
```

## 飞书应用配置

确保你的飞书应用具备以下权限：

- `im:message` — 发送和接收消息
- `im:message.p2p_msg:readonly` — 读取私聊消息
- `im:message.group_msg:readonly` — 读取群聊消息
- `im:message:send_as_bot` — 以机器人身份发送消息

并在飞书开放平台的「事件订阅」中配置：
- 订阅 `im.message.receive_v1` 事件
- 请求地址指向你的 Hermes 网关 URL

## 功能细节

### 流式卡片生命周期

1. **创建**：收到用户消息后，立即创建一张 CardKit 2.0 卡片，显示「思考中...」
2. **流式更新**：AI 生成内容时，每次 `send()` 调用更新卡片的同一元素，内容渐进追加
3. **收尾**：对话结束时调用 `edit_message(finalize=True)`，关闭流式模式，添加底部信息栏（配置名称 · 耗时 · 模型/提供商）
4. **自动收尾**：定时任务等一次性消息在 3 秒后自动收尾

### 工具调用分离

当 AI 调用工具并收到结果后继续推理时，工具调用的卡片会自动完结，后续回复创建新卡片。这样工具调用和 AI 回复在视觉上分为独立的卡片，对话结构更清晰。

### 底部信息栏

每张收尾后的卡片底部会显示：

> 配置名称 · 15.3s · openai/gpt-4o

包括：
- **配置名称**：当前 Hermes 活跃配置的名称
- **耗时**：从发送第一条消息到收尾的实际耗时
- **模型信息**：实际使用的模型和提供商（含 fallback 后的真实模型）

## 许可证

MIT
