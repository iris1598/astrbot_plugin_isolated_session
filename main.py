"""
astrbot_plugin_isolated_memory - 随时间衰减记忆 + 会话指令（官方会话隔离后端）

从 astrbot_plugin_isolated_session v1.5.x 拆分而来：
- 记忆系统：召回注入 / 间隔抽取 / 衰减遗忘 / 共享知识库存储；
- 会话指令：/会话重置 /会话信息 /会话压缩 /存档 /读档 /存档列表 /删档，
  后端不再使用 isolated__ 私有命名空间，直接操作 AstrBot 官方对话体系
  （ConversationManager + 当前事件 UMO）。

会话归属 owner 直接取当前事件的 unified_msg_origin。
推荐搭配 AstrBot 官方「会话隔离（platform_settings.unique_session）」：
群聊中每位成员的 UMO 天然按 群×用户 独立，记忆与会话指令即按成员独立。
旧插件数据可通过 astrbot_plugin_isolated_session_export 迁移。

- 召回注入：on_llm_request → 混合检索(稠密+BM25+RRF) → 衰减打分 → top_k 注入
  （extra_user_content_parts + mark_as_temp，不写入对话历史）
- 抽取写入：on_llm_response → 每 N 轮把积累的对话交给抽取模型 → 去重后写入
  共享知识库（后台任务，不阻塞回复）
- 遗忘：超过 TTL 不再注入并惰性清扫删除；LRU 上限裁剪；召回强化刷新时间钟
- 存档 = 官方「同一会话的多对话（带标题）」，WebUI 对话管理同样可见可管
"""

import asyncio
import re
import time

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.active_event_registry import active_event_registry

try:  # 与内置 reset 对齐：第三方 Agent 状态键（常量路径变动时降级）
    from astrbot.core.agent.runners.deerflow.constants import (
        DEERFLOW_THREAD_ID_KEY,
    )
except Exception:  # pragma: no cover
    DEERFLOW_THREAD_ID_KEY = "deerflow_thread_id"

THIRD_PARTY_RUNNER_KEYS = {
    "dify": "dify_conversation_id",
    "coze": "coze_conversation_id",
    "dashscope": "dashscope_conversation_id",
    "deerflow": DEERFLOW_THREAD_ID_KEY,
}

from . import favorability_bridge as FB
from . import session_tools as T
from .memory import MemoryManager


class Main(Star):
    """衰减记忆 + 官方会话指令工具插件。"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context, config)
        self.config: AstrBotConfig = config or {}
        self.memory: MemoryManager | None = None
        self._pending_tasks: set = set()
        self._extract_locks: dict[str, asyncio.Lock] = {}
        self._warned_no_isolation = False
        # 惰性自愈：上次初始化尝试时间与失败原因（配置改后无需重启）
        self._last_init_try: float = 0.0
        self._init_reason: str = ""

    # ── 初始化 ──────────────────────────────────────────────────

    async def _try_init(self) -> str | None:
        """尝试初始化记忆系统；成功返回 None，失败返回人类可读原因。"""
        if not self._mcfg("memory_enabled", False):
            return "全局开关未开启：请在插件配置「记忆系统」分组中启用 memory_enabled"
        try:
            if getattr(self.context, "kb_manager", None) is None:
                return "当前 AstrBot 版本的知识库模块不可用"
            mgr = MemoryManager(self.context, self.config)
            probe = await mgr.ensure_kb()
            if probe is None:
                kb_name = mgr._kb_name()
                if not kb_name:
                    return "请在插件配置 memory_kb_name 中选择共享记忆知识库"
                return f"知识库「{kb_name}」不可用（不存在、未配置 Embedding 模型或初始化失败）"
            self.memory = mgr
            self._init_reason = ""
            logger.info(f"[IsolatedMemory] 记忆系统就绪，共享知识库: {probe.kb.kb_name}")
            cfg = self.context.get_config() or {}
            ps = cfg.get("platform_settings", {}) or {}
            if not ps.get("unique_session", False):
                logger.warning(
                    "[IsolatedMemory] 注意：官方「会话隔离(unique_session)」未开启。"
                    "群聊记忆将按整个群共享（owner=群会话 UMO），"
                    "建议开启 platform_settings.unique_session 以实现按成员独立记忆。"
                )
            return None
        except Exception as e:
            self.memory = None
            return f"初始化异常: {e}"

    async def initialize(self) -> None:
        self._last_init_try = time.time()
        reason = await self._try_init()
        if reason:
            self._init_reason = reason
            logger.info(f"[IsolatedMemory] 记忆系统未启用: {reason}")

    async def _ensure_memory(self) -> MemoryManager | None:
        """返回可用的 MemoryManager；初始化未成功时按 15 秒节流自动重试，
        使「先加载后改配置」无需重启插件即可生效。"""
        if self.memory:
            return self.memory
        now = time.time()
        if now - self._last_init_try >= 15.0:
            self._last_init_try = now
            reason = await self._try_init()
            if reason:
                self._init_reason = reason
        return self.memory

    async def terminate(self) -> None:
        for task in list(self._pending_tasks):
            task.cancel()

    # ══════════════════════════════════════════════════════════
    #  记忆：核心钩子
    # ══════════════════════════════════════════════════════════

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """LLM 请求前：召回衰减记忆并注入为临时内容块。"""
        if await self._ensure_memory() is None:
            return
        group_cfg = self._group_gate(event)
        if group_cfg is None:
            return
        owner = event.unified_msg_origin
        try:
            if not await sp.session_get(owner, "memory_enabled", True):
                return
            if not req.prompt or not req.prompt.strip():
                return

            # 绑定本轮会话 cid，供 on_llm_response 的抽取缓冲做隔离
            cid = getattr(req.conversation, "cid", None)
            if not cid:
                cid = await self.context.conversation_manager.get_curr_conversation_id(
                    owner
                )
            if cid:
                event.set_extra("_isolated_memory_conversation_id", str(cid))

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
                        f"[IsolatedMemory] 注入记忆 {len(hits)} 条: "
                        + "; ".join(
                            f"{h['text'][:16]}…({h['effective']:.4f})" for h in hits
                        )
                    )
            self._schedule_task(self.memory.sweep(owner))
        except Exception as e:
            logger.warning(f"[IsolatedMemory] 记忆注入失败: {e}")

    @filter.on_llm_response()
    async def on_llm_response(
        self, event: AstrMessageEvent, response: LLMResponse
    ) -> None:
        """LLM 回复后：按间隔抽取对话中的可记忆事实并写入记忆库。"""
        if await self._ensure_memory() is None:
            return
        group_cfg = self._group_gate(event)
        if group_cfg is None:
            return
        try:
            owner = event.unified_msg_origin
            if not await sp.session_get(owner, "memory_enabled", True):
                return

            user_text = (event.message_str or "").strip()
            reply_text = ((response.completion_text or "") if response else "").strip()
            if not reply_text:
                return

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
                f"[IsolatedMemory] 触发记忆抽取: owner={owner}, 输入 {len(turns)} 轮对话, "
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
            logger.warning(f"[IsolatedMemory] 记忆抽取调度失败: {e}")

    # ── 记忆：群聊门控 ─────────────────────────────────────────

    def _memory_groups(self) -> list[dict]:
        """启用的群列表：优先本插件 memory_groups，
        兼容旧插件 astrbot_plugin_isolated_session 的 whitelist_groups
        （直接沿用旧配置内容时无需再手工搬一次）。"""
        groups = self.config.get("memory_groups") or []
        if [g for g in groups if isinstance(g, dict) and str(g.get("group_id", ""))]:
            return [g for g in groups if isinstance(g, dict)]
        legacy = self.config.get("whitelist_groups") or []
        out = []
        for g in legacy:
            if isinstance(g, dict) and str(g.get("group_id", "")):
                out.append({
                    "group_id": str(g.get("group_id", "")),
                    "group_name": str(g.get("group_name", "") or ""),
                    "memory_enabled": bool(g.get("memory_enabled", True)),
                })
        return out

    def _group_gate(self, event: AstrMessageEvent) -> dict | None:
        """消息所在群聊在启用列表且允许记忆时返回其配置，否则 None。"""
        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            return None
        group_cfg = self._find_group_config(str(group_id), self._memory_groups())
        if not group_cfg or not group_cfg.get("memory_enabled", True):
            return None
        return group_cfg

    def _gate_block_reason(self, event: AstrMessageEvent) -> str | None:
        """记忆命令的精确拦截原因；None 表示放行。"""
        group_id = getattr(event.message_obj, "group_id", None)
        if not group_id:
            return "此命令仅在群聊中可用。"
        groups = self._memory_groups()
        if not groups:
            return (
                f"群 {group_id} 未启用记忆：插件配置的启用列表（memory_groups）为空，"
                "请在 WebUI 插件配置中添加该群。"
            )
        cfg = self._find_group_config(str(group_id), groups)
        if cfg is None:
            listed = "、".join(
                f"{g.get('group_id')}({g.get('group_name') or '未命名'})"
                for g in groups[:10]
            )
            return (
                f"群 {group_id} 不在记忆启用列表中。当前已启用: {listed}"
                "（如需启用请在插件配置 memory_groups 添加本群）"
            )
        if not cfg.get("memory_enabled", True):
            return f"群 {group_id} 的记忆开关在插件配置中已关闭。"
        return None

    @staticmethod
    def _find_group_config(group_id: str, groups: list[dict]) -> dict | None:
        for item in groups or []:
            if str(item.get("group_id", "")) == group_id:
                return item
        return None

    # ── 记忆：人设捕获 ─────────────────────────────────────────

    async def _capture_persona(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> str | None:
        """捕获当前会话生效的人设文本（供记忆抽取 LLM 参考）。"""
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

    # ── 记忆：抽取间隔缓冲（与旧插件键格式兼容，迁移后可无缝续用）──

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
            try:
                await sp.session_remove(owner, "memory_turn_count")
            except Exception as e:
                logger.debug(f"[IsolatedMemory] 清理旧记忆抽取计数失败: {e}")
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
                        f"[IsolatedMemory] 清理记忆抽取状态失败({key}): {e}"
                    )

    # ── 记忆：辅助 ─────────────────────────────────────────────

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
        def _done(_: asyncio.Task) -> None:
            self._pending_tasks.discard(task)
            if not task.cancelled() and task.exception():
                logger.error(f"[IsolatedMemory] 后台任务异常: {task.exception()}")

        task.add_done_callback(_done)
        self._pending_tasks.add(task)

    def _schedule_task(self, coro) -> None:
        try:
            self._track_task(asyncio.create_task(coro))
        except RuntimeError:
            pass

    def _system_off_message(self) -> str:
        return f"ℹ️ 记忆系统未启用：{self._init_reason or '原因未知，请检查插件配置。'}"

    # ══════════════════════════════════════════════════════════
    #  会话指令（官方 ConversationManager 后端）
    # ══════════════════════════════════════════════════════════

    @property
    def _conv_mgr(self):
        return self.context.conversation_manager

    async def _current(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        cid = await self._conv_mgr.get_curr_conversation_id(umo)
        if not cid:
            return umo, None, None
        conv = await self._conv_mgr.get_conversation(umo, cid)
        return umo, cid, conv

    @filter.command("会话重置", alias={"session_reset", "reset_session"})
    async def cmd_reset(self, event: AstrMessageEvent):
        """重置当前会话的对话上下文（与官方 /reset 同语义：清空当前对话历史）"""
        umo = event.unified_msg_origin
        try:
            cfg = self.context.get_config(umo=umo) or {}
        except Exception:
            cfg = {}
        ps_cfg = cfg.get("platform_settings", {}) or {}
        unique = bool(ps_cfg.get("unique_session", False))
        is_group = bool(getattr(event.message_obj, "group_id", None))

        # 1) 权限场景：对齐官方 reset（支持 WebUI alter_cmd 覆盖，默认与官方一致：
        #    群聊+隔离关闭 → 管理员；其余 → 成员）
        if is_group:
            scene_key = "group_unique_on" if unique else "group_unique_off"
        else:
            scene_key = "private"
        required = "admin" if (is_group and not unique) else "member"
        try:
            alter = await sp.get_async("global", "global", "alter_cmd", {}) or {}
            reset_cfg = (alter.get("astrbot") or {}).get("reset") or {}
            required = reset_cfg.get(scene_key, required)
        except Exception:
            pass
        if required == "admin" and getattr(event, "role", "member") != "admin":
            yield event.plain_result(
                f"❌ 当前场景（{scene_key}）下重置需要管理员权限。"
                "官方隔离开启后每人独立，群成员即可重置自己的会话。"
            )
            return

        # 2) 模型可用性（与官方一致：无可用模型时不执行）
        try:
            has_provider = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            has_provider = True
        if not has_provider:
            yield event.plain_result(
                "😕 未找到可用的 LLM 模型，请先在 WebUI 配置。"
            )
            return

        # 3) 停止该会话正在运行的 Agent（官方 reset 行为）
        try:
            active_event_registry.stop_all(umo, exclude=event)
        except Exception as e:
            logger.debug(f"[IsolatedMemory] stop_all 失败(忽略): {e}")

        # 4) 清空当前对话历史 —— 官方语义：就地清空，不删除对话本身，
        #     更不影响其它对话（存档）。
        cleared_conv = False
        cid = await self._conv_mgr.get_curr_conversation_id(umo)
        if cid:
            await self._conv_mgr.update_conversation(umo, cid, [])
            cleared_conv = True
            event.set_extra("_clean_group_context_session", True)

        # 5) 第三方 Agent 运行器的会话状态一并清理
        runner_type = ""
        try:
            runner_type = (cfg.get("provider_settings", {}) or {}).get(
                "agent_runner_type", ""
            )
        except Exception:
            pass
        if runner_type in THIRD_PARTY_RUNNER_KEYS:
            try:
                await sp.session_remove(umo, THIRD_PARTY_RUNNER_KEYS[runner_type])
            except Exception:
                pass

        # 6) 本插件附加行为：待抽取缓冲随上下文作废
        await self._clear_extract_state(umo)

        msg = (
            "✅ 已清空当前对话上下文，下次发言将全新开始。（存档不受影响）"
            if cleared_conv
            else "ℹ️ 当前会话没有活跃对话，无需清空。（存档不受影响）"
        )
        # 7) 记忆联动（等价旧插件 memory_reset_with_session）
        mem = await self._ensure_memory()
        if (
            mem
            and self._mcfg("memory_reset_with_session", False)
            and self._group_gate(event) is not None
        ):
            cleared = await mem.clear(umo)
            msg += f"\n已同步清空记忆 {cleared} 条。"

        # 8) 好感度联动（若安装了 astrbot_plugin_favorability 则同步清除该人格下的评价）
        if self.config.get("favorability_reset_eval_with_session", True):
            try:
                fav_cleared, fav_persona = await FB.clear_favorability_eval(
                    self.context, event
                )
                if fav_cleared:
                    tag = (
                        f"【{fav_persona}】"
                        if fav_persona and fav_persona != "default"
                        else ""
                    )
                    msg += f"\n已同步清除{tag}好感度评价。"
            except Exception as e:
                logger.warning(
                    f"[IsolatedMemory] 会话重置同步清除好感度评价异常(已跳过): {e}"
                )

        yield event.plain_result(msg)

    @filter.command("会话信息", alias={"session_info"})
    async def cmd_info(self, event: AstrMessageEvent):
        """查看当前会话的对话状态（轮次/消息/Token/官方限制）"""
        umo, cid, conv = await self._current(event)
        contexts = T.parse_history(conv)
        non_system = [m for m in contexts if m.get("role") != "system"]
        turns = len(T.group_into_turns(non_system))

        all_convs = await self._conv_mgr.get_conversations(umo)
        slots = [c for c in all_convs if (getattr(c, "title", None) or "")]

        try:
            cfg = self.context.get_config(umo=umo) or {}
            ps = cfg.get("provider_settings", {}) or {}
        except Exception:
            ps = {}
        max_len = ps.get("max_context_length", "-")
        strategy = ps.get("context_limit_reached_strategy", "truncate_by_turns")

        lines = [
            "【会话状态】",
            f"会话: {umo}",
            f"对话: {cid[:8] + '…' if cid else '（尚无）'}"
            + (f"  标题: {conv.title}" if conv and conv.title else ""),
            f"对话轮次: {turns}",
            f"消息数量: {len(contexts)}",
            f"估算Token: {T.estimate_tokens(contexts)}",
            f"官方轮次上限: {max_len}",
            f"超限策略: {strategy}",
            f"存档数量: {len(slots)}",
            "",
            "/会话压缩 [保留条数]  手动压缩（默认保留 5 条，0=全部）",
            "/存档 <名称>  /读档 <名称>  /存档列表  /删档 <名称>",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("会话压缩", alias={"session_compress"})
    async def cmd_compress(self, event: AstrMessageEvent, keep_count: int = 5):
        """手动压缩当前对话上下文：LLM 摘要旧内容，保留最近 N 条（0=全部压缩）"""
        if keep_count < 0:
            yield event.plain_result(
                "❌ 保留条数不能为负数。\n"
                "用法: /会话压缩 [保留条数]，默认保留 5 条，0=全部压缩。"
            )
            return
        umo, cid, conv = await self._current(event)
        contexts = T.parse_history(conv)
        if not cid or not contexts:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需压缩。")
            return
        split = T.split_for_manual_compress(contexts, keep_count)
        if split is None:
            yield event.plain_result(
                f"ℹ️ 最近 {keep_count} 条以内的内容无需压缩，未做修改。"
            )
            return
        system_msgs, old_msgs, recent_msgs = split
        original_count = len(contexts)
        original_tokens = T.estimate_tokens(contexts)

        status, summary = await self._call_llm_summary(old_msgs, event, umo)
        if status == "timeout":
            yield event.plain_result(
                "❌ 手动压缩失败：LLM 请求超时，上下文未修改，请稍后重试。"
            )
            return
        if status != "ok":
            yield event.plain_result(
                "❌ 手动压缩失败：LLM 出错或返回空摘要，上下文未修改，请稍后重试。"
            )
            return

        compressed = T.assemble_compressed(system_msgs, summary, recent_msgs)
        await self._conv_mgr.update_conversation(
            unified_msg_origin=umo, conversation_id=cid, history=compressed
        )
        # 上下文被替换，旧待抽取轮次不再适用于当前对话
        await self._clear_extract_state(umo)
        keep_desc = (
            f"保留最近 {keep_count} 条" if keep_count > 0 else "全部压缩（不保留消息）"
        )
        yield event.plain_result(
            f"✅ 手动压缩完成\n"
            f"{keep_desc}\n"
            f"消息: {original_count} → {len(compressed)}\n"
            f"Token: {original_tokens} → {T.estimate_tokens(compressed)}"
        )

    async def _call_llm_summary(
        self, old_msgs: list[dict], event: AstrMessageEvent, umo: str
    ) -> tuple[str, str | None]:
        """调用 LLM 生成历史摘要：("ok", text) / ("timeout", None) / ("failed", None)。"""
        instruction = (
            self.config.get("compress_instruction") or T.DEFAULT_COMPRESS_INSTRUCTION
        )
        compress_prompt = (
            f"{instruction}\n\nFull conversation history to summarize:\n"
            f"{T.contexts_to_text(old_msgs)}"
        )
        provider_id = str(self.config.get("compress_provider_id", "") or "").strip()
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                provider_id = ""
        if not provider_id:
            return "failed", None
        timeout = float(self.config.get("compress_timeout", 30) or 0)
        try:
            coro = self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=compress_prompt,
                session_id=f"isolated_tools_compress_{int(time.time())}",
            )
            resp = (
                await asyncio.wait_for(coro, timeout=timeout) if timeout > 0
                else await coro
            )
            summary = resp.completion_text.strip() if resp else ""
            if not summary:
                return "failed", None
            return "ok", summary
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(f"[IsolatedMemory] 压缩 LLM 超时（{timeout}s）")
            return "timeout", None
        except Exception as e:
            logger.error(f"[IsolatedMemory] 压缩 LLM 失败: {e}")
            return "failed", None

    # ── 存档 / 读档 / 列表 / 删档 ───────────────────────────────

    @filter.command("存档", alias={"session_save"})
    async def cmd_save(self, event: AstrMessageEvent, slot_name: str = ""):
        """将当前对话上下文保存为命名存档（同名覆盖）"""
        slot_name = (slot_name or "").strip()
        if not slot_name or not T.SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 字符。\n"
                "用法: /存档 <存档名>"
            )
            return
        umo, cid, conv = await self._current(event)
        contexts = T.parse_history(conv)
        if not cid or not contexts:
            yield event.plain_result("ℹ️ 当前会话无历史内容，无需存档。")
            return
        try:
            overwritten = await T.create_or_overwrite_slot(
                self._conv_mgr, umo, event.get_platform_id(), slot_name, contexts
            )
        except Exception as e:
            logger.error(f"[IsolatedMemory] /存档 失败: {e}")
            yield event.plain_result(f"❌ 存档失败: {e}")
            return
        yield event.plain_result(
            f"💾 存档成功（{'已覆盖同名存档' if overwritten else '新建存档'}）\n"
            f"存档名: {slot_name}\n"
            f"消息: {len(contexts)} 条\n"
            f"Token: {T.estimate_tokens(contexts)}"
        )

    @filter.command("读档", alias={"session_load"})
    async def cmd_load(self, event: AstrMessageEvent, slot_name: str = ""):
        """载入命名存档，替换当前对话上下文（当前未存档内容将被覆盖）"""
        slot_name = (slot_name or "").strip()
        if not slot_name or not T.SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 字符。\n"
                "用法: /读档 <存档名>"
            )
            return
        umo = event.unified_msg_origin
        try:
            status, archive_history = await T.load_slot_into_current(
                self._conv_mgr, umo, slot_name, event.get_platform_id()
            )
        except Exception as e:
            logger.error(f"[IsolatedMemory] /读档 失败: {e}")
            yield event.plain_result(f"❌ 读档失败: {e}")
            return
        if status == "not_found":
            yield event.plain_result(
                f"❌ 未找到存档「{slot_name}」。可用 /存档列表 查看全部存档。"
            )
            return
        if status == "empty":
            yield event.plain_result(f"❌ 存档「{slot_name}」内容为空，无法载入。")
            return
        # 读档替换了当前上下文，不能继续拼接读档前的待抽取轮次
        await self._clear_extract_state(umo)
        yield event.plain_result(
            f"📂 读档成功，当前对话已替换为存档内容\n"
            f"存档名: {slot_name}\n"
            f"消息: {len(archive_history)} 条\n"
            f"Token: {T.estimate_tokens(archive_history)}"
        )

    @filter.command("存档列表", alias={"session_slots"})
    async def cmd_slots(self, event: AstrMessageEvent):
        """列出当前会话的所有命名存档"""
        umo = event.unified_msg_origin
        slots = await T.list_slots(self._conv_mgr, umo)
        if not slots:
            yield event.plain_result(
                "📭 您当前没有任何存档。使用 /存档 <存档名> 保存当前对话。"
            )
            return
        lines = ["🗂 您的存档列表:"]
        for i, conv in enumerate(slots, 1):
            history = T.parse_history(conv)
            ts = conv.updated_at or 0
            time_str = (
                time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "未知"
            )
            lines.append(
                f"{i}. {conv.title} | {len(history)} 条 | "
                f"{T.estimate_tokens(history)} token | {time_str}"
            )
        lines.append("")
        lines.append("使用 /读档 <存档名> 读档，/删档 <存档名> 删除存档")
        yield event.plain_result("\n".join(lines))

    @filter.command("删档", alias={"session_slot_delete"})
    async def cmd_slot_delete(self, event: AstrMessageEvent, slot_name: str = ""):
        """删除指定的命名存档"""
        slot_name = (slot_name or "").strip()
        if not slot_name or not T.SLOT_NAME_RE.match(slot_name):
            yield event.plain_result(
                "❌ 存档名称只能包含中英文、数字、下划线或短横线，且不超过 20 字符。\n"
                "用法: /删档 <存档名>"
            )
            return
        umo = event.unified_msg_origin
        try:
            ok = await T.delete_slot(self._conv_mgr, umo, slot_name)
        except Exception as e:
            logger.error(f"[IsolatedMemory] /删档 失败: {e}")
            yield event.plain_result(f"❌ 删除存档失败: {e}")
            return
        if not ok:
            yield event.plain_result(
                f"❌ 未找到存档「{slot_name}」。可用 /存档列表 查看全部存档。"
            )
            return
        yield event.plain_result(f"🗑 已删除存档「{slot_name}」。")

    @filter.command("会话工具", alias={"session_tools"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看会话与记忆指令帮助"""
        yield event.plain_result("\n".join([
            "【会话 + 记忆 工具】（官方会话隔离后端）",
            "/会话重置             清空当前对话上下文（与官方 reset 同语义，存档不受影响）",
            "/会话信息             轮次/消息/Token/官方限制/存档数",
            "/会话压缩 [保留条数]  手动 LLM 摘要压缩（默认保留 5 条，0=全部）",
            "/存档 <名称>  /读档 <名称>  /存档列表  /删档 <名称>",
            "—",
            "/记忆状态  /记忆查询 <内容>  /记忆开关 开|关  /记忆清除",
            "/记忆测评             依据全部记忆做锚点比对生成 MBTI 报告（娱乐向·结果可复现）",
            "提示：存档即官方「同会话多对话」，WebUI 对话管理同样可见。",
        ]))

    # ══════════════════════════════════════════════════════════
    #  记忆：用户命令
    # ══════════════════════════════════════════════════════════

    @filter.command("记忆状态", alias={"memory_status"})
    async def cmd_memory_status(self, event: AstrMessageEvent):
        """查看你在当前会话中的记忆状态"""
        if await self._ensure_memory() is None:
            yield event.plain_result(self._system_off_message())
            return
        reason = self._gate_block_reason(event)
        if reason:
            yield event.plain_result("❌ " + reason)
            return

        owner = event.unified_msg_origin
        user_on = await sp.session_get(owner, "memory_enabled", True)
        stats = await self.memory.stats(owner)
        tokens = T.estimate_tokens(
            [{"role": "user", "content": t} for t in stats.get("texts", [])]
        )
        half_life = float(self._mcfg("memory_half_life_days", 30) or 30)
        ttl = float(self._mcfg("memory_ttl_days", 90) or 90)
        top_k = int(self._mcfg("memory_inject_top_k", 3) or 3)

        def fmt(ts):
            return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "无"

        lines = [
            "【记忆状态】",
            f"会话归属(owner): {owner}",
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
        """清空你在当前会话中的全部记忆"""
        if await self._ensure_memory() is None:
            yield event.plain_result(self._system_off_message())
            return
        reason = self._gate_block_reason(event)
        if reason:
            yield event.plain_result("❌ " + reason)
            return
        owner = event.unified_msg_origin
        count = await self.memory.clear(owner)
        await self._clear_extract_state(owner)
        yield event.plain_result(f"🗑 已清除 {count} 条记忆。")

    @filter.command("记忆开关", alias={"memory_toggle"})
    async def cmd_memory_toggle(self, event: AstrMessageEvent, state: str = ""):
        """开启或关闭你在当前会话中的记忆功能"""
        if await self._ensure_memory() is None:
            yield event.plain_result(self._system_off_message())
            return
        reason = self._gate_block_reason(event)
        if reason:
            yield event.plain_result("❌ " + reason)
            return
        owner = event.unified_msg_origin
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
    async def cmd_memory_query(self, event: AstrMessageEvent, query: GreedyStr):
        """预览记忆的召回结果（含衰减后分数），用于调试衰减效果"""
        if await self._ensure_memory() is None:
            yield event.plain_result(self._system_off_message())
            return
        reason = self._gate_block_reason(event)
        if reason:
            yield event.plain_result("❌ " + reason)
            return
        query = (query or "").strip()
        if not query:
            yield event.plain_result("用法: /记忆查询 <内容>")
            return
        owner = event.unified_msg_origin
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

    @filter.command("记忆测评", alias={"memory_mbti", "mbti"})
    async def cmd_memory_mbti(self, event: AstrMessageEvent):
        """依据你在当前会话保存的全部记忆生成一份 MBTI 推测报告（娱乐向）"""
        if await self._ensure_memory() is None:
            yield event.plain_result(self._system_off_message())
            return
        reason = self._gate_block_reason(event)
        if reason:
            yield event.plain_result("❌ " + reason)
            return
        if not self._mcfg("memory_mbti_enabled", True):
            yield event.plain_result(
                "ℹ️ MBTI 测评功能已在插件配置（memory_mbti_enabled）中关闭。"
            )
            return

        owner = event.unified_msg_origin
        entries = await self.memory.collect_memory_entries(owner)
        min_count = max(1, int(self._mcfg("memory_mbti_min_memories", 8) or 8))
        if len(entries) < min_count:
            yield event.plain_result(
                f"ℹ️ 记忆数量不足，暂无法生成报告（当前 {len(entries)} 条，"
                f"至少需要 {min_count} 条）。\n"
                "记忆会在日常对话中按间隔自动积累，可用 /记忆状态 查看当前条数，"
                "或在插件配置中调低 memory_mbti_min_memories。"
            )
            return

        method = (
            str(self._mcfg("memory_mbti_method", "anchor") or "anchor").strip().lower()
        )
        if method == "llm":
            report = await self.memory.build_mbti_report(
                [entry["text"] for entry in entries], umo=owner
            )
        else:
            method = "anchor"
            report = await self.memory.build_mbti_anchor_report(entries)
        if report is None:
            reason = (
                "模型未返回有效结果（超时、无可用模型或返回为空）"
                if method == "llm"
                else "无法计算记忆向量（知识库未配置 Embedding 模型或调用失败）"
            )
            yield event.plain_result(
                f"❌ 生成失败：{reason}。请稍后重试，或检查记忆系统的知识库配置。"
            )
            return
        yield event.plain_result(self.memory.format_mbti_report(report))
