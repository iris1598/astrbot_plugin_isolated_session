"""astrbot_plugin_session_tools.session_tools

会话指令的纯逻辑层（不依赖 astrbot，可独立单元测试）：
- Token 估算 / 轮次分组 / 摘要消息对：与 astrbot_plugin_isolated_session 及
  AstrBot EstimateTokenCounter 规则一致；
- 手动压缩的内容切分；
- 存档名校验；
- 基于官方 ConversationManager 接口的存档槽位原语（duck-typed，
  只依赖 new_conversation/switch_conversation/get_conversations/
  delete_conversation/update_conversation 这些官方公开方法）。
"""

from __future__ import annotations

import json
import re
from typing import Any

# ── 存档名称规则：中英文、数字、下划线、短横线，1-20 个字符 ──
SLOT_NAME_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,20}$")

# ── 默认 LLM 压缩提示词（与 AstrBot 默认值一致） ──
DEFAULT_COMPRESS_INSTRUCTION = (
    "Based on our full conversation history, produce a concise summary "
    "of the key topics, context, and important details discussed. "
    "The summary should capture all essential information needed to continue "
    "the conversation coherently."
)


def estimate_text_tokens(text: str) -> int:
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - chinese
    return int(chinese * 0.6 + other * 0.3)


def estimate_tokens(messages: list[dict]) -> int:
    """Token 估算（与旧插件/AstrBot EstimateTokenCounter 规则一致）。"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type", "")
                if ptype == "text":
                    total += estimate_text_tokens(part.get("text", ""))
                elif ptype == "image_url":
                    total += 765
                elif ptype == "audio_url":
                    total += 500
    return total


def group_into_turns(messages: list[dict]) -> list[list[dict]]:
    """按 (user + assistant [+ tool]) 分组为轮次。"""
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


def build_summary_pair(summary: str) -> list[dict]:
    """构建「摘要 user + 确认 assistant」消息对。"""
    return [
        {"role": "user", "content": f"我们的历史对话摘要:\n{summary}"},
        {"role": "assistant", "content": "已确认理解之前的对话内容。"},
    ]


def contexts_to_text(messages: list[dict]) -> str:
    """OpenAI 格式上下文集 → 纯文本（供 LLM 压缩）。"""
    lines: list[str] = []
    for msg in messages:
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


def split_for_manual_compress(
    contexts: list[dict], keep_count: int
) -> tuple[list[dict], list[dict], list[dict]] | None:
    """手动压缩切分：返回 (system, old_msgs, recent_msgs)；无需压缩返回 None。

    keep_count=0 表示全部压缩（不保留任何非 system 消息）。
    """
    keep_count = max(0, int(keep_count))
    system_msgs = [m for m in contexts if m.get("role") == "system"]
    non_system = [m for m in contexts if m.get("role") != "system"]
    if not non_system or keep_count >= len(non_system):
        return None
    if keep_count > 0:
        return system_msgs, non_system[:-keep_count], non_system[-keep_count:]
    return system_msgs, non_system, []


def assemble_compressed(
    system_msgs: list[dict], summary: str, recent_msgs: list[dict]
) -> list[dict]:
    return list(system_msgs) + build_summary_pair(summary) + list(recent_msgs)


def parse_history(conv: Any) -> list[dict]:
    """安全解析 conversation.history（JSON 字符串）。"""
    raw = getattr(conv, "history", None) if conv is not None else None
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


# ── 存档槽位原语（对官方 ConversationManager 的薄封装）──────────


def find_slot(convs: list, slot_name: str):
    """按标题精确匹配存档（排除当前对话由调用方决定）。"""
    for conv in convs:
        if (getattr(conv, "title", None) or "") == slot_name:
            return conv
    return None


def sort_by_updated(convs: list) -> list:
    return sorted(convs, key=lambda c: getattr(c, "updated_at", 0) or 0,
                  reverse=True)


async def create_or_overwrite_slot(
    mgr, umo: str, platform_id: str | None, slot_name: str,
    contexts: list[dict],
) -> bool:
    """新建命名存档；同名存档先删除。返回是否发生覆盖。

    官方 new_conversation 会把当前指针切到新对话，创建后切回原对话，
    保证存档操作不影响正在进行的会话。
    """
    overwritten = False
    convs = await mgr.get_conversations(umo)
    slot = find_slot(convs, slot_name)
    if slot is not None:
        await mgr.delete_conversation(umo, slot.cid)
        overwritten = True
    prev_cid = await mgr.get_curr_conversation_id(umo)
    await mgr.new_conversation(
        unified_msg_origin=umo,
        platform_id=platform_id,
        content=contexts,
        title=slot_name,
    )
    if prev_cid:
        await mgr.switch_conversation(umo, prev_cid)
    return overwritten


async def list_slots(mgr, umo: str) -> list:
    convs = await mgr.get_conversations(umo)
    return sort_by_updated([c for c in convs if (getattr(c, "title", None) or "")])


async def load_slot_into_current(
    mgr, umo: str, slot_name: str, platform_id: str | None = None
) -> tuple[str, list]:
    """读档：把存档内容覆盖进当前对话（与旧插件语义一致）。
    当前无活跃对话时先新建一个再载入。

    返回 (status, history)：
      ("not_found", [])  存档不存在
      ("empty", [])      存档内容为空
      ("ok", history)    已载入当前对话
    """
    convs = await mgr.get_conversations(umo)
    slot = find_slot(convs, slot_name)
    if slot is None:
        return "not_found", []
    archive_history = parse_history(slot)
    if not archive_history:
        return "empty", []
    cid = await mgr.get_curr_conversation_id(umo)
    if not cid:
        cid = await mgr.new_conversation(umo, platform_id)
    await mgr.update_conversation(
        unified_msg_origin=umo,
        conversation_id=cid,
        history=archive_history,
    )
    return "ok", archive_history


async def delete_slot(mgr, umo: str, slot_name: str) -> bool:
    convs = await mgr.get_conversations(umo)
    slot = find_slot(convs, slot_name)
    if slot is None:
        return False
    await mgr.delete_conversation(umo, slot.cid)
    return True
