"""
astrbot_plugin_isolated_session - 群聊会话隔离插件

为白名单内的群聊实现每位成员的独立对话上下文，
支持每群聊配置独立的轮次限制、最大 Token 数及压缩策略。
"""

import asyncio
import json
import time

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.provider import ProviderRequest
from astrbot.api import logger, AstrBotConfig
from astrbot.core.platform.message_type import MessageType
from astrbot.core.db.po import Conversation

# ── 默认 LLM 压缩提示词（与 AstrBot 默认值一致） ──────────────
DEFAULT_COMPRESS_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary "
    "of the key topics, context, and important details discussed so far. "
    "The summary should capture all essential information needed to continue "
    "the conversation coherently."
)

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
        logger.info(
            f"[IsolatedSession] 插件已加载，白名单群聊数: {len(whitelist)}"
        )

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
        req.contexts = (
            json.loads(user_conv.history) if user_conv.history else []
        )

        # 5. 预截断 / 压缩上下文
        await self._pre_truncate_contexts(req, group_cfg, event)

        if self.config.get("enable_debug_log"):
            logger.debug(
                f"[IsolatedSession] user={user_id} group={group_id} "
                f"cid={user_conv.cid} contexts={len(req.contexts)}"
            )

    # ── UMO 构造 ─────────────────────────────────────────────────

    def _build_user_umo(
        self, event: AstrMessageEvent, user_id: str, group_id: str
    ) -> str:
        """构造每用户隔离 UMO: platform:GroupMessage:isolated__{user_id}__{group_id}"""
        platform = event.get_platform_name()
        msg_type = MessageType.GROUP_MESSAGE.value  # "GroupMessage"
        return f"{platform}:{msg_type}:isolated__{user_id}__{group_id}"

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

        logger.info(
            f"[IsolatedSession] 新建隔离会话: user={user_id} group={group_id}"
        )
        return conv

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
                        contexts = system_msgs + self._build_summary_pair(summary) + recent_msgs
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
                contexts = await self._llm_compress(contexts, max_tokens, group_cfg, event)
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

    # ── 手动压缩：压缩所有内容 ────────────────────────────────────

    async def _manual_compress_all(
        self,
        contexts: list[dict],
        group_cfg: dict,
        event: AstrMessageEvent,
    ) -> tuple[list[dict], str]:
        """手动压缩全部上下文：所有非 system 消息压缩为摘要或截断至保留轮次。

        返回 (压缩结果, 状态)，状态取值：
        - "ok": 压缩成功（含 LLM 成功或回退到轮次截断）
        - "timeout": LLM 压缩超时，上下文未修改，由调用方报告压缩失败
        """
        if not contexts:
            return contexts, "ok"

        system_msgs = [m for m in contexts if m.get("role") == "system"]
        non_system = [m for m in contexts if m.get("role") != "system"]

        if not non_system:
            return contexts, "ok"

        strategy = group_cfg.get("compression_strategy", "truncate_by_turns")

        if strategy == "llm_compress":
            status, summary = await self._call_llm_summary(
                non_system, group_cfg, event
            )
            if status == "ok":
                return system_msgs + self._build_summary_pair(summary), "ok"
            if status == "timeout":
                # 手动压缩超时：返回压缩失败，不修改上下文
                logger.warning(
                    "[IsolatedSession] 手动 LLM 压缩超时，返回压缩失败"
                )
                return contexts, "timeout"
            # LLM 压缩失败，回退到轮次截断
            logger.warning(
                "[IsolatedSession] 手动 LLM 压缩失败，回退到轮次截断"
            )

        # truncate_by_turns 或回退：仅保留最近 max_turns 轮
        max_turns = group_cfg.get("max_turns", 50)
        keep_turns = max(1, max_turns if max_turns > 0 else 10)
        turns = self._group_into_turns(non_system)
        recent = turns[-keep_turns:]
        recent_msgs = [msg for turn in recent for msg in turn]
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
            f"{instruction}\n\n"
            f"Full conversation history to summarize:\n{old_text}"
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
            logger.error(
                f"[IsolatedSession] LLM 压缩请求超时（超过 {timeout} 秒）"
            )
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
        status, summary = await self._call_llm_summary(
            old_msgs, group_cfg, event
        )
        if status == "timeout":
            logger.warning(
                "[IsolatedSession] LLM 压缩超时，回退为丢弃固定轮次"
            )
            return self._discard_old_turns(
                contexts, self._get_dequeue_turns(group_cfg)
            )
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
                        total += self._estimate_text_tokens(
                            part.get("text", "")
                        )
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
    def _find_group_config(
        group_id: str, whitelist: list[dict]
    ) -> dict | None:
        """在白名单中按 group_id 查找配置"""
        for item in whitelist:
            if str(item.get("group_id", "")) == group_id:
                return item
        return None

    # ── 用户命令 ─────────────────────────────────────────────────

    @filter.command("session_reset")
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
            yield event.plain_result(
                "✅ 已重置您在此群聊中的对话上下文。下次发言将创建新的独立会话。"
            )
        except Exception as e:
            logger.error(f"[IsolatedSession] /session_reset 失败: {e}")
            yield event.plain_result(f"❌ 重置失败: {e}")

    @filter.command("session_info")
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
            turn_count = len(self._group_into_turns(
                [m for m in contexts if m.get("role") != "system"]
            ))

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
            "使用 /session_reset 重置此会话",
            "使用 /session_compress 手动压缩上下文",
        ]
        yield event.plain_result("\n".join(info_lines))

    @filter.command("session_compress")
    async def cmd_compress(self, event: AstrMessageEvent):
        """手动压缩当前隔离会话的上下文"""
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
            contexts, group_cfg, event
        )

        if status == "timeout":
            yield event.plain_result(
                "❌ 手动压缩失败：LLM 压缩请求超时，上下文未修改，请稍后重试。"
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

        yield event.plain_result(
            f"✅ 手动压缩完成\n"
            f"策略: {strategy_label}\n"
            f"消息: {original_count} → {new_count}\n"
            f"Token: {original_tokens} → {new_tokens}"
        )
