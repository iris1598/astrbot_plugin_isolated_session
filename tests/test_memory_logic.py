"""memory.py 纯逻辑与召回衰减单元测试（需在含 astrbot 的 Python 环境运行）。"""

import asyncio
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402

_HAS = bootstrap.bootstrap()

if _HAS:
    MEM_PATH = Path(__file__).resolve().parent.parent / "memory.py"
    _spec = importlib.util.spec_from_file_location("isolated_memory_mod", MEM_PATH)
    memory = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(memory)
    MemoryManager = memory.MemoryManager
else:
    memory = None
    MemoryManager = None


class FakeConfig(dict):
    pass


def make_manager(**mem):
    cfg = FakeConfig(memory=dict(mem))
    ctx = type("C", (), {"kb_manager": None})()
    return MemoryManager(ctx, cfg)


def run(coro):
    return asyncio.run(coro)


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestFuse(unittest.TestCase):
    def test_rrf_weights_rank(self):
        class R:
            def __init__(self, doc_id, sim):
                self.similarity = sim
                self.data = {"doc_id": doc_id, "text": doc_id,
                             "updated_at": "2026-01-01T00:00:00+00:00"}

        dense = [R("A", 0.9), R("B", 0.8)]
        sparse = [
            {"doc_id": "B", "text": "B", "score": 5.0, "updated_at": 1.0},
            {"doc_id": "C", "text": "C", "score": 4.0, "updated_at": 1.0},
        ]
        fused = MemoryManager._fuse(dense, sparse)
        by_id = {d: s for d, s, *_ in fused}
        self.assertEqual({d for d, *_ in fused}, {"A", "B", "C"})
        # B 同时命中 dense#2 + sparse#1，应高于仅 dense#1 的 A
        self.assertGreater(by_id["B"], by_id["A"])
        self.assertGreater(by_id["A"], by_id["C"])

    def test_empty(self):
        self.assertEqual(MemoryManager._fuse([], []), [])


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestParseExtraction(unittest.TestCase):
    def test_standard_json(self):
        got = MemoryManager._parse_extraction('{"memories":["a喜欢猫","b讨厌辣"]}')
        self.assertEqual(got, ["a喜欢猫", "b讨厌辣"])

    def test_markdown_codeblock(self):
        got = MemoryManager._parse_extraction(
            '```json\n{"memories":["用户住在杭州"]}\n```'
        )
        self.assertEqual(got, ["用户住在杭州"])

    def test_empty_payload(self):
        self.assertEqual(MemoryManager._parse_extraction('{"memories":[]}'), [])
        self.assertEqual(MemoryManager._parse_extraction("没有发现可记忆的信息"), [])

    def test_bullets(self):
        got = MemoryManager._parse_extraction("- 喜欢跑步\n- 讨厌香菜")
        self.assertEqual(got, ["喜欢跑步", "讨厌香菜"])

    def test_dedupe_and_maxlen(self):
        long = "记" * 300
        got = MemoryManager._parse_extraction(
            json.dumps({"memories": ["喜欢打篮球", "喜欢打篮球", long]})
        )
        self.assertIn("喜欢打篮球", got)
        self.assertEqual(sum(1 for x in got if x == "喜欢打篮球"), 1)
        self.assertTrue(all(len(x) <= memory.ENTRY_MAX_CHARS for x in got))

    def test_min_fact_chars_filters_noise(self):
        got = MemoryManager._parse_extraction(
            json.dumps({"memories": ["嗯", "好的呀", "用户是一名教师"]})
        )
        self.assertEqual(got, ["用户是一名教师"])


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestFormatInjection(unittest.TestCase):
    def test_age_labels(self):
        mgr = make_manager(memory_inject_max_chars=600)
        hits = [
            {"text": "住在上海", "age_days": 0.5, "effective": 0.9},
            {"text": "喜欢猫", "age_days": 10, "effective": 0.5},
        ]
        text = mgr.format_injection(hits)
        self.assertIn("住在上海（今天）", text)
        self.assertIn("喜欢猫（约10天前）", text)

    def test_hard_cap(self):
        # _inject_max_chars 有 100 的下限保护
        mgr = make_manager(memory_inject_max_chars=1)
        hits = [{"text": "很长的记忆内容" * 30, "age_days": 1, "effective": 0.9}]
        text = mgr.format_injection(hits)
        self.assertLessEqual(len(text), 101)
        self.assertTrue(text.endswith("…"))


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestDecayRecall(unittest.TestCase):
    def test_recall_orders_by_decayed_score_and_drops_expired(self):
        now = time.time()
        day = 86400.0

        class Res:
            def __init__(self, doc_id, sim, upd):
                self.similarity = sim
                self.data = {"doc_id": doc_id, "text": doc_id,
                             "updated_at": upd}

        old_ts = now - 20 * day
        fresh_ts = now - 1 * day
        ancient_ts = now - 200 * day  # 超过 ttl=90

        touched = []

        class DS:
            stopwords = set()

            async def search_sparse(self, tokens, limit):
                return []

            async def update_document_by_doc_id(self, doc_id, text):
                touched.append(doc_id)

        class Vec:
            document_storage = DS()

            async def retrieve(self, query, k, fetch_k, metadata_filters):
                # 旧记忆相似度更高，但衰减后应被新记忆反超
                return [
                    Res("old", 0.95, _iso(old_ts)),
                    Res("fresh", 0.90, _iso(fresh_ts)),
                    Res("ancient", 0.99, _iso(ancient_ts)),
                ]

        kb = type("KB", (), {
            "vec_db": Vec(),
            "kb": type("K", (), {"kb_id": "x", "kb_name": "x",
                                 "embedding_provider_id": "e"})(),
            "init_error": None,
        })()

        mgr = make_manager(
            memory_half_life_days=10, memory_ttl_days=90,
            memory_inject_top_k=3, memory_min_score=0.0,
        )

        async def ensure_ok():
            return kb

        mgr.ensure_kb = ensure_ok
        hits = run(mgr.recall("owner", "查询"))
        ids = [h["text"] for h in hits]
        self.assertNotIn("ancient", ids)
        self.assertEqual(ids[0], "fresh")
        # 入选记忆应被“回忆强化”touch
        self.assertEqual(sorted(touched), ["fresh", "old"])

    def test_top_k_limits(self):
        now = time.time()

        class Res:
            def __init__(self, doc_id, sim):
                self.similarity = sim
                self.data = {"doc_id": doc_id, "text": doc_id,
                             "updated_at": _iso(now)}

        class DS:
            stopwords = set()

            async def search_sparse(self, tokens, limit):
                return []

            async def update_document_by_doc_id(self, doc_id, text):
                pass

        class Vec:
            document_storage = DS()

            async def retrieve(self, query, k, fetch_k, metadata_filters):
                return [Res(f"m{i}", 0.9 - i * 0.01) for i in range(6)]

        kb = type("KB", (), {
            "vec_db": Vec(),
            "kb": type("K", (), {"kb_id": "x", "kb_name": "x",
                                 "embedding_provider_id": "e"})(),
            "init_error": None,
        })()
        mgr = make_manager(memory_inject_top_k=2)

        async def ensure_ok():
            return kb

        mgr.ensure_kb = ensure_ok
        hits = run(mgr.recall("owner", "q"))
        self.assertEqual(len(hits), 2)


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestConfigCompat(unittest.TestCase):
    def test_grouped_and_flat(self):
        grouped = FakeConfig(memory={"memory_half_life_days": 7})
        mgr = MemoryManager(type("C", (), {})(), grouped)
        self.assertEqual(mgr._half_life_days(), 7)

        flat = FakeConfig(memory_half_life_days=5)  # 顶层扁平键（旧版兼容）
        mgr2 = MemoryManager(type("C", (), {})(), flat)
        self.assertEqual(mgr2._half_life_days(), 5)

    def test_bounds(self):
        mgr = make_manager(memory_dup_threshold=99, memory_inject_top_k=0,
                           memory_half_life_days=-5)
        self.assertLessEqual(mgr._dup_threshold(), 1.0)
        self.assertGreaterEqual(mgr._inject_top_k(), 1)
        self.assertGreater(mgr._half_life_days(), 0)

    def test_kb_name_selection(self):
        mgr = make_manager(memory_kb_name=["", " 记忆库 "])
        self.assertEqual(mgr._kb_name(), "记忆库")


if __name__ == "__main__":
    unittest.main(verbosity=2)
