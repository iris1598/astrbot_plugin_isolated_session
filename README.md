# 隔离会话 (Isolated Session)

为 AstrBot 提供**群聊级别的成员独立对话上下文**，支持每群聊配置独立的轮次限制、最大 Token 数及压缩策略。

## 功能

- **群聊白名单隔离**：白名单内的群聊，每个成员拥有完全独立的 LLM 对话上下文，互不干扰
- **每群聊独立配置**：每个群聊可单独设置 `max_turns`、`max_tokens`、`dequeue_turns`
- **两种压缩策略**：
  - `truncate_by_turns` — 轮次截断，超限时直接丢弃旧轮次
  - `llm_compress` — LLM 摘要压缩，超限时用 LLM 将旧历史压缩为摘要（可指定独立的压缩模型）
- **自动回退**：LLM 压缩失败时自动降级为轮次截断，不丢上下文
- **独立命名空间**：隔离 UMO 使用 `isolated__` 前缀，与 AstrBot 原生 `unique_session` 不冲突

## 安装

将插件文件夹放入 AstrBot 的 `data/plugins/` 目录，在 WebUI 插件管理页启用即可。

```
data/plugins/astrbot_plugin_isolated_session/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

## 配置

在 WebUI 插件配置页中编辑。白名单使用 `template_list` 格式，支持动态增删群聊配置。

每群聊配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `group_id` | string | - | 群聊纯数字 ID |
| `group_name` | string | - | 备注名（可选） |
| `max_turns` | int | 50 | 最大保留轮次，-1=不限制 |
| `max_tokens` | int | 0 | 最大 Token 数，0=不限制 |
| `dequeue_turns` | int | 10 | 超限时每次丢弃的最少轮数 |
| `compression_strategy` | string | `truncate_by_turns` | `truncate_by_turns` 或 `llm_compress` |
| `llm_compress_provider_id` | string | 空 | LLM 压缩专用模型（留空使用当前对话模型） |
| `llm_compress_keep_recent_ratio` | float | 0.15 | LLM 压缩时保留最近上下文比例 (0.0-0.3) |
| `llm_compress_instruction` | text | 空 | 自定义压缩提示词（留空使用默认） |

## 命令

| 命令 | 说明 |
|------|------|
| `/session_info` | 查看当前群聊中你的隔离会话状态（轮次、Token 数、策略等） |
| `/session_reset` | 重置你在当前群聊中的隔离会话上下文 |
| `/session_compress` | 手动压缩当前隔离会话的上下文。根据配置的压缩策略（`llm_compress` 或 `truncate_by_turns`）压缩所有内容，不受自动触发的轮次/Token 上限限制 |

## 工作原理

```
用户消息到达
  → WakingCheckStage（唤醒检查）
  → ProcessStage → build_main_agent（按群聊 UMO 加载对话）
  → 【插件 on_llm_request 钩子】
      1. 检测群聊是否在白名单
      2. 构造每用户 UMO（isolated__ 前缀）
      3. 获取/创建用户的隔离对话
      4. 替换 req.conversation → 后续 _save_to_history 自动写入隔离对话
      5. 应用预截断/LLM 压缩
  → agent_runner.step()（LLM 生成回复）
  → 回复保存到用户的隔离对话
```

关键点：AstrBot 的 `_save_to_history` 使用 `req.conversation.cid` 保存历史，插件在钩子中替换 `req.conversation` 后，历史正确写入每用户的隔离对话。

## 注意事项

- **建议关闭 AstrBot 全局 `unique_session`**。两者同时开启不冲突（使用独立命名空间），但可能造成混淆
- **本插件的轮次/Token 限制在 AstrBot 全局限制之前生效**。如果全局 `max_context_length` 比群聊配置更严，会被全局值二次截断。建议将每群聊的 `max_turns` 设为 ≤ 全局值
- **LLM 压缩会额外消耗一次 LLM 调用**，请合理设置触发阈值
- **插件重载后内存缓存丢失**，但隔离对话数据持久化在数据库中，不影响使用
