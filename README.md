# 衰减记忆与会话工具

**一个插件补齐 astrbot_plugin_isolated_session 停用后的全部用户侧能力**：
随时间衰减的长期记忆系统 + 中文会话管理指令。
后端不再使用 `isolated__` 私有命名空间，全部直接对接
AstrBot 官方对话体系（`ConversationManager`）与
官方「平台设置 → 会话隔离（unique_session）」。

数据格式与旧插件 v1.5.x 完全兼容；旧会话与记忆可通过
**astrbot_plugin_isolated_session_export** 一键迁移。

## 会话隔离与 owner

- 开启 `unique_session` 后群聊 UMO 为 `{平台ID}:GroupMessage:{用户}_{群}`，
  记忆与会话指令天然**按群×成员独立**；未开启时对当前会话（如整群）生效。
- `/记忆状态` 会显示当前会话归属(owner)，可直接核对隔离是否生效。
- 官方隔离内置支持的平台：aiocqhttp、slack、dingtalk、qq_official、
  qq_official_webhook、lark、misskey、matrix。

## 命令

### 会话指令（官方后端）
| 命令 | 说明 | 官方实现 |
|------|------|----------|
| `/会话重置`（session_reset） | 清空当前对话上下文，**存档不受影响** | 与官方 `/reset` 同语义：`update_conversation(umo, cid, [])` 就地清空 + 停止该会话运行中的 Agent + 权限场景（群聊未开隔离需管理员，alter_cmd 可覆盖）+ 第三方 runner 状态清理；同步丢弃待抽取缓冲；可联动清记忆 |
| `/会话信息`（session_info） | 轮次/消息/估算Token/官方轮次上限与超限策略/存档数 | 当前对话 + `get_config(umo)` |
| `/会话压缩 [保留条数]`（session_compress） | LLM 摘要压缩旧上下文，默认保留最近 5 条，0=全部 | `update_conversation`；超时/失败不改动内容 |
| `/存档 <名称>`（session_save） | 上下文快照为命名存档，同名覆盖 | 官方多对话：`new_conversation(content,title)` 后切回原对话 |
| `/读档 <名称>`（session_load） | 载入存档替换当前上下文 | 写入当前对话；无活跃对话时自动补建 |
| `/存档列表`（session_slots） / `/删档 <名称>`（session_slot_delete） | 管理存档 | 按标题过滤该 UMO 的带标题对话 |
| `/会话工具`（session_tools） | 帮助 | - |

> **存档 = 官方"同会话多对话"**：WebUI 对话管理同样可见可删；
> 经导出插件迁移的旧存档（标题即存档名）会被这些命令直接识别。

### 记忆命令
| 命令 | 说明 |
|------|------|
| `/记忆状态`（memory_status） | 条数、时间、Token、衰减参数、owner |
| `/记忆查询 <内容>`（memory_query） | 召回预览（相似度/衰减分/天数） |
| `/记忆开关 开\|关`（memory_toggle） | 按成员开关 |
| `/记忆清除`（memory_clear） | 清空当前群×成员的全部记忆 |
| `xxti`（或 `/xxti` / `/记忆测评`） | 依据全部已保存记忆生成 MBTI 测评海报（免/触发，只读不写） |

## 记忆工作原理

```
on_llm_request：捕获人设 → 混合检索(稠密+BM25+RRF, 按 memory_owner 过滤)
  → 衰减打分 effective = 融合分 × 0.5^(天数/半衰期) → top_k 临时注入（不入历史）
  → 被注入的记忆刷新时间戳（回忆强化）；惰性清扫（TTL 删除 + LRU 裁剪）
on_llm_response：每 memory_extract_interval 轮把积累的对话交给抽取模型
  → 去重（≥dup_threshold 只强化）→ 写入共享知识库（后台任务不阻塞回复）
```

## 配置

- `memory_groups`：启用记忆的群列表（group_id / group_name / memory_enabled）。
  **为空时自动兼容读取旧插件的 `whitelist_groups` 结构**，配置可直接粘贴。
- `memory` 分组：与旧插件「记忆系统」分组同名同义
  （`memory_enabled / memory_kb_name / memory_extract_* / memory_half_life_days /
  memory_ttl_days / memory_inject_* / memory_fetch_k / memory_dup_threshold /
  memory_max_docs_per_user / memory_sweep_interval_minutes /
  memory_consolidate_enabled / memory_reset_with_session`）。
- `memory_reset_with_session`（默认 false）：**开启后 `/会话重置` 会同步清空
  该成员在当前群的记忆**（即旧插件的重置-清记忆联动）。
- `favorability_reset_eval_with_session`（默认 true）：**若安装了 `astrbot_plugin_favorability`，
  `/会话重置` 时会同步清除当前人格对该成员的评价（恢复为初始「初次见面」）**，
  严格区分各人格独立存储，且绝不动好感度数值、关系档位、禁言等其它任何数据。
- `memory_mbti_*`：`/记忆测评` 的开关、生成方法（`anchor` 锚点比对 / `llm`）、
  锚点中性阈值、最少记忆条数、参与分析的字符上限，以及仅 `llm` 方法使用的
  专用模型、超时与自定义提示词。留空模型则用当前会话聊天模型。
- `compress_provider_id / compress_timeout / compress_instruction`：
  `/会话压缩` 的模型、超时与提示词。
- `enable_debug_log`：调试日志。

## MBTI 测评报告（xxti / /xxti）

直接发送 `xxti`（或带前缀 `/xxti`、`/记忆测评`）读取当前成员**在当前群已保存的全部记忆**，推测四维倾向
（E/I、S/N、T/F、J/P），生成精美单屏视觉海报（含 8 极雷达图、两极平衡图、可能人格与极简速描）。

两种方法由 `memory_mbti_method` 选择：

| 方法 | 说明 |
|------|------|
| `anchor`（默认） | **不调用 LLM**。把记忆和每极的锚点句都做嵌入，逐条比余弦相似度：记忆更贴近哪一极就投给哪一极，差值过小的记为中性。结论是纯算术，**同一批记忆必定得到同一结果**，且每个维度都能追溯到具体记忆 |
| `llm` | 把全部记忆交给大模型推测。表达更自然，但**每次结果会有波动**，且结论无法追溯到具体记忆 |

### anchor 方法怎么算

```
每条记忆 → 与「E 极锚点 / I 极锚点」各取最高余弦相似度 → 差值 < 阈值 记为中性
        → 否则按 差值 × 时效权重 投给更近的一极（权重 = 0.5^(天数/半衰期)，与召回一致）
每个维度 → ratio = |两极得分差| / 总得分，strength = ratio × n/(n+4) × 100
```

- **证据量收缩**：`n/(n+4)` 让证据少时结论自动变保守——1 条记忆最多只能给到 20%，
  19 条才能到 83%。这比"一条记忆就敢下结论"诚实。
- **中性记忆不参与**：两极相似度差值低于 `memory_mbti_anchor_threshold` 的记忆
  计为中性并排除，报告会写出有多少条真正体现了倾向。
- **判不出的维度写 `?`**：证据不足或两极正好抵消时该维度标 `?`（如 `I??P`），
  不会硬凑一个字母。
- **阈值要按模型调**：余弦相似度的尺度因 embedding 模型而异。报告里几乎全是中性
  就调低阈值，判定明显是噪声就调高。

### 公共行为

- **娱乐向**：记忆是抽取器写入的短事实（每条 ≤200 字符），样本小且偏"偏好/事实"，
  报告是倾向统计而非心理测评，正文末尾固定附带声明。
- **只读不写**：报告不会写回记忆库，避免结论被后续召回当成用户事实。
- **owner 边界**：记忆按 `群×成员` 隔离，只有在存过记忆的那个群内可用；
  条数少于 `memory_mbti_min_memories`（默认 8）时提示继续积累。
- **长度控制**：按最近使用时间倒序截取 `memory_mbti_max_chars`（默认 3000）字符。
- **图片海报渲染**：基于 **Pillow** 本地原生超采样渲染紧凑精美卡片海报（`memory_mbti_render_mode` 默认 `image`），
  **0 浏览器与 Playwright 依赖**，毫秒级极速直出。卡片包含 8 极能量雷达图、4 轴平衡对比条、可能的人格与精炼速描。
  **严格保护隐私：完全不展示记忆原文证据**；遇到环境异常自动无缝降级为纯文本，亦可用 `xxti 文本`（或 `/xxti 文本`）强制文本输出。

## 启用步骤

1. WebUI「平台设置」开启 **会话隔离（unique_session）**
2. WebUI「知识库」创建知识库并配置 **Embedding 模型**
3. 插件配置开启 `memory.memory_enabled` 并选择 `memory.memory_kb_name`
4. `memory_groups` 添加需要记忆的群（或直接从旧插件粘贴 `whitelist_groups` 结构）
5. 从旧插件迁移数据：先跑 astrbot_plugin_isolated_session_export 的
   `/会话迁移` 流程，再停用旧插件、启用本插件

## 注意事项

- **配置即改即生效**：初始化失败会在收消息/命令时按 15 秒节流自动重试，
  WebUI 修改配置后无需重启。
- **报错有精确原因**：全局开关 / 知识库不可用（含库名）/ 群不在启用列表
  （列出当前已启用的群）/ 群开关关闭，分别提示。
- **知识库前置**：勿删除 `memory_kb_name` 选择的库；WebUI 删除 `[记忆] xx`
  虚拟文档 = 清空该成员记忆。
- 与旧 astrbot_plugin_isolated_session **勿同时启用**（指令重名）。
- 每群差异化的自动轮次/Token 上限由官方 provider_settings 管理
  （WebUI 支持按会话覆盖），本插件只提供手动指令。

## 测试

```bash
# 纯逻辑（任意 Python）
python -m unittest discover -s astrbot_plugin_isolated_memory/tests -p "test_tools.py"
# 完整（AstrBot 自带 Python）
AstrBot\backend\python\python.exe -m unittest discover -s astrbot_plugin_isolated_memory/tests
```
