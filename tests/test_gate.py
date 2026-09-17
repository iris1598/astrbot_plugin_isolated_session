"""记忆插件门控/自愈初始化回归测试。

事故场景：安装插件后才在 WebUI 开启 memory_enabled / 选择知识库
（未重载插件），self.memory 永远为 None，/记忆清除 只会说
"记忆系统未启用"，且新旧配置结构(memory_groups/whitelist_groups)
不互通。

修复验证：
1. _ensure_memory 惰性重试（节流 15s），配置改后即生效无需重启；
2. _gate_block_reason 输出精确原因（含群号与当前启用列表）；
3. whitelist_groups（旧插件配置结构）自动兼容。

运行（AstrBot 自带 Python）：
  AstrBot\\backend\\python\\python.exe -m unittest discover ^
      -s astrbot_plugin_isolated_memory\\tests -p "test_gate.py"
"""

import importlib.util
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import bootstrap  # noqa: E402

_HAS = bootstrap.bootstrap()
PLUGIN_DIR = HERE.parent


def load_memory_main():
    pkg = PLUGIN_DIR.name
    sys.modules.pop(pkg, None)
    for m in [m for m in sys.modules if m.startswith(pkg + ".")]:
        del sys.modules[m]
    spec = importlib.util.spec_from_file_location(
        pkg, PLUGIN_DIR / "_no_such__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    sys.modules[pkg] = importlib.util.module_from_spec(spec)
    m_spec = importlib.util.spec_from_file_location(
        pkg + ".main", PLUGIN_DIR / "main.py"
    )
    mod = importlib.util.module_from_spec(m_spec)
    m_spec.loader.exec_module(mod)
    return mod


if _HAS:
    _mod = load_memory_main()
    Main = _mod.Main
else:
    Main = None


class FakeKB:
    kb_id = "kb-1"
    kb_name = "记忆库"
    embedding_provider_id = "emb-1"


class FakeHelper:
    init_error = None
    kb = FakeKB()


class FakeKBManager:
    async def get_kb_by_name(self, name):
        return FakeHelper()


class FakeContext:
    kb_manager = FakeKBManager()

    def get_config(self):
        return {"platform_settings": {"unique_session": True}}


class Msg:
    def __init__(self, group_id=None):
        self.group_id = group_id


class Ev:
    def __init__(self, group_id=None):
        self.message_obj = Msg(group_id)


def make_plugin(config):
    return Main(FakeContext(), config)


def mem_config(**over):
    mem = {"memory_enabled": True, "memory_kb_name": ["记忆库"]}
    mem.update(over.pop("memory", {}))
    cfg = {"memory_groups": [], "memory": mem}
    cfg.update(over)
    return cfg


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestSelfHeal(unittest.TestCase):
    def test_lazy_reinit_after_config_enable(self):
        cfg = {"memory": {"memory_enabled": False}, "memory_groups": []}
        plug = make_plugin(cfg)

        async def scenario():
            await plug.initialize()
            self.assertIsNone(plug.memory)
            self.assertIn("memory_enabled", plug._init_reason)
            # 用户随后在 WebUI 开启并选库（未重启）
            cfg["memory"] = {"memory_enabled": True,
                             "memory_kb_name": ["记忆库"]}
            plug._last_init_try = 0.0  # 越过 15s 节流
            mem = await plug._ensure_memory()
            self.assertIsNotNone(mem)
        import asyncio
        asyncio.run(scenario())

    def test_throttle(self):
        plug = make_plugin({"memory": {}, "memory_groups": []})
        calls = []

        async def fake_try():
            calls.append(1)
            return "x"

        plug._try_init = fake_try
        plug._last_init_try = time.time()

        import asyncio

        async def scenario():
            self.assertIsNone(await plug._ensure_memory())
            self.assertEqual(calls, [])          # 节流内不重试
            plug._last_init_try = 0.0
            self.assertIsNone(await plug._ensure_memory())
            self.assertEqual(calls, [1])         # 节流过后重试一次
        asyncio.run(scenario())


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestGate(unittest.TestCase):
    def test_empty_list_message(self):
        plug = make_plugin(mem_config())
        reason = plug._gate_block_reason(Ev(group_id="1041386550"))
        self.assertIn("memory_groups", reason)
        self.assertIn("1041386550", reason)

    def test_group_missing_lists_configured(self):
        plug = make_plugin(mem_config(memory_groups=[
            {"group_id": "999", "group_name": "另一群", "memory_enabled": True},
        ]))
        reason = plug._gate_block_reason(Ev(group_id="1041386550"))
        self.assertIn("999", reason)
        self.assertIn("另一群", reason)

    def test_group_disabled_switch(self):
        plug = make_plugin(mem_config(memory_groups=[
            {"group_id": "1041386550", "memory_enabled": False},
        ]))
        reason = plug._gate_block_reason(Ev(group_id="1041386550"))
        self.assertIn("已关闭", reason)

    def test_pass(self):
        plug = make_plugin(mem_config(memory_groups=[
            {"group_id": "1041386550", "memory_enabled": True},
        ]))
        self.assertIsNone(plug._gate_block_reason(Ev(group_id="1041386550")))
        self.assertIsNotNone(plug._group_gate(Ev(group_id="1041386550")))

    def test_non_group(self):
        plug = make_plugin(mem_config())
        self.assertIn("仅在群聊", plug._gate_block_reason(Ev(group_id=None)))

    def test_legacy_whitelist_fallback(self):
        # 旧插件配置结构：只有 whitelist_groups
        plug = make_plugin({
            "memory": {"memory_enabled": True, "memory_kb_name": ["记忆库"]},
            "whitelist_groups": [
                {"group_id": "1041386550", "group_name": "主群",
                 "max_turns": 50, "memory_enabled": True},
                {"group_id": "888", "memory_enabled": False},
            ],
        })
        groups = plug._memory_groups()
        self.assertEqual([g["group_id"] for g in groups], ["1041386550", "888"])
        self.assertIsNone(plug._gate_block_reason(Ev(group_id="1041386550")))
        self.assertIn("已关闭", plug._gate_block_reason(Ev(group_id="888")))

    def test_memory_groups_preferred_over_whitelist(self):
        plug = make_plugin({
            "memory": {"memory_enabled": True},
            "memory_groups": [{"group_id": "111", "memory_enabled": True}],
            "whitelist_groups": [{"group_id": "222", "memory_enabled": True}],
        })
        ids = [g["group_id"] for g in plug._memory_groups()]
        self.assertEqual(ids, ["111"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
