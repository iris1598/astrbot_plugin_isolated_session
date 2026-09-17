"""测试引导：把 AstrBot 自带 app 目录加入 sys.path，并探测可用依赖。

运行方式（仓库根目录，使用 AstrBot 自带 Python 才能启用 astrbot 相关测试）：
    AstrBot\\backend\\python\\python.exe -m unittest discover ^
        -s astrbot_plugin_isolated_memory\\tests -p "test_*.py"
"""

import importlib
import os
import sys
from pathlib import Path


def _ensure_path() -> None:
    env = os.environ.get("ASTRBOT_APP", "")
    cands = []
    if env:
        cands.append(Path(env))
    here = Path(__file__).resolve()
    for parent in here.parents:
        cands.append(parent / "AstrBot" / "backend" / "app")
    for cand in cands:
        try:
            if cand and (cand / "astrbot").is_dir():
                if str(cand) not in sys.path:
                    sys.path.insert(0, str(cand))
                break
        except OSError:
            continue


_ensure_path()


def astrbot_available() -> bool:
    """astrbot 核心链路（含知识库/faiss 等重依赖）是否可导入。"""
    try:
        importlib.import_module("astrbot.api")
        importlib.import_module("astrbot.core.knowledge_base.kb_helper")
        return True
    except Exception:
        return False


def has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def bootstrap() -> bool:
    """向后兼容：等价于 astrbot_available()。"""
    return astrbot_available()
