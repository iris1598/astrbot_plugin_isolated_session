"""/会话重置 对齐官方 reset 逻辑的回归测试。

官方语义（builtin conversation.py reset）：
- 就地清空当前对话历史 update_conversation(umo, cid, [])
- 不删除对话本身、不影响存档
- 群聊+隔离关闭 需管理员（alter_cmd 可覆盖）；隔离开启 成员即可
- 停止该会话运行中的 Agent；无可用模型时拒绝
本插件附加：待抽取缓冲作废、memory_reset_with_session 记忆联动。
"""

import asyncio
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_gate  # noqa: E402  (复用加载器与 _mod)

_HAS = test_gate._HAS
Main = test_gate.Main


class Msg:
    def __init__(self, group_id=None):
        self.group_id = group_id


class Ev:
    def __init__(self, group_id="1041386550", role="member",
                 umo="Iris:GroupMessage:10086_1041386550"):
        self.message_obj = Msg(group_id)
        self.unified_msg_origin = umo
        self.role = role
        self.extras = {}

    def get_platform_id(self):
        return "Iris"

    def set_extra(self, k, v):
        self.extras[k] = v

    def plain_result(self, text):
        return ("MSG", text)


class FakeConvMgr:
    def __init__(self, curr="cid-cur"):
        self.curr = curr
        self.updates = []      # (cid, history)
        self.deletes = 0

    async def get_curr_conversation_id(self, umo):
        return self.curr

    async def update_conversation(self, umo, cid, history=None, **kw):
        self.updates.append((cid, history))

    async def delete_conversations_by_user_id(self, umo):
        self.deletes += 1

    async def new_conversation(self, *a, **kw):  # 不应被 reset 调用
        raise AssertionError("reset 不应新建/删除对话（官方语义为就地清空）")


class FakeContextBase(test_gate.FakeContext):
    def __init__(self, unique=True):
        self._unique = unique

    def get_config(self, umo=None):
        return {
            "platform_settings": {"unique_session": self._unique},
            "provider_settings": {"agent_runner_type": "local"},
        }

    async def get_using_provider_async(self, umo=None):
        return object()  # 有模型


class FakeMemory:
    def __init__(self, n=7):
        self.n = n
        self.cleared = []

    async def clear(self, umo):
        self.cleared.append(umo)
        return self.n


class FakeSP:
    def __init__(self):
        self.removed = []

    async def get_async(self, scope, scope_id, key, default=None):
        return default

    async def session_get(self, umo, key, default=None):
        return default

    async def session_put(self, umo, key, value):
        pass

    async def session_remove(self, umo, key):
        self.removed.append((umo, key))


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestOfficialReset(unittest.TestCase):
    def _make(self, reset_with_memory=False, unique=True, has_provider=True):
        mod = test_gate._mod
        cfg = {
            "memory": {
                "memory_enabled": True, "memory_kb_name": ["记忆库"],
                "memory_reset_with_session": reset_with_memory,
            },
            "memory_groups": [
                {"group_id": "1041386550", "memory_enabled": True},
            ],
        }
        ctx = FakeContextBase(unique=unique)
        plug = Main(ctx, cfg)
        plug.memory = FakeMemory()
        mgr = FakeConvMgr()
        ctx.conversation_manager = mgr
        mod.sp = FakeSP()
        if not has_provider:
            async def none_provider(umo=None):
                return None
            ctx.get_using_provider_async = none_provider
        return plug, mgr

    def _run(self, plug, event):
        out = []

        async def go():
            async for m in plug.cmd_reset(event):
                out.append(m[1])
        asyncio.run(go())
        return "\n".join(out)

    def test_clears_current_in_place_and_keeps_archives(self):
        plug, mgr = self._make()
        msg = self._run(plug, Ev())
        self.assertIn("✅", msg)
        self.assertIn("存档不受影响", msg)
        self.assertEqual(mgr.updates, [("cid-cur", [])])  # 只清当前
        self.assertEqual(mgr.deletes, 0)
        self.assertTrue(True)  # new_conversation 被调用会直接 AssertionError

    def test_no_active_conversation(self):
        plug, mgr = self._make()
        mgr.curr = None
        msg = self._run(plug, Ev())
        self.assertIn("没有活跃对话", msg)
        self.assertEqual(mgr.updates, [])

    def test_permission_group_without_unique_requires_admin(self):
        plug, mgr = self._make(unique=False)
        msg = self._run(plug, Ev(role="member"))
        self.assertIn("需要管理员权限", msg)
        self.assertEqual(mgr.updates, [])
        # 管理员可以
        msg = self._run(plug, Ev(role="admin"))
        self.assertIn("✅", msg)

    def test_unique_session_member_can_reset(self):
        plug, mgr = self._make(unique=True)
        msg = self._run(plug, Ev(role="member"))
        self.assertIn("✅", msg)

    def test_no_provider_refused(self):
        plug, mgr = self._make(has_provider=False)
        msg = self._run(plug, Ev())
        self.assertIn("未找到可用的 LLM 模型", msg)
        self.assertEqual(mgr.updates, [])

    def test_memory_link_on(self):
        plug, mgr = self._make(reset_with_memory=True)
        msg = self._run(plug, Ev())
        self.assertIn("已同步清空记忆 7 条", msg)
        self.assertEqual(plug.memory.cleared,
                         ["Iris:GroupMessage:10086_1041386550"])

    def test_memory_link_off(self):
        plug, mgr = self._make(reset_with_memory=False)
        self._run(plug, Ev())
        self.assertEqual(plug.memory.cleared, [])

    def test_group_not_in_memory_list_skips_clear(self):
        plug, mgr = self._make(reset_with_memory=True)
        self._run(plug, Ev(group_id="999999",
                           umo="Iris:GroupMessage:10086_999999"))
        self.assertEqual(plug.memory.cleared, [])

    def test_extract_state_cleared(self):
        import test_gate
        plug, mgr = self._make()
        self._run(plug, Ev())
        removed = [k for _u, k in test_gate._mod.sp.removed]
        self.assertIn("memory_extract_state", removed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
