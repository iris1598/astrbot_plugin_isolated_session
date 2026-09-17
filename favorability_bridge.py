"""
favorability_bridge - 与 astrbot_plugin_favorability 的联动桥接模块

功能：
1. 探测 AstrBot 是否安装并启用了 astrbot_plugin_favorability 插件；
2. 解析当前会话生效的特定人格（Persona），严格区分不同人格的数据文件；
3. 会话重置时，仅同步清除对应用户在当前人格下的评价（恢复为 '初次见面'），
   严格保留好感度数值(score)、关系档位(relation)、待确认提议(pending_rel)、
   禁言时间戳(muted_until)、冷却时间(rel_cooldown_until)、昵称(name)等所有其它数据，
   绝不修改同群其他成员、其他群聊或其他任何未生效人格的数据。
"""

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

DEFAULT_EVAL = "初次见面"
_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}


def _get_local_lock(key: str) -> asyncio.Lock:
    if key not in _LOCAL_LOCKS:
        _LOCAL_LOCKS[key] = asyncio.Lock()
    return _LOCAL_LOCKS[key]


def group_storage_key(umo: str, sender_id: str) -> str:
    """把会话隔离(unique_session)的每用户群 UMO 归一化为群级存储键。

    官方隔离开启后群聊 UMO 形如 {平台}:GroupMessage:{用户}_{群}，
    好感度数据按群共享一个桶，剥掉发话者前缀还原为 {平台}:GroupMessage:{群}。
    私聊/webchat 等非群 UMO 原样返回。
    """
    parts = umo.split(":", 2)
    if len(parts) != 3 or parts[1] != "GroupMessage":
        return umo
    sid = parts[2]
    prefix = f"{sender_id}_"
    if sender_id and sid.startswith(prefix) and len(sid) > len(prefix):
        return f"{parts[0]}:{parts[1]}:{sid[len(prefix):]}"
    return umo


def _find_legacy_short_key(
    group_users: dict, full_id: str, user_name: Optional[str] = None
) -> Optional[str]:
    """检查是否存在历史兼容截断的数字 key。"""
    if not full_id or full_id in group_users or not isinstance(group_users, dict):
        return None
    m = re.search(r"(\d+)", full_id)
    if not m:
        return None
    candidate = m.group(1)
    if candidate in group_users and candidate != full_id:
        short_rec = group_users[candidate]
        if isinstance(short_rec, dict):
            short_name = short_rec.get("name")
            if not short_name or not user_name or short_name == user_name:
                return candidate
    return None


def get_favorability_plugin() -> Optional[Any]:
    """获取 AstrBot 中已激活的 astrbot_plugin_favorability 插件实例。"""
    try:
        from astrbot.core.star.star import star_registry

        for star in star_registry:
            if star.name == "astrbot_plugin_favorability" and star.activated:
                return star.star_cls
    except Exception:
        pass
    return None


def is_favorability_installed() -> bool:
    """判断当前 AstrBot 环境是否安装并启用了 astrbot_plugin_favorability。"""
    plug = get_favorability_plugin()
    if plug is not None:
        return True

    try:
        from astrbot.core.star.star import star_registry

        for star in star_registry:
            if star.name == "astrbot_plugin_favorability":
                return bool(star.activated)
    except Exception:
        pass

    try:
        from astrbot.api.star import StarTools

        data_dir = StarTools.get_data_dir("astrbot_plugin_favorability")
        if data_dir.exists() and (
            (data_dir / "favorability.json").exists()
            or (data_dir / "personas").exists()
        ):
            return True
    except Exception:
        pass

    return False


async def resolve_persona_id(
    context: Any, event: AstrMessageEvent, fav_plugin: Optional[Any] = None
) -> str:
    """多层级智能解析当前会话生效的人格 ID / 名称，未识别时回退为 'default'。"""
    if fav_plugin is not None and hasattr(fav_plugin, "resolve_persona_id"):
        try:
            pid = await fav_plugin.resolve_persona_id(event)
            if pid and str(pid).strip() and str(pid).strip() != "[%None]":
                return str(pid).strip()
        except Exception as e:
            logger.debug(f"[IsolatedMemory] fav_plugin 解析 persona 异常: {e}")

    # 1. 尝试通过 AstrBot context.persona_manager 解析
    pm = getattr(context, "persona_manager", None)
    if pm and hasattr(pm, "resolve_selected_persona"):
        try:
            conv_pid = None
            cm = getattr(context, "conversation_manager", None)
            if cm and hasattr(cm, "get_curr_conversation_id"):
                curr_cid = await cm.get_curr_conversation_id(
                    event.unified_msg_origin
                )
                if curr_cid:
                    conv = await cm.get_conversation(
                        event.unified_msg_origin, curr_cid
                    )
                    if conv:
                        conv_pid = getattr(conv, "persona_id", None)

            cfg = (
                getattr(context, "get_config", lambda umo=None: {})(
                    umo=event.unified_msg_origin
                )
                or {}
            )
            res = await pm.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=conv_pid,
                platform_name=event.get_platform_name()
                if hasattr(event, "get_platform_name")
                else "",
                provider_settings=cfg,
            )
            if (
                res
                and res[0]
                and str(res[0]).strip()
                and str(res[0]).strip() != "[%None]"
            ):
                return str(res[0]).strip()
        except Exception as e:
            logger.debug(f"[IsolatedMemory] 解析 persona 异常: {e}")

    # 2. 尝试通过 conversation_manager 获取当前 conversation 的 persona_id
    cm = getattr(context, "conversation_manager", None)
    if cm and hasattr(cm, "get_curr_conversation_id"):
        try:
            curr_cid = await cm.get_curr_conversation_id(
                event.unified_msg_origin
            )
            if curr_cid:
                conv = await cm.get_conversation(
                    event.unified_msg_origin, curr_cid
                )
                if conv and getattr(conv, "persona_id", None):
                    pid = str(conv.persona_id).strip()
                    if pid and pid != "[%None]":
                        return pid
        except Exception as e:
            logger.debug(f"[IsolatedMemory] 从 conversation_manager 获取 persona 异常: {e}")

    return "default"


async def clear_favorability_eval(
    context: Any, event: AstrMessageEvent
) -> tuple[bool, str]:
    """会话重置时清除当前用户在当前人格下的评价。

    Returns:
        tuple[bool, str]: (是否成功重置评价, 当前生效的人格名称)
    """
    if not is_favorability_installed():
        return False, ""

    fav_plugin = get_favorability_plugin()
    persona_id = await resolve_persona_id(context, event, fav_plugin)
    user_id = str(event.get_sender_id())

    # 提取归一化的 group_key
    if fav_plugin is not None and hasattr(fav_plugin, "keys"):
        try:
            group_key, _ = fav_plugin.keys(event)
        except Exception:
            group_key = group_storage_key(event.unified_msg_origin, user_id)
    else:
        group_key = group_storage_key(event.unified_msg_origin, user_id)

    # 1. 优先使用 FavorabilityManager 原生方法（如果可用）
    if fav_plugin is not None and hasattr(fav_plugin, "db"):
        db = fav_plugin.db
        if hasattr(db, "clear_user_eval"):
            try:
                cleared = await db.clear_user_eval(
                    group_key, user_id, persona_id=persona_id
                )
                if cleared:
                    logger.info(
                        f"[IsolatedMemory] 已成功清除用户 {user_id} 在人格【{persona_id}】下的好感度评价"
                    )
                return cleared, persona_id
            except Exception as e:
                logger.warning(
                    f"[IsolatedMemory] 调用 clear_user_eval 异常: {e}"
                )

        # 若旧版插件尚未包含 clear_user_eval，则通过 db 锁与读写接口安全操作
        try:
            lock = db.get_lock(persona_id)
            async with lock:
                data = db._read(persona_id)
                group_data = data.get(group_key)
                if not isinstance(group_data, dict):
                    return False, persona_id

                target_id = user_id
                if target_id not in group_data:
                    if hasattr(db, "_find_legacy_short_key"):
                        short_k = db._find_legacy_short_key(group_data, user_id)
                    else:
                        short_k = _find_legacy_short_key(group_data, user_id)
                    if short_k:
                        target_id = short_k
                    else:
                        return False, persona_id

                user_data = group_data.get(target_id)
                if not isinstance(user_data, dict):
                    return False, persona_id

                curr_eval = user_data.get("eval")
                if curr_eval == DEFAULT_EVAL:
                    return False, persona_id

                user_data["eval"] = DEFAULT_EVAL
                db._write(data, persona_id)
                logger.info(
                    f"[IsolatedMemory] (兼容模式) 已成功清除用户 {user_id} 在人格【{persona_id}】下的好感度评价"
                )
                return True, persona_id
        except Exception as e:
            logger.warning(
                f"[IsolatedMemory] 操作好感度数据异常: {e}"
            )
            return False, persona_id

    # 2. 如果插件实例尚未创建（如测试或解耦运行），尝试直接通过数据目录安全修改
    try:
        from astrbot.api.star import StarTools

        data_dir = StarTools.get_data_dir("astrbot_plugin_favorability")
        target_file = (
            data_dir / "favorability.json"
            if persona_id == "default"
            else data_dir / "personas" / persona_id / "favorability.json"
        )
        if not target_file.exists():
            return False, persona_id

        lock = _get_local_lock(f"{data_dir}:{persona_id}")
        async with lock:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            group_data = data.get(group_key)
            if not isinstance(group_data, dict):
                return False, persona_id

            target_id = user_id
            if target_id not in group_data:
                short_k = _find_legacy_short_key(group_data, user_id)
                if short_k:
                    target_id = short_k
                else:
                    return False, persona_id

            user_data = group_data.get(target_id)
            if not isinstance(user_data, dict):
                return False, persona_id

            curr_eval = user_data.get("eval")
            if curr_eval == DEFAULT_EVAL:
                return False, persona_id

            user_data["eval"] = DEFAULT_EVAL

            # 原子替换写入
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=target_file.parent, prefix="fav_bridge_tmp_", suffix=".json"
            )
            with open(tmp_fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, target_file)
            logger.info(
                f"[IsolatedMemory] (直连文件模式) 已清除用户 {user_id} 在人格【{persona_id}】下的评价"
            )
            return True, persona_id
    except Exception as e:
        logger.warning(
            f"[IsolatedMemory] 直连修改好感度文件异常: {e}"
        )
        return False, persona_id
