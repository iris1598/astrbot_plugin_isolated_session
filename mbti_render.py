"""MBTI 记忆测评精简海报渲染器。

采用紧凑型单屏海报设计（Side-by-Side 维度图谱 + 极简人格速描），
绝不展示具体记忆原文证据，保护隐私且视觉优雅。
"""

import math
import os
import re
import tempfile
from typing import Any

import jinja2

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

# 16 种 MBTI 人格极简画像资料库（40~60 字精简速描）
MBTI_PROFILES: dict[str, dict[str, Any]] = {
    # ── 分析家 (NT) ──
    "INTJ": {
        "code": "INTJ",
        "name": "建筑师",
        "en_name": "The Architect",
        "temperament": "分析家 · NT",
        "color": "#8a4fff",
        "gradient": "linear-gradient(135deg, #6c3ce9, #8a4fff)",
        "tagline": "深谋远虑的战略架构师，以理性与长远构筑系统",
        "tags": ["战略全局", "独立理性", "深度洞察", "严谨追求"],
        "desc": "习惯站在全局和底层原理审视问题，崇尚逻辑自洽与独立决断。对事物有极高标准，善于将复杂构想拆解并落地为精妙条理的方案。",
    },
    "INTP": {
        "code": "INTP",
        "name": "逻辑学家",
        "en_name": "The Logician",
        "temperament": "分析家 · NT",
        "color": "#8a4fff",
        "gradient": "linear-gradient(135deg, #6c3ce9, #8a4fff)",
        "tagline": "探索真理底层脉络的思辨者，对本质保持无限好奇",
        "tags": ["求真务实", "敏锐思辨", "理论构建", "开放探索"],
        "desc": "拥有敏锐的思想深度，热衷探索事物运转的终极逻辑与规律。不喜墨守成规，以客观超然的视角剖析世界，在纯粹智力探索中充满生机。",
    },
    "ENTJ": {
        "code": "ENTJ",
        "name": "指挥官",
        "en_name": "The Commander",
        "temperament": "分析家 · NT",
        "color": "#8a4fff",
        "gradient": "linear-gradient(135deg, #6c3ce9, #8a4fff)",
        "tagline": "兼具魄力与远见的天生统帅，果敢开拓并引领全局",
        "tags": ["高效决断", "统筹大局", "目标坚定", "开拓先锋"],
        "desc": "目标清晰且决断力极强，擅长整合资源与统领复杂局面。崇尚高效与规则，在困难面前果敢进取，能迅速将长远战略转化为实打实的成果。",
    },
    "ENTP": {
        "code": "ENTP",
        "name": "辩论家",
        "en_name": "The Debater",
        "temperament": "分析家 · NT",
        "color": "#8a4fff",
        "gradient": "linear-gradient(135deg, #6c3ce9, #8a4fff)",
        "tagline": "灵感四溢的破局智多星，享受思维碰撞与可能性",
        "tags": ["创新灵动", "敏捷辩才", "破界重构", "幽默机智"],
        "desc": "思维敏捷风趣，擅长从出人意料的角度重构观点。不喜条条框框，享受新想法的迸发与推演，永远在追寻下一个充满挑战的新奇可能。",
    },
    # ── 外交家 (NF) ──
    "INFJ": {
        "code": "INFJ",
        "name": "提倡者",
        "en_name": "The Advocate",
        "temperament": "外交家 · NF",
        "color": "#10b981",
        "gradient": "linear-gradient(135deg, #059669, #10b981)",
        "tagline": "心怀远见与利他理想的引路人，温和而坚定地关怀世界",
        "tags": ["理想主义", "深刻共情", "远见洞察", "坚韧温良"],
        "desc": "兼具直觉深度与共情温度，对人性和生命怀有崇高信念。看似安静内敛，内心却有着强大的原则与行动力，默默成为照亮身边的温暖灯塔。",
    },
    "INFP": {
        "code": "INFP",
        "name": "调停者",
        "en_name": "The Mediator",
        "temperament": "外交家 · NF",
        "color": "#10b981",
        "gradient": "linear-gradient(135deg, #059669, #10b981)",
        "tagline": "守护真实与诗意的心灵漫游者，以温柔目光凝视平凡",
        "tags": ["纯粹本心", "细腻同理", "诗意浪漫", "忠于自我"],
        "desc": "内心世界细腻而辽阔，极度重视自我真诚与精神共鸣。对万物抱有真挚同情与理解，在守护内心原则与珍视之人时拥有惊人的坚韧。",
    },
    "ENFJ": {
        "code": "ENFJ",
        "name": "主人公",
        "en_name": "The Protagonist",
        "temperament": "外交家 · NF",
        "color": "#10b981",
        "gradient": "linear-gradient(135deg, #059669, #10b981)",
        "tagline": "充满感染力与使命感的导师，真诚启发并温暖同行者",
        "tags": ["温暖鼓舞", "领袖感染", "促成和谐", "积极真诚"],
        "desc": "天生具备强大的共情力与沟通魅力，热忱于发掘他人潜能并构建和谐集体。胸怀广阔、善解人意，总能凝聚共识为周围带来希望与光芒。",
    },
    "ENFP": {
        "code": "ENFP",
        "name": "竞选者",
        "en_name": "The Campaigner",
        "temperament": "外交家 · NF",
        "color": "#10b981",
        "gradient": "linear-gradient(135deg, #059669, #10b981)",
        "tagline": "自由灵动的热情探索家，拥抱万千可能与生活的美好",
        "tags": ["热情洋溢", "丰富想象", "真诚链接", "自由无界"],
        "desc": "充满活力与好奇心，总能捕捉生活中未被发现的新奇与温情。善于与人建立真挚深层的情感连接，是驱散沉闷、点燃热情的活力发光体。",
    },
    # ── 守护者 (SJ) ──
    "ISTJ": {
        "code": "ISTJ",
        "name": "物流师",
        "en_name": "The Logistician",
        "temperament": "守护者 · SJ",
        "color": "#3b82f6",
        "gradient": "linear-gradient(135deg, #2563eb, #3b82f6)",
        "tagline": "恪尽职守、踏实可靠的秩序基石，用责任确保步步落实",
        "tags": ["严谨守信", "务实负责", "井然有序", "稳健周密"],
        "desc": "为人沉稳本分、重诺守信。极其注重事实细节与操作规范，行事条理分明、有始有终，是任何团队与日常生活中最值得依赖的中流砥柱。",
    },
    "ISFJ": {
        "code": "ISFJ",
        "name": "守卫者",
        "en_name": "The Defender",
        "temperament": "守护者 · SJ",
        "color": "#3b82f6",
        "gradient": "linear-gradient(135deg, #2563eb, #3b82f6)",
        "tagline": "体贴入微的温情后盾，用细致入微的付出守护身边人",
        "tags": ["细腻关怀", "默默奉献", "务实耐心", "忠诚可靠"],
        "desc": "兼具温暖包容与务实耐心，擅长感知并照顾他人的生活细节。做事有条不紊且甘居幕后，用持久细致的关怀构筑令人安心的避风港湾。",
    },
    "ESTJ": {
        "code": "ESTJ",
        "name": "总经理",
        "en_name": "The Executive",
        "temperament": "守护者 · SJ",
        "color": "#3b82f6",
        "gradient": "linear-gradient(135deg, #2563eb, #3b82f6)",
        "tagline": "崇尚秩序的高效推进者，以清晰规则统筹秩序落地",
        "tags": ["果敢统筹", "立足现实", "规则秩序", "高效执行"],
        "desc": "讲求效率与落地结果，具备出色的组织协调与管理才能。信守承诺与原则，处事雷厉风行，在混乱局面中总能迅速建立清晰稳固的秩序。",
    },
    "ESFJ": {
        "code": "ESFJ",
        "name": "执政官",
        "en_name": "The Consul",
        "temperament": "守护者 · SJ",
        "color": "#3b82f6",
        "gradient": "linear-gradient(135deg, #2563eb, #3b82f6)",
        "tagline": "热忱周到的社交纽带，维系人际温暖与群体的融洽",
        "tags": ["热情亲和", "善解人意", "乐于助人", "维系和睦"],
        "desc": "极富社交热情与奉献精神，敏锐体察并满足他人的需求。擅长组织活动与营造温暖和谐的氛围，用实际行动织就紧密温暖的人际网络。",
    },
    # ── 探险家 (SP) ──
    "ISTP": {
        "code": "ISTP",
        "name": "鉴赏家",
        "en_name": "The Virtuoso",
        "temperament": "探险家 · SP",
        "color": "#f59e0b",
        "gradient": "linear-gradient(135deg, #d97706, #f59e0b)",
        "tagline": "冷静沉着的实干匠人，在实践探索中游刃有余地破局",
        "tags": ["敏锐观察", "冷静应变", "巧手实操", "自洽独立"],
        "desc": "拥有敏锐的现实观察力与动手能力，擅长拆解与解决实际机械或战术问题。面对突发波澜不惊，崇尚自由独立，以务实洒脱的姿态应对挑战。",
    },
    "ISFP": {
        "code": "ISFP",
        "name": "探险家",
        "en_name": "The Adventurer",
        "temperament": "探险家 · SP",
        "color": "#f59e0b",
        "gradient": "linear-gradient(135deg, #d97706, #f59e0b)",
        "tagline": "沉浸当下的灵动艺术家，用细腻感知捕捉生活韵律",
        "tags": ["独特审美", "温和真挚", "活在当下", "包容随性"],
        "desc": "内心温厚谦逊，对审美与生活细节拥有独特感悟。不喜条条框框，遵从内心感受生活，待人真诚友善，低调从容地展现独具魅力的自我。",
    },
    "ESTP": {
        "code": "ESTP",
        "name": "企业家",
        "en_name": "The Entrepreneur",
        "temperament": "探险家 · SP",
        "color": "#f59e0b",
        "gradient": "linear-gradient(135deg, #d97706, #f59e0b)",
        "tagline": "果敢敏锐的现场破浪者，随时随地把握机遇付诸行动",
        "tags": ["行动先锋", "即兴决断", "活力四射", "务实直击"],
        "desc": "充满冒险精神与行动力，擅长在变化莫测的环境中快速决断。崇尚动手解决现实问题而非空谈理论，风趣幽默，永远处于行动的最前线。",
    },
    "ESFP": {
        "code": "ESFP",
        "name": "表演者",
        "en_name": "The Entertainer",
        "temperament": "探险家 · SP",
        "color": "#f59e0b",
        "gradient": "linear-gradient(135deg, #d97706, #f59e0b)",
        "tagline": "聚光灯下的快乐传递者，以充沛热情感染周遭世界",
        "tags": ["热情乐观", "社交焦点", "即兴风趣", "享受此刻"],
        "desc": "天生具备强大的感染力，享受当下每一刻的美好与欢笑。乐于成为人群中心并分享快乐，待人慷慨大方、活力充沛，是生活中的阳光焦点。",
    },
}

# 默认待定型资料
UNKNOWN_PROFILE: dict[str, Any] = {
    "code": "中性平衡",
    "name": "多维平衡型",
    "en_name": "Balanced Spectrum",
    "temperament": "综合平衡 · Multi-trait",
    "color": "#8b5cf6",
    "gradient": "linear-gradient(135deg, #6366f1, #8b5cf6)",
    "tagline": "记忆呈现均衡中性特征，思维与行为兼具多面包容性",
    "tags": ["多维平衡", "灵活兼蓄", "情境适应", "随和自洽"],
    "desc": "当前记忆样本在各维度倾向相对均衡，未展现单一极化特征。具备随情境灵活切换思考与社交模式的自适应潜能。",
}

# 8 极雷达图极点配置（对称 diametrical 布局）
# 角度：E(-90°), S(-45°), T(0°), J(45°), I(90°), N(135°), F(180°), P(225°)
RADAR_POLES = [
    {"pole": "E", "label": "E 外向", "angle": -90, "dim": "E/I", "side": "left"},
    {"pole": "S", "label": "S 实感", "angle": -45, "dim": "S/N", "side": "left"},
    {"pole": "T", "label": "T 思考", "angle": 0, "dim": "T/F", "side": "left"},
    {"pole": "J", "label": "J 判断", "angle": 45, "dim": "J/P", "side": "left"},
    {"pole": "I", "label": "I 内向", "angle": 90, "dim": "E/I", "side": "right"},
    {"pole": "N", "label": "N 直觉", "angle": 135, "dim": "S/N", "side": "right"},
    {"pole": "F", "label": "F 情感", "angle": 180, "dim": "T/F", "side": "right"},
    {"pole": "P", "label": "P 知觉", "angle": 225, "dim": "J/P", "side": "right"},
]


def resolve_possible_personalities(type_str: str) -> list[dict[str, Any]]:
    """依据测评推测出的 MBTI 类型字符串（可能含 ?）展开候选人格列表。

    Args:
        type_str: 如 "INTJ", "IN?J", "I??P", "????"。

    Returns:
        list[dict]: 匹配的 MBTI 人格资料列表（最多返回 4 个最匹配候选）。
    """
    clean_type = re.sub(r"[^A-Za-z?]", "", str(type_str or "")).upper()
    if len(clean_type) != 4:
        clean_type = "????"

    options = [
        ["E", "I"] if clean_type[0] == "?" else [clean_type[0]],
        ["S", "N"] if clean_type[1] == "?" else [clean_type[1]],
        ["T", "F"] if clean_type[2] == "?" else [clean_type[2]],
        ["J", "P"] if clean_type[3] == "?" else [clean_type[3]],
    ]

    matched_codes: list[str] = []
    for c0 in options[0]:
        for c1 in options[1]:
            for c2 in options[2]:
                for c3 in options[3]:
                    candidate = f"{c0}{c1}{c2}{c3}"
                    if candidate in MBTI_PROFILES:
                        matched_codes.append(candidate)

    if not matched_codes:
        return [UNKNOWN_PROFILE]

    # 最多取前 4 个代表性候选人格展示
    return [MBTI_PROFILES[code] for code in matched_codes[:4]]


def calculate_dimension_view(
    dimensions: list[dict],
) -> tuple[list[dict], dict[str, int], dict[str, Any]]:
    """根据报告维度数据计算两极平衡滑块数据与 SVG 8 极雷达图几何参数。

    Args:
        dimensions: 报告中的 dimensions 列表。

    Returns:
        (bars_data, pole_scores, radar_svg_data)
    """
    dim_map: dict[str, dict] = {}
    for d in dimensions:
        name = str(d.get("name") or "").upper().replace("-", "/")
        dim_map[name] = d

    # 4 轴滑块标准配置
    axis_specs = [
        ("E/I", "E", "外向", "I", "内向"),
        ("S/N", "S", "实感", "N", "直觉"),
        ("T/F", "T", "思考", "F", "情感"),
        ("J/P", "J", "判断", "P", "知觉"),
    ]

    bars = []
    pole_scores: dict[str, int] = {}

    for dim_name, pole_l, label_l, pole_r, label_r in axis_specs:
        dim_data = dim_map.get(dim_name, {})
        active_pole = str(dim_data.get("pole") or "").upper()
        strength = max(0, min(100, int(dim_data.get("strength") or 0)))

        if active_pole == pole_l:
            if strength > 50:
                dom_pct = strength
            elif strength > 0:
                dom_pct = min(98, 50 + max(2, round(strength * 0.5)))
            else:
                dom_pct = 54
            pct_l = dom_pct
            pct_r = 100 - pct_l
            dominant = pole_l
        elif active_pole == pole_r:
            if strength > 50:
                dom_pct = strength
            elif strength > 0:
                dom_pct = min(98, 50 + max(2, round(strength * 0.5)))
            else:
                dom_pct = 54
            pct_r = dom_pct
            pct_l = 100 - pct_r
            dominant = pole_r
        else:
            pct_l = 50
            pct_r = 50
            dominant = ""

        pole_scores[pole_l] = pct_l
        pole_scores[pole_r] = pct_r

        bars.append(
            {
                "dim_name": dim_name,
                "pole_l": pole_l,
                "label_l": label_l,
                "pct_l": pct_l,
                "pole_r": pole_r,
                "label_r": label_r,
                "pct_r": pct_r,
                "dominant": dominant,
                "strength": strength,
                "is_neutral": not bool(dominant),
            }
        )

    # ── SVG 雷达图计算（260x260）──
    cx, cy = 130, 130
    radius = 86
    svg_points = []
    svg_dots = []
    svg_labels = []

    # 8 轴坐标
    for item in RADAR_POLES:
        pole = item["pole"]
        score = pole_scores.get(pole, 50)
        # 归一化得分 (30~100 映射到半径比例，避免缩太小失真)
        r_ratio = max(0.25, min(1.0, score / 100.0))
        r_val = radius * r_ratio
        rad = math.radians(item["angle"])

        px = cx + r_val * math.cos(rad)
        py = cy + r_val * math.sin(rad)
        svg_points.append(f"{px:.1f},{py:.1f}")

        is_active = any(b["dominant"] == pole for b in bars)
        svg_dots.append({"x": f"{px:.1f}", "y": f"{py:.1f}", "active": is_active})

        # 顶点文本外延位置
        label_r = radius + 22
        lx = cx + label_r * math.cos(rad)
        ly = cy + label_r * math.sin(rad)
        svg_labels.append(
            {
                "x": f"{lx:.1f}",
                "y": f"{ly:.1f}",
                "text": item["label"],
                "active": is_active,
            }
        )

    # 同心多边形环 (25%, 50%, 75%, 100%)
    rings = []
    for step in (0.25, 0.5, 0.75, 1.0):
        ring_pts = []
        r_step = radius * step
        for item in RADAR_POLES:
            rad = math.radians(item["angle"])
            rx = cx + r_step * math.cos(rad)
            ry = cy + r_step * math.sin(rad)
            ring_pts.append(f"{rx:.1f},{ry:.1f}")
        rings.append(" ".join(ring_pts))

    # 轴线连接
    spokes = []
    for item in RADAR_POLES:
        rad = math.radians(item["angle"])
        ox = cx + radius * math.cos(rad)
        oy = cy + radius * math.sin(rad)
        spokes.append({"x1": cx, "y1": cy, "x2": f"{ox:.1f}", "y2": f"{oy:.1f}"})

    radar_svg = {
        "width": 260,
        "height": 260,
        "rings": rings,
        "spokes": spokes,
        "polygon_points": " ".join(svg_points),
        "dots": svg_dots,
        "labels": svg_labels,
    }

    return bars, pole_scores, radar_svg


def prepare_mbti_render_data(report: dict, user_name: str = "") -> dict[str, Any]:
    """把报告字典处理为适合海报模板渲染的数据集。

    注：完全不包含具体证据内容（不提取 dim['evidence']）。
    """
    raw_type = str(report.get("type") or "????").strip()
    candidates = resolve_possible_personalities(raw_type)
    primary = candidates[0] if candidates else UNKNOWN_PROFILE

    confidence = report.get("confidence", 0)
    sample_count = report.get("sample_count", 0)
    used_count = report.get("used_count", 0)

    dimensions = report.get("dimensions") or []
    bars, pole_scores, radar_svg = calculate_dimension_view(dimensions)

    has_uncertainty = "?" in raw_type or len(candidates) > 1

    return {
        "type": raw_type,
        "primary": primary,
        "candidates": candidates,
        "has_uncertainty": has_uncertainty,
        "confidence": confidence,
        "sample_count": sample_count,
        "used_count": used_count,
        "user_name": user_name or "会话成员",
        "bars": bars,
        "pole_scores": pole_scores,
        "radar": radar_svg,
        "disclaimer": report.get("disclaimer")
        or "⚠️ 本测评由长时记忆向量分析生成，仅供娱乐与自我探索参考。",
    }


# 紧凑型现代深色卡片 HTML 模板
MBTI_COMPACT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  * {
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }
  html, body {
    width: 100%;
    margin: 0;
    padding: 0;
    background-color: #0b0f19;
    color: #e2e8f0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .card {
    width: 100%;
    box-sizing: border-box;
    background: linear-gradient(160deg, rgba(26, 32, 48, 0.98) 0%, rgba(15, 20, 31, 0.99) 100%);
    padding: 28px 36px;
    position: relative;
    overflow: hidden;
  }
  .glow-decor {
    position: absolute;
    top: -60px;
    right: -60px;
    width: 220px;
    height: 220px;
    background: radial-gradient(circle, {{ primary.color }}33 0%, rgba(0,0,0,0) 70%);
    border-radius: 50%;
    pointer-events: none;
    z-index: 0;
  }
  .header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 18px;
    position: relative;
    z-index: 1;
  }
  .brand-tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.12);
    padding: 4px 10px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 500;
    color: #94a3b8;
    letter-spacing: 0.5px;
  }
  .brand-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background-color: {{ primary.color }};
    box-shadow: 0 0 8px {{ primary.color }};
  }
  .meta-stats {
    font-size: 12px;
    color: #64748b;
  }
  .meta-stats span {
    color: #cbd5e1;
    font-weight: 600;
  }
  .hero-block {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 14px;
    padding: 16px 20px;
    margin-bottom: 20px;
    position: relative;
    z-index: 1;
  }
  .hero-main {
    flex: 1;
  }
  .type-row {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 6px;
  }
  .type-code {
    font-size: 32px;
    font-weight: 800;
    letter-spacing: 1px;
    background: {{ primary.gradient }};
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .type-name {
    font-size: 20px;
    font-weight: 600;
    color: #f1f5f9;
  }
  .temperament-badge {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 6px;
    background: {{ primary.color }}22;
    color: {{ primary.color }};
    border: 1px solid {{ primary.color }}44;
    font-weight: 500;
  }
  .tagline {
    font-size: 13px;
    color: #94a3b8;
    line-height: 1.4;
  }
  .confidence-pill {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 2px;
  }
  .conf-num {
    font-size: 22px;
    font-weight: 700;
    color: #38bdf8;
  }
  .conf-label {
    font-size: 11px;
    color: #64748b;
  }

  /* 维度图谱双列并排 */
  .section-title {
    font-size: 13px;
    font-weight: 600;
    color: #94a3b8;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .section-title::before {
    content: "";
    width: 3px;
    height: 12px;
    background: {{ primary.color }};
    border-radius: 2px;
  }
  .spectrum-grid {
    display: flex;
    gap: 16px;
    align-items: center;
    background: rgba(255, 255, 255, 0.02);
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 14px;
    padding: 14px;
    margin-bottom: 20px;
  }
  .radar-col {
    width: 260px;
    height: 260px;
    display: flex;
    justify-content: center;
    align-items: center;
    position: relative;
    flex-shrink: 0;
  }
  .radar-svg {
    overflow: visible;
  }
  .bars-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .bar-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .bar-header {
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    font-weight: 500;
  }
  .bar-pole-left {
    color: #94a3b8;
  }
  .bar-pole-left.active {
    color: #38bdf8;
    font-weight: 600;
  }
  .bar-pole-right {
    color: #94a3b8;
  }
  .bar-pole-right.active {
    color: #a855f7;
    font-weight: 600;
  }
  .bar-track {
    height: 7px;
    background: rgba(255, 255, 255, 0.07);
    border-radius: 4px;
    display: flex;
    position: relative;
    overflow: hidden;
  }
  .bar-fill-left {
    height: 100%;
    background: linear-gradient(90deg, #0ea5e9, #38bdf8);
    transition: width 0.3s;
  }
  .bar-fill-right {
    height: 100%;
    background: linear-gradient(90deg, #a855f7, #c084fc);
    transition: width 0.3s;
  }
  .bar-center-pin {
    position: absolute;
    left: 50%;
    top: 0;
    bottom: 0;
    width: 1px;
    background: rgba(255, 255, 255, 0.2);
    z-index: 2;
  }

  /* 极简人格速描 */
  .profile-section {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 14px;
    padding: 16px 20px;
    margin-bottom: 16px;
  }
  .uncertainty-tip {
    font-size: 12px;
    color: #38bdf8;
    margin-bottom: 10px;
  }
  .tags-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 10px;
  }
  .tag-pill {
    background: rgba(255, 255, 255, 0.05);
    border: 1px solid rgba(255, 255, 255, 0.1);
    padding: 3px 10px;
    border-radius: 6px;
    font-size: 12px;
    color: #cbd5e1;
  }
  .profile-desc {
    font-size: 13px;
    color: #cbd5e1;
    line-height: 1.6;
  }

  /* 候选人格列表（当包含 ? 时） */
  .candidates-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-top: 10px;
  }
  .candidate-card {
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid rgba(255, 255, 255, 0.07);
    border-radius: 10px;
    padding: 10px 12px;
  }
  .cand-top {
    display: flex;
    align-items: baseline;
    gap: 6px;
    margin-bottom: 4px;
  }
  .cand-code {
    font-size: 14px;
    font-weight: 700;
    color: #f1f5f9;
  }
  .cand-name {
    font-size: 12px;
    color: #94a3b8;
  }
  .cand-desc {
    font-size: 11px;
    color: #94a3b8;
    line-height: 1.4;
  }

  .footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 11px;
    color: #475569;
    padding-top: 4px;
  }
</style>
</head>
<body>
<div class="card">
  <div class="glow-decor"></div>

  <!-- 头部信息 -->
  <div class="header">
    <div class="brand-tag">
      <div class="brand-dot"></div>
      <span>ASTRBOT MEMORY · 记忆画像测评</span>
    </div>
    <div class="meta-stats">
      有效记忆 <span>{{ used_count }}</span> 条 (共 {{ sample_count }} 条)
    </div>
  </div>

  <!-- 人格定性 Hero -->
  <div class="hero-block">
    <div class="hero-main">
      <div class="type-row">
        <div class="type-code">{{ type }}</div>
        <div class="type-name">{{ primary.name }}</div>
        <div class="temperament-badge">{{ primary.temperament }}</div>
      </div>
      <div class="tagline">“{{ primary.tagline }}”</div>
    </div>
    <div class="confidence-pill">
      <div class="conf-num">{{ confidence }}%</div>
      <div class="conf-label">分析置信度</div>
    </div>
  </div>

  <!-- 维度图谱（雷达图 + 4 轴滑块并排） -->
  <div class="section-title">四维倾向图谱</div>
  <div class="spectrum-grid">
    <!-- 左：SVG 雷达图 -->
    <div class="radar-col">
      <svg class="radar-svg" width="260" height="260" viewBox="0 0 260 260">
        <!-- 同心环背景 -->
        {% for ring in radar.rings %}
        <polygon points="{{ ring }}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>
        {% endfor %}
        <!-- 轴线 -->
        {% for spoke in radar.spokes %}
        <line x1="{{ spoke.x1 }}" y1="{{ spoke.y1 }}" x2="{{ spoke.x2 }}" y2="{{ spoke.y2 }}" stroke="rgba(255,255,255,0.08)" stroke-width="1"/>
        {% endfor %}
        <!-- 数据多边形 -->
        <polygon points="{{ radar.polygon_points }}" fill="{{ primary.color }}33" stroke="{{ primary.color }}" stroke-width="2"/>
        <!-- 顶点点缀 -->
        {% for dot in radar.dots %}
        <circle cx="{{ dot.x }}" cy="{{ dot.y }}" r="3" fill="{{ primary.color }}" {% if dot.active %}stroke="#ffffff" stroke-width="1.5"{% endif %}/>
        {% endfor %}
        <!-- 极性标签 -->
        {% for label in radar.labels %}
        <text x="{{ label.x }}" y="{{ label.y }}" font-size="10" text-anchor="middle" dominant-baseline="central" fill="{% if label.active %}#f1f5f9{% else %}#64748b{% endif %}" font-weight="{% if label.active %}600{% else %}400{% endif %}">{{ label.text }}</text>
        {% endfor %}
      </svg>
    </div>

    <!-- 右：4 轴平衡滑块 -->
    <div class="bars-col">
      {% for bar in bars %}
      <div class="bar-item">
        <div class="bar-header">
          <span class="bar-pole-left {% if bar.dominant == bar.pole_l %}active{% endif %}">
            {{ bar.label_l }} {{ bar.pole_l }} ({{ bar.pct_l }}%)
          </span>
          <span class="bar-pole-right {% if bar.dominant == bar.pole_r %}active{% endif %}">
            ({{ bar.pct_r }}%) {{ bar.pole_r }} {{ bar.label_r }}
          </span>
        </div>
        <div class="bar-track">
          <div class="bar-center-pin"></div>
          <div class="bar-fill-left" style="width: {{ bar.pct_l }}%;"></div>
          <div class="bar-fill-right" style="width: {{ bar.pct_r }}%;"></div>
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <!-- 人格速描（不冗长，短小精炼） -->
  <div class="section-title">人格速描</div>
  <div class="profile-section">
    {% if has_uncertainty and candidates|length > 1 %}
    <div class="uncertainty-tip">✦ 记忆在该维度上较为均衡，可能兼具以下性格倾向：</div>
    <div class="candidates-grid">
      {% for cand in candidates %}
      <div class="candidate-card">
        <div class="cand-top">
          <span class="cand-code">{{ cand.code }}</span>
          <span class="cand-name">{{ cand.name }}</span>
        </div>
        <div class="cand-desc">{{ cand.tagline }}</div>
      </div>
      {% endfor %}
    </div>
    {% else %}
    <div class="tags-list">
      {% for tag in primary.tags %}
      <div class="tag-pill">{{ tag }}</div>
      {% endfor %}
    </div>
    <div class="profile-desc">
      {{ primary.desc }}
    </div>
    {% endif %}
  </div>

  <!-- 页脚声明 -->
  <div class="footer">
    <span>{{ disclaimer }}</span>
    <span>Isolated Memory</span>
  </div>
</div>
</body>
</html>
"""


def render_mbti_html(report: dict, user_name: str = "") -> str:
    """把报告字典渲染为精美的 HTML 文本。

    Args:
        report: 报告字典。
        user_name: 成员昵称。

    Returns:
        str: 渲染后的 HTML 源码。
    """
    data = prepare_mbti_render_data(report, user_name=user_name)
    template = jinja2.Template(MBTI_COMPACT_HTML_TEMPLATE)
    return template.render(**data)


# ── Pillow 原生海报渲染引擎 ────────────────────────────────────────────────────────

_FONT_PATHS_RESOLVED: tuple[str | None, str | None] | None = None
_FONT_CACHE: dict[tuple[int, bool], Any] = {}


def _find_font_paths() -> tuple[str | None, str | None]:
    """探测跨平台的中文字体路径 (bold_path, regular_path)。"""
    global _FONT_PATHS_RESOLVED
    if _FONT_PATHS_RESOLVED is not None:
        return _FONT_PATHS_RESOLVED

    candidates = [
        # Windows
        ("C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"),
        ("C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simhei.ttf"),
        ("C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/simsun.ttc"),
        # Linux (Debian / Ubuntu / CentOS / Docker)
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ),
        (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        ),
        (
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ),
        (
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        ),
        # macOS
        (
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ),
        (
            "/Library/Fonts/Arial Unicode.ttf",
            "/Library/Fonts/Arial Unicode.ttf",
        ),
    ]

    for bold_p, reg_p in candidates:
        if os.path.exists(reg_p):
            _FONT_PATHS_RESOLVED = (bold_p if os.path.exists(bold_p) else reg_p, reg_p)
            return _FONT_PATHS_RESOLVED

    _FONT_PATHS_RESOLVED = (None, None)
    return _FONT_PATHS_RESOLVED


def _get_font(size: int, bold: bool = False):
    """获取缓存的 FreeType 字体对象或系统默认字体。"""
    cache_key = (size, bold)
    if cache_key in _FONT_CACHE:
        return _FONT_CACHE[cache_key]

    if ImageFont is None:
        return None

    bold_file, reg_file = _find_font_paths()
    font_file = bold_file if bold else reg_file
    if font_file:
        try:
            f = ImageFont.truetype(font_file, size)
            _FONT_CACHE[cache_key] = f
            return f
        except Exception:
            pass

    f = ImageFont.load_default()
    _FONT_CACHE[cache_key] = f
    return f


def _hex_to_rgb(hex_str: str, alpha: int = 255) -> tuple:
    """把 Hex 颜色代码转为 RGBA 元组。"""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) == 6:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return (r, g, b, alpha) if alpha < 255 else (r, g, b)
    return (99, 102, 241, alpha) if alpha < 255 else (99, 102, 241)


def _wrap_text(
    text: str, font: Any, max_width: int, draw: Any
) -> list[str]:
    """字符级自动换行算法（完美支持中英混排）。"""
    lines = []
    current_line = ""
    for char in text:
        test_line = current_line + char
        bbox = draw.textbbox((0, 0), test_line, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = char
    if current_line:
        lines.append(current_line)
    return lines


def render_mbti_poster_pillow(
    data_or_report: dict[str, Any],
    user_name: str = "",
    output_path: str | None = None,
) -> str:
    """使用 Pillow 原生渲染精致现代的 MBTI 测评卡片海报。

    特点：
    - 纯 Python 原生绘图，0 浏览器/Playwright 依赖，秒级极速渲染；
    - 严格遵循用户隐私：绝不包含任何具体交互记忆引用（evidence）；
    - 1:1 原生直接渲染（无超采样缩放开销），资源占用低且清晰；
    - 紧凑单屏尺寸设计（760x700），完美契合移动端与桌面端屏幕。

    Args:
        data_or_report: 原始测评报告字典，或经 prepare_mbti_render_data 处理后的数据集。
        user_name: 用户名/昵称。
        output_path: 可选的输出图片文件路径。若未提供，则自动写入安全系统临时目录。

    Returns:
        str: 生成的 PNG 图片绝对路径。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        raise RuntimeError(
            "缺少 Pillow 库，请使用 pip install Pillow 安装后再使用图片渲染功能"
        )

    # 规范化渲染数据集
    if "bars" in data_or_report and "primary" in data_or_report:
        data = data_or_report
    else:
        data = prepare_mbti_render_data(data_or_report, user_name=user_name)

    WIDTH = 760
    HEIGHT = 700

    # 基础暗黑科技背景底板
    img = Image.new("RGBA", (WIDTH, HEIGHT), (11, 15, 25, 255))
    draw = ImageDraw.Draw(img)

    # 1. 外层圆角卡片容器
    card_margin = 24
    draw.rounded_rectangle(
        (card_margin, card_margin, WIDTH - card_margin, HEIGHT - card_margin),
        radius=20,
        fill=(17, 24, 39, 255),
        outline=(30, 41, 59, 255),
        width=2,
    )

    primary = data.get("primary") or UNKNOWN_PROFILE
    theme_color_hex = primary.get("color", "#8b5cf6")
    theme_rgb = _hex_to_rgb(theme_color_hex)

    # 2. 头部区域 (Header)
    header_x = card_margin + 28
    header_y = card_margin + 26

    # 品牌标识微章 (Logo Badge)
    draw.rounded_rectangle(
        (header_x, header_y + 4, header_x + 32, header_y + 36),
        radius=8,
        fill=theme_rgb,
    )
    font_logo = _get_font(18, bold=True)
    m_box = draw.textbbox((0, 0), "M", font=font_logo)
    mw = m_box[2] - m_box[0]
    mh = m_box[3] - m_box[1]
    draw.text(
        (
            header_x + (32 - mw) // 2,
            header_y + 4 + (32 - mh) // 2 - 2,
        ),
        "M",
        fill=(255, 255, 255),
        font=font_logo,
    )

    # 标题与副标题
    font_title = _get_font(24, bold=True)
    draw.text(
        (header_x + 44, header_y + 4),
        "MBTI 记忆性格测评",
        fill=(248, 250, 252),
        font=font_title,
    )

    font_sub = _get_font(12, bold=False)
    draw.text(
        (header_x + 46, header_y + 38),
        "基于专属交互记忆分析 · 娱乐向",
        fill=(148, 163, 184),
        font=font_sub,
    )

    # 右上角元信息微章 (Badges)
    badge_y = header_y + 8
    current_right = WIDTH - card_margin - 28
    font_badge = _get_font(12, bold=False)

    # 置信度徽章
    conf_val = data.get("confidence", 0)
    conf_label = (
        f"置信度: {conf_val}%"
        if isinstance(conf_val, (int, float)) and conf_val > 0
        else "置信度: 中"
    )
    bbox = draw.textbbox((0, 0), conf_label, font=font_badge)
    bw = bbox[2] - bbox[0] + 20
    bh = 26
    bx = current_right - bw
    draw.rounded_rectangle(
        (bx, badge_y, current_right, badge_y + bh),
        radius=6,
        fill=(30, 41, 59, 255),
        outline=(51, 65, 85, 255),
        width=1,
    )
    draw.text(
        (bx + 10, badge_y + 5),
        conf_label,
        fill=(203, 213, 225),
        font=font_badge,
    )
    current_right = bx - 10

    # 用户名徽章
    target_user = str(data.get("user_name") or "").strip()
    if target_user and target_user != "会话成员":
        u_text = f"用户: {target_user[:12]}"
        bbox = draw.textbbox((0, 0), u_text, font=font_badge)
        bw = bbox[2] - bbox[0] + 20
        bx = current_right - bw
        draw.rounded_rectangle(
            (bx, badge_y, current_right, badge_y + bh),
            radius=6,
            fill=(30, 41, 59, 255),
            outline=(51, 65, 85, 255),
            width=1,
        )
        draw.text(
            (bx + 10, badge_y + 5),
            u_text,
            fill=(203, 213, 225),
            font=font_badge,
        )

    # 顶部分隔线
    div_y = header_y + 68
    draw.line(
        (card_margin + 20, div_y, WIDTH - card_margin - 20, div_y),
        fill=(30, 41, 59, 255),
        width=1,
    )

    # 3. 主体内容：左右双栏布局 (Left: Radar, Right: Dimensions & Profile)
    content_top = div_y + 20
    content_bottom = HEIGHT - card_margin - 46

    col_gap = 24
    left_col_w = 320
    left_x1 = card_margin + 24
    left_x2 = left_x1 + left_col_w
    right_x1 = left_x2 + col_gap
    right_x2 = WIDTH - card_margin - 24

    # ── 左栏：八维能量分布雷达图 ──
    font_section = _get_font(15, bold=True)
    draw.text(
        (left_x1 + 6, content_top),
        "八维能量分布",
        fill=(226, 232, 240),
        font=font_section,
    )

    radar_cx = left_x1 + left_col_w // 2
    radar_cy = content_top + 210
    radar_radius = 110

    # 绘制 4 圈同心八边形环
    pole_angles = [-90, -45, 0, 45, 90, 135, 180, 225]
    for step in (0.25, 0.5, 0.75, 1.0):
        r_step = radar_radius * step
        oct_pts = []
        for ang in pole_angles:
            rad = math.radians(ang)
            oct_pts.append(
                (
                    radar_cx + r_step * math.cos(rad),
                    radar_cy + r_step * math.sin(rad),
                )
            )
        draw.polygon(oct_pts, outline=(36, 48, 71, 255), width=1)

    # 绘制 8 条辐射中轴线
    for ang in pole_angles:
        rad = math.radians(ang)
        draw.line(
            (
                radar_cx,
                radar_cy,
                radar_cx + radar_radius * math.cos(rad),
                radar_cy + radar_radius * math.sin(rad),
            ),
            fill=(36, 48, 71, 255),
            width=1,
        )

    # 计算 8 轴雷达多边形点集
    radar_poly_pts = []
    radar_poles_def = [
        ("E", -90, "E 外向"),
        ("S", -45, "S 实感"),
        ("T", 0, "T 思考"),
        ("J", 45, "J 判断"),
        ("I", 90, "I 内向"),
        ("N", 135, "N 直觉"),
        ("F", 180, "F 情感"),
        ("P", 225, "P 知觉"),
    ]
    pole_scores = data.get("pole_scores") or {}
    font_radar_lbl = _get_font(11, bold=False)
    font_radar_lbl_b = _get_font(11, bold=True)

    active_poles = {
        b.get("dominant") for b in data.get("bars", []) if b.get("dominant")
    }

    for code, ang, label in radar_poles_def:
        raw_score = float(pole_scores.get(code, 50))
        mapped_ratio = max(
            0.25, min(0.95, raw_score / 100.0 if raw_score > 1 else raw_score)
        )
        rad = math.radians(ang)
        px = radar_cx + radar_radius * mapped_ratio * math.cos(rad)
        py = radar_cy + radar_radius * mapped_ratio * math.sin(rad)
        radar_poly_pts.append((px, py))

        # 轴端极性标签位置
        lbl_r = radar_radius + 26
        lx = radar_cx + lbl_r * math.cos(rad)
        ly = radar_cy + lbl_r * math.sin(rad)

        is_active_pole = code in active_poles
        f_lbl = font_radar_lbl_b if is_active_pole else font_radar_lbl
        c_lbl = (248, 250, 252) if is_active_pole else (148, 163, 184)

        bbox = draw.textbbox((0, 0), label, font=f_lbl)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        draw.text(
            (lx - lw // 2, ly - lh // 2), label, fill=c_lbl, font=f_lbl
        )

    # 半透明填充多边形遮罩
    poly_overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    poly_draw = ImageDraw.Draw(poly_overlay)
    theme_fill = (theme_rgb[0], theme_rgb[1], theme_rgb[2], 85)
    poly_draw.polygon(
        radar_poly_pts,
        fill=theme_fill,
        outline=(theme_rgb[0], theme_rgb[1], theme_rgb[2], 230),
        width=2,
    )
    # 多边形顶点发光微圆
    for px, py in radar_poly_pts:
        dot_r = 4
        poly_draw.ellipse(
            (px - dot_r, py - dot_r, px + dot_r, py + dot_r),
            fill=(255, 255, 255, 255),
            outline=theme_rgb,
            width=1,
        )

    img = Image.alpha_composite(img, poly_overlay)
    draw = ImageDraw.Draw(img)

    # ── 右栏：四维倾向对比平衡条 ──
    draw.text(
        (right_x1, content_top),
        "四维倾向对比",
        fill=(226, 232, 240),
        font=font_section,
    )

    dim_bars = data.get("bars", [])
    bar_y_start = content_top + 34
    bar_row_h = 42

    font_dim_active = _get_font(12, bold=True)
    font_dim_inactive = _get_font(12, bold=False)

    for i, bar in enumerate(dim_bars):
        curr_y = bar_y_start + i * bar_row_h
        left_label = f"{bar['pole_l']} {bar['label_l']}  {bar['pct_l']}%"
        right_label = f"{bar['pct_r']}%  {bar['label_r']} {bar['pole_r']}"

        l_active = bar.get("dominant") == bar["pole_l"]
        r_active = bar.get("dominant") == bar["pole_r"]
        l_color = (248, 250, 252) if l_active else (100, 116, 139)
        r_color = (248, 250, 252) if r_active else (100, 116, 139)
        l_font = font_dim_active if l_active else font_dim_inactive
        r_font = font_dim_active if r_active else font_dim_inactive

        draw.text((right_x1, curr_y), left_label, fill=l_color, font=l_font)
        bbox_r = draw.textbbox((0, 0), right_label, font=r_font)
        rw = bbox_r[2] - bbox_r[0]
        draw.text((right_x2 - rw, curr_y), right_label, fill=r_color, font=r_font)

        # 进度底槽
        track_y = curr_y + 20
        track_h = 7
        track_w = right_x2 - right_x1
        draw.rounded_rectangle(
            (right_x1, track_y, right_x2, track_y + track_h),
            radius=4,
            fill=(26, 35, 51, 255),
        )

        # 优势侧高亮填充
        if l_active:
            fill_w = int(track_w * (bar["pct_l"] / 100.0))
            draw.rounded_rectangle(
                (right_x1, track_y, right_x1 + fill_w, track_y + track_h),
                radius=4,
                fill=theme_rgb,
            )
        elif r_active:
            fill_w = int(track_w * (bar["pct_r"] / 100.0))
            draw.rounded_rectangle(
                (right_x2 - fill_w, track_y, right_x2, track_y + track_h),
                radius=4,
                fill=theme_rgb,
            )
        else:
            mid_w = track_w // 2
            draw.rounded_rectangle(
                (right_x1, track_y, right_x1 + mid_w, track_y + track_h),
                radius=4,
                fill=(71, 85, 105),
            )

    # ── 右栏下半部分：可能人格与核心解析 ──
    box_top = bar_y_start + 4 * bar_row_h + 16
    box_bottom = content_bottom

    draw.rounded_rectangle(
        (right_x1, box_top, right_x2, box_bottom),
        radius=14,
        fill=(22, 31, 49, 255),
        outline=(36, 50, 76, 255),
        width=1,
    )

    candidates = data.get("candidates") or []
    box_pad = 18
    box_inner_x = right_x1 + box_pad
    box_inner_w = (right_x2 - right_x1) - 2 * box_pad

    if len(candidates) <= 1:
        # 单一明确人格排版
        profile = candidates[0] if candidates else primary
        p_y = box_top + box_pad

        # 人格代码
        font_code = _get_font(28, bold=True)
        draw.text((box_inner_x, p_y), profile["code"], fill=theme_rgb, font=font_code)
        bbox = draw.textbbox((0, 0), profile["code"], font=font_code)
        code_w = bbox[2] - bbox[0]

        # 称号
        font_name = _get_font(18, bold=True)
        name_x = box_inner_x + code_w + 12
        draw.text(
            (name_x, p_y + 8),
            profile["name"],
            fill=(248, 250, 252),
            font=font_name,
        )
        bbox_n = draw.textbbox((0, 0), profile["name"], font=font_name)
        name_w = bbox_n[2] - bbox_n[0]

        # 气质类型胶囊徽章
        font_temp = _get_font(11, bold=False)
        temp_text = profile.get("temperament", "")
        bbox_t = draw.textbbox((0, 0), temp_text, font=font_temp)
        tw = bbox_t[2] - bbox_t[0] + 14
        th = 20
        tx = name_x + name_w + 14
        ty = p_y + 9
        draw.rounded_rectangle(
            (tx, ty, tx + tw, ty + th),
            radius=5,
            fill=(30, 41, 59, 255),
            outline=(51, 65, 85, 255),
            width=1,
        )
        draw.text(
            (tx + 7, ty + 3),
            temp_text,
            fill=(199, 210, 254),
            font=font_temp,
        )

        # 特征标签药丸列表
        tags_y = p_y + 44
        font_tag = _get_font(11, bold=False)
        cur_tx = box_inner_x
        for tag in profile.get("tags", [])[:4]:
            t_box = draw.textbbox((0, 0), tag, font=font_tag)
            tag_w = t_box[2] - t_box[0] + 14
            tag_h = 20
            draw.rounded_rectangle(
                (cur_tx, tags_y, cur_tx + tag_w, tags_y + tag_h),
                radius=4,
                fill=(26, 36, 56, 255),
                outline=(41, 56, 84, 255),
                width=1,
            )
            draw.text(
                (cur_tx + 7, tags_y + 3),
                tag,
                fill=(148, 163, 184),
                font=font_tag,
            )
            cur_tx += tag_w + 8

        # 一句话高亮引言
        quote_y = tags_y + 30
        font_tagline = _get_font(12, bold=True)
        tagline_text = (
            f"“{profile.get('tagline', '')}”" if profile.get("tagline") else ""
        )
        if tagline_text:
            t_lines = _wrap_text(tagline_text, font_tagline, box_inner_w, draw)
            for line in t_lines[:2]:
                draw.text(
                    (box_inner_x, quote_y),
                    line,
                    fill=(226, 232, 240),
                    font=font_tagline,
                )
                quote_y += 18

        # 精简解析文本 (40~60字)
        desc_y = quote_y + 6
        font_desc = _get_font(11, bold=False)
        desc_text = profile.get("desc") or ""
        lines = _wrap_text(desc_text, font_desc, box_inner_w, draw)
        for line in lines[:3]:
            draw.text(
                (box_inner_x, desc_y),
                line,
                fill=(148, 163, 184),
                font=font_desc,
            )
            desc_y += 18

    else:
        # 多项可能候选人格并列排版
        p_y = box_top + 14
        font_cand_title = _get_font(14, bold=True)
        draw.text(
            (box_inner_x, p_y),
            f"可能人格候选 ({data.get('type', '????')})",
            fill=theme_rgb,
            font=font_cand_title,
        )

        cur_cand_y = p_y + 26
        font_c_code = _get_font(13, bold=True)
        font_c_desc = _get_font(11, bold=False)

        for cand in candidates[:2]:
            cand_box_h = 60
            draw.rounded_rectangle(
                (
                    box_inner_x,
                    cur_cand_y,
                    box_inner_x + box_inner_w,
                    cur_cand_y + cand_box_h,
                ),
                radius=8,
                fill=(26, 36, 56, 255),
                outline=(41, 56, 84, 255),
                width=1,
            )
            head_txt = f"{cand['code']} · {cand['name']}  ({cand.get('temperament', '')})"
            draw.text(
                (box_inner_x + 10, cur_cand_y + 8),
                head_txt,
                fill=(248, 250, 252),
                font=font_c_code,
            )
            c_desc = cand.get("tagline") or cand.get("desc") or ""
            c_lines = _wrap_text(c_desc, font_c_desc, box_inner_w - 20, draw)
            if c_lines:
                draw.text(
                    (box_inner_x + 10, cur_cand_y + 32),
                    c_lines[0],
                    fill=(148, 163, 184),
                    font=font_c_desc,
                )
            cur_cand_y += cand_box_h + 10

    # 4. 页脚声明
    font_footer = _get_font(11, bold=False)
    footer_text = "※ 仅供娱乐参考 · 依据全部本地交互记忆推断分析"
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
    fw = f_bbox[2] - f_bbox[0]
    draw.text(
        ((WIDTH - fw) // 2, HEIGHT - card_margin - 24),
        footer_text,
        fill=(71, 85, 105),
        font=font_footer,
    )

    # 5. 直接转为 RGB 格式保存，无超采样与缩放开销
    final_img = img.convert("RGB")

    if not output_path:
        tmp_file = tempfile.NamedTemporaryFile(
            prefix="mbti_card_", suffix=".png", delete=False
        )
        output_path = tmp_file.name
        tmp_file.close()

    final_img.save(output_path, "PNG", quality=95)
    return output_path


# 别名导出
render_mbti_image = render_mbti_poster_pillow

