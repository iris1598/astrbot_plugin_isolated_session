"""MBTI 锚点比对（anchor 方法）纯逻辑单元测试。

用 8 维单位轴向量的合成数据精确控制余弦相似度，
验证打分是确定性的、可追溯的，且证据量会影响结论强度。
"""

import asyncio
import importlib.util
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402

_HAS = bootstrap.bootstrap()

if _HAS:
    MEM_PATH = Path(__file__).resolve().parent.parent / "memory.py"
    _spec = importlib.util.spec_from_file_location(
        "isolated_memory_anchor_mod", MEM_PATH
    )
    memory = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(memory)
else:
    memory = None


POLES = ("E", "I", "S", "N", "T", "F", "J", "P")

POLE_INDEX = {pole: index for index, pole in enumerate(POLES)}
# 锚点句 -> 所属极：让假 embedding provider 给锚点返回确定的轴向量
ANCHOR_LOOKUP = (
    {
        anchor: pole
        for pole, anchors in memory.MBTI_POLE_ANCHORS.items()
        for anchor in anchors
    }
    if _HAS
    else {}
)


def axis(index: int, size: int = 8) -> list[float]:
    """返回 8 维空间里第 index 个轴的单位向量。"""
    vector = [0.0] * size
    vector[index] = 1.0
    return vector


def tilted(major: float, minor: float, major_axis: int, minor_axis: int) -> list[float]:
    """返回在两个轴之间倾斜的向量，用于制造可控的相似度差值。"""
    vector = [0.0] * 8
    vector[major_axis] = major
    vector[minor_axis] = minor
    return vector


ANCHOR_VECTORS = {pole: [axis(i)] for i, pole in enumerate(POLES)}


def build(texts, vectors, weights=None, threshold=0.02, shrinkage=4.0):
    return memory._mbti_build_anchor_report(
        texts,
        vectors,
        weights if weights is not None else [1.0] * len(texts),
        ANCHOR_VECTORS,
        threshold=threshold,
        shrinkage=shrinkage,
    )


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestCosine(unittest.TestCase):
    def test_same_and_orthogonal(self):
        self.assertAlmostEqual(memory._cosine(axis(0), axis(0)), 1.0)
        self.assertAlmostEqual(memory._cosine(axis(0), axis(1)), 0.0)

    def test_scale_invariant(self):
        self.assertAlmostEqual(memory._cosine([3.0, 0.0], [0.0, 7.0]), 0.0)
        self.assertAlmostEqual(memory._cosine([2.0, 0.0], [5.0, 0.0]), 1.0)

    def test_degenerate_inputs(self):
        self.assertEqual(memory._cosine([], [1.0]), 0.0)
        self.assertEqual(memory._cosine([1.0, 2.0], [1.0]), 0.0)
        self.assertEqual(memory._cosine([0.0, 0.0], [1.0, 1.0]), 0.0)

    def test_pole_similarity_takes_the_best_anchor(self):
        self.assertAlmostEqual(
            memory._mbti_pole_similarity(axis(0), [axis(1), axis(0)]), 1.0
        )
        self.assertEqual(memory._mbti_pole_similarity(axis(0), []), 0.0)


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestAnchorReport(unittest.TestCase):
    def test_deterministic_for_same_input(self):
        args = (["m1", "m2"], [axis(0), axis(0)])
        self.assertEqual(build(*args), build(*args))

    def test_pole_and_strength(self):
        report = build(["m1", "m2", "m3"], [axis(0)] * 3)
        self.assertEqual(report["type"], "E???")
        dim = report["dimensions"][0]
        self.assertEqual(dim["name"], "E/I")
        self.assertEqual(dim["pole"], "E")
        # 3/3 全票 → ratio=1，再乘证据收缩 3/(3+4)
        self.assertEqual(dim["strength"], round(3 / 7 * 100))
        self.assertIn("3 条记忆倾向 E 极", dim["evidence"])
        self.assertIn("m1", dim["evidence"])

    def test_undetermined_dimension_marks_question(self):
        report = build(["m1"], [axis(0)])
        self.assertEqual(report["type"], "E???")
        dim = report["dimensions"][1]
        self.assertEqual(dim["pole"], "")
        self.assertEqual(dim["strength"], 0)
        self.assertIn("证据不足", dim["evidence"])
        self.assertIn("未能判定的维度：S/N、T/F、J/P", report["caveats"])

    def test_neutral_memory_is_ignored(self):
        # 45° 平分 E/I 两轴，差值 0 → 中性，四维全部无法判定
        report = build(["m"], [tilted(1.0, 1.0, 0, 1)])
        self.assertEqual(report["type"], "????")
        self.assertEqual(report["confidence"], 0)
        self.assertIn("没有任何记忆", report["summary"])

    def test_threshold_gates_small_lean(self):
        vector = tilted(1.0, 0.8, 0, 1)  # 差值约 0.156
        self.assertEqual(build(["m"], [vector], threshold=0.2)["type"], "????")
        self.assertEqual(build(["m"], [vector], threshold=0.1)["type"], "E???")

    def test_strength_grows_with_evidence_count(self):
        one = build(["m"], [axis(0)])
        many = build([f"m{i}" for i in range(19)], [axis(0)] * 19)
        self.assertLess(
            one["dimensions"][0]["strength"], many["dimensions"][0]["strength"]
        )
        self.assertEqual(many["dimensions"][0]["strength"], round(19 / 23 * 100))

    def test_weights_shift_balance(self):
        # 最近（权重大）偏 E、很久以前（权重极小）偏 I → 结果跟 E 走
        report = build(["recent", "stale"], [axis(0), axis(1)], weights=[1.0, 0.01])
        self.assertEqual(report["dimensions"][0]["pole"], "E")

    def test_cancelling_evidence_is_undetermined(self):
        report = build(["e", "i"], [axis(0), axis(1)])
        self.assertEqual(report["dimensions"][0]["pole"], "")
        self.assertEqual(report["type"], "????")
        self.assertIn("正好抵消", report["dimensions"][0]["evidence"])

    def test_conflict_lowers_strength(self):
        split = build(["e", "i", "e2", "i2"], [axis(0), axis(1)] * 2)
        one_sided = build(["e"] * 4, [axis(0)] * 4)
        self.assertLess(
            split["dimensions"][0]["strength"],
            one_sided["dimensions"][0]["strength"],
        )

    def test_multi_dimension_type_and_summary(self):
        # E 轴 + N 轴 + T 轴 + J 轴 → 四条记忆各投一个维度
        report = build(
            ["e", "n", "t", "j"],
            [axis(0), axis(3), axis(4), axis(6)],
        )
        self.assertEqual(report["type"], "ENTJ")
        self.assertIn("ENTJ", report["summary"])
        self.assertIn("4 条记忆中有 4 条体现明显倾向", report["summary"])

    def test_report_is_formatter_compatible(self):
        report = build(["m1", "m2"], [axis(0), axis(0)])
        report.update(sample_count=2, used_count=2, truncated=False)
        mgr = memory.MemoryManager(type("C", (), {})(), {})
        text = mgr.format_mbti_report(report)
        self.assertIn("类型: E???", text)
        self.assertIn("E·外向", text)
        self.assertIn("S/N  ?·未判定", text)
        self.assertIn("不构成心理测评", text)

    def test_empty_anchors_never_claims_a_pole(self):
        report = memory._mbti_build_anchor_report(
            ["m"], [axis(0)], [1.0], {}, threshold=0.0, shrinkage=4.0
        )
        self.assertEqual(report["type"], "????")

    def test_uses_anchor_disclaimer_not_ai_one(self):
        report = build(["m1"], [axis(0)])
        self.assertEqual(report["disclaimer"], memory.MBTI_ANCHOR_DISCLAIMER)
        text = memory.MemoryManager(type("C", (), {})(), {}).format_mbti_report(report)
        self.assertIn("锚点句比对自动生成", text)
        self.assertNotIn("由 AI", text)

    def test_caveats_are_multiline_but_never_empty(self):
        report = build(["m1"], [axis(0)])
        self.assertGreater(len(str(report["caveats"]).splitlines()), 1)
        self.assertIn("未能判定的维度：S/N、T/F、J/P", report["caveats"])


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestAnchorConfig(unittest.TestCase):
    def test_threshold_default_and_override(self):
        default = memory.MemoryManager(type("C", (), {})(), {"memory": {}})
        self.assertEqual(
            default._mbti_anchor_threshold(), memory.MBTI_ANCHOR_THRESHOLD
        )

        custom = memory.MemoryManager(
            type("C", (), {})(), {"memory": {"memory_mbti_anchor_threshold": 0.35}}
        )
        self.assertAlmostEqual(custom._mbti_anchor_threshold(), 0.35)

    def test_threshold_bad_values_fall_back(self):
        for bad in ("abc", None):
            mgr = memory.MemoryManager(
                type("C", (), {})(),
                {"memory": {"memory_mbti_anchor_threshold": bad}},
            )
            self.assertEqual(
                mgr._mbti_anchor_threshold(), memory.MBTI_ANCHOR_THRESHOLD
            )

    def test_threshold_negative_clamped(self):
        mgr = memory.MemoryManager(
            type("C", (), {})(), {"memory": {"memory_mbti_anchor_threshold": -3}}
        )
        self.assertEqual(mgr._mbti_anchor_threshold(), 0.0)

    def test_anchors_cover_every_pole(self):
        self.assertEqual(set(memory.MBTI_POLE_ANCHORS), set(POLES))
        for pole in POLES:
            self.assertGreaterEqual(len(memory.MBTI_POLE_ANCHORS[pole]), 5)


def run(coro):
    return asyncio.run(coro)


class FakeEP:
    """假 embedding provider：锚点句按所属极返回轴向量，测试记忆按指定极返回。"""

    def __init__(self, memory_axes=None):
        self.memory_axes = memory_axes or {}
        self.calls = 0

    async def get_embeddings(self, texts):
        self.calls += 1
        vectors = []
        for text in texts:
            if text in ANCHOR_LOOKUP:
                vectors.append(axis(POLE_INDEX[ANCHOR_LOOKUP[text]]))
            else:
                vectors.append(axis(POLE_INDEX[self.memory_axes[text]]))
        return vectors


class FakeKB:
    def __init__(self, ep, provider_id="emb-1"):
        self.ep = ep
        self.kb = type("K", (), {"embedding_provider_id": provider_id})()

    async def get_ep(self):
        return self.ep


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestAnchorManagerIntegration(unittest.TestCase):
    @staticmethod
    def _manager(kb, **mem_over):
        mgr = memory.MemoryManager(type("C", (), {})(), {"memory": dict(mem_over)})

        async def ensure_ok():
            return kb

        mgr.ensure_kb = ensure_ok
        return mgr

    def test_report_from_embeddings(self):
        day = 86400.0
        now = time.time()
        entries = [
            {"text": "记忆E1", "updated_at": now},
            {"text": "记忆E2", "updated_at": now},
            {"text": "记忆I1", "updated_at": now - day},
        ]
        ep = FakeEP({"记忆E1": "E", "记忆E2": "E", "记忆I1": "I"})
        mgr = self._manager(FakeKB(ep))

        report = run(mgr.build_mbti_anchor_report(entries))
        self.assertEqual(report["type"], "E???")
        # 2 票 E、1 票 I（I 略旧、权重略小）→ ratio 由权重决定，强度含收缩
        self.assertEqual(report["dimensions"][0]["pole"], "E")
        self.assertEqual(report["sample_count"], 3)
        self.assertEqual(report["used_count"], 3)
        self.assertFalse(report["truncated"])

    def test_anchor_vectors_are_cached_per_provider(self):
        entries = [{"text": "记忆E1", "updated_at": None}]
        ep = FakeEP({"记忆E1": "E"})
        mgr = self._manager(FakeKB(ep))

        run(mgr.build_mbti_anchor_report(entries))
        self.assertEqual(ep.calls, 2)  # 锚点批次 + 记忆批次
        run(mgr.build_mbti_anchor_report(entries))
        self.assertEqual(ep.calls, 3)  # 锚点走缓存，只重新嵌入记忆

    def test_decay_weight_favours_recent_memory(self):
        day = 86400.0
        now = time.time()
        # 很久以前偏 I（权重趋近 0）、最近偏 E → 结论跟着最近的走
        entries = [
            {"text": "记忆E1", "updated_at": now},
            {"text": "记忆I1", "updated_at": now - 300 * day},
        ]
        ep = FakeEP({"记忆E1": "E", "记忆I1": "I"})
        mgr = self._manager(FakeKB(ep), memory_half_life_days=30)

        report = run(mgr.build_mbti_anchor_report(entries))
        self.assertEqual(report["dimensions"][0]["pole"], "E")

    def test_char_cap_keeps_only_whole_memories(self):
        long_e = "甲" * 300
        long_i = "乙" * 300
        entries = [
            {"text": long_e, "updated_at": None},
            {"text": long_i, "updated_at": None},
        ]
        ep = FakeEP({long_e: "E", long_i: "I"})
        mgr = self._manager(FakeKB(ep), memory_mbti_max_chars=500)

        report = run(mgr.build_mbti_anchor_report(entries))
        self.assertTrue(report["truncated"])
        self.assertEqual(report["used_count"], 1)   # 第二条会越过 500 上限
        self.assertEqual(report["sample_count"], 2)
        self.assertEqual(report["dimensions"][0]["pole"], "E")

    def test_no_kb_returns_none(self):
        mgr = memory.MemoryManager(type("C", (), {})(), {"memory": {}})

        async def ensure_none():
            return None

        mgr.ensure_kb = ensure_none
        self.assertIsNone(
            run(mgr.build_mbti_anchor_report([{"text": "x", "updated_at": None}]))
        )

    def test_embedding_failure_returns_none(self):
        class BoomEP:
            async def get_embeddings(self, texts):
                raise RuntimeError("embedding 服务不可用")

        mgr = self._manager(FakeKB(BoomEP()))
        self.assertIsNone(
            run(mgr.build_mbti_anchor_report([{"text": "x", "updated_at": None}]))
        )

    def test_memory_vector_count_mismatch_returns_none(self):
        class ShortEP:
            async def get_embeddings(self, texts):
                return []

        kb = FakeKB(ShortEP())
        mgr = self._manager(kb)

        async def fake_anchors(_kb):
            return {pole: [axis(POLE_INDEX[pole])] for pole in POLES}

        mgr._anchor_vectors = fake_anchors
        self.assertIsNone(
            run(mgr.build_mbti_anchor_report([{"text": "x", "updated_at": None}]))
        )

    def test_empty_entries_returns_none(self):
        mgr = self._manager(FakeKB(FakeEP()))
        self.assertIsNone(run(mgr.build_mbti_anchor_report([])))
        self.assertIsNone(run(mgr.build_mbti_anchor_report([{"text": "  "}])))


if __name__ == "__main__":
    unittest.main(verbosity=2)
