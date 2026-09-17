"""MBTI 记忆测评海报渲染模块测试。"""

import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(PLUGIN_DIR))

import mbti_render  # noqa: E402


class TestMbtiRenderProfiles(unittest.TestCase):
    """测试 16 个人格资料库的完整性与格式。"""

    EXPECTED_16 = {
        "INTJ", "INTP", "ENTJ", "ENTP",
        "INFJ", "INFP", "ENFJ", "ENFP",
        "ISTJ", "ISFJ", "ESTJ", "ESFJ",
        "ISTP", "ISFP", "ESTP", "ESFP",
    }

    def test_all_16_profiles_present(self):
        self.assertEqual(set(mbti_render.MBTI_PROFILES.keys()), self.EXPECTED_16)

    def test_profile_fields_complete(self):
        for code, profile in mbti_render.MBTI_PROFILES.items():
            self.assertEqual(profile["code"], code)
            self.assertTrue(profile["name"], f"{code} missing name")
            self.assertTrue(profile["en_name"], f"{code} missing en_name")
            self.assertTrue(profile["temperament"], f"{code} missing temperament")
            self.assertTrue(profile["color"].startswith("#"), f"{code} invalid color")
            self.assertTrue(profile["tagline"], f"{code} missing tagline")
            self.assertIsInstance(profile["tags"], list)
            self.assertGreaterEqual(len(profile["tags"]), 3)
            # 简短速描不超过 120 字符
            self.assertLess(len(profile["desc"]), 120, f"{code} desc too long")


class TestResolvePersonalities(unittest.TestCase):
    """测试可能人格（含 ?）展开逻辑。"""

    def test_exact_type(self):
        cands = mbti_render.resolve_possible_personalities("INTJ")
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0]["code"], "INTJ")

    def test_single_uncertain_dimension(self):
        cands = mbti_render.resolve_possible_personalities("IN?J")
        codes = [c["code"] for c in cands]
        self.assertEqual(codes, ["INTJ", "INFJ"])

    def test_two_uncertain_dimensions(self):
        cands = mbti_render.resolve_possible_personalities("I??P")
        codes = [c["code"] for c in cands]
        self.assertEqual(codes, ["ISTP", "ISFP", "INTP", "INFP"])

    def test_all_uncertain_fallback(self):
        cands = mbti_render.resolve_possible_personalities("????")
        self.assertLessEqual(len(cands), 4)
        self.assertGreaterEqual(len(cands), 1)

    def test_invalid_type_returns_unknown(self):
        cands = mbti_render.resolve_possible_personalities("123")
        self.assertGreaterEqual(len(cands), 1)


class TestDimensionView(unittest.TestCase):
    """测试维度计算与 SVG 雷达图几何数据。"""

    def test_polar_percentages(self):
        dimensions = [
            {"name": "E/I", "pole": "I", "strength": 72},
            {"name": "S/N", "pole": "S", "strength": 60},
            {"name": "T/F", "pole": "", "strength": 0},
            {"name": "J/P", "pole": "P", "strength": 80},
        ]
        bars, pole_scores, radar_svg = mbti_render.calculate_dimension_view(dimensions)

        self.assertEqual(len(bars), 4)
        # E/I: I 72%, E 28%
        ei_bar = next(b for b in bars if b["dim_name"] == "E/I")
        self.assertEqual(ei_bar["dominant"], "I")
        self.assertEqual(ei_bar["pct_r"], 72)
        self.assertEqual(ei_bar["pct_l"], 28)

        # T/F: neutral 50/50
        tf_bar = next(b for b in bars if b["dim_name"] == "T/F")
        self.assertEqual(tf_bar["dominant"], "")
        self.assertEqual(tf_bar["pct_l"], 50)
        self.assertEqual(tf_bar["pct_r"], 50)

        # Radar SVG components
        self.assertIn("polygon_points", radar_svg)
        self.assertEqual(len(radar_svg["rings"]), 4)
        self.assertEqual(len(radar_svg["spokes"]), 8)
        self.assertEqual(len(radar_svg["dots"]), 8)
        self.assertEqual(len(radar_svg["labels"]), 8)


class TestHtmlTemplateRendering(unittest.TestCase):
    """测试 HTML 模板渲染与证据隐藏。"""

    def test_renders_valid_html_without_evidence(self):
        sample = {
            "type": "INTJ",
            "confidence": 85,
            "sample_count": 20,
            "used_count": 20,
            "dimensions": [
                {"name": "E/I", "pole": "I", "strength": 72, "evidence": "绝密回忆：某天夜里独处"},
                {"name": "S/N", "pole": "N", "strength": 65, "evidence": "绝密回忆：喜欢看科幻"},
                {"name": "T/F", "pole": "T", "strength": 80, "evidence": "绝密回忆：讲求逻辑"},
                {"name": "J/P", "pole": "J", "strength": 64, "evidence": "绝密回忆：每天做计划"},
            ],
            "disclaimer": "⚠️ 测试声明",
        }
        html = mbti_render.render_mbti_html(sample, user_name="测试用户")

        # 核心元素验证
        self.assertIn("INTJ", html)
        self.assertIn("建筑师", html)
        self.assertIn("85%", html)
        self.assertIn("四维倾向图谱", html)
        self.assertIn("<svg", html)
        self.assertIn("polygon", html)
        self.assertIn("⚠️ 测试声明", html)

        # 绝对不展示具体记忆证据
        self.assertNotIn("绝密回忆", html)
        self.assertNotIn("某天夜里独处", html)
        self.assertNotIn("喜欢看科幻", html)
        self.assertNotIn("讲求逻辑", html)
        self.assertNotIn("每天做计划", html)

    def test_candidate_rendering_with_question_mark(self):
        sample = {
            "type": "IN?J",
            "confidence": 70,
            "dimensions": [
                {"name": "E/I", "pole": "I", "strength": 60},
                {"name": "S/N", "pole": "N", "strength": 60},
                {"name": "T/F", "pole": "", "strength": 0},
                {"name": "J/P", "pole": "J", "strength": 60},
            ],
        }
        html = mbti_render.render_mbti_html(sample)
        self.assertIn("IN?J", html)
        self.assertIn("INTJ", html)
        self.assertIn("INFJ", html)
        self.assertIn("建筑师", html)
        self.assertIn("提倡者", html)


import bootstrap

_HAS = bootstrap.bootstrap()


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestCommandDeliveryBranches(unittest.IsolatedAsyncioTestCase):
    """测试命令执行分发逻辑（图片发送、文本指令强制、降级回退）。"""

    def setUp(self):
        from unittest.mock import AsyncMock, MagicMock
        from main import Main

        self.Main = Main
        self.plug = MagicMock()
        self.plug.config = {"memory_mbti_render_mode": "image", "memory_mbti_enabled": True}
        self.plug._mcfg = lambda k, d=None: self.plug.config.get(k, d)
        self.plug._gate_block_reason = lambda ev: None
        self.plug._ensure_memory = AsyncMock(return_value=self.plug)

        self.plug.collect_memory_entries = AsyncMock(
            return_value=[{"text": f"m{i}"} for i in range(10)]
        )
        self.plug.build_mbti_anchor_report = AsyncMock(
            return_value={
                "type": "INTJ",
                "confidence": 80,
                "dimensions": [{"name": "E/I", "pole": "I", "strength": 70}],
                "sample_count": 10,
                "used_count": 10,
            }
        )
        self.plug.format_mbti_report = MagicMock(return_value="[TEXT REPORT]")

    async def test_image_delivery_success(self):
        from unittest.mock import MagicMock
        ev = MagicMock()
        ev.unified_msg_origin = "umo_1"
        ev.image_result = lambda path: ("IMAGE", path)
        ev.plain_result = lambda text: ("TEXT", text)

        res = [r async for r in self.Main.cmd_memory_mbti(self.plug, ev)]
        self.assertEqual(res[0][0], "IMAGE")
        img_path = res[0][1]
        self.assertTrue(os.path.exists(img_path))
        self.assertTrue(img_path.endswith(".png"))

    async def test_force_text_arg(self):
        from unittest.mock import MagicMock
        ev = MagicMock()
        ev.unified_msg_origin = "umo_1"
        ev.image_result = lambda path: ("IMAGE", path)
        ev.plain_result = lambda text: ("TEXT", text)

        res = [r async for r in self.Main.cmd_memory_mbti(self.plug, ev, arg="文本")]
        self.assertEqual(res[0][0], "TEXT")
        self.assertEqual(res[0][1], "[TEXT REPORT]")

    async def test_fallback_when_render_fails(self):
        from unittest.mock import MagicMock, patch
        ev = MagicMock()
        ev.unified_msg_origin = "umo_1"
        ev.image_result = lambda path: ("IMAGE", path)
        ev.plain_result = lambda text: ("TEXT", text)

        with patch("mbti_render.render_mbti_poster_pillow", side_effect=RuntimeError("Pillow crash")):
            res = [r async for r in self.Main.cmd_memory_mbti(self.plug, ev)]
            self.assertEqual(res[0][0], "TEXT")
            self.assertEqual(res[0][1], "[TEXT REPORT]")

    async def test_prefixless_xxti_and_arg_extraction(self):
        from unittest.mock import MagicMock
        # 模拟免 / 直接发送 "xxti 文本"
        ev = MagicMock()
        ev.unified_msg_origin = "umo_1"
        ev.get_message_str = lambda: "xxti 文本"
        ev.image_result = lambda path: ("IMAGE", path)
        ev.plain_result = lambda text: ("TEXT", text)
        stopped = []
        ev.stop_event = lambda: stopped.append(True)

        res = [r async for r in self.Main.cmd_memory_mbti(self.plug, ev)]
        self.assertEqual(res[0][0], "TEXT")
        self.assertEqual(res[0][1], "[TEXT REPORT]")
        self.assertTrue(len(stopped) > 0, "stop_event 应被调用以拦截 LLM")

class TestPillowPosterDirectGeneration(unittest.TestCase):
    """测试 Pillow 原生海报生成功能。"""

    def test_pillow_poster_direct_generation(self):
        from PIL import Image
        sample = {
            "type": "ENFJ",
            "confidence": 75,
            "sample_count": 16,
            "dimensions": [
                {"name": "E/I", "pole": "E", "strength": 20},
                {"name": "S/N", "pole": "N", "strength": 25},
                {"name": "T/F", "pole": "F", "strength": 30},
                {"name": "J/P", "pole": "J", "strength": 18},
            ],
        }
        out_path = mbti_render.render_mbti_poster_pillow(sample, user_name="测试员")
        self.assertTrue(os.path.exists(out_path))
        with Image.open(out_path) as img:
            self.assertEqual(img.size, (760, 700))
        try:
            os.remove(out_path)
        except OSError:
            pass

    def test_pillow_poster_uncertain_generation(self):
        from PIL import Image
        sample = {
            "type": "IN?J",
            "confidence": 50,
            "sample_count": 10,
            "dimensions": [
                {"name": "E/I", "pole": "I", "strength": 20},
                {"name": "S/N", "pole": "N", "strength": 20},
                {"name": "T/F", "pole": "", "strength": 0},
                {"name": "J/P", "pole": "J", "strength": 20},
            ],
        }
        out_path = mbti_render.render_mbti_poster_pillow(sample, user_name="探险者")
        self.assertTrue(os.path.exists(out_path))
        with Image.open(out_path) as img:
            self.assertEqual(img.size, (760, 700))
        try:
            os.remove(out_path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)

