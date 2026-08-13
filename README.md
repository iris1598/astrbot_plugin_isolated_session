# 隔离会话 (Isolated Session)

为 AstrBot 提供**群聊级别的成员独立对话上下文**，支持每群聊配置独立的轮次限制、最大 Token 数及压缩策略；内置**基于共享知识库的随时间衰减记忆系统**。

## 功能

- **群聊白名单隔离**：白名单内的群聊，每个成员拥有完全独立的 LLM 对话上下文，互不干扰
- **每群聊独立配置**：每个群聊可单独设置 `max_turns`、`max_tokens`、`dequeue_turns`
- **两种压缩策略**：
  - `truncate_by_turns` — 轮次截断，超限时直接丢弃旧轮次
  - `llm_compress` — LLM 摘要压缩，超限时用 LLM 将旧历史压缩为摘要（可指定独立的压缩模型）
- **自动回退**：自动压缩时 LLM 压缩失败会自动降级为轮次截断，不丢上下文
- **超时/失败保护**：LLM 压缩请求超过 `llm_compress_timeout` 秒未返回时，自动压缩回退为丢弃固定轮次（`dequeue_turns`）；手动压缩超时或失败则直接提示压缩失败且不改动历史——避免压缩卡住或静默丢消息
- **存档/读档**：每位成员可随时将当前隔离会话保存为命名存档，之后按名称读档恢复、查看或删除存档，存档持久化在 AstrBot 数据库中，重启不丢失
- **独立命名空间**：隔离 UMO 使用 `isolated__` 前缀，与 AstrBot 原生 `unique_session` 不冲突
- **随时间衰减的记忆系统**：复用用户自建自选的**单个共享知识库**，每位成员的对话自动抽取为长期记忆，后续对话按"相关度 × 时效衰减"召回注入；越久远的记忆影响力越低，超过 TTL 自动遗忘（清扫删除），被召回的记忆会被强化（刷新时间戳）

## 安装

将插件文件夹放入 AstrBot 的 `data/plugins/` 目录，在 WebUI 插件管理页启用即可。

```
data/plugins/astrbot_plugin_isolated_session/
├── main.py
├── memory.py
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

## 配置

在 WebUI 插件配置页中编辑。白名单使用 `template_list` 格式，支持动态增删群聊配置。**记忆相关配置集中在独立的「记忆系统」分组中**（WebUI 中显示为「记忆系统」折叠子分组），分组内所有键名以 `memory_` 开头。

每群聊配置项：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `group_id` | string | - | 群聊纯数字 ID |
| `group_name` | string | - | 备注名（可选） |
| `max_turns` | int | 50 | 最大保留轮次，-1=不限制 |
| `max_tokens` | int | 0 | 最大 Token 数，0=不限制 |
| `dequeue_turns` | int | 10 | 超限时每次丢弃的最少轮数；LLM 压缩超时回退时按此丢弃最旧轮次 |
| `compression_strategy` | string | `truncate_by_turns` | `truncate_by_turns` 或 `llm_compress` |
| `llm_compress_provider_id` | string | 空 | LLM 压缩专用模型（留空使用当前对话模型） |
| `llm_compress_timeout` | int | 30 | LLM 压缩请求超时时间（秒），0=不限制 |
| `llm_compress_keep_recent_ratio` | float | 0.15 | LLM 压缩时保留最近上下文比例 (0.0-0.3) |
| `llm_compress_instruction` | text | 空 | 自定义压缩提示词（留空使用默认） |
| `memory_enabled` | bool | false | 该群聊是否启用成员记忆（需「记忆系统」分组中的全局 `memory_enabled` 开启） |

全局配置项（位于「记忆系统」分组中，除白名单外）：

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `memory_enabled` | bool | false | 记忆系统总开关 |
| `memory_kb_name` | list | 空 | 共享记忆知识库（单选，WebUI 选择器） |
| `memory_extract_provider_id` | string | 空 | 记忆抽取专用 LLM 模型（留空用当前聊天模型） |
| `memory_extract_include_persona` | bool | true | 是否把当前人设提供给抽取 LLM |
| `memory_extract_persona_max_chars` | int | 1000 | 注入抽取提示词的人设最大字符数 |
| `memory_extract_use_names` | bool | true | 抽取提示词用用户昵称与机器人名替代「用户/助手」称呼 |
| `memory_extract_bot_name` | string | 空 | 抽取提示词中机器人的称呼（留空用「助手」） |
| `memory_half_life_days` | float | 30 | 记忆衰减半衰期（天） |
| `memory_ttl_days` | float | 90 | 记忆遗忘阈值 TTL（天） |
| `memory_inject_top_k` | int | 3 | 每次请求注入的记忆条数 |
| `memory_min_score` | float | 0.0 | 注入最低有效分数 |
| `memory_inject_max_chars` | int | 600 | 注入记忆文本总字符上限 |
| `memory_fetch_k` | int | 200 | 记忆检索池大小 |
| `memory_extract_interval` | int | 3 | **每多少轮对话触发一次记忆抽取**（默认 3；触发时把间隔内积累的全部对话轮次一并交给提取 LLM；0=每轮都抽取，不推荐） |
| `memory_extract_timeout` | int | 30 | 抽取 LLM 请求超时（秒） |
| `memory_dup_threshold` | float | 0.9 | 记忆去重相似度阈值 |
| `memory_max_docs_per_user` | int | 200 | 每用户记忆条数上限（LRU 裁剪） |
| `memory_sweep_interval_minutes` | int | 60 | 记忆惰性清扫间隔（分钟） |
| `memory_consolidate_enabled` | bool | false | 遗忘前巩固：过期记忆先由 LLM 折叠为长期摘要再删除 |
| `memory_reset_with_session` | bool | false | 会话重置时是否一并清空记忆 |

## 命令

| 命令 | 说明 |
|------|------|
| `/会话信息`（`session_info`） | 查看当前群聊中你的隔离会话状态（轮次、Token 数、策略等） |
| `/会话重置`（`session_reset`） | 重置你在当前群聊中的隔离会话上下文 |
| `/会话压缩 [保留条数]`（`session_compress`） | 手动压缩当前隔离会话的上下文。根据配置的压缩策略（`llm_compress` 或 `truncate_by_turns`）压缩旧内容，可选保留最近 N 条消息：不填默认保留 5 条，填 0 表示全部压缩；不受自动触发的轮次/Token 上限限制 |
| `/存档 <名称>`（`session_save`） | 将当前隔离会话保存为命名存档（同名存档会被覆盖） |
| `/读档 <名称>`（`session_load`） | 载入指定存档，替换当前隔离会话的上下文（当前对话将被覆盖，可先存档备份） |
| `/存档列表`（`session_slots`） | 列出你的全部存档（名称、消息数、Token、更新时间） |
| `/删档 <名称>`（`session_slot_delete`） | 删除指定存档 |
| `/记忆状态`（`memory_status`） | 查看你的记忆状态（条数、最早/最近时间、估算 Token、衰减参数） |
| `/记忆清除`（`memory_clear`） | 清空你在当前群聊中的全部记忆 |
| `/记忆开关 [开\|关]`（`memory_toggle`） | 开启/关闭你的记忆功能 |
| `/记忆查询 <内容>`（`memory_query`） | 预览当前记忆的召回结果（含相似度、衰减后分数），用于调试衰减效果 |

> 以上中文名称是主命令名，括号内为兼容保留的英文别名。存档名称仅支持中英文、数字、下划线、短横线，长度 1-20 个字符。

## 记忆系统（随时间衰减）

基于 **AstrBot 知识库（RAG）** 的每位成员长时记忆，按 **群 × 用户** 隔离，共存于**用户自建自选的单个共享知识库**。

### 启用步骤

1. 在 AstrBot WebUI「知识库」页创建知识库并配置 **Embedding 模型**（需要 AstrBot ≥ 4.5.0）
2. 在插件配置页「记忆系统」分组中开启全局 `memory_enabled`，并在 `memory_kb_name` 选择该知识库
3. 在目标群聊的白名单配置中开启 `memory_enabled`

### 工作原理

```
用户消息 → build_main_agent（人设注入 system_prompt）
  → 【on_llm_request 钩子】
      1. 替换为隔离会话 + 预截断/压缩（原有逻辑）
      2. 捕获当前人设（Persona）存入事件
      3. 记忆召回：query=用户消息 → 共享库按 owner 过滤的混合检索（稠密+BM25+RRF）
         → 衰减打分（effective = 融合分 × 0.5^(天数/半衰期)）→ 注入 top_k 条
         → 追加到 extra_user_content_parts（mark_as_temp，不写入对话历史）
  → LLM 生成回复
  → 【on_llm_response 钩子】
      4. 每 memory_extract_interval 轮触发：把间隔内积累的全部对话轮次交给独立抽取模型（可含人设）抽取可记忆事实
         → 去重（相似度 ≥ memory_dup_threshold 则强化现有记忆）→ 写入共享库
      5. 惰性清扫：删除超过 TTL 的记忆；按每用户上限 LRU 裁剪
```

### 衰减模型

- **时间钟**：每条记忆的 `updated_at`（写入或**被召回强化**时刷新，模拟"回忆增强记忆痕迹"）
- **半衰期衰减**：`effective = fused_score × 0.5 ** (age_days / memory_half_life_days)`（默认半衰期 30 天）
- **遗忘（TTL）**：超过 `memory_ttl_days`（默认 90 天）的记忆不再召回，惰性清扫时从库中删除
- **LRU 上限**：每用户最多 `memory_max_docs_per_user` 条（默认 200），超出裁剪最久未使用的记忆
- **可选巩固**：`memory_consolidate_enabled` 开启时，过期记忆先由 LLM 折叠为一条"长期摘要"再删除

### 记忆命令

| 命令 | 说明 |
|------|------|
| `/记忆状态` | 查看记忆条数、最早/最近记忆时间、估算 Token、当前衰减参数 |
| `/记忆查询 <内容>` | 调试：展示该查询的召回结果（相似度、衰减后分数、记忆天数） |
| `/记忆清除` | 清空你在当前群聊中的全部记忆 |
| `/记忆开关 [开\|关]` | 按用户开关记忆（默认开） |

### 记忆系统注意事项

- **知识库前置**：请勿删除 `memory_kb_name` 中选择的知识库，否则记忆全部丢失；插件不会自动创建或删除该库
- **隔离与共存**：所有成员的记忆以带 `memory_owner` 元数据的 chunk 共存于同一库，按 群×用户 严格隔离；每个用户对应 WebUI 知识库里的一个 **`[记忆] 群×用户` 虚拟文档**，可点开逐条查看/删除记忆；在 WebUI 删除该文档 = 清空该用户全部记忆（与 `/记忆清除` 等价）。记忆 chunk 遵循 AstrBot 的文档元数据约定（`kb_doc_id`/`chunk_index`），WebUI 知识库检索可正常搜索到记忆内容
- **独立抽取模型**：`memory_extract_provider_id` 可指定抽取专用模型（成本可控）；抽取每 `memory_extract_interval` 轮触发一次，触发时把间隔内积累的**全部对话轮次**一并交给抽取模型（而非只抽当轮），抽取的视角更完整
- **抽取不阻塞对话**：记忆抽取在 `on_llm_response` 钩子中作为**后台任务**执行，回复已生成后才启动，`asyncio.wait_for(memory_extract_timeout)` 超时（默认 30 秒）或失败只会记日志并跳过，**绝不会阻塞或拖慢当前对话**——可以放心使用响应较慢的抽取模型；阻塞当前请求的只有向量检索（不含 LLM 调用），且失败会被捕获跳过
- **人设注入**：开启 `memory_extract_include_persona` 后，抽取提示词会附上当前会话人设文本（取自会话 persona_id 或 system_prompt 的 Persona Instructions 块），使抽取的记忆与人设对齐
- **相对时间处理**：抽取提示词会附上当前日期，并要求把「今天/明天/下周」等相对时间换算为具体日期（如「明天穿长袖」→「计划 2026-08-15 穿长袖」）后再记录，避免几天后召回时读到已过期的「明天」；明确的一次性临时安排（如「明天去买菜」）默认不记录为长期记忆
- **称呼替换**：开启 `memory_extract_use_names`（默认开）后，抽取提示词用消息发送者的昵称称呼用户、用 `memory_extract_bot_name`（留空用「助手」）称呼机器人，替代冰冷的「用户/助手」标签；获取不到昵称时自动回退「用户」。注意：抽取出的记忆可能因此直接带上昵称（如「小明喜欢喝冰美式」）
- **与全局知识库并存**：本插件记忆注入与 AstrBot 全局知识库检索各自注入独立的临时内容块，互不覆盖；注入的记忆不写入对话历史。注意：记忆 chunk 与普通文档共存于同一知识库，若同时启用 AstrBot 全局知识库检索（`kb_names` 选择该库），检索结果会包含记忆内容——如不希望如此，请勿在全局知识库设置中勾选启用
- **升级提示（旧记忆数据）**：旧版本写入的记忆 chunk 缺少 `kb_doc_id`/`chunk_index` 元数据，会导致 WebUI 知识库检索报错。升级到本版本后请**先删除旧记忆数据**（在 WebUI 删除知识库重建，或用 `/记忆清除` 逐用户清空）再继续使用，新写入的记忆将自动附带完整元数据
- **与 `/会话重置` 的关系**：默认**不**清空记忆（长时记忆独立于对话上下文）；如希望重置时一并清空，开启 `memory_reset_with_session`

## 工作原理

```
用户消息到达
  → WakingCheckStage（唤醒检查）
  → ProcessStage → build_main_agent（按群聊 UMO 加载对话，注入人设与全局知识库）
  → 【插件 on_llm_request 钩子】
      1. 检测群聊是否在白名单
      2. 构造每用户 UMO（isolated__ 前缀）
      3. 获取/创建用户的隔离对话
      4. 替换 req.conversation → 后续 _save_to_history 自动写入隔离对话
      5. 应用预截断/LLM 压缩
      6. 【记忆】召回衰减后的相关记忆 → 追加为临时内容块（mark_as_temp）
  → agent_runner.step()（LLM 生成回复）
  → 【插件 on_llm_response 钩子】
      7. 【记忆】按间隔把积累的全部对话轮次交给独立模型（可含人设）抽取可记忆事实 → 去重后写入共享知识库
      8. 【记忆】惰性清扫（删除过期记忆、LRU 裁剪）
  → 回复保存到用户的隔离对话
```

关键点：AstrBot 的 `_save_to_history` 使用 `req.conversation.cid` 保存历史，插件在钩子中替换 `req.conversation` 后，历史正确写入每用户的隔离对话。记忆注入使用 `extra_user_content_parts` + `mark_as_temp()`，不会进入对话历史。

## 注意事项

- **建议关闭 AstrBot 全局 `unique_session`**。两者同时开启不冲突（使用独立命名空间），但可能造成混淆
- **本插件的轮次/Token 限制在 AstrBot 全局限制之前生效**。如果全局 `max_context_length` 比群聊配置更严，会被全局值二次截断。建议将每群聊的 `max_turns` 设为 ≤ 全局值
- **LLM 压缩会额外消耗一次 LLM 调用**，请合理设置触发阈值
- **超时保护机制**：自动压缩（轮次/Token 超限触发）若 LLM 请求超过 `llm_compress_timeout` 秒未返回，将丢弃最旧的 `dequeue_turns` 轮并继续对话，不再等待；手动 `/会话压缩` 超时或 LLM 压缩失败则直接提示压缩失败且不改动历史。建议将超时时间设为低于正常回复超时，避免整个群的对话被卡住
- **存档说明**：存档使用独立的 `isolated_archive__` 命名空间存储在 AstrBot 数据库中，不会出现在正常会话里；`/会话重置` 只清空当前对话，不影响已保存的存档
- **记忆系统依赖**：记忆功能需要 AstrBot ≥ 4.5.0 且配置了 Embedding 模型；未配置或知识库不可用时记忆功能自动禁用，不影响其余功能。记忆抽取/巩固会额外消耗 LLM 调用，建议设置合理的 `memory_extract_interval` 与独立的 `memory_extract_provider_id`
- **升级迁移提示**：本版本将记忆配置收纳到「记忆系统」分组。AstrBot 的配置完整性检查会**移除顶层旧的扁平 `memory_*` 键**并以默认值生成新分组（不迁移旧值）——若从旧版本升级后记忆参数回到默认值，请到「记忆系统」分组中重新配置；插件运行时仍会兼容读取顶层扁平键（双读兜底），仅作防御
- **插件重载后内存缓存丢失**，但隔离对话数据持久化在数据库中，不影响使用
