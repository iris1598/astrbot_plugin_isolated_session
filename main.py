"""
astrbot_plugin_isolated_session - 群聊会话隔离插件

为白名单内的群聊实现每位成员的独立对话上下文，
支持每群聊配置独立的轮次限制、最大 Token 数及压缩策略。
"""

import asyncio
import json
import re
import time

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.db.po import Conversation
from astrbot.core.platform.message_type import MessageType

from .memory import MemoryManager

# ── 默认 LLM 压缩提示词（与 AstrBot 默认值一致） ──────────────
DEFAULT_COMPRESS_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary "
    "of the key topics, context, and important details discussed so far. "
    "The summary should capture all essential information needed to continue "
    "the conversation coherently."
)

# ── 存档名称规则：中英文、数字、下划线、短横线，1-20 个字符 ──
SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,20}$")

# ── 主类 ────────────────────────────────────────────────────────


class Main(Star):
    """群聊会话隔离插件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config: AstrBotConfig = config
        # 内存缓存: {(group_id, user_id): conversation_id}
        self._conv_cache: dict[tuple[str, str], str] = {}
        # 末次活跃时间: {conversation_id: timestamp}
        self._last_active: dict[str, float] = {}
        # 记忆系统（基于共享知识库的随时间衰减记忆）
        self.memory: MemoryManager | None = None
        # 后台任务引用集合，防止被垃圾回收
        self._pending_tasks: set = set()
        # 记忆抽取状态锁：同一用户的并发回复串行更新持久化缓冲。
        self._extract_locks: dict[str, asyncio.Lock] = {}

    # ── 初始化 ──────────────────────────────────────────────────

    async def initialize(self) -> None:
        """插件激活时检测 unique_session 冲突并加载配置"""
        try:
            global_cfg = self._get_context_config()
            if global_cfg:
                ps = global_cfg.get("platform_settings", {})
                if ps.get("unique_session", False):
                    logger.warning(
                        "[IsolatedSession] 检测到 AstrBot 全局 unique_session 已开启。"
                        "建议关闭全局 unique_session，由本插件接管群聊隔离（仅白名单内群聊生效）。"
                        "两者同时开启不冲突（使用独立 UMO 命名空间），但可能造成混淆。"
                    )
        except Exception:
            pass  # 非关键，获取配置失败时静默忽略

        whitelist = self.config.get("whitelist_groups", [])
        logger.info(f"[IsolatedSession] 插件已加载，白名单群聊数: {len(whitelist)}")

        # 记忆系统初始化（失败仅禁用记忆，不影响会话隔离等功能）
        self.memory = None
        if self._mcfg("memory_enabled", False):
            try:
                if getattr(self.context, "kb_manager", None) is None:
                    logger.warning(
                        "[IsolatedSession] 记忆功能已启用，但 AstrBot 知识库模块不可用。"
                    )
                else:
                    self.memory = MemoryManager(self.context, self.config)
                    probe = await self.memory.ensure_kb()
                    if probe is None:
                        logger.warning(
                            "[IsolatedSession] 记忆功能已启用，但共享记忆知识库不可用："
                            "请在 WebUI 创建知识库（配置 Embedding 模型），"
                            "并在插件配置的 memory_kb_name 中选择。"
                        )
                    else:
                        logger.info(
                            f"[IsolatedSession] 记忆系统就绪，共享知识库: {probe.kb.kb_name}"
                        )
            except Exception as e:
                logger.error(f"[IsolatedSession] 记忆系统初始化失败: {e}")
                self.memory = None

    # ── 核心钩子：on_llm_request ─────────────────────────────────

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在 LLM 请求发送前替换 conversation，实现会话隔离"""

        # 1. 仅处理群聊消息
        group_id = event.message_obj.group_id
        if not group_id:
            return

        # 2. 查找群聊是否在白名单中
        whitelist = self.config.get("whitelist_groups", [])
        group_cfg = self._find_group_config(str(group_id), whitelist)
        if not group_cfg:
            return

        # 3. 构造每用户的 UMO 并获取/创建隔离对话
        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, str(group_id))
        user_conv: Conversation = await self._get_or_create_conv(
            event, user_umo, str(group_id), user_id
        )

        # 4. 替换 req.conversation 和 req.contexts
        req.conversation = user_conv
        req.contexts = json.loads(user_conv.history) if user_conv.history else []
        # 响应钩子据此把待抽取轮次绑定到本次实际使用的隔离会话。
        event.set_extra("_isolated_memory_conversation_id", str(user_conv.cid))

        # 5. 预截断 / 压缩上下文
        await self._pre_truncate_contexts(req, group_cfg, event)

        # 6. 记忆召回与注入（基于共享知识库的随时间衰减记忆）
        if self.memory and group_cfg.get("memory_enabled", False):
            await self._inject_memory(event, req, user_id, str(group_id))

        if self.config.get("enable_debug_log"):
            logger.debug(
                f"[IsolatedSession] user={user_id} group={group_id} "
                f"cid={user_conv.cid} contexts={len(req.contexts)}"
            )

    # ── 核心钩子：on_llm_response ───────────────────────────────

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """LLM 回复后：按间隔抽取对话中的可记忆事实并写入记忆库。

        抽取使用独立配置的 LLM 模型（memory_extract_provider_id），
        并可把当前会话人设（on_llm_request 中捕获）提供给抽取模型。
        """
        if not self.memory:
            if self.config.get("enable_debug_log"):
                logger.debug(
                    "[IsolatedSession] 记忆抽取跳过: 记忆系统未初始化"
                    "（全局 memory_enabled 未开启，或共享知识库不可用）"
                )
            return
        if not event.message_obj.group_id:
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        group_cfg = self._find_group_config(group_id, whitelist)
        if not group_cfg or not group_cfg.get("memory_enabled", False):
            if self.config.get("enable_debug_log"):
                logger.debug(
                    f"[IsolatedSession] 记忆抽取跳过: 群 {group_id} 未启用群级 memory_enabled"
                )
            return

        user_id = event.get_sender_id()
        owner = self._build_user_umo(event, user_id, group_id)
        try:
            if not await sp.session_get(owner, "memory_enabled", True):
                if self.config.get("enable_debug_log"):
                    logger.debug(
                        f"[IsolatedSession] 记忆抽取跳过: 用户 {user_id} 已通过 /记忆开关 关闭"
                    )
                return

            user_text = (event.message_str or "").strip()
            reply_text = ((response.completion_text or "") if response else "").strip()
            if not reply_text:
                if self.config.get("enable_debug_log"):
                    logger.debug(
                        f"[IsolatedSession] 记忆抽取跳过: 本轮无助手回复（owner={owner}）"
                    )
                return

            # 间隔控制：持久化缓存按 owner + conversation_id 隔离。
            # 直接以缓存长度判断触发，避免插件重载后出现“计数保留、对话丢失”。
            interval = max(0, int(self._mcfg("memory_extract_interval", 3) or 0))
            if interval > 0:
                conversation_id = str(
                    event.get_extra("_isolated_memory_conversation_id") or ""
                )
                if not conversation_id:
                    conversation_id = str(
                        await self.context.conversation_manager.get_curr_conversation_id(
                            owner
                        )
                        or ""
                    )
                turns = await self._buffer_extract_turn(
                    owner=owner,
                    conversation_id=conversation_id,
                    turn=(user_text, reply_text),
                    interval=interval,
                )
                if not turns:
                    return
            else:
                await self._clear_extract_state(owner)
                turns = [(user_text, reply_text)]

            logger.info(
                f"[IsolatedSession] 触发记忆抽取: owner={owner}, 输入 {len(turns)} 轮对话, "
                f"interval={interval}"
            )
            persona = event.get_extra("_isolated_memory_persona")
            user_name = (event.get_sender_name() or "").strip()
            task = asyncio.create_task(
                self.memory.extract_memories(
                    owner=owner,
                    turns=turns,
                    persona=persona,
                    umo=event.unified_msg_origin,
                    user_name=user_name,
                )
            )
            self._track_task(task)
        except Exception as e:
            logger.warning(f"[IsolatedSession] 记忆抽取调度失败: {e}")

    # ── 记忆注入 ────────────────────────────────────────────────

    async def _inject_memory(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
        user_id: str,
        group_id: str,
    ) -> None:
        """从共享记忆库召回衰减后的记忆并注入 LLM 请求。

        全程容错：任何失败只跳过记忆注入，不阻塞对话。
        """
        try:
            owner = self._build_user_umo(event, user_id, group_id)
            if not await sp.session_get(owner, "memory_enabled", True):
                return
            if not req.prompt or not req.prompt.strip():
                return

            # 捕获当前会话人设，供 on_llm_response 抽取时使用
            persona = await self._capture_persona(event, req)
            if persona:
                event.set_extra("_isolated_memory_persona", persona)

            hits = await self.memory.recall(owner, req.prompt.strip())
            if hits:
                req.extra_user_content_parts.append(
                    TextPart(text=self.memory.format_injection(hits)).mark_as_temp()
                )
                if self.config.get("enable_debug_log"):
                    logger.debug(
                        f"[IsolatedSession] 注入记忆 {len(hits)} 条: "
                        + "; ".join(
                            f"{h['text'][:16]}…({h['effective']:.4f})" for h in hits
                        )
                    )

            # 惰性清扫（后台任务，避免阻塞请求）
            self._schedule_task(self.memory.sweep(owner))
        except Exception as e:
            logger.warning(f"[IsolatedSession] 记忆注入失败: {e}")

    async def _capture_persona(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> str | None:
        """捕获当前会话生效的人设文本（供记忆抽取 LLM 参考）。

        优先从隔离会话的 persona_id 获取结构化人设；
        失败时回退提取 system_prompt 中的 "# Persona Instructions" 块。
        """
        try:
            persona_id = getattr(req.conversation, "persona_id", None) or ""
            if persona_id:
                try:
                    persona = await self.context.persona_manager.get_persona(persona_id)
                    prompt = (persona.system_prompt or "").strip()
                    if prompt:
                        return prompt
                except Exception:
                    pass
            sp_text = req.system_prompt or ""
            match = re.search(
                r"# Persona Instructions\n(.*?)(?=\n# |\Z)", sp_text, re.DOTALL
            )
            if match and match.group(1).strip():
                return match.group(1).strip()
        except Exception:
            pass
        return None

    # ── UMO 构造 ─────────────────────────────────────────────────

    def _build_user_umo(
        self, event: AstrMessageEvent, user_id: str, group_id: str
    ) -> str:
        """构造每用户隔离 UMO: platform:GroupMessage:isolated__{user_id}__{group_id}"""
        platform = event.get_platform_name()
        msg_type = MessageType.GROUP_MESSAGE.value  # "GroupMessage"
        return f"{platform}:{msg_type}:isolated__{user_id}__{group_id}"

    def _build_archive_umo(
        self, event: AstrMessageEvent, user_id: str, group_id: str
    ) -> str:
        """构造存档命名空间 UMO: platform:GroupMessage:isolated_archive__{user_id}__{group_id}"""
        platform = event.get_platform_name()
        msg_type = MessageType.GROUP_MESSAGE.value  # "GroupMessage"
        return f"{platform}:{msg_type}:isolated_archive__{user_id}__{group_id}"

    # ── 对话管理 ─────────────────────────────────────────────────

    async def _get_or_create_conv(
        self,
        event: AstrMessageEvent,
        user_umo: str,
        group_id: str,
        user_id: str,
    ) -> Conversation:
        """获取或创建用户的隔离对话（缓存 → DB → 新建）"""
        conv_mgr = self.context.conversation_manager
        cache_key = (group_id, user_id)

        # 检查内存缓存
        cid = self._conv_cache.get(cache_key)
        if not cid:
            cid = await conv_mgr.get_curr_conversation_id(user_umo)

        if cid:
            conv = await conv_mgr.get_conversation(user_umo, cid)
            if conv:
                self._conv_cache[cache_key] = cid
                self._last_active[cid] = time.time()
                return conv

        # 创建新的隔离对话
        platform_id = event.get_platform_id()
        new_cid = await conv_mgr.new_conversation(user_umo, platform_id)
        conv = await conv_mgr.get_conversation(user_umo, new_cid)

        self._conv_cache[cache_key] = new_cid
        self._last_active[new_cid] = time.time()

        logger.info(f"[IsolatedSession] 新建隔离会话: user={user_id} group={group_id}")
        return conv

    # ── 存档管理 ─────────────────────────────────────────────────

    async def _get_archives(self, archive_umo: str) -> list[Conversation]:
        """获取某用户在群聊中的所有存档，按更新时间倒序"""
        conv_mgr = self.context.conversation_manager
        convs = await conv_mgr.get_conversations(archive_umo)
        convs.sort(key=lambda c: c.updated_at or 0, reverse=True)
        return convs

    async def _find_archive(
        self, archive_umo: str, slot_name: str
    ) -> Conversation | None:
        """按存档名称查找存档"""
        for conv in await self._get_archives(archive_umo):
            if (conv.title or "") == slot_name:
                return conv
        return None

    # ── 上下文预截断 / 压缩 ──────────────────────────────────────

    async def _pre_truncate_contexts(
        self,
        req: ProviderRequest,
        group_cfg: dict,
        event: AstrMessageEvent,
    ) -> None:
        """对 req.contexts 执行预截断 / LLM 压缩（两种超限均按策略选择）"""
        contexts: list[dict] = req.contexts
        if not contexts:
            return

        max_turns: int = group_cfg.get("max_turns", 50)
        max_tokens: int = group_cfg.get("max_tokens", 0)
        strategy: str = group_cfg.get("compression_strategy", "truncate_by_turns")
        keep_ratio: float = min(
            max(float(group_cfg.get("llm_compress_keep_recent_ratio", 0.15)), 0.0), 0.3
        )

        # ── 第 1 步：轮次超限 → 按策略处理 ──
        llm_timed_out = False
        if max_turns > 0:
            system_msgs = [m for m in contexts if m.get("role") == "system"]
            non_system = [m for m in contexts if m.get("role") != "system"]
            turns = self._group_into_turns(non_system)

            if len(turns) > max_turns:
                if strategy == "llm_compress":
                    # 保留最近 max_turns * keep_ratio 轮，其余压缩
                    keep_turn_count = max(1, int(max_turns * keep_ratio))
                    recent = turns[-keep_turn_count:]
                    old = turns[:-keep_turn_count]
                    old_msgs = [msg for turn in old for msg in turn]
                    status, summary = await self._call_llm_summary(
                        old_msgs, group_cfg, event
                    )
                    if status == "ok":
                        recent_msgs = [msg for turn in recent for msg in turn]
                        contexts = (
                            system_msgs
                            + self._build_summary_pair(summary)
                            + recent_msgs
                        )
                    elif status == "timeout":
                        # 压缩超时：回退为丢弃固定轮次，避免对话卡住
                        llm_timed_out = True
                        excess = len(turns) - max_turns
                        contexts = self._discard_old_turns(
                            contexts,
                            max(self._get_dequeue_turns(group_cfg), excess),
                        )
                        logger.warning(
                            "[IsolatedSession] LLM 压缩超时，已回退为丢弃固定轮次"
                        )
                    else:
                        # 压缩失败，回退到按轮次截断
                        turns = turns[-max_turns:]
                        non_system = [msg for turn in turns for msg in turn]
                        contexts = system_msgs + non_system
                else:
                    # truncate_by_turns：保留最近 max_turns 轮
                    turns = turns[-max_turns:]
                    non_system = [msg for turn in turns for msg in turn]
                    contexts = system_msgs + non_system

        # ── 第 2 步：Token 超限 → 按策略处理 ──
        if max_tokens > 0 and self._count_tokens(contexts) > max_tokens:
            if strategy == "llm_compress" and not llm_timed_out:
                if self.config.get("enable_debug_log"):
                    logger.debug("[IsolatedSession] Token 超限，触发 LLM 上下文压缩")
                contexts = await self._llm_compress(
                    contexts, max_tokens, group_cfg, event
                )
            elif strategy == "llm_compress":
                # 本轮请求压缩已超时，避免再次等待 LLM，直接回退为丢弃固定轮次
                logger.warning(
                    "[IsolatedSession] 压缩已超时，Token 超限回退为丢弃固定轮次"
                )
                contexts = self._discard_old_turns(
                    contexts, self._get_dequeue_turns(group_cfg)
                )
            else:
                contexts = self._truncate_by_tokens_full(contexts, max_tokens)

        req.contexts = contexts

    # ── 手动压缩：保留最近 N 条，其余压缩 ─────────────────────────

    async def _manual_compress_all(
        self,
        contexts: list[dict],
        group_cfg: dict,
        event: AstrMessageEvent,
        keep_count: int = 5,
    ) -> tuple[list[dict], str]:
        """手动压缩上下文：保留最近 keep_count 条非 system 消息，其余压缩或丢弃。

        keep_count=0 表示全部压缩，不保留任何非 system 消息；
        keep_count 大于等于非 system 消息总数时无可压缩内容，原样返回。

        返回 (压缩结果, 状态)，状态取值：
        - "ok": 压缩成功（含 LLM 成功或轮次截断）
        - "timeout": LLM 压缩超时，上下文未修改，由调用方报告压缩失败
        - "failed": LLM 压缩失败，上下文未修改，由调用方报告压缩失败
        """
        if not contexts:
            return contexts, "ok"

        system_msgs = [m for m in contexts if m.get("role") == "system"]
        non_system = [m for m in contexts if m.get("role") != "system"]

        if not non_system:
            return contexts, "ok"

        keep_count = max(0, int(keep_count))
        if keep_count >= len(non_system):
            # 需要保留的条数 >= 现有消息数，没有旧消息可压缩
            return contexts, "ok"

        if keep_count > 0:
            old_msgs = non_system[:-keep_count]
            recent_msgs = non_system[-keep_count:]
        else:
            old_msgs = non_system
            recent_msgs = []

        strategy = group_cfg.get("compression_strategy", "truncate_by_turns")

        if strategy == "llm_compress":
            status, summary = await self._call_llm_summary(old_msgs, group_cfg, event)
            if status == "ok":
                return (
                    system_msgs + self._build_summary_pair(summary) + recent_msgs,
                    "ok",
                )
            if status == "timeout":
                # 手动压缩超时：返回压缩失败，不修改上下文
                logger.warning("[IsolatedSession] 手动 LLM 压缩超时，返回压缩失败")
                return contexts, "timeout"
            # LLM 压缩失败：返回压缩失败，不修改上下文
            logger.warning(
                "[IsolatedSession] 手动 LLM 压缩失败，返回压缩失败，上下文未修改"
            )
            return contexts, "failed"

        # truncate_by_turns：仅保留最近 keep_count 条消息
        return system_msgs + recent_msgs, "ok"

    # ── LLM 摘要调用（提取公共逻辑）──────────────────────────────

    async def _call_llm_summary(
        self,
        old_msgs: list[dict],
        group_cfg: dict,
        event: AstrMessageEvent,
    ) -> tuple[str, str | None]:
        """调用 LLM 生成历史对话摘要。

        返回 (status, summary)：
        - ("ok", summary): 压缩成功
        - ("failed", None): 压缩失败（异常或返回空摘要）
        - ("timeout", None): 压缩请求超时
        """
        old_text = self._contexts_to_text(old_msgs)
        instruction = (
            group_cfg.get("llm_compress_instruction") or DEFAULT_COMPRESS_INSTRUCTION
        )
        compress_prompt = (
            f"{instruction}\n\nFull conversation history to summarize:\n{old_text}"
        )

        compress_provider_id = group_cfg.get("llm_compress_provider_id", "")
        if not compress_provider_id:
            compress_provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            if self.config.get("enable_debug_log"):
                logger.debug(
                    f"[IsolatedSession] 未配置压缩模型，使用当前聊天模型: {compress_provider_id}"
                )

        timeout = float(group_cfg.get("llm_compress_timeout", 30) or 0)

        try:
            coro = self.context.llm_generate(
                chat_provider_id=compress_provider_id,
                prompt=compress_prompt,
                session_id=f"isolated_compress_{int(time.time())}",
            )
            if timeout > 0:
                llm_resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                llm_resp = await coro
            summary = llm_resp.completion_text.strip() if llm_resp else ""
            if not summary:
                logger.warning("[IsolatedSession] LLM 压缩返回空摘要")
                return "failed", None
            return "ok", summary
        except (asyncio.TimeoutError, TimeoutError):
            logger.error(f"[IsolatedSession] LLM 压缩请求超时（超过 {timeout} 秒）")
            return "timeout", None
        except Exception as e:
            logger.error(f"[IsolatedSession] LLM 压缩失败: {e}")
            return "failed", None

    @staticmethod
    def _build_summary_pair(summary: str) -> list[dict]:
        """构建「摘要 user + 确认 assistant」消息对"""
        return [
            {"role": "user", "content": f"我们的历史对话摘要:\n{summary}"},
            {"role": "assistant", "content": "已确认理解之前的对话内容。"},
        ]

    # ── LLM 压缩（Token 超限时） ─────────────────────────────────

    async def _llm_compress(
        self,
        contexts: list[dict],
        max_tokens: int,
        group_cfg: dict,
        event: AstrMessageEvent,
    ) -> list[dict]:
        """LLM 摘要压缩（Token 超限触发）：旧历史 → 摘要 + 最近 N% 精确上下文"""
        keep_ratio = min(
            max(float(group_cfg.get("llm_compress_keep_recent_ratio", 0.15)), 0.0),
            0.3,
        )
        keep_tokens = int(max_tokens * keep_ratio)

        system_msgs = [m for m in contexts if m.get("role") == "system"]
        non_system = [m for m in contexts if m.get("role") != "system"]

        # 从后往前扫描，将轮次分为「最近（保留）」和「旧（压缩）」
        turns = self._group_into_turns(non_system)
        old_turns: list[list[dict]] = []
        recent_turns: list[list[dict]] = []
        recent_token_count = 0

        for turn in reversed(turns):
            turn_tokens = self._count_tokens(turn)
            if recent_token_count + turn_tokens <= keep_tokens:
                recent_turns.insert(0, turn)
                recent_token_count += turn_tokens
            else:
                old_turns.insert(0, turn)

        if not old_turns:
            return contexts

        old_msgs = [msg for turn in old_turns for msg in turn]
        status, summary = await self._call_llm_summary(old_msgs, group_cfg, event)
        if status == "timeout":
            logger.warning("[IsolatedSession] LLM 压缩超时，回退为丢弃固定轮次")
            return self._discard_old_turns(contexts, self._get_dequeue_turns(group_cfg))
        if status != "ok":
            logger.warning("[IsolatedSession] LLM 压缩失败，回退到轮次截断")
            return self._truncate_by_tokens_full(contexts, max_tokens)

        # 组装: [system] + [摘要消息对] + [recent turns]
        result: list[dict] = list(system_msgs)
        result.extend(self._build_summary_pair(summary))
        for turn in recent_turns:
            result.extend(turn)

        if self.config.get("enable_debug_log"):
            logger.debug(
                f"[IsolatedSession] LLM 压缩完成: "
                f"原始={len(contexts)}条 → 压缩后={len(result)}条, "
                f"摘要长度={len(summary)}"
            )

        return result

    # ── Token 估算 ───────────────────────────────────────────────

    def _count_tokens(self, messages: list[dict]) -> int:
        """Token 估算（与 AstrBot EstimateTokenCounter 规则一致）:
        中文 0.6 token/字, 其他 0.3 token/字符, 图片 765, 音频 500
        """
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self._estimate_text_tokens(content)
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        total += self._estimate_text_tokens(part.get("text", ""))
                    elif ptype == "image_url":
                        total += 765
                    elif ptype == "audio_url":
                        total += 500
        return total

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        """估算纯文本 Token 数"""
        chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - chinese
        return int(chinese * 0.6 + other * 0.3)

    # ── 轮次分组 & Token 截断 ────────────────────────────────────

    def _group_into_turns(self, messages: list[dict]) -> list[list[dict]]:
        """将扁平消息列表按 (user + assistant [+ tool]) 分组为轮次"""
        turns: list[list[dict]] = []
        current: list[dict] = []
        for msg in messages:
            if msg.get("role") == "user" and current:
                turns.append(current)
                current = []
            current.append(msg)
        if current:
            turns.append(current)
        return turns

    @staticmethod
    def _get_dequeue_turns(group_cfg: dict) -> int:
        """获取压缩超时回退时一次丢弃的固定轮次数（至少 1 轮）"""
        return max(1, int(group_cfg.get("dequeue_turns", 10) or 1))

    def _discard_old_turns(
        self, contexts: list[dict], discard_turns: int
    ) -> list[dict]:
        """丢弃最旧的固定轮次（保留 system 消息），用于压缩超时回退"""
        system_msgs = [m for m in contexts if m.get("role") == "system"]
        non_system = [m for m in contexts if m.get("role") != "system"]
        if not non_system:
            return contexts

        turns = self._group_into_turns(non_system)
        drop_count = min(max(1, int(discard_turns)), len(turns) - 1)
        if drop_count <= 0:
            return contexts

        remaining = [msg for turn in turns[drop_count:] for msg in turn]
        return system_msgs + remaining

    def _truncate_by_tokens_full(
        self, contexts: list[dict], max_tokens: int
    ) -> list[dict]:
        """按 Token 数从旧到新截断，保留 system 消息"""
        system_msgs = [m for m in contexts if m.get("role") == "system"]
        non_system = [m for m in contexts if m.get("role") != "system"]

        sys_tokens = self._count_tokens(system_msgs)
        available = max_tokens - sys_tokens

        result: list[dict] = []
        remaining = available
        for msg in reversed(non_system):
            t = self._count_tokens([msg])
            if remaining - t < 0:
                break
            remaining -= t
            result.append(msg)
        result.reverse()
        return system_msgs + result

    @staticmethod
    def _contexts_to_text(contexts: list[dict]) -> str:
        """将 OpenAI 格式上下文集转为纯文本（供 LLM 压缩使用）"""
        lines: list[str] = []
        for msg in contexts:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                lines.append(f"[{role}]: {content}")
            elif isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                lines.append(f"[{role}]: {' '.join(texts)}")
        return "\n".join(lines)

    # ── 辅助 ─────────────────────────────────────────────────────

    @staticmethod
    def _find_group_config(group_id: str, whitelist: list[dict]) -> dict | None:
        """在白名单中按 group_id 查找配置"""
        for item in whitelist:
            if str(item.get("group_id", "")) == group_id:
                return item
        return None

    def _mcfg(self, key: str, default):
        """读取记忆配置：优先「memory」分组，兼容旧版扁平键。"""
        try:
            group = self.config.get("memory")
            if isinstance(group, dict) and key in group:
                return group.get(key, default)
        except Exception:
            pass
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _track_task(self, task: asyncio.Task) -> None:
        """登记后台任务引用并自动清理，防止任务被垃圾回收。"""

        def _done(_: asyncio.Task) -> None:
            self._pending_tasks.discard(task)
            if not task.cancelled() and task.exception():
                logger.error(f"[IsolatedSession] 后台任务异常: {task.exception()}")

        task.add_done_callback(_done)
        self._pending_tasks.add(task)

    async def _buffer_extract_turn(
        self,
        owner: str,
        conversation_id: str,
        turn: tuple[str, str],
        interval: int,
    ) -> list[tuple[str, str]]:
        """持久化一轮待抽取对话，达到间隔时返回一个完整批次。"""
        lock = self._extract_locks.setdefault(owner, asyncio.Lock())
        async with lock:
            raw_state = await sp.session_get(owner, "memory_extract_state", {})
            state = raw_state if isinstance(raw_state, dict) else {}
            if str(state.get("conversation_id") or "") != conversation_id:
                state = {"conversation_id": conversation_id, "turns": []}

            pending: list[tuple[str, str]] = []
            raw_turns = state.get("turns", [])
            if isinstance(raw_turns, list):
                for item in raw_turns:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        pending.append((str(item[0] or ""), str(item[1] or "")))
            pending.append(turn)

            if len(pending) < interval:
                await sp.session_put(
                    owner,
                    "memory_extract_state",
                    {"conversation_id": conversation_id, "turns": pending},
                )
                return []

            batch = pending[:interval]
            remaining = pending[interval:]
            if remaining:
                await sp.session_put(
                    owner,
                    "memory_extract_state",
                    {"conversation_id": conversation_id, "turns": remaining},
                )
            else:
                await sp.session_remove(owner, "memory_extract_state")
            # 清理旧版本遗留的独立计数，避免升级后产生歧义。
            try:
                await sp.session_remove(owner, "memory_turn_count")
            except Exception as e:
                logger.debug(f"[IsolatedSession] 清理旧记忆抽取计数失败: {e}")
            return batch

    async def _clear_extract_state(self, owner: str) -> None:
        """清除某用户当前会话的待抽取轮次及旧版计数。"""
        lock = self._extract_locks.setdefault(owner, asyncio.Lock())
        async with lock:
            for key in ("memory_extract_state", "memory_turn_count"):
                try:
                    await sp.session_remove(owner, key)
                except Exception as e:
                    logger.debug(
                        f"[IsolatedSession] 清理记忆抽取状态失败({key}): {e}"
                    )

    def _schedule_task(self, coro) -> None:
        """调度一个后台协程（无事件循环时静默忽略）。"""
        try:
            self._track_task(asyncio.create_task(coro))
        except RuntimeError:
            pass

    # ── 用户命令 ─────────────────────────────────────────────────

    @filter.command("会话重置", alias={"session_reset"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """重置当前用户在当前群聊中的隔离会话"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        try:
            await conv_mgr.delete_conversations_by_user_id(user_umo)
            self._conv_cache.pop((group_id, user_id), None)
            # 旧会话的待抽取缓冲随之丢弃，避免把重置前的对话抽成记忆。
            await self._clear_extract_state(user_umo)
            msg = "✅ 已重置您在此群聊中的对话上下文。下次发言将创建新的独立会话。"
            # 可选：随会话重置一并清空记忆
            if self._mcfg("memory_reset_with_session", False) and self.memory:
                cleared = await self.memory.clear(user_umo)
                msg += f"\n已同步清空记忆 {cleared} 条。"
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"[IsolatedSession] /会话重置 失败: {e}")
            yield event.plain_result(f"❌ 重置失败: {e}")

    @filter.command("会话信息", alias={"session_info"})
    async def cmd_info(self, event: AstrMessageEvent):
        """查看当前隔离会话信息"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        group_cfg = self._find_group_config(group_id, whitelist)
        if not group_cfg:
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        cid = await conv_mgr.get_curr_conversation_id(user_umo)
        conv = None
        if cid:
            conv = await conv_mgr.get_conversation(user_umo, cid)

        ctx_count = 0
        est_tokens = 0
        turn_count = 0
        if conv and conv.history:
            contexts = json.loads(conv.history)
            ctx_count = len(contexts)
            est_tokens = self._count_tokens(contexts)
            turn_count = len(
                self._group_into_turns(
                    [m for m in contexts if m.get("role") != "system"]
                )
            )

        max_turns = group_cfg.get("max_turns", -1)
        max_tokens = group_cfg.get("max_tokens", 0)
        strategy = group_cfg.get("compression_strategy", "truncate_by_turns")
        strategy_label = {
            "truncate_by_turns": "轮次截断",
            "llm_compress": "LLM摘要压缩",
        }.get(strategy, strategy)

        info_lines = [
            "【隔离会话状态】",
            f"群聊ID: {group_id}",
            f"对话轮次: {turn_count}",
            f"消息数量: {ctx_count}",
            f"估算Token: {est_tokens}",
            f"最大轮次: {'无限制' if max_turns <= 0 else max_turns}",
            f"最大Token: {'无限制' if max_tokens <= 0 else max_tokens}",
            f"压缩策略: {strategy_label}",
            "",
            "使用 /会话重置 重置此会话",
            "使用 /会话压缩 [保留条数] 手动压缩上下文（默认保留 5 条，0=全部压缩）",
            "使用 /存档 <名称> 存档，/读档 <名称> 读档",
        ]
        yield event.plain_result("\n".join(info_lines))

    @filter.command("会话压缩", alias={"session_compress"})
    async def cmd_compress(self, event: AstrMessageEvent, keep_count: int = 5):
        """手动压缩当前隔离会话的上下文，可指定保留最近多少条消息（默认 5，0=全部压缩）"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        group_cfg = self._find_group_config(group_id, whitelist)
        if not group_cfg:
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        if keep_count < 0:
            yield event.plain_result(
                "❌ 保留条数不能为负数。\n"
                "用法: /会话压缩 [保留条数]\n"
                "不填默认保留 5 条最近消息，填 0 表示全部压缩。"
            )
            return

        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        cid = await conv_mgr.get_curr_conversation_id(user_umo)
        if not cid:
            yield event.plain_result("ℹ️ 您当前没有活跃的隔离会话，无需压缩。")
            return

        conv = await conv_mgr.get_conversation(user_umo, cid)
        if not conv or not conv.history:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需压缩。")
            return

        contexts = json.loads(conv.history)
        original_count = len(contexts)
        original_tokens = self._count_tokens(contexts)

        if original_count == 0:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需压缩。")
            return

        compressed, status = await self._manual_compress_all(
            contexts, group_cfg, event, keep_count
        )

        if status == "timeout":
            yield event.plain_result(
                "❌ 手动压缩失败：LLM 压缩请求超时，上下文未修改，请稍后重试。"
            )
            return

        if status == "failed":
            yield event.plain_result(
                "❌ 手动压缩失败：LLM 压缩请求出错或返回空摘要，上下文未修改，请稍后重试。"
            )
            return

        if compressed == contexts:
            yield event.plain_result(
                f"ℹ️ 当前会话最近 {keep_count} 条消息以内的内容无需压缩，未做修改。\n"
                f"消息: {original_count} 条 | Token: {original_tokens}"
            )
            return

        # 持久化到数据库
        await conv_mgr.update_conversation(
            unified_msg_origin=user_umo,
            conversation_id=cid,
            history=compressed,
        )
        # 更新内存缓存
        self._conv_cache[(group_id, user_id)] = cid
        self._last_active[cid] = time.time()

        new_count = len(compressed)
        new_tokens = self._count_tokens(compressed)
        strategy = group_cfg.get("compression_strategy", "truncate_by_turns")
        strategy_label = {
            "truncate_by_turns": "轮次截断",
            "llm_compress": "LLM摘要压缩",
        }.get(strategy, strategy)
        keep_desc = (
            f"保留最近 {keep_count} 条" if keep_count > 0 else "全部压缩（不保留消息）"
        )

        yield event.plain_result(
            f"✅ 手动压缩完成\n"
            f"策略: {strategy_label}\n"
            f"{keep_desc}\n"
            f"消息: {original_count} → {new_count}\n"
            f"Token: {original_tokens} → {new_tokens}"
        )

    @filter.command("存档", alias={"session_save"})
    async def cmd_save(self, event: AstrMessageEvent, slot_name: str = ""):
        """将当前隔离会话保存为命名存档"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        slot_name = slot_name.strip()
        if not slot_name or not SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 个字符。\n"
                "用法: /存档 <存档名>"
            )
            return

        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, group_id)
        archive_umo = self._build_archive_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        cid = await conv_mgr.get_curr_conversation_id(user_umo)
        if not cid:
            yield event.plain_result("ℹ️ 您当前没有活跃的隔离会话，无需存档。")
            return

        conv = await conv_mgr.get_conversation(user_umo, cid)
        if not conv or not conv.history:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需存档。")
            return

        contexts = json.loads(conv.history)
        if not contexts:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需存档。")
            return

        try:
            overwritten = False
            existing = await self._find_archive(archive_umo, slot_name)
            if existing:
                await conv_mgr.delete_conversation(archive_umo, existing.cid)
                overwritten = True

            await conv_mgr.new_conversation(
                unified_msg_origin=archive_umo,
                platform_id=event.get_platform_name(),
                content=contexts,
                title=slot_name,
            )
        except Exception as e:
            logger.error(f"[IsolatedSession] /存档 失败: {e}")
            yield event.plain_result(f"❌ 存档失败: {e}")
            return

        msg_count = len(contexts)
        tokens = self._count_tokens(contexts)
        yield event.plain_result(
            f"💾 存档成功（{'已覆盖同名存档' if overwritten else '新建存档'}）\n"
            f"存档名: {slot_name}\n"
            f"消息: {msg_count} 条\n"
            f"Token: {tokens}"
        )

    @filter.command("读档", alias={"session_load"})
    async def cmd_load(self, event: AstrMessageEvent, slot_name: str = ""):
        """载入命名存档，替换当前隔离会话上下文"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        slot_name = slot_name.strip()
        if not slot_name or not SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 个字符。\n"
                "用法: /读档 <存档名>"
            )
            return

        user_id = event.get_sender_id()
        user_umo = self._build_user_umo(event, user_id, group_id)
        archive_umo = self._build_archive_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        slot = await self._find_archive(archive_umo, slot_name)
        if not slot:
            yield event.plain_result(
                f"❌ 未找到存档「{slot_name}」。可用 /存档列表 查看全部存档。"
            )
            return

        archive_history = json.loads(slot.history) if slot.history else []
        if not archive_history:
            yield event.plain_result(f"❌ 存档「{slot_name}」内容为空，无法载入。")
            return

        try:
            cid = await conv_mgr.get_curr_conversation_id(user_umo)
            if not cid:
                # 无活跃会话时先创建新的隔离对话再载入
                cid = await conv_mgr.new_conversation(
                    user_umo, event.get_platform_name()
                )
            await conv_mgr.update_conversation(
                unified_msg_origin=user_umo,
                conversation_id=cid,
                history=archive_history,
            )
            # 读档替换了当前上下文，不能继续拼接读档前的待抽取轮次。
            await self._clear_extract_state(user_umo)
            # 更新内存缓存
            self._conv_cache[(group_id, user_id)] = cid
            self._last_active[cid] = time.time()
        except Exception as e:
            logger.error(f"[IsolatedSession] /读档 失败: {e}")
            yield event.plain_result(f"❌ 读档失败: {e}")
            return

        msg_count = len(archive_history)
        tokens = self._count_tokens(archive_history)
        yield event.plain_result(
            f"📂 读档成功，当前对话已替换为存档内容\n"
            f"存档名: {slot_name}\n"
            f"消息: {msg_count} 条\n"
            f"Token: {tokens}"
        )

    @filter.command("存档列表", alias={"session_slots"})
    async def cmd_slots(self, event: AstrMessageEvent):
        """列出当前用户在群聊中的所有存档"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        user_id = event.get_sender_id()
        archive_umo = self._build_archive_umo(event, user_id, group_id)
        slots = await self._get_archives(archive_umo)

        if not slots:
            yield event.plain_result(
                "📭 您当前没有任何存档。使用 /存档 <存档名> 保存当前对话。"
            )
            return

        lines = ["🗂 您的存档列表:"]
        for i, conv in enumerate(slots, 1):
            history = json.loads(conv.history) if conv.history else []
            ts = conv.updated_at or 0
            time_str = (
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未知"
            )
            lines.append(
                f"{i}. {conv.title or '(未命名)'} | "
                f"{len(history)} 条 | {self._count_tokens(history)} token | {time_str}"
            )
        lines.append("")
        lines.append("使用 /读档 <存档名> 读档，/删档 <存档名> 删除存档")
        yield event.plain_result("\n".join(lines))

    @filter.command("删档", alias={"session_slot_delete"})
    async def cmd_slot_delete(self, event: AstrMessageEvent, slot_name: str = ""):
        """删除指定的命名存档"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        slot_name = slot_name.strip()
        if not slot_name or not SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 个字符。\n"
                "用法: /删档 <存档名>"
            )
            return

        user_id = event.get_sender_id()
        archive_umo = self._build_archive_umo(event, user_id, group_id)
        conv_mgr = self.context.conversation_manager

        slot = await self._find_archive(archive_umo, slot_name)
        if not slot:
            yield event.plain_result(
                f"❌ 未找到存档「{slot_name}」。可用 /存档列表 查看全部存档。"
            )
            return

        try:
            await conv_mgr.delete_conversation(archive_umo, slot.cid)
        except Exception as e:
            logger.error(f"[IsolatedSession] /删档 失败: {e}")
            yield event.plain_result(f"❌ 删除存档失败: {e}")
            return

        yield event.plain_result(f"🗑 已删除存档「{slot_name}」。")

    # ── 记忆系统命令 ────────────────────────────────────────────

    @filter.command("记忆状态", alias={"memory_status"})
    async def cmd_memory_status(self, event: AstrMessageEvent):
        """查看当前用户在当前群聊中的记忆状态"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        if not self.memory:
            yield event.plain_result(
                "ℹ️ 记忆系统未启用。请在插件配置中启用 memory_enabled，"
                "并在 memory_kb_name 中选择共享记忆知识库。"
            )
            return

        user_id = event.get_sender_id()
        owner = self._build_user_umo(event, user_id, group_id)
        user_on = await sp.session_get(owner, "memory_enabled", True)
        stats = await self.memory.stats(owner)
        tokens = self._count_tokens(
            [{"role": "user", "content": t} for t in stats.get("texts", [])]
        )
        half_life = float(self._mcfg("memory_half_life_days", 30) or 30)
        ttl = float(self._mcfg("memory_ttl_days", 90) or 90)
        top_k = int(self._mcfg("memory_inject_top_k", 3) or 3)

        def fmt(ts):
            return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "无"

        lines = [
            "【记忆状态】",
            f"功能开关: {'开' if user_on else '关（/记忆开关 开）'}",
            f"记忆条数: {stats.get('count', 0)}",
            f"最早记忆: {fmt(stats.get('oldest'))}",
            f"最近记忆: {fmt(stats.get('newest'))}",
            f"估算Token: {tokens}",
            f"衰减半衰期: {half_life} 天",
            f"遗忘阈值(TTL): {ttl} 天",
            f"每次注入: 最多 {top_k} 条",
            "",
            "使用 /记忆查询 <内容> 预览召回结果",
            "使用 /记忆清除 清空当前记忆",
            "使用 /记忆开关 开|关 切换",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("记忆清除", alias={"memory_clear"})
    async def cmd_memory_clear(self, event: AstrMessageEvent):
        """清空当前用户在当前群聊中的全部记忆"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        if not self.memory:
            yield event.plain_result("ℹ️ 记忆系统未启用。")
            return

        user_id = event.get_sender_id()
        owner = self._build_user_umo(event, user_id, group_id)
        count = await self.memory.clear(owner)
        yield event.plain_result(f"🗑 已清除 {count} 条记忆。")

    @filter.command("记忆开关", alias={"memory_toggle"})
    async def cmd_memory_toggle(self, event: AstrMessageEvent, state: str = ""):
        """开启或关闭当前用户在当前群聊中的记忆功能"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        if not self.memory:
            yield event.plain_result("ℹ️ 记忆系统未启用。")
            return

        user_id = event.get_sender_id()
        owner = self._build_user_umo(event, user_id, group_id)
        state = (state or "").strip().lower()
        if state in ("开", "on", "true", "1", "启用"):
            await sp.session_put(owner, "memory_enabled", True)
            yield event.plain_result("✅ 已开启记忆功能。")
        elif state in ("关", "off", "false", "0", "禁用"):
            await sp.session_put(owner, "memory_enabled", False)
            await self._clear_extract_state(owner)
            yield event.plain_result("✅ 已关闭记忆功能。")
        else:
            cur = await sp.session_get(owner, "memory_enabled", True)
            yield event.plain_result(
                f"ℹ️ 当前记忆功能: {'开' if cur else '关'}\n用法: /记忆开关 开|关"
            )

    @filter.command("记忆查询", alias={"memory_query"})
    async def cmd_memory_query(self, event: AstrMessageEvent, query: str = ""):
        """预览当前用户记忆的召回结果（含衰减后分数），用于调试衰减效果"""
        if not event.message_obj.group_id:
            yield event.plain_result("❌ 此命令仅在群聊中可用。")
            return

        group_id = str(event.message_obj.group_id)
        whitelist = self.config.get("whitelist_groups", [])
        if not self._find_group_config(group_id, whitelist):
            yield event.plain_result("❌ 当前群聊未启用会话隔离。")
            return

        if not self.memory:
            yield event.plain_result("ℹ️ 记忆系统未启用。")
            return

        query = (query or "").strip()
        if not query:
            yield event.plain_result("用法: /记忆查询 <内容>")
            return

        user_id = event.get_sender_id()
        owner = self._build_user_umo(event, user_id, group_id)
        hits = await self.memory.recall(owner, query)
        if not hits:
            yield event.plain_result("🔍 未召回相关记忆。")
            return

        lines = ["🔍 记忆召回结果（按衰减后分数排序）:"]
        for i, hit in enumerate(hits, 1):
            age = float(hit.get("age_days") or 0.0)
            age_label = "今天" if age < 1 else f"{int(age)}天前"
            lines.append(
                f"{i}. {hit['text']}\n"
                f"   相似度={hit.get('similarity', 0):.3f} "
                f"衰减分={hit.get('effective', 0):.4f}（{age_label}）"
            )
        yield event.plain_result("\n".join(lines))
