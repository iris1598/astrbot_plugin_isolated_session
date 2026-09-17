"""session_tools 纯逻辑与存档槽位原语测试（stdlib only，任意 Python 可跑）。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import session_tools as T  # noqa: E402


class FakeConv:
    def __init__(self, cid, history=None, title=None, updated_at=0):
        self.cid = cid
        self.history = json.dumps(history) if history is not None else None
        self.title = title
        self.updated_at = updated_at


class FakeMgr:
    """模拟官方 ConversationManager 语义（含指针切换与删除行为）。"""

    def __init__(self):
        self.convs = {}          # cid -> FakeConv
        self.curr = None
        self.order = 0
        self.sp = {}

    async def get_curr_conversation_id(self, umo):
        return self.curr

    async def new_conversation(self, unified_msg_origin=None, platform_id=None,
                               content=None, title=None):
        self.order += 1
        cid = f"cid-{self.order}"
        self.convs[cid] = FakeConv(cid, content or [], title=title,
                                   updated_at=100 + self.order)
        self.curr = cid
        self.sp["sel_conv_id"] = cid
        return cid

    async def switch_conversation(self, umo, cid):
        assert cid in self.convs, f"switch to missing {cid}"
        self.curr = cid
        self.sp["sel_conv_id"] = cid

    async def get_conversation(self, umo, cid):
        return self.convs.get(cid)

    async def get_conversations(self, umo=None):
        return list(self.convs.values())

    async def delete_conversation(self, umo, cid=None):
        cid = cid or self.curr
        if cid in self.convs:
            del self.convs[cid]
            if self.curr == cid:
                self.curr = None
                self.sp.pop("sel_conv_id", None)

    async def delete_conversations_by_user_id(self, umo):
        self.convs.clear()
        self.curr = None
        self.sp.pop("sel_conv_id", None)

    async def update_conversation(self, unified_msg_origin=None,
                                  conversation_id=None, history=None, **kw):
        conv = self.convs[conversation_id]
        conv.history = json.dumps(history)


def run(coro):
    return asyncio.run(coro)


MSGS = [
    {"role": "system", "content": "人设"},
    {"role": "user", "content": "一"},
    {"role": "assistant", "content": "二"},
    {"role": "user", "content": "三"},
    {"role": "assistant", "content": "四"},
]


class TestPureLogic(unittest.TestCase):
    def test_estimate_tokens(self):
        # 中文 0.6/字，其他 0.3/字符（向下取整）
        self.assertEqual(T.estimate_text_tokens("你好世界"), int(4 * 0.6))
        self.assertEqual(T.estimate_text_tokens("abcd"), int(4 * 0.3))
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "user", "content": [{"type": "text", "text": "哈"},
                                         {"type": "image_url", "image_url": {}}]},
        ]
        self.assertEqual(T.estimate_tokens(msgs), 1 + int(0.6) + 765)

    def test_group_into_turns(self):
        turns = T.group_into_turns(MSGS[1:])
        self.assertEqual(len(turns), 2)
        self.assertEqual(len(turns[0]), 2)

    def test_group_with_tool_msgs(self):
        msgs = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "tool_calls": [{}]},
            {"role": "tool", "content": "r"},
            {"role": "assistant", "content": "a"},
        ]
        turns = T.group_into_turns(msgs)
        self.assertEqual(len(turns), 1)
        self.assertEqual(len(turns[0]), 4)

    def test_split_manual_compress(self):
        sysm, old, recent = T.split_for_manual_compress(MSGS, 2)
        self.assertEqual(len(sysm), 1)
        self.assertEqual([m["content"] for m in old], ["一", "二"])
        self.assertEqual([m["content"] for m in recent], ["三", "四"])
        # keep >= len(non_system) → None
        self.assertIsNone(T.split_for_manual_compress(MSGS, 4))
        self.assertIsNone(T.split_for_manual_compress(MSGS, 99))
        # keep=0 → 全部压缩
        sysm, old, recent = T.split_for_manual_compress(MSGS, 0)
        self.assertEqual(len(old), 4)
        self.assertEqual(recent, [])
        # 空输入
        self.assertIsNone(T.split_for_manual_compress([], 5))

    def test_assemble(self):
        out = T.assemble_compressed([MSGS[0]], "摘要文本", MSGS[3:])
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("摘要", out[1]["content"])
        self.assertEqual(out[2]["content"], "已确认理解之前的对话内容。")
        self.assertEqual(len(out), 5)

    def test_contexts_to_text_multimodal(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "你好"},
                                         {"type": "image_url"}]},
            {"role": "assistant", "content": "呀"},
        ]
        text = T.contexts_to_text(msgs)
        self.assertEqual(text, "[user]: 你好\n[assistant]: 呀")

    def test_slot_name_rules(self):
        for ok in ["a", "存档一", "slot_1", "S-2", "0", "二十个字符二十个字符二十个字符二十个字符"]:
            self.assertTrue(T.SLOT_NAME_RE.match(ok), ok)
        for bad in ["有 空格", "带!符号", "", "超长的名字超长的名字超长的名字超长的名字超长的"]:
            self.assertIsNone(T.SLOT_NAME_RE.match(bad), bad)

    def test_parse_history_guards(self):
        self.assertEqual(T.parse_history(None), [])
        self.assertEqual(T.parse_history(FakeConv("x")), [])
        broken = FakeConv("x")
        broken.history = "not-json"
        self.assertEqual(T.parse_history(broken), [])
        notlist = FakeConv("x")
        notlist.history = '{"a":1}'
        self.assertEqual(T.parse_history(notlist), [])


class TestSlots(unittest.TestCase):
    def setUp(self):
        self.mgr = FakeMgr()
        run(self.mgr.new_conversation("U", content=MSGS))  # 当前对话 cid-1

    def test_save_creates_slot_and_keeps_pointer(self):
        overwritten = run(T.create_or_overwrite_slot(
            self.mgr, "U", "Iris", "我的档", MSGS
        ))
        self.assertFalse(overwritten)
        # 当前指针仍在原对话
        self.assertEqual(self.mgr.curr, "cid-1")
        slots = run(T.list_slots(self.mgr, "U"))
        self.assertEqual([s.title for s in slots], ["我的档"])
        # 存档内容独立快照
        self.assertEqual(T.parse_history(slots[0]), MSGS)

    def test_save_overwrites_same_name(self):
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "档", MSGS[:3]))
        n_before = len(self.mgr.convs)
        overwritten = run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "档", MSGS))
        self.assertTrue(overwritten)
        self.assertEqual(len(self.mgr.convs), n_before)  # 删旧增新
        slots = run(T.list_slots(self.mgr, "U"))
        self.assertEqual(len(slots), 1)
        self.assertEqual(T.parse_history(slots[0]), MSGS)
        self.assertEqual(self.mgr.curr, "cid-1")

    def test_load_into_current(self):
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "档", MSGS[:3]))
        # 当前对话换个内容
        run(self.mgr.update_conversation("U", "cid-1", history=[{"role": "user", "content": "z"}]))
        status, hist = run(T.load_slot_into_current(self.mgr, "U", "档"))
        self.assertEqual(status, "ok")
        self.assertEqual(hist, MSGS[:3])
        self.assertEqual(T.parse_history(self.mgr.convs["cid-1"]), MSGS[:3])

    def test_load_missing_and_empty(self):
        status, _ = run(T.load_slot_into_current(self.mgr, "U", "不存在"))
        self.assertEqual(status, "not_found")
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "空档", []))
        status, _ = run(T.load_slot_into_current(self.mgr, "U", "空档"))
        self.assertEqual(status, "empty")

    def test_load_without_active_creates(self):
        # 有存档但当前对话被删（如官方 /del 删掉当前）→ 读档自动新建
        run(self.mgr.new_conversation("U", content=[]))          # cid-2 当前
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "备份", MSGS))
        run(self.mgr.delete_conversation("U", self.mgr.curr))    # 只删当前对话
        status, hist = run(T.load_slot_into_current(self.mgr, "U", "备份", "Iris"))
        self.assertEqual(status, "ok")
        self.assertEqual(hist, MSGS)
        self.assertIsNotNone(self.mgr.curr)
        self.assertEqual(T.parse_history(self.mgr.convs[self.mgr.curr]), MSGS)

    def test_delete_slot(self):
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "档", MSGS))
        self.assertTrue(run(T.delete_slot(self.mgr, "U", "档")))
        self.assertFalse(run(T.delete_slot(self.mgr, "U", "档")))
        self.assertEqual(self.mgr.curr, "cid-1")

    def test_list_slots_sorted_and_filtered(self):
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "A", MSGS))
        run(T.create_or_overwrite_slot(self.mgr, "U", "Iris", "B", MSGS))
        slots = run(T.list_slots(self.mgr, "U"))
        self.assertEqual([s.title for s in slots], ["B", "A"])  # updated 倒序
        # 当前未命名对话不出现在列表
        self.assertNotIn(None, [s.title for s in slots])


class TestSaveRoundtripIntegrity(unittest.TestCase):
    def test_history_content_byte_equal(self):
        mgr = FakeMgr()
        run(mgr.new_conversation("U", content=MSGS))
        run(T.create_or_overwrite_slot(mgr, "U", "Iris", "快照", MSGS))
        run(mgr.update_conversation("U", "cid-1", history=[]))
        status, hist = run(T.load_slot_into_current(mgr, "U", "快照"))
        self.assertEqual(status, "ok")
        self.assertEqual(hist, MSGS)
        self.assertEqual(T.parse_history(mgr.convs["cid-1"]), MSGS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
