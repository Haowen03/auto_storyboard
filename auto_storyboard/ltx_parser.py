"""
从 VLM 的 LTX Shot Plan 回复中解析逐 shot 参数（images / image_idxs / prompt 等）。
"""

from __future__ import annotations

import json
import ast
import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_FRAME_FILE_RE = re.compile(
    r"case_(?:final|edit|base)_frame\d+\.png", re.IGNORECASE
)


@dataclass
class LTXShotSpec:
    shot_id: str = ""
    image_files: List[str] = field(default_factory=list)
    image_idxs: List[float] = field(default_factory=list)
    image_strengths: List[float] = field(default_factory=list)
    video_seconds: float = 5.0
    prompt: str = ""
    negative_prompt: str = ""
    shot_function: str = ""
    transition_notes: str = ""
    shot_type: str = "main"  # main | bridge | micro_action_bridge
    is_bridge: bool = False
    bridge_subtype: str = ""  # transition | micro_action | physical_contact
    bridges_between_frames: List[str] = field(default_factory=list)
    semantic_stage_count: Optional[int] = None
    continuous_motion_chain: bool = False
    action_chain_group: str = ""
    risk_reason: str = ""
    stitch_mode: str = ""
    bridges_between: str = ""
    bridge_id: str = ""
    transition_carrier: str = ""
    can_skip_bridge: bool = True
    generated_by_default: bool = False
    raw_block: str = ""


class LTXResponseParser:
    BRIDGE_SHOT_TYPES = frozenset({
        "bridge", "micro_action_bridge", "micro-action-bridge",
    })
    HIGH_RISK_RELATION_TYPES = frozenset({
        "high_risk_pair", "contact", "boarding", "grasping", "grasp",
        "landing", "impact", "lift_off", "pickup", "physical_contact",
    })
    HIGH_RISK_TEXT_RE = re.compile(
        r"\b(?:"
        r"foot\s+(?:steps?|lands?|places?)\s+(?:onto|on)|"
        r"steps?\s+onto|stepping\s+onto|"
        r"hand\s+(?:touches?|grasps?|grips?)|"
        r"grasp(?:ing|s)?|"
        r"boarding|"
        r"lift[- ]?off|lifts?\s+off|"
        r"lands?\s+on|landing|"
        r"physical\s+contact|"
        r"foot[- ]?to[- ]?sword|"
        r"picks?\s+up|picked\s+up"
        r")\b|"
        r"(?:脚踩|踏上|触碰|握住|踩上|离地|起飞|落地|接触)",
        re.IGNORECASE,
    )
    MOTION_FIRST_BOILERPLATE_RE = re.compile(
        r"The sequence stays within the story shown by the reference frames.{80,}",
        re.IGNORECASE | re.DOTALL,
    )
    FRAME_INDEX_RE = re.compile(r"frame\s*0*(\d+)", re.IGNORECASE)

    @staticmethod
    def is_bridge_shot(shot: Dict[str, Any]) -> bool:
        if bool(shot.get("is_bridge")):
            return True
        st = (shot.get("shot_type") or "").lower().replace("-", "_")
        return st in LTXResponseParser.BRIDGE_SHOT_TYPES or st.startswith(
            "micro_action"
        )

    @staticmethod
    def frame_index_from_label(label: str) -> int:
        if not label:
            return 0
        m = LTXResponseParser.FRAME_INDEX_RE.search(label)
        if m:
            return int(m.group(1))
        m = re.search(r"case_(?:final|edit|base)_frame(\d+)", label, re.I)
        return int(m.group(1)) if m else 0

    @staticmethod
    def normalize_shot_type_fields(shot: Dict[str, Any]) -> None:
        """统一 bridge / micro_action_bridge 标记与 bridge_subtype。"""
        raw = (shot.get("shot_type") or "main").lower().replace("-", "_")
        subtype = (shot.get("bridge_subtype") or "").lower().replace("-", "_")
        if raw in ("micro_action_bridge",) or subtype in (
            "micro_action", "physical_contact", "micro_action_bridge",
        ):
            shot["shot_type"] = "micro_action_bridge"
            shot["is_bridge"] = True
            if not subtype:
                shot["bridge_subtype"] = "physical_contact"
        elif raw == "bridge" or shot.get("is_bridge"):
            shot["shot_type"] = "bridge"
            shot["is_bridge"] = True
            shot.setdefault("bridge_subtype", subtype or "transition")
        else:
            shot["shot_type"] = "main"
            shot["is_bridge"] = False

    @staticmethod
    def normalize_stitch_mode(value: str) -> str:
        """将 VLM/文档别名统一为 direct_concat | trim_overlap。"""
        v = (value or "").strip().lower().replace("-", "_")
        if v in ("direct", "direct_concat", "concat", "directconcat"):
            return "direct_concat"
        if v in ("trim", "trim_overlap", "trimoverlap", "overlap", "trim_and_overlap"):
            return "trim_overlap"
        return v

    @staticmethod
    def infer_shot_type(
        *,
        shot_label: str = "",
        shot_function: str = "",
        is_bridge: bool = False,
        shot_type: str = "",
    ) -> str:
        if is_bridge or (shot_type or "").lower().replace("-", "_") in (
            "bridge", "micro_action_bridge",
        ):
            return "bridge"
        label = (shot_label or "").lower()
        func = (shot_function or "").lower()
        if re.search(r"\bmicro[- ]?action\b", label) or "micro" in func:
            return "bridge"
        if re.search(r"\bbridge\b", label) or "桥接" in label or "bridge" in func:
            return "bridge"
        if "跨 shot" in func or "跨段转场" in func or "转场桥" in func:
            return "bridge"
        return "main"

    @staticmethod
    def warn_bridge_shot_prompt(prompt: str, shot_id: str = "") -> None:
        if not prompt:
            return
        carrier_markers = (
            r"\b(?:wipe|light[- ]?wipe|motion[- ]?wipe|match\s+cut|flare|sweep|"
            r"obscur|motion[- ]?bridge|carry[- ]?over|whoosh|hum|trail|"
            r"transition into|bridges the|fills the lens|sweeps across)\b"
        )
        if not re.search(carrier_markers, prompt, re.IGNORECASE):
            weak = re.search(
                r"\b(?:transitions?\s+(?:from|to|into)|cuts?\s+to|"
                r"the\s+scene\s+transitions?)\b",
                prompt,
                re.IGNORECASE,
            )
            if weak or len(prompt.split()) < 20:
                logger.warning(
                    "%s bridge shot prompt 可能缺少明确转场载体（v1.5 §7.5 / §15），"
                    "与直接拼接无本质区别",
                    shot_id or "bridge",
                )

    @staticmethod
    def infer_transition_carrier(
        prompt: str,
        transition_notes: str = "",
    ) -> str:
        """从 prompt / Transition Notes 推断转场载体类型标签。"""
        text = f"{prompt} {transition_notes}".lower()
        rules = (
            (r"light[- ]?wipe|flare|glow.*lens|golden flare", "light wipe"),
            (r"motion[- ]?wipe|sweep|ribbon|obscur|wipe", "motion wipe"),
            (r"follows the|trajectory|beam|trail|sword|energy", "trajectory bridge"),
            (r"camera push|camera pull|dolly|tracking|tilt", "camera bridge"),
            (r"foot|hand|step|turn|rise|lift|land", "action bridge"),
            (r"hum|whoosh|audio|sound|thunder|chime", "audio bridge"),
        )
        for pattern, label in rules:
            if re.search(pattern, text):
                return label
        return ""

    @staticmethod
    def warn_boundary_idxs_for_stitch_mode(
        idxs: List[float],
        n_images: int,
        stitch_mode: str,
        shot_type: str,
        shot_id: str = "",
        *,
        has_prev: bool = False,
        has_next: bool = False,
    ) -> None:
        if n_images <= 0 or not idxs:
            return
        mode = LTXResponseParser.normalize_stitch_mode(stitch_mode)
        if mode not in ("direct_concat", "trim_overlap"):
            return
        first, last = float(idxs[0]), float(idxs[-1])
        tag = shot_id or shot_type

        if mode == "direct_concat":
            if shot_type == "bridge":
                if first > 0.12:
                    logger.warning(
                        "%s direct_concat：bridge 首锚点 %.2f 偏晚，建议 0.00–0.08（§13.1.3）",
                        tag, first,
                    )
                if last < 0.82:
                    logger.warning(
                        "%s direct_concat：bridge 尾锚点 %.2f 偏早，建议 0.85–0.95",
                        tag, last,
                    )
            else:
                if has_prev and first > 0.12:
                    logger.warning(
                        "%s direct_concat：主 shot 开场锚点 %.2f 偏晚，接 bridge 时建议 0.00–0.08",
                        tag, first,
                    )
                if has_next and last < 0.85:
                    logger.warning(
                        "%s direct_concat：主 shot 尾锚点 %.2f 偏早，接 bridge 时建议 0.88–0.95",
                        tag, last,
                    )
        elif mode == "trim_overlap":
            if shot_type != "bridge" and has_next and last > 0.85:
                logger.warning(
                    "%s trim_overlap：主 shot 尾锚点 %.2f 偏靠后，建议 0.70–0.82 留剪辑余量",
                    tag, last,
                )

    # ── v1.6 Shot Capacity / Reference Frame Density ─────────────────

    @staticmethod
    def reference_frame_density(n_frames: int, video_seconds: float) -> float:
        if video_seconds <= 0 or n_frames <= 0:
            return 0.0
        return round(n_frames / float(video_seconds), 3)

    @staticmethod
    def shot_capacity_limits(
        video_seconds: float,
        *,
        is_bridge: bool = False,
    ) -> Dict[str, int]:
        """返回推荐/可接受参考帧数量区间（文档 §2.1）。"""
        secs = max(0.0, float(video_seconds))
        if is_bridge:
            return {
                "recommended_min": 2,
                "recommended_max": 2,
                "acceptable_max": 3,
                "hard_reject_above": 3,
            }
        if secs <= 6:
            return {
                "recommended_min": 1,
                "recommended_max": 2,
                "acceptable_max": 2,
                "hard_reject_above": 3,
            }
        if secs <= 9:
            return {
                "recommended_min": 2,
                "recommended_max": 3,
                "acceptable_max": 3,
                "hard_reject_above": 4,
            }
        if secs <= 12:
            return {
                "recommended_min": 3,
                "recommended_max": 4,
                "acceptable_max": 5,
                "hard_reject_above": 6,
            }
        if secs <= 15:
            return {
                "recommended_min": 4,
                "recommended_max": 5,
                "acceptable_max": 6,
                "hard_reject_above": 7,
            }
        if secs <= 20:
            return {
                "recommended_min": 5,
                "recommended_max": 6,
                "acceptable_max": 7,
                "hard_reject_above": 8,
            }
        return {
            "recommended_min": 6,
            "recommended_max": 8,
            "acceptable_max": 8,
            "hard_reject_above": 9,
        }

    @staticmethod
    def evaluate_shot_capacity(
        shot: Dict[str, Any],
        *,
        max_density: float = 0.42,
        comfort_density: float = 0.36,
    ) -> Dict[str, Any]:
        """评估单 shot 参考帧容量，返回密度与 verdict（不修改 shot）。"""
        from .config import LTX_DENSITY_COMFORT_MAX, LTX_MAX_REFERENCE_DENSITY

        max_density = max_density or LTX_MAX_REFERENCE_DENSITY
        comfort_density = comfort_density or LTX_DENSITY_COMFORT_MAX
        is_bridge = LTXResponseParser.is_bridge_shot(shot)
        n = len(shot.get("image_files") or shot.get("image_paths") or [])
        secs = float(shot.get("video_seconds", 0) or 0)
        density = LTXResponseParser.reference_frame_density(n, secs)
        limits = LTXResponseParser.shot_capacity_limits(secs, is_bridge=is_bridge)
        semantic_stage_count = shot.get("semantic_stage_count")
        if semantic_stage_count is not None:
            try:
                semantic_stage_count = int(semantic_stage_count)
            except (TypeError, ValueError):
                semantic_stage_count = None
        continuous_chain = bool(
            shot.get("continuous_motion_chain")
            or shot.get("action_chain_group")
        )
        issues: List[str] = []
        verdict = "ok"

        if n <= 0 or secs <= 0:
            return {
                "reference_frame_count": n,
                "reference_frame_density": density,
                "capacity_verdict": "unknown",
                "capacity_issues": issues,
                "capacity_limits": limits,
            }

        if is_bridge:
            if n > limits["hard_reject_above"]:
                issues.append(
                    f"bridge {secs:.0f}s 使用 {n} 张参考帧，超过上限 {limits['acceptable_max']}"
                )
                verdict = "reject"
        else:
            if secs <= 12 and n >= 6:
                issues.append(
                    f"{secs:.0f}s 主 shot 使用 {n} 张参考帧（≥6），极易产生 PPT 轮播感（§2.2）"
                )
                verdict = "reject"
            elif secs <= 15 and n >= 7:
                issues.append(
                    f"{secs:.0f}s 主 shot 使用 {n} 张参考帧（≥7），过密（§8.1）"
                )
                verdict = "reject"
            elif n > limits["hard_reject_above"] - 1:
                issues.append(
                    f"{secs:.0f}s 主 shot 使用 {n} 张参考帧，超过可接受上限 "
                    f"{limits['acceptable_max']}（推荐 {limits['recommended_min']}–"
                    f"{limits['recommended_max']}）"
                )
                verdict = "reject"
            elif secs <= 12 and n == 5:
                if semantic_stage_count is not None and semantic_stage_count >= 4:
                    issues.append(
                        f"12s 主 shot 使用 5 帧但含 {semantic_stage_count} 个独立语义阶段，"
                        "应拆 micro-bridge 或减少阶段（v1.7 §4.2）"
                    )
                    verdict = "reject"
                elif not continuous_chain and semantic_stage_count is None:
                    issues.append(
                        "12s 使用 5 帧但未标注 continuous_motion_chain / "
                        "semantic_stage_count，请确认属于同一连续动作链（v1.7 §5）"
                    )
                    if verdict == "ok":
                        verdict = "warn"
                elif n > limits["acceptable_max"]:
                    pass  # 5 frames at 12s is acceptable_max, handled below
            elif n > limits["acceptable_max"]:
                issues.append(
                    f"{secs:.0f}s 主 shot 使用 {n} 张参考帧，超过可接受上限 "
                    f"{limits['acceptable_max']}"
                )
                if verdict != "reject":
                    verdict = "warn"

        if (
            not is_bridge
            and semantic_stage_count is not None
            and secs > 0
        ):
            sem_density = semantic_stage_count / secs
            from .config import LTX_MAX_SEMANTIC_EVENT_DENSITY
            if sem_density > LTX_MAX_SEMANTIC_EVENT_DENSITY and not continuous_chain:
                issues.append(
                    f"semantic_event_density={sem_density:.2f} 过高（阶段数 "
                    f"{semantic_stage_count}），应拆 shot 或 micro-bridge（v1.7 §4）"
                )
                verdict = "reject"

        if not is_bridge and density > max_density:
            issues.append(
                f"reference_frame_density={density:.2f} > {max_density}，须减帧或拆 shot（§3.3）"
            )
            verdict = "reject"
        elif not is_bridge and density > comfort_density and verdict == "ok":
            issues.append(
                f"reference_frame_density={density:.2f} 偏紧（推荐 ≤{comfort_density}）"
            )
            verdict = "warn"

        idxs = list(shot.get("image_idxs") or [])
        if len(idxs) >= 4:
            gaps = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
            if gaps and max(gaps) < 0.18:
                issues.append(
                    f"image_idxs 间距过密 {gaps}，像均匀轮播（§4.2）"
                )
                if verdict == "ok":
                    verdict = "warn"

        return {
            "reference_frame_count": n,
            "reference_frame_density": density,
            "semantic_stage_count": semantic_stage_count,
            "continuous_motion_chain": continuous_chain,
            "capacity_verdict": verdict,
            "capacity_issues": issues,
            "capacity_limits": limits,
        }

    @staticmethod
    def collect_shot_capacity_issues(
        shots: List[Dict[str, Any]],
        *,
        reject_only: bool = False,
    ) -> List[str]:
        """汇总所有 shot 的容量问题（用于规划校验与 VLM 重试）。"""
        issues: List[str] = []
        for s in shots:
            sid = s.get("shot_id") or s.get("bridge_id") or "shot"
            ev = LTXResponseParser.evaluate_shot_capacity(s)
            verdict = ev["capacity_verdict"]
            if reject_only and verdict != "reject":
                continue
            for msg in ev["capacity_issues"]:
                if verdict == "reject":
                    issues.append(f"{sid}: {msg}")
                elif not reject_only:
                    issues.append(f"{sid}（建议优化）: {msg}")
        return issues

    @staticmethod
    def annotate_shot_capacity(shot: Dict[str, Any]) -> None:
        """将容量评估结果写入 shot 字典。"""
        ev = LTXResponseParser.evaluate_shot_capacity(shot)
        shot.pop("_capacity_eval", None)
        shot["reference_frame_count"] = ev["reference_frame_count"]
        shot["reference_frame_density"] = ev["reference_frame_density"]
        shot["capacity_verdict"] = ev["capacity_verdict"]
        shot["capacity_issues"] = ev["capacity_issues"]
        limits = ev["capacity_limits"]
        shot["capacity_recommended_refs"] = (
            f"{limits['recommended_min']}–{limits['recommended_max']}"
        )
        if ev.get("semantic_stage_count") is not None:
            shot["semantic_stage_count"] = ev["semantic_stage_count"]
        shot["continuous_motion_chain"] = ev.get("continuous_motion_chain", False)

    # ── v1.7 Semantic / Action Chain ─────────────────────────────────

    @staticmethod
    def _try_parse_json_array(text: str) -> List[Any]:
        text = text.strip()
        if not text:
            return []
        try:
            val = json.loads(text)
            if isinstance(val, list):
                return val
        except json.JSONDecodeError:
            pass
        try:
            val = ast.literal_eval(text)
            if isinstance(val, list):
                return val
        except (SyntaxError, ValueError):
            pass
        return []

    @staticmethod
    def extract_json_block(response: str, key: str) -> List[Dict[str, Any]]:
        """从 VLM 回复中提取 frame_semantic_inventory 等 JSON 数组。"""
        patterns = [
            rf'"{re.escape(key)}"\s*:\s*(\[[\s\S]*?\])\s*(?:,|\n|\}})',
            rf"{re.escape(key)}\s*```json\s*([\s\S]*?)```",
            rf"##\s*{re.escape(key.replace('_', ' '))}[\s\S]*?```json\s*([\s\S]*?)```",
        ]
        for pat in patterns:
            m = re.search(pat, response, re.IGNORECASE)
            if not m:
                continue
            arr = LTXResponseParser._try_parse_json_array(m.group(1))
            if arr and isinstance(arr[0], dict):
                return [dict(x) for x in arr if isinstance(x, dict)]
        return []

    @staticmethod
    def parse_frame_semantic_inventory(
        response: str,
    ) -> List[Dict[str, Any]]:
        return LTXResponseParser.extract_json_block(
            response, "frame_semantic_inventory",
        )

    @staticmethod
    def parse_frame_pair_relations(
        response: str,
    ) -> List[Dict[str, Any]]:
        return LTXResponseParser.extract_json_block(
            response, "frame_pair_relations",
        )

    @staticmethod
    def parse_shot_capacity_review(
        response: str,
    ) -> List[Dict[str, Any]]:
        rows = LTXResponseParser.extract_json_block(
            response, "shot_capacity_review",
        )
        if rows:
            return rows
        # Markdown 表：Shot Capacity Review
        out: List[Dict[str, Any]] = []
        in_section = False
        for line in response.splitlines():
            if re.search(r"shot\s+capacity\s+review", line, re.I):
                in_section = True
                continue
            if not in_section or not line.strip().startswith("|"):
                continue
            if re.search(r"^\|\s*[-:]+", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 5:
                continue
            first = cells[0].lower()
            if "shot" in first and "verdict" not in first:
                if re.match(r"^shot\s*\d", first, re.I) or re.match(
                    r"^shot_\d", first, re.I,
                ):
                    out.append({
                        "shot_id": cells[0],
                        "video_seconds": cells[1] if len(cells) > 1 else "",
                        "reference_frame_count": cells[2] if len(cells) > 2 else "",
                        "semantic_stage_count": (
                            cells[3] if len(cells) > 3 else ""
                        ),
                        "semantic_density_verdict": (
                            cells[4] if len(cells) > 4 else ""
                        ),
                    })
        return out

    @staticmethod
    def has_action_chain_analysis(response: str) -> bool:
        if LTXResponseParser.parse_frame_semantic_inventory(response):
            return True
        if LTXResponseParser.parse_frame_pair_relations(response):
            return True
        strict_markers = (
            r"frame_semantic_inventory",
            r"frame_pair_relations",
            r"##\s*Frame\s+Semantic",
            r"##\s*Action\s+Chain\s+Graph",
            r"Semantic\s+Parser",
        )
        return any(
            re.search(p, response, re.IGNORECASE) for p in strict_markers
        )

    @staticmethod
    def micro_bridge_frame_pairs(
        shots: List[Dict[str, Any]],
    ) -> set[tuple[int, int]]:
        pairs: set[tuple[int, int]] = set()
        for s in shots:
            if not LTXResponseParser.is_bridge_shot(s):
                continue
            subtype = (s.get("bridge_subtype") or "").lower()
            is_micro = (
                s.get("shot_type") == "micro_action_bridge"
                or subtype in ("micro_action", "physical_contact")
            )
            if not is_micro and subtype not in ("", "transition"):
                is_micro = "micro" in (s.get("shot_function") or "").lower()
            if not is_micro:
                continue
            files = list(s.get("image_files") or [])
            if len(files) >= 2:
                a = LTXResponseParser.frame_index_from_label(files[0])
                b = LTXResponseParser.frame_index_from_label(files[1])
                if a and b:
                    pairs.add((min(a, b), max(a, b)))
            for pair in s.get("bridges_between_frames") or []:
                if isinstance(pair, str) and "->" in pair:
                    parts = pair.split("->", 1)
                    a = LTXResponseParser.frame_index_from_label(parts[0])
                    b = LTXResponseParser.frame_index_from_label(parts[1])
                    if a and b:
                        pairs.add((min(a, b), max(a, b)))
        return pairs

    @staticmethod
    def warn_motion_first_prompt(prompt: str, shot_id: str = "") -> None:
        if not prompt:
            return
        if LTXResponseParser.MOTION_FIRST_BOILERPLATE_RE.search(prompt):
            logger.warning(
                "%s prompt 含过长安全约束句，占用动作描写空间（v1.7 §10）",
                shot_id or "shot",
            )
        then_count = len(re.findall(r"\bthen\b", prompt, re.IGNORECASE))
        if then_count >= 4:
            logger.warning(
                "%s prompt 含 %d 处 then，可能像逐帧罗列而非连续动作曲线（v1.7 §10）",
                shot_id or "shot", then_count,
            )

    @staticmethod
    def merge_capacity_review_into_shots(
        shots: List[Dict[str, Any]],
        review_rows: List[Dict[str, Any]],
    ) -> None:
        """将 shot_capacity_review 中的语义字段合并进 shots。"""
        by_index: Dict[int, Dict[str, Any]] = {}
        for i, row in enumerate(review_rows):
            by_index[i] = row
            sid = (row.get("shot_id") or "").lower()
            m = re.search(r"(\d+)", sid)
            if m:
                by_index[int(m.group(1))] = row
        main_i = 0
        for s in shots:
            if LTXResponseParser.is_bridge_shot(s):
                continue
            main_i += 1
            row = by_index.get(main_i - 1) or by_index.get(main_i) or {}
            if not row:
                continue
            for key in (
                "semantic_stage_count",
                "reference_frame_count",
                "semantic_density_verdict",
                "action_chain_group",
            ):
                if row.get(key) not in (None, ""):
                    s[key] = row[key]
            verdict = (row.get("semantic_density_verdict") or "").lower()
            if verdict in ("continuous_chain", "flight_chain", "ok_chain"):
                s["continuous_motion_chain"] = True
            sem = row.get("semantic_stage_count")
            if sem not in (None, ""):
                try:
                    s["semantic_stage_count"] = int(sem)
                except (TypeError, ValueError):
                    pass

    @staticmethod
    def collect_semantic_planning_issues(
        plan_response: str,
        shots: List[Dict[str, Any]],
    ) -> List[str]:
        """v1.7 语义动作链规划校验（reject 级问题）。"""
        issues: List[str] = []
        if not LTXResponseParser.has_action_chain_analysis(plan_response):
            issues.append(
                "规划缺少 frame_semantic_inventory / frame_pair_relations / "
                "动作链分析（v1.7 §13，禁止机械按张数切分）"
            )

        relations = LTXResponseParser.parse_frame_pair_relations(plan_response)
        micro_pairs = LTXResponseParser.micro_bridge_frame_pairs(shots)
        for rel in relations:
            rtype = (rel.get("relation_type") or "").lower().replace("-", "_")
            pair = rel.get("pair") or rel.get("frame_pair") or ""
            handling = (rel.get("recommended_handling") or "").lower()
            if rtype not in LTXResponseParser.HIGH_RISK_RELATION_TYPES:
                if "high_risk" not in rtype and "risk" not in handling:
                    continue
            a, b = 0, 0
            if "->" in str(pair):
                parts = str(pair).split("->", 1)
                a = LTXResponseParser.frame_index_from_label(parts[0])
                b = LTXResponseParser.frame_index_from_label(parts[1])
            if a and b:
                key = (min(a, b), max(a, b))
                if key not in micro_pairs:
                    issues.append(
                        f"高风险帧对 {pair}（{rtype}）未规划 micro-action bridge（v1.7 §6）"
                    )

        # 主 shot 内相邻帧启发式高风险检测
        for s in shots:
            if LTXResponseParser.is_bridge_shot(s):
                continue
            files = list(s.get("image_files") or [])
            notes = (
                s.get("transition_notes", "")
                + " "
                + s.get("shot_function", "")
            )
            for i in range(len(files) - 1):
                chunk = notes  # 简化：用 shot 级文本
                a = LTXResponseParser.frame_index_from_label(files[i])
                b = LTXResponseParser.frame_index_from_label(files[i + 1])
                if a and b and (min(a, b), max(a, b)) in micro_pairs:
                    continue
                if len(files) >= 5:
                    secs = float(s.get("video_seconds", 0))
                    if secs <= 12 and LTXResponseParser.HIGH_RISK_TEXT_RE.search(chunk):
                        issues.append(
                            f"{s.get('shot_id', 'shot')} 主 shot 内可能含高风险动作对 "
                            f"({files[i]}->{files[i+1]})，建议拆 micro-bridge（v1.7 §6）"
                        )
                    break

        for s in shots:
            if LTXResponseParser.is_bridge_shot(s):
                if not (s.get("transition_carrier") or "").strip():
                    issues.append(
                        f"{s.get('shot_id') or s.get('bridge_id', 'bridge')}: "
                        "bridge 缺少 transition_carrier（v1.7 §12.5）"
                    )

        cap_issues = LTXResponseParser.collect_shot_capacity_issues(
            shots, reject_only=True,
        )
        issues.extend(cap_issues)
        return issues

    @staticmethod
    def _parse_float_list(text: str) -> List[float]:
        text = text.strip()
        if not text:
            return []
        try:
            val = ast.literal_eval(text)
            if isinstance(val, (list, tuple)):
                return [float(x) for x in val]
        except (SyntaxError, ValueError):
            pass
        return [float(x) for x in re.findall(r"[-+]?\d*\.?\d+", text)]

    @staticmethod
    def _assignment_prefix(name: str) -> str:
        """避免匹配 negative_prompt 中的 prompt 子串。"""
        if name == "prompt":
            return r"(?<!negative_)prompt"
        if name == "negative_prompt":
            return "negative_prompt"
        return re.escape(name)

    @staticmethod
    def _parse_paren_string_concat(inner: str) -> str:
        """解析 ("part1 " "part2 ") 形式拼接为单段文本（含 Nezha's 等撇号）。"""
        inner = inner.strip()
        try:
            value = ast.literal_eval(f"({inner})")
            if isinstance(value, str):
                return value.strip()
        except (SyntaxError, ValueError):
            pass
        parts = re.findall(r'"((?:\\.|[^"\\])*)"', inner, re.DOTALL)
        if not parts:
            return ""
        return " ".join(p.strip() for p in parts if p.strip())

    @staticmethod
    def _parse_string_assignment(block: str, name: str) -> str:
        key = LTXResponseParser._assignment_prefix(name)

        if name == "prompt":
            m = re.search(
                rf"{key}\s*=\s*\((.*?)\)\s*(?=\n\s*negative_prompt\s*=|\Z)",
                block,
                re.DOTALL | re.IGNORECASE,
            )
            if m:
                text = LTXResponseParser._parse_paren_string_concat(m.group(1))
                if text:
                    return text

        patterns = [
            rf"{key}\s*=\s*\"\"\"(.*?)\"\"\"",
            rf"{key}\s*=\s*'''(.*?)'''",
            rf'{key}\s*=\s*"((?:\\.|[^"\\])*)"',
            rf"{key}\s*=\s*'((?:\\.|[^'\\])*)'",
        ]
        for pat in patterns:
            m = re.search(pat, block, re.DOTALL | re.IGNORECASE)
            if m:
                return m.group(1).strip()
        return ""

    @staticmethod
    def _normalize_video_seconds(seconds: float) -> float:
        """LTX 文档 §8.2：默认取整秒，4.5s→5s。"""
        if seconds <= 0:
            return 5.0
        if abs(seconds - round(seconds)) < 1e-6:
            return float(int(round(seconds)))
        return float(math.ceil(seconds - 1e-9))

    # §21.2 / §23.4 常见无参考「创意越界」表述（仅警告，不自动删改 prompt）
    _GROUNDING_DRIFT_RE = re.compile(
        r"\b(?:"
        r"three\s+heads?(?:\s+and\s+six\s+arms?)?|six\s+arms?|multiple\s+heads?|"
        r"cosmic\s+ring|spiritual\s+chains?|ascends?\s+into\s+(?:the\s+)?(?:sky|heaven)|"
        r"flying\s+into\s+heaven|leaving\s+the\s+(?:lotus|platform)|"
        r"new\s+(?:enemy|weapon|artifact|battlefield|location)|"
        r"random\s+power[- ]?up|divine\s+form|extra\s+divine|"
        r"molten\s+fissure(?:\s+replacing)?"
        r")\b",
        re.IGNORECASE,
    )
    _TAIL_CLIMAX_RE = re.compile(
        r"\b(?:"
        r"epic\s+orchestral\s+climax|vocal\s+chant\s+layer|"
        r"thunderclap\s+impact|slamming\s+onto\s+his\s+wrist|"
        r"between\s+heaven\s+and\s+earth|soaring\s+crane[- ]?up"
        r")\b",
        re.IGNORECASE,
    )

    _WEAK_TRANSITION_RE = re.compile(
        r"\b(?:then\s+cuts?\s+to|the\s+scene\s+transitions?\s+to|"
        r"the\s+camera\s+transitions?\s+to|cuts?\s+to\s+a)\b",
        re.IGNORECASE,
    )
    _STRONG_TRANSITION_RE = re.compile(
        r"\b(?:light[- ]?wipe|motion\s+wipe|match\s+cut|fills?\s+the\s+lens|"
        r"sweep(?:s|ing)?\s+across|wipe\s+transition|follow(?:s|ing)?\s+the\s+(?:beam|"
        r"energy|trail)|contract(?:s|ing)?\s+inward|arc(?:s|ing)?\s+across|"
        r"reveals?\s+(?:into|a))\b",
        re.IGNORECASE,
    )

    @staticmethod
    def warn_grounding_violations(prompt: str, shot_label: str = "") -> None:
        """检测 prompt 中可能的无参考剧情越界表述。"""
        if not prompt.strip():
            return
        label = shot_label or "shot"
        if LTXResponseParser._GROUNDING_DRIFT_RE.search(prompt):
            logger.warning(
                "%s：prompt 含可能无参考支撑的元素（三头六臂/宇宙环/升空等），"
                "请对照 §21 Forbidden Expansion List 重写",
                label,
            )
        if LTXResponseParser._TAIL_CLIMAX_RE.search(prompt):
            logger.info(
                "%s：prompt 尾段含强高潮表述，收束帧后宜改为余波收束（§22.4）",
                label,
            )

    @staticmethod
    def clamp_image_strengths_safe(
        strengths: List[float],
        min_strength: float,
        aggressive_threshold: float,
    ) -> tuple[List[float], bool]:
        """safe mode：将过低 strength 抬升至 min_strength（§22.1）。"""
        changed = False
        out: List[float] = []
        for s in strengths:
            if s < aggressive_threshold:
                logger.warning(
                    "image_strength %.2f < %.2f，safe mode 抬升至 %.2f",
                    s, aggressive_threshold, min_strength,
                )
                out.append(max(s, min_strength))
                changed = True
            elif s < min_strength:
                out.append(min_strength)
                changed = True
            else:
                out.append(s)
        return out, changed

    @staticmethod
    def warn_image_idxs_drift(
        idxs: List[float], num_images: int, shot_label: str = "",
    ) -> None:
        """§22.2 / §22.4：首帧过晚、末帧贴 1.0 的漂移风险。"""
        if not idxs or num_images < 2:
            return
        label = shot_label or "shot"
        if idxs[0] > 0.08:
            logger.info(
                "%s：首张参考 image_idxs=%.2f 偏晚，safe mode 建议 0.0~0.05 锁定场景",
                label, idxs[0],
            )
        if idxs[-1] >= 0.95:
            logger.warning(
                "%s：末张参考 image_idxs=%.2f 过近 1.0，易尾段定格；建议 0.82~0.90 并只写余波",
                label, idxs[-1],
            )

    @staticmethod
    def warn_transition_prompt(
        prompt: str, num_images: int, shot_label: str = "",
    ) -> None:
        """多参考帧 shot：记录转场描述过弱或未写具体转场载体的警告。"""
        if num_images < 2 or not prompt.strip():
            return
        label = shot_label or "shot"
        if LTXResponseParser._WEAK_TRANSITION_RE.search(prompt) and (
            not LTXResponseParser._STRONG_TRANSITION_RE.search(prompt)
        ):
            logger.warning(
                "%s：prompt 含空泛切镜表述且缺少 light-wipe/motion wipe/match cut 等"
                "具体转场载体，建议按文档 §12.4 重写",
                label,
            )
        elif num_images >= 2 and not LTXResponseParser._STRONG_TRANSITION_RE.search(
            prompt
        ):
            logger.info(
                "%s：多参考帧 shot 的 prompt 未检测到明确转场关键词，"
                "请确认已按 §12.4 描述相邻帧转场路径",
                label,
            )

    @staticmethod
    def warn_image_idxs(idxs: List[float], num_images: int, shot_label: str = "") -> None:
        """仅记录可疑 image_idxs，不修改 VLM 决策。"""
        if not idxs:
            return
        if num_images == 1 and len(idxs) == 1 and idxs[0] <= 0.0:
            logger.info(
                "%s image_idxs=%s：贴近 0 可能造成首帧硬锁，规划阶段宜考虑 0.10~0.20",
                shot_label, idxs,
            )
        if num_images >= 2 and idxs == [0.0, 1.0]:
            logger.info(
                "%s image_idxs=[0.0, 1.0]：易导致首尾幻灯片感，宜改为如 [0.10, 0.82]",
                shot_label,
            )

    @staticmethod
    def _parse_images_from_block(block: str) -> List[str]:
        names = _FRAME_FILE_RE.findall(block)
        if names:
            return list(dict.fromkeys(names))
        m = re.search(r"images\s*=\s*(\[[^\]]*\])", block, re.DOTALL)
        if m:
            try:
                raw = ast.literal_eval(m.group(1))
                out: List[str] = []
                for item in raw:
                    found = _FRAME_FILE_RE.search(str(item))
                    if found:
                        out.append(found.group(0))
                if out:
                    return list(dict.fromkeys(out))
            except (SyntaxError, ValueError):
                pass
        return []

    @staticmethod
    def extract_latest_plan_section(response: str) -> str:
        """多轮 VLM 修订追加在同一 response 时，只取最后一次可执行规划正文。"""
        if not response:
            return response
        best = 0
        for pat in (
            r"##\s*VLM\s*修订",
            r"##\s*重新规划",
            r"---\s*\n+##\s*VLM",
        ):
            for m in re.finditer(pat, response, re.IGNORECASE):
                if m.start() >= best:
                    best = m.start()
        if best > 0:
            return response[best:]
        last_plan: Optional[int] = None
        for m in re.finditer(
            r"#{1,3}\s*(?:\d+[\).]?\s*)?Shot\s*Plan",
            response,
            re.IGNORECASE,
        ):
            last_plan = m.start()
        if last_plan is not None and last_plan > 0:
            return response[last_plan:]
        return response

    @staticmethod
    def _shot_dedupe_key(shot: Dict[str, Any]) -> tuple:
        files = tuple(shot.get("image_files") or [])
        is_bridge = LTXResponseParser.is_bridge_shot(shot)
        secs = int(round(float(shot.get("video_seconds") or 0)))
        return (files, is_bridge, secs)

    @staticmethod
    def dedupe_parsed_shots(shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """同一参考图签名出现多次时保留最后一次（修订版优先）。"""
        if not shots:
            return shots
        by_key: Dict[tuple, Dict[str, Any]] = {}
        order: List[tuple] = []
        for s in shots:
            key = LTXResponseParser._shot_dedupe_key(s)
            if key in by_key:
                order.remove(key)
            by_key[key] = s
            order.append(key)
        return [by_key[k] for k in order]

    @staticmethod
    def dedupe_shot_plan_table_rows(
        rows: List[Dict[str, str]],
    ) -> List[Dict[str, str]]:
        seen: set = set()
        out: List[Dict[str, str]] = []
        for row in rows:
            key = (
                row.get("shot", "").strip().lower(),
                row.get("reference_images", "").strip().lower(),
                row.get("duration", "").strip().lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            out.append(row)
        return out

    @staticmethod
    def extract_python_fence_blocks(response: str) -> List[str]:
        """从回复中提取 ```python / ``` 围栏代码块；兼容未闭合尾块。"""
        blocks = re.findall(
            r"```(?:python)?\s*(.*?)```",
            response,
            re.DOTALL | re.IGNORECASE,
        )
        if blocks:
            return [b.strip() for b in blocks if b.strip()]
        # 输出被截断时，最后一个 ```python 可能没有闭合
        m = re.search(
            r"```(?:python)?\s*(.*)$",
            response,
            re.DOTALL | re.IGNORECASE,
        )
        if m:
            tail = m.group(1).strip()
            if re.search(r"(?<!negative_)prompt\s*=", tail, re.IGNORECASE):
                return [tail]
        return []

    @staticmethod
    def extract_loose_shot_blocks(response: str) -> List[str]:
        """无围栏时，按 images = [ 起始切分松散 shot 块。"""
        chunks = re.split(
            r"(?=\nimages\s*=\s*\[)",
            response,
            flags=re.IGNORECASE,
        )
        out: List[str] = []
        for chunk in chunks:
            chunk = chunk.strip()
            if not chunk.lower().startswith("images"):
                continue
            if not re.search(r"(?<!negative_)prompt\s*=", chunk, re.IGNORECASE):
                continue
            out.append(chunk)
        return out

    @staticmethod
    def parse_python_block(block: str) -> Optional[LTXShotSpec]:
        if not re.search(r"(?<!negative_)prompt\s*=", block, re.IGNORECASE):
            return None
        spec = LTXShotSpec(raw_block=block.strip())
        spec.image_files = LTXResponseParser._parse_images_from_block(block)
        m = re.search(r"image_idxs\s*=\s*(\[[^\]]*\])", block, re.DOTALL)
        if m:
            spec.image_idxs = LTXResponseParser._parse_float_list(m.group(1))
        m = re.search(r"image_strengths\s*=\s*(\[[^\]]*\])", block, re.DOTALL)
        if m:
            spec.image_strengths = LTXResponseParser._parse_float_list(m.group(1))
        m = re.search(r"video_seconds\s*=\s*([-+]?\d*\.?\d+)", block)
        if m:
            spec.video_seconds = LTXResponseParser._normalize_video_seconds(
                float(m.group(1))
            )
        spec.negative_prompt = LTXResponseParser._parse_string_assignment(
            block, "negative_prompt"
        )
        spec.prompt = LTXResponseParser._parse_string_assignment(block, "prompt")
        m = re.search(
            r"shot_type\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.shot_type = m.group(1).strip().lower()
        m = re.search(r"is_bridge\s*=\s*(True|False)", block, re.IGNORECASE)
        if m:
            spec.is_bridge = m.group(1).lower() == "true"
        m = re.search(
            r"stitch_mode\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.stitch_mode = LTXResponseParser.normalize_stitch_mode(m.group(1))
        m = re.search(
            r"bridges_between\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.bridges_between = m.group(1).strip()
        m = re.search(
            r"bridge_id\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.bridge_id = m.group(1).strip()
        m = re.search(
            r"transition_carrier\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.transition_carrier = m.group(1).strip()
        m = re.search(r"can_skip_bridge\s*=\s*(True|False)", block, re.IGNORECASE)
        if m:
            spec.can_skip_bridge = m.group(1).lower() == "true"
        m = re.search(
            r"generated_by_default\s*=\s*(True|False)", block, re.IGNORECASE,
        )
        if m:
            spec.generated_by_default = m.group(1).lower() == "true"
        m = re.search(
            r"bridge_subtype\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.bridge_subtype = m.group(1).strip().lower()
        m = re.search(
            r"bridges_between_frames\s*=\s*(\[[^\]]*\])", block, re.DOTALL,
        )
        if m:
            try:
                val = ast.literal_eval(m.group(1))
                if isinstance(val, list):
                    spec.bridges_between_frames = [str(x) for x in val]
            except (SyntaxError, ValueError):
                pass
        m = re.search(
            r"continuous_motion_chain\s*=\s*(True|False)", block, re.IGNORECASE,
        )
        if m:
            spec.continuous_motion_chain = m.group(1).lower() == "true"
        m = re.search(
            r"action_chain_group\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.action_chain_group = m.group(1).strip()
        m = re.search(r"semantic_stage_count\s*=\s*(\d+)", block)
        if m:
            spec.semantic_stage_count = int(m.group(1))
        m = re.search(
            r"risk_reason\s*=\s*['\"]([^'\"]+)['\"]", block, re.IGNORECASE,
        )
        if m:
            spec.risk_reason = m.group(1).strip()
        spec.shot_type = LTXResponseParser.infer_shot_type(
            shot_function=spec.shot_function,
            is_bridge=spec.is_bridge,
            shot_type=spec.shot_type,
        )
        tmp = {
            "shot_type": spec.shot_type,
            "is_bridge": spec.is_bridge,
            "bridge_subtype": spec.bridge_subtype,
        }
        LTXResponseParser.normalize_shot_type_fields(tmp)
        spec.shot_type = tmp["shot_type"]
        spec.is_bridge = tmp["is_bridge"]
        spec.bridge_subtype = tmp.get("bridge_subtype", spec.bridge_subtype)
        if not spec.prompt:
            return None
        if (
            spec.negative_prompt
            and spec.prompt.strip() == spec.negative_prompt.strip()
        ):
            logger.warning("prompt 与 negative_prompt 相同，跳过该代码块")
            return None
        if spec.image_files and not spec.image_idxs:
            logger.warning(
                "代码块未给出 image_idxs，使用兜底值（建议由 VLM 在规划中显式填写）"
            )
            n = len(spec.image_files)
            if n == 1:
                spec.image_idxs = [0.15]
            elif n == 2:
                spec.image_idxs = [0.10, 0.72]
            else:
                spec.image_idxs = [
                    round(i / (n - 1) * 0.82, 2) for i in range(n)
                ]
        if spec.image_files and spec.image_idxs:
            LTXResponseParser.warn_image_idxs(
                spec.image_idxs, len(spec.image_files)
            )
        if spec.image_files and len(spec.image_files) >= 2:
            LTXResponseParser.warn_transition_prompt(
                spec.prompt, len(spec.image_files)
            )
            LTXResponseParser.warn_grounding_violations(spec.prompt)
            LTXResponseParser.warn_image_idxs_drift(
                spec.image_idxs, len(spec.image_files)
            )
        if spec.image_files and not spec.image_strengths:
            spec.image_strengths = [1.0] * len(spec.image_files)
        if spec.image_files:
            LTXResponseParser._align_spec_ref_arrays(spec)
        return spec

    @staticmethod
    def _default_image_idxs(n: int) -> List[float]:
        if n <= 0:
            return []
        if n == 1:
            return [0.15]
        return [round(i / (n - 1) * 0.82, 2) for i in range(n)]

    @staticmethod
    def _ensure_strict_image_idxs(idxs: List[float]) -> List[float]:
        """保证 0<idx<1 且严格递增，满足 LTX insert_frame_map 约束。"""
        if not idxs:
            return idxs
        n = len(idxs)
        min_gap = 0.04
        max_end = 0.96
        out: List[float] = []
        for i, raw in enumerate(idxs):
            v = float(raw)
            if i == 0:
                v = max(0.02, min(v, max_end - min_gap * max(0, n - 1)))
            else:
                v = max(v, out[-1] + min_gap)
                v = min(v, max_end)
            if i > 0 and v <= out[-1]:
                logger.warning(
                    "image_idxs 无法保持单调递增，均匀重排 %d 个锚点",
                    n,
                )
                return [
                    round(0.06 + j * (max_end - 0.06) / max(1, n - 1), 2)
                    for j in range(n)
                ]
            out.append(round(v, 2))
        return out

    @staticmethod
    def _align_float_list(
        values: List[float],
        n: int,
        *,
        fill: float,
        spread: bool,
    ) -> List[float]:
        vals = list(values or [])
        if len(vals) > n:
            vals = vals[:n]
        if len(vals) < n:
            if spread:
                default = LTXResponseParser._default_image_idxs(n)
                out = vals[:]
                for j in range(len(out), n):
                    out.append(default[j])
                vals = out
            else:
                pad = vals[-1] if vals else fill
                while len(vals) < n:
                    vals.append(pad)
        if spread:
            return LTXResponseParser._ensure_strict_image_idxs(vals)
        return vals

    @staticmethod
    def _align_spec_ref_arrays(spec: LTXShotSpec) -> None:
        n = len(spec.image_files)
        if n <= 0:
            return
        orig_idxs = list(spec.image_idxs)
        spec.image_idxs = LTXResponseParser._align_float_list(
            spec.image_idxs, n, fill=0.0, spread=True,
        )
        if len(orig_idxs) != n:
            logger.warning(
                "shot 参数 image_idxs 数量(%d)与 images(%d)不一致，已自动对齐",
                len(orig_idxs),
                n,
            )
        elif spec.image_idxs != orig_idxs:
            logger.warning("shot 参数 image_idxs 非严格递增，已自动修正")
        if len(spec.image_strengths) != n:
            logger.warning(
                "shot 参数 image_strengths 数量(%d)与 images(%d)不一致，已自动对齐",
                len(spec.image_strengths),
                n,
            )
            spec.image_strengths = LTXResponseParser._align_float_list(
                spec.image_strengths, n, fill=0.95, spread=False,
            )

    @staticmethod
    def normalize_shot_dict(shot: Dict[str, Any]) -> Dict[str, Any]:
        """生成前对齐 image_paths / image_idxs / image_strengths 长度。"""
        files = list(shot.get("image_files") or [])
        paths = list(shot.get("image_paths") or [])
        valid_files: List[str] = []
        valid_paths: List[str] = []
        for i, p in enumerate(paths):
            if p and os.path.isfile(p):
                valid_paths.append(p)
                valid_files.append(
                    files[i] if i < len(files) else os.path.basename(p)
                )
        if valid_paths:
            shot["image_files"] = valid_files
            shot["image_paths"] = valid_paths
            n = len(valid_paths)
        else:
            n = len(files)
        if n <= 0:
            return shot
        idxs = list(shot.get("image_idxs") or [])
        strengths = list(shot.get("image_strengths") or [])
        orig_idxs = idxs[:]
        shot["image_idxs"] = LTXResponseParser._align_float_list(
            idxs, n, fill=0.0, spread=True,
        )
        if len(orig_idxs) != n:
            logger.warning(
                "%s image_idxs=%d vs images=%d，自动对齐",
                shot.get("shot_id", "shot"),
                len(orig_idxs),
                n,
            )
        elif shot["image_idxs"] != orig_idxs:
            logger.warning(
                "%s image_idxs 非严格递增，已自动修正",
                shot.get("shot_id", "shot"),
            )
        if len(strengths) != n:
            logger.warning(
                "%s image_strengths=%d vs images=%d，自动对齐",
                shot.get("shot_id", "shot"),
                len(strengths),
                n,
            )
            shot["image_strengths"] = LTXResponseParser._align_float_list(
                strengths, n, fill=0.95, spread=False,
            )
        return shot

    @staticmethod
    def _shot_dict_from_spec(
        spec: LTXShotSpec,
        shot_index: int,
        frames_dir: str,
        default_negative: str,
    ) -> Dict[str, Any]:
        shot_id = f"shot_{shot_index:02d}"
        image_paths = [os.path.join(frames_dir, fname) for fname in spec.image_files]
        return LTXResponseParser.normalize_shot_dict({
            "shot_id": shot_id,
            "image_files": spec.image_files,
            "image_paths": image_paths,
            "image_idxs": spec.image_idxs,
            "image_strengths": spec.image_strengths,
            "video_seconds": spec.video_seconds,
            "prompt": spec.prompt,
            "negative_prompt": spec.negative_prompt or default_negative,
            "output_file": f"case_ltx_{shot_id}.mp4",
            "shot_function": spec.shot_function,
            "transition_notes": spec.transition_notes,
            "shot_type": spec.shot_type,
            "is_bridge": spec.is_bridge,
            "stitch_mode": spec.stitch_mode,
            "bridges_between": spec.bridges_between,
            "bridge_id": spec.bridge_id,
            "transition_carrier": spec.transition_carrier,
            "can_skip_bridge": spec.can_skip_bridge,
            "generated_by_default": spec.generated_by_default,
            "bridge_subtype": spec.bridge_subtype,
            "bridges_between_frames": list(spec.bridges_between_frames),
            "semantic_stage_count": spec.semantic_stage_count,
            "continuous_motion_chain": spec.continuous_motion_chain,
            "action_chain_group": spec.action_chain_group,
            "risk_reason": spec.risk_reason,
        })

    @staticmethod
    def parse_ltx_plan_response(
        response: str,
        frames_dir: str,
        default_negative: str = "",
    ) -> List[Dict[str, Any]]:
        """解析 VLM 回复中的多个 ```python``` shot 参数块。"""
        effective = LTXResponseParser.extract_latest_plan_section(response)
        blocks = LTXResponseParser.extract_python_fence_blocks(effective)
        if not blocks:
            blocks = LTXResponseParser.extract_loose_shot_blocks(effective)
        shots: List[Dict[str, Any]] = []
        for i, block in enumerate(blocks, start=1):
            spec = LTXResponseParser.parse_python_block(block)
            if not spec:
                continue
            shots.append(
                LTXResponseParser._shot_dict_from_spec(
                    spec, i, frames_dir, default_negative
                )
            )
        return LTXResponseParser.dedupe_parsed_shots(shots)

    @staticmethod
    def merge_shots_with_table(
        shots: List[Dict[str, Any]],
        table_rows: List[Dict[str, str]],
    ) -> List[Dict[str, Any]]:
        row_idx = 0
        for s in shots:
            is_bridge = LTXResponseParser.is_bridge_shot(s)
            while row_idx < len(table_rows):
                row = table_rows[row_idx]
                row_is_bridge = (
                    row.get("shot_type") == "bridge"
                    or "micro" in (row.get("shot", "") + row.get("shot_type", "")).lower()
                    or bool(re.search(r"\bbridge\b", row.get("shot", ""), re.I))
                )
                if row_is_bridge != is_bridge:
                    row_idx += 1
                    continue
                s["shot_function"] = row.get("shot_function", "") or s.get(
                    "shot_function", ""
                )
                s["image_idxs_reason"] = row.get("image_idxs_reason", "")
                s["transition_notes"] = row.get("transition_notes", "") or s.get(
                    "transition_notes", ""
                )
                if row.get("stitch_mode"):
                    s["stitch_mode"] = LTXResponseParser.normalize_stitch_mode(
                        row["stitch_mode"]
                    )
                if row.get("bridges_between"):
                    s["bridges_between"] = row["bridges_between"]
                s["shot_type"] = LTXResponseParser.infer_shot_type(
                    shot_label=row.get("shot", ""),
                    shot_function=s.get("shot_function", ""),
                    is_bridge=bool(s.get("is_bridge")),
                    shot_type=s.get("shot_type", ""),
                )
                LTXResponseParser.normalize_shot_type_fields(s)
                row_idx += 1
                break
        return shots

    @staticmethod
    def parse_shot_plan_table(response: str) -> List[Dict[str, str]]:
        """尽力从 Markdown 表格提取 Shot 元信息（可选）。"""
        effective = LTXResponseParser.extract_latest_plan_section(response)
        rows: List[Dict[str, str]] = []
        for line in effective.splitlines():
            if not line.strip().startswith("|"):
                continue
            if re.search(r"^\|\s*[-:]+", line):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            first = cells[0].strip()
            if re.search(
                r"(?:reference|duration|image_idxs|shot\s*function|transition)",
                first,
                re.I,
            ):
                continue
            if not (
                re.match(r"^\d{1,2}$", first)
                or re.match(r"^shot\s*\d", first, re.I)
                or re.match(r"^bridge\s", first, re.I)
            ):
                continue
            row = {
                "shot": first,
                "reference_images": cells[1] if len(cells) > 1 else "",
                "duration": cells[2] if len(cells) > 2 else "",
                "image_idxs": cells[3] if len(cells) > 3 else "",
                "shot_function": "",
                "transition_notes": "",
                "image_idxs_reason": "",
                "stitch_mode": "",
                "shot_type": "",
                "bridges_between": "",
            }
            if len(cells) >= 7:
                c4 = cells[4]
                c5 = cells[5]
                c6 = cells[6]
                if LTXResponseParser.normalize_stitch_mode(c5) in (
                    "direct_concat",
                    "trim_overlap",
                ):
                    # v1.4：Shot Function | Stitch Mode | Transition Notes
                    row["shot_function"] = c4
                    row["stitch_mode"] = LTXResponseParser.normalize_stitch_mode(c5)
                    row["transition_notes"] = c6
                else:
                    # v1.2：image_idxs 理由 | Shot Function | Transition Notes
                    row["image_idxs_reason"] = c4
                    row["shot_function"] = c5
                    row["transition_notes"] = c6
            else:
                row["shot_function"] = cells[4] if len(cells) > 4 else ""
                row["transition_notes"] = cells[5] if len(cells) > 5 else ""
            row["shot_type"] = LTXResponseParser.infer_shot_type(
                shot_label=first,
                shot_function=row["shot_function"],
            )
            bridge_m = re.search(
                r"bridge\s*(\d+)\s*[-–]\s*(\d+)",
                first,
                re.IGNORECASE,
            )
            if bridge_m:
                row["bridges_between"] = f"shot_{bridge_m.group(1)}_shot_{bridge_m.group(2)}"
            rows.append(row)
        return LTXResponseParser.dedupe_shot_plan_table_rows(rows)
