"""/记忆测评 命令链路测试（方法分支、门控、开关、条数下限、失败分支）。

报告本身的解析/截断/渲染见 test_mbti_report.py，
锚点打分逻辑见 test_mbti_anchor.py；这里只驱动 main.py 的命令体，
把「读取记忆」「LLM 调用」「锚点报告」三处替换为桩。
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import test_gate  # noqa: E402  (复用加载器与 _mod)
import test_mbti_report  # noqa: E402

_HAS = test_gate._HAS
Main = test_gate.Main
VALID_REPORT = test_mbti_report.VALID_REPORT

GROUP_ID = "1041386550"
UMO = f"Iris:GroupMessage:10086_{GROUP_ID}"


class CmdEv:
    def __init__(self, group_id=GROUP_ID, umo=UMO):
        self.message_obj = test_gate.Msg(group_id)
        self.unified_msg_origin = umo
        self.role = "member"

    def plain_result(self, text):
        return ("MSG", text)


def complete_report(**overrides):
    report = test_mbti_report.MemoryManager._parse_mbti_report(
        json.dumps(VALID_REPORT)
    )
    report.update(sample_count=overrides.pop("sample_count", 9),
                  used_count=9, truncated=False)
    report.update(overrides)
    return report


def make_memory(entries, reply=None, calls=None, anchor_report=None, **mem_over):
    """真实 MemoryManager + 桩化的记忆读取 / LLM / 锚点报告。"""
    mgr = test_mbti_report.make_manager(**mem_over)

    async def fake_entries(owner):
        if calls is not None:
            calls.append(("collect", owner))
        return list(entries)

    async def fake_llm(prompt, provider_id="", timeout=30.0, umo=""):
        if calls is not None:
            calls.append(("llm", umo))
        return reply

    async def fake_anchor(anchor_entries):
        if calls is not None:
            calls.append(("anchor", len(anchor_entries)))
        return anchor_report

    mgr.collect_memory_entries = fake_entries
    mgr._llm_chat = fake_llm
    mgr.build_mbti_anchor_report = fake_anchor
    return mgr


def make_plugin(memory_impl, **mem_over):
    cfg = {
        "memory": {
            "memory_enabled": True,
            "memory_kb_name": ["记忆库"],
            **mem_over,
        },
        "memory_groups": [{"group_id": GROUP_ID, "memory_enabled": True}],
    }
    plug = Main(test_gate.FakeContext(), cfg)
    plug.memory = memory_impl
    return plug


def run_command(plug, event):
    out = []

    async def go():
        async for message in plug.cmd_memory_mbti(event):
            out.append(message[1])

    asyncio.run(go())
    return "\n".join(out)


def entries(count=9):
    return [{"text": f"记忆{i}", "updated_at": 0.0} for i in range(count)]


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestMbtiCommand(unittest.TestCase):
    def test_default_method_is_anchor_without_llm(self):
        calls = []
        plug = make_plugin(
            make_memory(entries(), reply=None, calls=calls,
                        anchor_report=complete_report())
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("类型: INFP", msg)
        self.assertIn("不构成心理测评", msg)
        self.assertEqual([c[0] for c in calls], ["collect", "anchor"])

    def test_llm_method_uses_llm(self):
        calls = []
        plug = make_plugin(
            make_memory(entries(), reply=json.dumps(VALID_REPORT), calls=calls),
            memory_mbti_method="llm",
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("类型: INFP", msg)
        self.assertEqual([c[0] for c in calls], ["collect", "llm"])
        self.assertEqual(calls[1][1], UMO)  # umo 透传给 LLM 以解析会话模型

    def test_unknown_method_falls_back_to_anchor(self):
        calls = []
        plug = make_plugin(
            make_memory(entries(), calls=calls, anchor_report=complete_report()),
            memory_mbti_method="  ANCHOR  ",
        )
        run_command(plug, CmdEv())
        self.assertEqual([c[0] for c in calls], ["collect", "anchor"])

    def test_insufficient_memories_skips_generation(self):
        calls = []
        plug = make_plugin(
            make_memory(entries(2), calls=calls, anchor_report=complete_report())
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("记忆数量不足", msg)
        self.assertIn("当前 2 条", msg)
        self.assertIn("至少需要 8 条", msg)
        self.assertEqual([c[0] for c in calls], ["collect"])

    def test_min_memories_configurable(self):
        plug = make_plugin(
            make_memory(entries(2), anchor_report=complete_report()),
            memory_mbti_min_memories=2,
        )
        self.assertIn("类型: INFP", run_command(plug, CmdEv()))

    def test_feature_disabled(self):
        calls = []
        plug = make_plugin(
            make_memory(entries(), calls=calls, anchor_report=complete_report()),
            memory_mbti_enabled=False,
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("已在插件配置", msg)
        self.assertEqual(calls, [])

    def test_anchor_failure_message(self):
        plug = make_plugin(make_memory(entries(), anchor_report=None))
        msg = run_command(plug, CmdEv())
        self.assertIn("生成失败", msg)
        self.assertIn("记忆向量", msg)

    def test_llm_failure_message(self):
        plug = make_plugin(
            make_memory(entries(), reply=None), memory_mbti_method="llm"
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("生成失败", msg)
        self.assertIn("模型", msg)

    def test_group_not_enabled_blocked(self):
        plug = make_plugin(make_memory(entries(), anchor_report=complete_report()))
        msg = run_command(plug, CmdEv(group_id="999999",
                                      umo="Iris:GroupMessage:10086_999999"))
        self.assertIn("❌", msg)
        self.assertIn("999999", msg)

    def test_raw_fallback_still_delivered(self):
        plug = make_plugin(
            make_memory(entries(), reply="模型直接输出了散文"),
            memory_mbti_method="llm",
        )
        msg = run_command(plug, CmdEv())
        self.assertIn("模型直接输出了散文", msg)
        self.assertIn("不构成心理测评", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
