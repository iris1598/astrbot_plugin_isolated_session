"""好感度插件联动（/会话重置 清除对应人格用户评价）回归测试。

测试覆盖：
1. 未安装好感度插件：正常重置，无多余提示，不抛异常；
2. 配置关闭 favorability_reset_eval_with_session：不执行好感度清除；
3. 人格隔离（区分好人格）：
   - 当会话生效人格为 西格莉卡 时，仅修改 西格莉卡/favorability.json；
   - 其他人格（如 爱弥斯）对应用户的数据完全不受影响；
4. 数据安全保障（不能动其它数据）：
   - 目标用户仅 eval 字段重置为 "初次见面"；
   - 目标用户的 score, relation, pending_rel, name, muted_until, rel_cooldown_until 必须 100% 保持原样；
   - 同群其他用户的所有数据必须 100% 保持原样；
   - 其他群组的所有数据必须 100% 保持原样；
5. 真实数据格式兼容：OneBot / QQ / 官方隔离 UMO 剥离。
"""

import asyncio
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))

import test_gate  # noqa: E402
import test_reset_memory as TRM  # noqa: E402

_HAS = test_gate._HAS
Main = test_gate.Main
if _HAS:
    import favorability_bridge as FB
else:
    FB = None


SAMPLE_XIGELIKA_DATA = {
    "Iris:GroupMessage:1041386550": {
        "846370266": {
            "score": 3855,
            "eval": "分享大肥鱼吃瘪漫画的前辈",
            "name": "xp是摆烂㊗️",
            "muted_until": 1785654739.670079,
            "relation": "挚爱恋人",
            "pending_rel": None,
            "rel_cooldown_until": None,
        },
        "394677174": {
            "score": 371,
            "relation": "挚爱恋人",
            "pending_rel": None,
            "eval": "试图单方面除名的坏心眼",
            "name": "压力一只大肥鱼",
            "muted_until": None,
            "rel_cooldown_until": None,
        },
    },
    "Iris:GroupMessage:999999": {
        "846370266": {
            "score": 100,
            "relation": "普通朋友",
            "eval": "其他群的评价",
            "name": "xp是摆烂㊗️",
        }
    },
}

SAMPLE_AIMISI_DATA = {
    "Iris:GroupMessage:1041386550": {
        "846370266": {
            "score": 999,
            "eval": "爱弥斯的专属印象",
            "name": "测试用户",
            "muted_until": None,
            "relation": "知心挚友",
            "pending_rel": None,
            "rel_cooldown_until": None,
        }
    }
}


class FakeStarMetadata:
    def __init__(self, name, star_cls, activated=True):
        self.name = name
        self.star_cls = star_cls
        self.activated = activated


class FakePersonaManager:
    def __init__(self, current_persona="default"):
        self.current_persona = current_persona

    async def resolve_selected_persona(self, **kwargs):
        return (self.current_persona, None)


class FakeConv:
    def __init__(self, persona_id="default"):
        self.persona_id = persona_id
        self.title = "测试会话"


@unittest.skipUnless(_HAS, "需要含 astrbot 的 Python 环境")
class TestFavorabilityLink(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_fav_link_"))
        self.data_dir = self.temp_dir / "plugin_data" / "astrbot_plugin_favorability"
        self.personas_dir = self.data_dir / "personas"
        self.personas_dir.mkdir(parents=True, exist_ok=True)

        # 写入真实格式样本
        (self.personas_dir / "西格莉卡").mkdir(parents=True, exist_ok=True)
        with open(self.personas_dir / "西格莉卡" / "favorability.json", "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(SAMPLE_XIGELIKA_DATA), f, ensure_ascii=False, indent=2)

        (self.personas_dir / "爱弥斯").mkdir(parents=True, exist_ok=True)
        with open(self.personas_dir / "爱弥斯" / "favorability.json", "w", encoding="utf-8") as f:
            json.dump(copy.deepcopy(SAMPLE_AIMISI_DATA), f, ensure_ascii=False, indent=2)

        # 模拟 StarTools.get_data_dir
        from astrbot.api.star import StarTools
        self._orig_get_data_dir = getattr(StarTools, "get_data_dir", None)
        StarTools.get_data_dir = lambda name=None: self.data_dir

    def tearDown(self):
        from astrbot.api.star import StarTools
        if self._orig_get_data_dir:
            StarTools.get_data_dir = self._orig_get_data_dir
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_plugin(self, persona="西格莉卡", fav_plugin_inst=None, fav_enabled_cfg=True):
        ctx = TRM.FakeContextBase(unique=True)
        ctx.persona_manager = FakePersonaManager(persona)
        conv = FakeConv(persona)

        async def get_conv(umo, cid):
            return conv

        mgr = TRM.FakeConvMgr()
        mgr.get_conversation = get_conv
        ctx.conversation_manager = mgr

        cfg = {
            "favorability_reset_eval_with_session": fav_enabled_cfg,
            "memory": {"memory_enabled": False},
        }
        plug = Main(ctx, cfg)
        return plug, ctx

    def test_not_installed_no_crash(self):
        """未安装好感度插件时，重置正常完成，无好感度提示。"""
        # 清理数据目录以模拟未安装
        shutil.rmtree(self.data_dir, ignore_errors=True)

        plug, ctx = self._make_plugin()
        event = TRM.Ev(group_id="1041386550", umo="Iris:GroupMessage:846370266_1041386550")
        event.get_sender_id = lambda: "846370266"

        out = []
        async def go():
            async for m in plug.cmd_reset(event):
                out.append(m[1])
        asyncio.run(go())
        msg = "\n".join(out)

        self.assertIn("✅", msg)
        self.assertNotIn("好感度评价", msg)

    def test_installed_clears_eval_and_preserves_all_other_data(self):
        """已安装好感度插件：重置时仅清除对应用户的评价，严格保留好感度等其它所有数据。"""
        from astrbot.core.star.star import star_registry

        # 实例化真正的 FavorabilityManager
        from astrbot_plugin_favorability.models.manager import FavorabilityManager

        fav_mgr = FavorabilityManager(self.data_dir)

        class MockFavPlugin:
            def __init__(self, db):
                self.db = db
            async def resolve_persona_id(self, event, req=None):
                return "西格莉卡"
            def keys(self, event):
                uid = str(event.get_sender_id())
                return FB.group_storage_key(event.unified_msg_origin, uid), uid

        fav_plugin = MockFavPlugin(fav_mgr)
        meta = FakeStarMetadata("astrbot_plugin_favorability", fav_plugin, activated=True)
        star_registry.append(meta)

        try:
            plug, ctx = self._make_plugin(persona="西格莉卡")
            event = TRM.Ev(group_id="1041386550", umo="Iris:GroupMessage:846370266_1041386550")
            event.get_sender_id = lambda: "846370266"

            out = []
            async def go():
                async for m in plug.cmd_reset(event):
                    out.append(m[1])
            asyncio.run(go())
            msg = "\n".join(out)

            self.assertIn("已同步清除【西格莉卡】好感度评价", msg)

            # 验证 西格莉卡 数据
            with open(self.personas_dir / "西格莉卡" / "favorability.json", "r", encoding="utf-8") as f:
                x_data = json.load(f)

            user_target = x_data["Iris:GroupMessage:1041386550"]["846370266"]
            # 1. 评价必须被重置为 '初次见面'
            self.assertEqual(user_target["eval"], "初次见面")
            # 2. 其它关键数据必须 100% 毫发无损！
            self.assertEqual(user_target["score"], 3855)
            self.assertEqual(user_target["relation"], "挚爱恋人")
            self.assertEqual(user_target["name"], "xp是摆烂㊗️")
            self.assertEqual(user_target["muted_until"], 1785654739.670079)
            self.assertIsNone(user_target["pending_rel"])
            self.assertIsNone(user_target["rel_cooldown_until"])

            # 3. 同群其它用户必须 100% 毫发无损！
            other_user = x_data["Iris:GroupMessage:1041386550"]["394677174"]
            self.assertEqual(other_user["score"], 371)
            self.assertEqual(other_user["relation"], "挚爱恋人")
            self.assertEqual(other_user["eval"], "试图单方面除名的坏心眼")

            # 4. 其它群组的数据必须 100% 毫发无损！
            other_grp = x_data["Iris:GroupMessage:999999"]["846370266"]
            self.assertEqual(other_grp["eval"], "其他群的评价")
            self.assertEqual(other_grp["score"], 100)

            # 5. 其他人格（爱弥斯）必须 100% 毫发无损！
            with open(self.personas_dir / "爱弥斯" / "favorability.json", "r", encoding="utf-8") as f:
                a_data = json.load(f)
            aimisi_user = a_data["Iris:GroupMessage:1041386550"]["846370266"]
            self.assertEqual(aimisi_user["eval"], "爱弥斯的专属印象")
            self.assertEqual(aimisi_user["score"], 999)
            self.assertEqual(aimisi_user["relation"], "知心挚友")

        finally:
            if meta in star_registry:
                star_registry.remove(meta)

    def test_config_disabled_does_not_clear(self):
        """当配置 favorability_reset_eval_with_session=False 时，不清除好感度评价。"""
        plug, ctx = self._make_plugin(persona="西格莉卡", fav_enabled_cfg=False)
        event = TRM.Ev(group_id="1041386550", umo="Iris:GroupMessage:846370266_1041386550")
        event.get_sender_id = lambda: "846370266"

        out = []
        async def go():
            async for m in plug.cmd_reset(event):
                out.append(m[1])
        asyncio.run(go())
        msg = "\n".join(out)

        self.assertNotIn("好感度评价", msg)
        with open(self.personas_dir / "西格莉卡" / "favorability.json", "r", encoding="utf-8") as f:
            x_data = json.load(f)
        self.assertEqual(x_data["Iris:GroupMessage:1041386550"]["846370266"]["eval"], "分享大肥鱼吃瘪漫画的前辈")


if __name__ == "__main__":
    unittest.main(verbosity=2)
