"""
LTX 音视频生成阶段：规划 shot → 调用 keyframe_interpolation 生成视频。
在参考帧工作流（final_assembly）之后执行，或单独 --pipeline video 模式运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .config import (
    LTX_AGGRESSIVE_STRENGTH_THRESHOLD,
    LTX_BRIDGE_DEFAULT_SECONDS,
    LTX_DEFAULT_HEIGHT,
    LTX_DEFAULT_STITCH_MODE,
    LTX_DEFAULT_WIDTH,
    LTX_DENSITY_COMFORT_MAX,
    LTX_GENERATE_BRIDGE_CANDIDATES,
    LTX_GROUNDED_SAFE_MODE,
    LTX_MAX_REFERENCE_DENSITY,
    LTX_MAX_SEMANTIC_EVENT_DENSITY,
    LTX_MAX_SHOT_SECONDS,
    LTX_MIN_IMAGE_STRENGTH,
    LTX_MAX_PARALLEL,
    LTX_RESOLUTION_LABELS,
    LTX_STITCH_MODES,
    LTX_USE_BRIDGE_AT_EXPORT,
    LTX_VIDEO_CANDIDATES,
    LTX_WORKFLOW_DOC_PATH,
    PIPELINE_MODES,
    is_valid_ltx_dimensions,
    resolve_ltx_resolution,
)
from .ltx_client import VideoGeneratorLTX23API
from .ltx_parser import LTXResponseParser

if TYPE_CHECKING:
    from .orchestrator import WorkflowOrchestrator

logger = logging.getLogger(__name__)

DEFAULT_NEGATIVE = (
    "subtitles, captions, lower-third, chyron, nameplate, UI overlay, watermark, "
    "title card, floating text, speech bubble, comic annotation, character introduction "
    "overlay, freeze frame, still image sequence, slideshow effect, static pose, "
    "mannequin pose, duplicate character, extra people, distorted hands, broken fingers, "
    "extra fingers, warped face, unstable scene layout, sudden camera jump, "
    "abrupt unrelated scene cut, uncontrolled random transition, chaotic camera rotation"
)

# 多参考帧 shot 追加 negative（文档 §12.4.5，与 DEFAULT_NEGATIVE 去重合并）
MULTI_REF_TRANSITION_NEGATIVE = (
    "abrupt unrelated scene cut, uncontrolled random transition, "
    "repeated still image, chaotic camera rotation"
)

# 文档 §4 / §12 bridge shot 追加 negative
BRIDGE_STITCH_NEGATIVE = (
    "hard cut feeling, motion discontinuity, abrupt unrelated scene cut, "
    "uncontrolled random transition, slideshow effect"
)

# 文档 §21.6 创意越界抑制（safe mode 默认合并进 negative_prompt）
GROUNDING_NEGATIVE = (
    "unsupported new plot, unrelated transformation, new character, new weapon, "
    "new prop, new location, changing ending, random power-up, inconsistent final state, "
    "extra heads, extra arms, extra faces, multiple bodies, uncontrolled transformation, "
    "changing outfit, wrong hairstyle, unrelated landscape, new building, "
    "changing platform, inconsistent prop shape, disappearing core object"
)

_FINAL_FRAME_RE = re.compile(r"^case_final_frame(\d+)\.png$", re.IGNORECASE)
_EDIT_FRAME_RE = re.compile(r"^case_edit_frame(\d+)\.png$", re.IGNORECASE)
_BASE_FRAME_RE = re.compile(r"^case_base_frame(\d+)\.png$", re.IGNORECASE)


class LTXWorkflowExtension:
    def __init__(self, orch: "WorkflowOrchestrator"):
        self.orch = orch
        self.parser = LTXResponseParser()
        self.ltx = VideoGeneratorLTX23API()
        self.plan_file = os.path.join(orch.output_dir, "ltx_shot_plan.json")
        self.summary_file = os.path.join(orch.output_dir, "ltx_shot_summary.json")

    @property
    def output_dir(self) -> str:
        return self.orch.output_dir

    # ── 参考帧扫描 ──────────────────────────────────────────────

    def collect_reference_frames(self) -> Dict[str, str]:
        """final > edit > base，返回 {case_final_frame01.png: abs_path}."""
        by_idx: Dict[int, tuple] = {}
        for fname in os.listdir(self.output_dir):
            if not fname.lower().endswith(".png"):
                continue
            path = os.path.join(self.output_dir, fname)
            if not os.path.isfile(path):
                continue
            for pat, prio in (
                (_FINAL_FRAME_RE, 0),
                (_EDIT_FRAME_RE, 1),
                (_BASE_FRAME_RE, 2),
            ):
                m = pat.match(fname)
                if m:
                    idx = int(m.group(1))
                    prev = by_idx.get(idx)
                    if prev is None or prio < prev[0]:
                        canonical = f"case_final_frame{idx:02d}.png"
                        by_idx[idx] = (prio, canonical, path)
                    break
        return {v[1]: v[2] for v in sorted(by_idx.values(), key=lambda x: x[1])}

    def shot_candidates_dir(self, shot: Dict[str, Any]) -> str:
        """每个 shot 的候选视频目录，如 case_ltx_shot_01_candidates/。"""
        base = shot.get("output_file", "case_ltx_shot_01.mp4").replace(".mp4", "")
        return os.path.join(self.output_dir, f"{base}_candidates")

    def list_shot_video_candidates(self, candidates_dir: str) -> List[str]:
        if not os.path.isdir(candidates_dir):
            return []
        return sorted(
            os.path.join(candidates_dir, f)
            for f in os.listdir(candidates_dir)
            if f.lower().endswith(".mp4") and os.path.isfile(os.path.join(candidates_dir, f))
        )

    def shot_candidates_ready(self, shot: Dict[str, Any]) -> bool:
        n_required = int(self.orch.state.get("ltx_video_candidates", LTX_VIDEO_CANDIDATES))
        existing = self.list_shot_video_candidates(self.shot_candidates_dir(shot))
        return len(existing) >= n_required

    def get_ltx_video_size(self) -> tuple[int, int]:
        """当前运行使用的 LTX 输出尺寸（宽, 高），来自 state 或 config。"""
        w = int(self.orch.state.get("ltx_width", LTX_DEFAULT_WIDTH))
        h = int(self.orch.state.get("ltx_height", LTX_DEFAULT_HEIGHT))
        if not is_valid_ltx_dimensions(w, h):
            w, h, _ = resolve_ltx_resolution()
        return w, h

    def get_ltx_resolution_key(self) -> str:
        return str(self.orch.state.get("ltx_resolution", ""))

    def clear_ltx_video_candidates_only(self) -> List[str]:
        """仅删除各 shot 候选 mp4，保留 ltx_shot_plan.json。"""
        import glob

        removed: List[str] = []
        for candidates_dir in glob.glob(
            os.path.join(self.output_dir, "case_ltx_*_candidates")
        ):
            if not os.path.isdir(candidates_dir):
                continue
            for fname in os.listdir(candidates_dir):
                if not fname.lower().endswith(".mp4"):
                    continue
                path = os.path.join(candidates_dir, fname)
                if os.path.isfile(path):
                    os.remove(path)
                    removed.append(os.path.join(os.path.basename(candidates_dir), fname))
        self.orch.state["ltx_shots_done"] = []
        if self.orch.state.get("step") == "done":
            self.orch.state["step"] = "ltx_generate"
        return removed

    def _read_summary_video_dims(self) -> tuple[Optional[int], Optional[int]]:
        if not os.path.isfile(self.summary_file):
            return None, None
        try:
            with open(self.summary_file, "r", encoding="utf-8") as f:
                doc = json.load(f)
            w, h = doc.get("ltx_width"), doc.get("ltx_height")
            if w is None or h is None:
                return None, None
            return int(w), int(h)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None, None

    def invalidate_ltx_generate_if_dims_changed(
        self, new_width: int, new_height: int,
    ) -> bool:
        """config/CLI 改了 LTX 尺寸时，旧候选 mp4 与断点 done 列表失效。"""
        old_w = self.orch.state.get("ltx_width")
        old_h = self.orch.state.get("ltx_height")
        if old_w is None or old_h is None:
            sum_w, sum_h = self._read_summary_video_dims()
            if sum_w is not None:
                old_w, old_h = sum_w, sum_h
            else:
                return False
        if int(old_w) == int(new_width) and int(old_h) == int(new_height):
            return False
        removed = self.clear_ltx_video_candidates_only()
        print(
            f"  [LTX] 视频尺寸 {int(old_w)}×{int(old_h)} → {new_width}×{new_height}，"
            "已清理旧候选 mp4，将按新尺寸重新生成。"
        )
        if removed:
            print(f"        已删除 {len(removed)} 个候选文件")
        return True

    def apply_runtime_config(
        self,
        pipeline_mode: str,
        video_target_seconds: Optional[float],
        long_shot_mode: bool,
        long_shot_seconds: Optional[float] = None,
        ltx_video_candidates: Optional[int] = None,
        ltx_max_parallel: Optional[int] = None,
        ltx_grounded_safe_mode: Optional[bool] = None,
        ltx_width: Optional[int] = None,
        ltx_height: Optional[int] = None,
        ltx_resolution: Optional[str] = None,
        ltx_stitch_mode: Optional[str] = None,
        ltx_generate_bridge_candidates: Optional[bool] = None,
        ltx_use_bridge_at_export: Optional[bool] = None,
    ) -> None:
        if pipeline_mode not in PIPELINE_MODES:
            raise ValueError(f"pipeline_mode 必须是 {PIPELINE_MODES} 之一")
        self.orch.state["pipeline_mode"] = pipeline_mode
        if video_target_seconds is not None:
            self.orch.state["video_target_seconds"] = float(video_target_seconds)
        self.orch.state["long_shot_mode"] = bool(long_shot_mode)
        if long_shot_mode:
            if long_shot_seconds is not None:
                self.orch.state["long_shot_seconds"] = float(long_shot_seconds)
            else:
                # 未指定时每段时长由 LTX 规划 VLM 在 shot 表中自行决定
                self.orch.state["long_shot_seconds"] = None
        else:
            self.orch.state.pop("long_shot_seconds", None)
        if ltx_video_candidates is not None:
            self.orch.state["ltx_video_candidates"] = int(ltx_video_candidates)
        elif "ltx_video_candidates" not in self.orch.state:
            self.orch.state["ltx_video_candidates"] = LTX_VIDEO_CANDIDATES
        if ltx_max_parallel is not None:
            self.orch.state["ltx_max_parallel"] = max(1, int(ltx_max_parallel))
        elif "ltx_max_parallel" not in self.orch.state:
            self.orch.state["ltx_max_parallel"] = LTX_MAX_PARALLEL
        if ltx_grounded_safe_mode is not None:
            self.orch.state["ltx_grounded_safe_mode"] = bool(ltx_grounded_safe_mode)
        elif "ltx_grounded_safe_mode" not in self.orch.state:
            self.orch.state["ltx_grounded_safe_mode"] = LTX_GROUNDED_SAFE_MODE

        new_width, new_height, resolution_key = resolve_ltx_resolution(
            preset=ltx_resolution,
            width=ltx_width,
            height=ltx_height,
        )
        self.invalidate_ltx_generate_if_dims_changed(new_width, new_height)
        self.orch.state["ltx_width"] = new_width
        self.orch.state["ltx_height"] = new_height
        self.orch.state["ltx_resolution"] = resolution_key

        if ltx_stitch_mode is not None:
            mode = self.parser.normalize_stitch_mode(ltx_stitch_mode)
            if mode not in LTX_STITCH_MODES:
                raise ValueError(
                    f"ltx_stitch_mode 必须是 {LTX_STITCH_MODES} 之一，得到: {ltx_stitch_mode!r}"
                )
            self.orch.state["ltx_stitch_mode"] = mode
        elif "ltx_stitch_mode" not in self.orch.state:
            self.orch.state["ltx_stitch_mode"] = LTX_DEFAULT_STITCH_MODE

        if ltx_generate_bridge_candidates is not None:
            self.orch.state["ltx_generate_bridge_candidates"] = bool(
                ltx_generate_bridge_candidates
            )
        elif "ltx_generate_bridge_candidates" not in self.orch.state:
            self.orch.state["ltx_generate_bridge_candidates"] = (
                LTX_GENERATE_BRIDGE_CANDIDATES
            )
        if ltx_use_bridge_at_export is not None:
            self.orch.state["ltx_use_bridge_at_export"] = bool(
                ltx_use_bridge_at_export
            )
        elif "ltx_use_bridge_at_export" not in self.orch.state:
            self.orch.state["ltx_use_bridge_at_export"] = LTX_USE_BRIDGE_AT_EXPORT

        self.invalidate_ltx_plan_if_stale()

    def clear_ltx_video_artifacts(self) -> List[str]:
        """删除 LTX 候选目录、成片位与规划总结文件。"""
        import glob
        import shutil

        removed: List[str] = []
        for pattern in (
            os.path.join(self.output_dir, "case_ltx_shot_*.mp4"),
            os.path.join(self.output_dir, "case_ltx_shot_*_candidates"),
            os.path.join(self.output_dir, "case_ltx_bridge_*.mp4"),
            os.path.join(self.output_dir, "case_ltx_bridge_*_candidates"),
        ):
            for path in glob.glob(pattern):
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    removed.append(os.path.basename(path) + "/")
                elif os.path.isfile(path):
                    os.remove(path)
                    removed.append(os.path.basename(path))
        for name in ("ltx_shot_summary.json", "ltx_shot_plan.json"):
            p = os.path.join(self.output_dir, name)
            if os.path.isfile(p):
                os.remove(p)
                removed.append(name)
        return removed

    def reset_ltx_phase(self, *, announce: bool = True) -> List[str]:
        """清除 LTX 规划/候选/成片缓存，断点回退到 ltx_plan（保留参考帧）。"""
        import glob
        import shutil
        import stat

        removed: List[str] = []

        def _rm_path(path: str) -> None:
            nonlocal removed
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                    removed.append(os.path.basename(path) + "/")
                elif os.path.isfile(path):
                    os.remove(path)
                    removed.append(os.path.basename(path))
            except PermissionError:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, onerror=_chmod_and_retry)
                    else:
                        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
                        os.remove(path)
                    removed.append(os.path.basename(path.rstrip("/")) + (
                        "/" if os.path.isdir(path) else ""
                    ))
                except OSError as e:
                    raise PermissionError(
                        f"无法删除 LTX 缓存 {path}（可能为 root 创建）。"
                        f"请执行: sudo chown -R $USER:$USER {self.output_dir}/case_ltx_* "
                        f"{self.output_dir}/ltx_shot*.json"
                    ) from e

        def _chmod_and_retry(func, p, exc_info):
            os.chmod(p, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(p)

        for pattern in (
            os.path.join(self.output_dir, "case_ltx_shot_*.mp4"),
            os.path.join(self.output_dir, "case_ltx_shot_*_candidates"),
            os.path.join(self.output_dir, "case_ltx_bridge_*.mp4"),
            os.path.join(self.output_dir, "case_ltx_bridge_*_candidates"),
        ):
            for path in glob.glob(pattern):
                _rm_path(path)
        for name in ("ltx_shot_summary.json", "ltx_shot_plan.json"):
            p = os.path.join(self.output_dir, name)
            if os.path.isfile(p):
                _rm_path(p)
        for key in (
            "ltx_plan_response",
            "ltx_shots",
            "ltx_shots_done",
            "ltx_planned_long_shot_seconds",
            "ltx_planned_stitch_mode",
            "ltx_planned_generate_bridge",
            "ltx_planned_use_bridge_at_export",
        ):
            self.orch.state.pop(key, None)
        self.orch.state["step"] = "ltx_plan"
        self.orch.summary.pop("ltx_shots", None)
        self.orch.summary.pop("ltx", None)
        self.orch.summary.pop("ltx_shot_plan_table", None)
        self.orch._save_state()
        self.orch._save_summary()
        if announce:
            print(
                "  [LTX] 已重置视频生成阶段：将重新执行 Shot 规划 → 候选生成"
                "（参考帧不受影响）。"
            )
            if removed:
                print(f"        已删除: {', '.join(removed)}")
        return removed

    def _ltx_plan_is_stale(self) -> bool:
        """用户改了每段时长等约束时，旧 ltx_shots 不应继续用于 generate。"""
        shots: List[Dict] = list(self.orch.state.get("ltx_shots") or [])
        if not shots and not os.path.exists(self.plan_file):
            return False

        cap = self.orch.state.get("long_shot_seconds")
        planned_cap = self.orch.state.get("ltx_planned_long_shot_seconds")
        if (cap is None) != (planned_cap is None):
            return True
        if cap is not None and planned_cap is not None:
            if abs(float(cap) - float(planned_cap)) > 0.5:
                return True
        planned_stitch = self.orch.state.get("ltx_planned_stitch_mode")
        if planned_stitch and planned_stitch != self.stitch_mode():
            return True
        if cap is not None:
            for s in shots:
                if self.parser.is_bridge_shot(s):
                    continue
                if abs(float(s.get("video_seconds", 0)) - float(cap)) > 0.5:
                    return True
            target = self.orch.state.get("video_target_seconds")
            if target:
                mains = [s for s in shots if not self.parser.is_bridge_shot(s)]
                expected = self.expected_main_shot_count(float(target), float(cap))
                if len(mains) != expected:
                    return True
                main_sum = self.main_shots_planned_seconds(shots)
                if abs(main_sum - float(target)) > 1.0:
                    return True
        return False

    def invalidate_ltx_plan_if_stale(self) -> bool:
        if not self._ltx_plan_is_stale():
            return False
        removed = self.clear_ltx_video_artifacts()
        print(
            "  [LTX] 检测到每段时长约束与已存 shot 规划不一致，"
            "已清理旧 LTX 缓存并将断点回退到 ltx_plan。"
        )
        if removed:
            print(f"        已删除: {', '.join(removed)}")
        for key in (
            "ltx_plan_response",
            "ltx_shots",
            "ltx_shots_done",
            "ltx_planned_long_shot_seconds",
            "ltx_planned_stitch_mode",
        ):
            self.orch.state.pop(key, None)
        if self.orch.state.get("step") in ("ltx_generate", "done"):
            self.orch.state["step"] = "ltx_plan"
        return True

    def grounded_safe_mode(self) -> bool:
        return bool(self.orch.state.get("ltx_grounded_safe_mode", LTX_GROUNDED_SAFE_MODE))

    def stitch_mode(self) -> str:
        return self.parser.normalize_stitch_mode(
            self.orch.state.get("ltx_stitch_mode", LTX_DEFAULT_STITCH_MODE)
        )

    def generate_bridge_candidates(self) -> bool:
        return bool(
            self.orch.state.get(
                "ltx_generate_bridge_candidates",
                LTX_GENERATE_BRIDGE_CANDIDATES,
            )
        )

    def use_bridge_at_export(self) -> bool:
        return bool(
            self.orch.state.get(
                "ltx_use_bridge_at_export",
                LTX_USE_BRIDGE_AT_EXPORT,
            )
        )

    def ensure_video_pipeline_prerequisites(self) -> None:
        frames = self.collect_reference_frames()
        if not frames:
            raise RuntimeError(
                f"未在输出目录找到任何参考帧（case_final/edit/base_frameXX.png）。\n"
                f"目录：{self.output_dir}\n"
                "请先完成参考帧阶段，或使用 --pipeline full。"
            )

    def sync_step_from_disk(self) -> None:
        """根据磁盘产物修正 LTX 相关 step（断点续传）。"""
        mode = self.orch.state.get("pipeline_mode", "frames")
        if mode not in ("video", "full"):
            return

        frames = self.collect_reference_frames()
        if not frames:
            return

        shots: List[Dict] = list(self.orch.state.get("ltx_shots") or [])
        if not shots and os.path.exists(self.plan_file):
            with open(self.plan_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            shots = saved.get("shots", [])
            if shots:
                self.orch.state["ltx_shots"] = shots
                self.orch.state["ltx_plan_response"] = saved.get("plan_response", "")

        done: List[str] = []
        if shots:
            for s in shots:
                sid = s.get("shot_id", "")
                if self.shot_candidates_ready(s):
                    done.append(sid)
            self.orch.state["ltx_shots_done"] = sorted(set(done))

        step = self.orch.state.get("step", "init")
        if mode == "video" and step in (
            "init", "resources", "resource_review", "base_frames",
            "edit_plan", "edit_frames", "final_assembly",
        ):
            step = "ltx_plan" if not shots else (
                "ltx_generate" if len(done) < len(shots) else "done"
            )
        elif mode == "full" and step == "done" and shots and len(done) < len(shots):
            step = "ltx_generate"
        elif mode == "full" and step == "done" and not shots:
            step = "ltx_plan"
        elif mode in ("video", "full") and step == "final_assembly":
            pass
        elif mode in ("video", "full") and not shots and step not in ("ltx_plan", "ltx_generate"):
            if step == "done" or all(
                os.path.isfile(os.path.join(self.output_dir, n)) for n in frames
            ):
                step = "ltx_plan"

        if shots and len(done) >= len(shots) and len(shots) > 0:
            step = "done"

        self.orch.state["step"] = step
        self.sync_ltx_summary_from_disk()

    def _shot_status(self, shot: Dict[str, Any]) -> str:
        out_path = os.path.join(self.output_dir, shot["output_file"])
        if os.path.isfile(out_path):
            return "selected"
        n_required = int(
            self.orch.state.get("ltx_video_candidates", LTX_VIDEO_CANDIDATES)
        )
        cands = self.list_shot_video_candidates(self.shot_candidates_dir(shot))
        if len(cands) >= n_required:
            return "candidates_ready"
        if cands:
            return "candidates_partial"
        return "planned"

    def build_shot_summary_entry(self, shot: Dict[str, Any]) -> Dict[str, Any]:
        """单 shot 总结条目（对齐 workflow_summary 中 base_frames 字段风格）。"""
        candidates_dir = self.shot_candidates_dir(shot)
        candidates = self.list_shot_video_candidates(candidates_dir)
        out_name = shot["output_file"]
        out_path = os.path.join(self.output_dir, out_name)
        entry: Dict[str, Any] = {
            "shot_id": shot.get("shot_id", ""),
            "file": out_name,
            "output_file": out_name,
            "candidates_dir": os.path.basename(candidates_dir),
            "pipeline": "keyframe_interpolation_two_stage",
            "image_files": list(shot.get("image_files", [])),
            "image_idxs": list(shot.get("image_idxs", [])),
            "image_strengths": list(shot.get("image_strengths", [])),
            "video_seconds": float(shot.get("video_seconds", 0)),
            "prompt": shot.get("prompt", ""),
            "negative_prompt": shot.get("negative_prompt", DEFAULT_NEGATIVE),
            "shot_function": shot.get("shot_function", ""),
            "image_idxs_reason": shot.get("image_idxs_reason", ""),
            "transition_notes": shot.get("transition_notes", ""),
            "status": self._shot_status(shot),
            "ltx_video_candidates": int(
                self.orch.state.get("ltx_video_candidates", LTX_VIDEO_CANDIDATES)
            ),
            "grounded_safe_mode": self.grounded_safe_mode(),
            "shot_type": shot.get("shot_type", "main"),
            "is_bridge": bool(shot.get("is_bridge")),
            "stitch_mode": shot.get("stitch_mode") or self.stitch_mode(),
            "bridges_between": shot.get("bridges_between", ""),
            "bridge_id": shot.get("bridge_id", ""),
            "transition_carrier": shot.get("transition_carrier", ""),
            "can_skip_bridge": bool(shot.get("can_skip_bridge", True)),
            "generated_by_default": bool(shot.get("generated_by_default", False)),
            "optional_for_export": bool(shot.get("is_bridge")),
            "included_in_export": self._shot_included_in_export(shot),
            "reference_frame_count": shot.get("reference_frame_count"),
            "reference_frame_density": shot.get("reference_frame_density"),
            "capacity_verdict": shot.get("capacity_verdict", ""),
            "capacity_recommended_refs": shot.get("capacity_recommended_refs", ""),
            "capacity_issues": list(shot.get("capacity_issues") or []),
            "semantic_stage_count": shot.get("semantic_stage_count"),
            "continuous_motion_chain": bool(shot.get("continuous_motion_chain")),
            "action_chain_group": shot.get("action_chain_group", ""),
            "bridge_subtype": shot.get("bridge_subtype", ""),
            "bridges_between_frames": list(shot.get("bridges_between_frames") or []),
            "risk_reason": shot.get("risk_reason", ""),
            "candidates": [os.path.basename(p) for p in candidates],
            "chosen_candidate": None,
            "pick_reason": "",
            "selected_file": out_name if os.path.isfile(out_path) else None,
        }
        return entry

    def build_ltx_summary_document(
        self, shots: List[Dict[str, Any]], plan_response: str = "",
    ) -> Dict[str, Any]:
        shot_entries = {
            s["output_file"]: self.build_shot_summary_entry(s) for s in shots
        }
        total_secs = sum(float(s.get("video_seconds", 0)) for s in shots)
        all_ready = all(
            e["status"] in ("candidates_ready", "selected") for e in shot_entries.values()
        ) if shot_entries else False
        any_selected = any(e["status"] == "selected" for e in shot_entries.values())
        if any_selected and all_ready:
            status = "部分已选片" if not all(
                e["status"] == "selected" for e in shot_entries.values()
            ) else "已完成（含人工选片）"
        elif all_ready:
            status = "候选已生成，待人工挑选"
        elif shots:
            status = "已规划，待生成视频"
        else:
            status = "未开始"
        stitch = self.stitch_mode()
        main_count = sum(
            1 for s in shots if not self.parser.is_bridge_shot(s)
        )
        bridge_count = sum(1 for s in shots if self.parser.is_bridge_shot(s))
        export_order = self._build_export_playback_order(shots)
        return {
            "case_name": self.orch.case_name,
            "status": status,
            "pipeline": "keyframe_interpolation_two_stage",
            "ltx_stitch_mode": stitch,
            "ltx_generate_bridge_candidates": self.generate_bridge_candidates(),
            "ltx_use_bridge_at_export": self.use_bridge_at_export(),
            "stitch_playback_notes": self._stitch_playback_notes(stitch),
            "playback_order": self._build_playback_order(shots),
            "export_playback_order": export_order,
            "export_playback_notes": self._export_playback_notes(shots),
            "main_shot_count": main_count,
            "bridge_shot_count": bridge_count,
            "video_target_seconds": self.orch.state.get("video_target_seconds"),
            "long_shot_mode": self.orch.state.get("long_shot_mode", False),
            "long_shot_seconds": self.orch.state.get("long_shot_seconds"),
            "ltx_video_candidates": int(
                self.orch.state.get("ltx_video_candidates", LTX_VIDEO_CANDIDATES)
            ),
            "ltx_width": self.get_ltx_video_size()[0],
            "ltx_height": self.get_ltx_video_size()[1],
            "ltx_resolution": self.get_ltx_resolution_key(),
            "ltx_resolution_label": LTX_RESOLUTION_LABELS.get(
                self.get_ltx_resolution_key(), ""
            ),
            "shot_count": len(shots),
            "total_planned_seconds": total_secs,
            "reference_frames": list(self.collect_reference_frames().keys()),
            "plan_file": os.path.basename(self.plan_file),
            "shots": shot_entries,
        }

    def save_ltx_shot_summary(
        self,
        shots: List[Dict[str, Any]],
        plan_response: str = "",
    ) -> None:
        """写入 ltx_shot_summary.json，并同步 workflow_summary.json 的 ltx_shots 段。"""
        doc = self.build_ltx_summary_document(shots, plan_response)
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)

        self.orch.summary["ltx_shots"] = doc["shots"]
        self.orch.summary["ltx"] = {
            "status": doc["status"],
            "video_target_seconds": doc["video_target_seconds"],
            "long_shot_mode": doc["long_shot_mode"],
            "long_shot_seconds": doc["long_shot_seconds"],
            "ltx_stitch_mode": doc.get("ltx_stitch_mode"),
            "ltx_generate_bridge_candidates": doc.get("ltx_generate_bridge_candidates"),
            "ltx_use_bridge_at_export": doc.get("ltx_use_bridge_at_export"),
            "bridge_shot_count": doc.get("bridge_shot_count", 0),
            "grounded_safe_mode": self.grounded_safe_mode(),
            "shot_count": doc["shot_count"],
            "total_planned_seconds": doc["total_planned_seconds"],
            "summary_file": os.path.basename(self.summary_file),
            "plan_file": doc["plan_file"],
        }
        self.orch._save_summary()
        logger.info("LTX shot summary saved: %s", self.summary_file)

    def reparse_shots_from_plan(self, plan_response: str) -> List[Dict[str, Any]]:
        """用最新解析器从 plan_response 重新提取 shot 参数（修复历史错误缓存）。"""
        def _lookup_key(s: Dict[str, Any]) -> str:
            if s.get("shot_type") == "bridge":
                return s.get("bridge_id") or s.get("shot_id") or ""
            return s.get("shot_id") or ""

        old_shots = {
            _lookup_key(s): s for s in (self.orch.state.get("ltx_shots") or [])
        }
        shots = self.parser.parse_ltx_plan_response(
            plan_response, self.output_dir, default_negative=DEFAULT_NEGATIVE,
        )
        table_rows = self.parser.parse_shot_plan_table(plan_response)
        if table_rows:
            shots = self.parser.merge_shots_with_table(shots, table_rows)
        self.apply_multi_ref_transition_defaults(shots)
        self.apply_grounding_safeguards(shots)
        self.validate_long_shot_plan(shots)
        shots = self.collapse_duplicate_bridge_shots(shots)
        shots = self.ensure_bridge_candidates(shots)
        self.apply_bridge_stitch_safeguards(shots)
        self.apply_semantic_action_safeguards(plan_response, shots)
        self.assign_shot_ids_and_paths(shots)
        for s in shots:
            prev = old_shots.get(_lookup_key(s), {})
            if prev.get("image_paths"):
                s["image_paths"] = prev["image_paths"]
            else:
                self._resolve_shot_image_paths([s])
        return shots

    def sync_ltx_summary_from_disk(self) -> None:
        """根据 ltx_shot_plan.json 与磁盘候选/成片刷新总结。"""
        plan_response = self.orch.state.get("ltx_plan_response", "")
        shots: List[Dict] = list(self.orch.state.get("ltx_shots") or [])
        if os.path.exists(self.plan_file):
            with open(self.plan_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            plan_response = saved.get("plan_response", plan_response) or plan_response
            if plan_response:
                shots = self.reparse_shots_from_plan(plan_response)
                self.orch.state["ltx_shots"] = shots
                self.orch._save_state()
                table_rows = self.parser.parse_shot_plan_table(plan_response)
                payload = dict(saved)
                payload["shots"] = shots
                payload["shot_plan_table"] = table_rows
                with open(self.plan_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
        if not shots:
            return
        self.save_ltx_shot_summary(shots, plan_response)

    def _save_shot_plan(self, plan_response: str, shots: List[Dict]) -> None:
        table_rows = self.parser.parse_shot_plan_table(plan_response)
        self.orch.summary["ltx_shot_plan_table"] = table_rows
        if table_rows:
            shots = self.parser.merge_shots_with_table(
                list(shots), table_rows,
            )

        payload = {
            "plan_response": plan_response,
            "shot_plan_table": table_rows,
            "video_target_seconds": self.orch.state.get("video_target_seconds"),
            "long_shot_mode": self.orch.state.get("long_shot_mode", False),
            "long_shot_seconds": self.orch.state.get("long_shot_seconds"),
            "ltx_grounded_safe_mode": self.orch.state.get("ltx_grounded_safe_mode"),
            "ltx_stitch_mode": self.stitch_mode(),
            "ltx_resolution": self.get_ltx_resolution_key(),
            "ltx_width": self.get_ltx_video_size()[0],
            "ltx_height": self.get_ltx_video_size()[1],
            "ltx_generate_bridge_candidates": self.generate_bridge_candidates(),
            "ltx_use_bridge_at_export": self.use_bridge_at_export(),
            "ltx_video_candidates": self.orch.state.get("ltx_video_candidates"),
            "reference_frames": list(self.collect_reference_frames().keys()),
            "frame_semantic_inventory": self.orch.state.get(
                "ltx_frame_semantic_inventory", [],
            ),
            "frame_pair_relations": self.orch.state.get(
                "ltx_frame_pair_relations", [],
            ),
            "shot_capacity_review": self.orch.state.get(
                "ltx_shot_capacity_review", [],
            ),
            "shots": shots,
        }
        with open(self.plan_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        self.save_ltx_shot_summary(shots, plan_response)
        print(f"  LTX shot 总结已写入: {self.summary_file}")

    def _build_inventory_text(self, frame_map: Dict[str, str]) -> str:
        """拼装 LTX 规划用户消息中的上游剧情证据（v1.2 §2 / §21）。"""
        lines = ["| Frame | File |", "|---|---|"]
        for i, (fname, _path) in enumerate(sorted(frame_map.items()), start=1):
            lines.append(f"| frame{i:02d} | {fname} |")

        summary = self.orch.summary
        idea = summary.get("idea", "")
        overview = summary.get("overview", "")
        storyboard = summary.get("storyboard_plan", "")
        edit_plan: List[Dict] = (
            summary.get("edit_plan")
            or self.orch.state.get("edit_plan")
            or []
        )
        final_meta = summary.get("final_frames", {})
        edit_frames = summary.get("edit_frames", {})
        base_frames = summary.get("base_frames", {})
        resources = summary.get("resources", {})
        registry = summary.get("resource_registry", [])

        block = (
            f"用户 idea（剧情边界，不得越界扩展）：\n{idea}\n\n"
            f"workflow_summary 概述：\n{overview[:2500] or '（空，请严格以参考帧画面 + idea 为证据）'}\n\n"
        )
        if storyboard:
            block += f"storyboard_plan 摘要：\n{storyboard[:2000]}\n\n"

        block += (
            f"参考帧清单（共 {len(frame_map)} 张，已按剧情序号排列）：\n"
            + "\n".join(lines)
            + "\n\n"
        )

        if edit_plan:
            block += "### 上游 edit_plan（每帧镜头/剧情语义，规划时必须对齐）\n"
            for entry in edit_plan[:30]:
                if not isinstance(entry, dict):
                    continue
                bf = entry.get("base_frame", "")
                ff = entry.get("final_frame", entry.get("edit_frame", ""))
                block += (
                    f"- {ff or bf}: edit_level={entry.get('edit_level', '')}; "
                    f"strategy={entry.get('edit_strategy', '')[:200]}; "
                    f"diagnosis={entry.get('diagnosis', '')[:120]}; "
                    f"keep={entry.get('keep', '')[:120]}; "
                    f"fixes={entry.get('fixes', '')[:120]}\n"
                )
            block += "\n"

        if final_meta:
            block += (
                "final_frames 元数据：\n"
                f"{json.dumps(final_meta, ensure_ascii=False)[:2000]}\n\n"
            )

        if edit_frames and isinstance(edit_frames, dict):
            block += "### edit_frames 摘要（生成侧约束）\n"
            for fname in sorted(edit_frames.keys())[:20]:
                data = edit_frames[fname]
                if not isinstance(data, dict):
                    continue
                block += (
                    f"- {fname}: strategy={str(data.get('edit_strategy', ''))[:160]}; "
                    f"keep={str(data.get('keep', ''))[:100]}\n"
                )
            block += "\n"

        if base_frames and isinstance(base_frames, dict):
            block += f"base_frames 数量：{len(base_frames)}（剧情骨架已生成）\n\n"

        if resources:
            block += f"resources 数量：{len(resources)}\n"
        if registry:
            block += f"resource_registry 条目：{len(registry)}\n"

        return block

    @staticmethod
    def _merge_negative_terms(base: str, extra: str) -> str:
        """将 extra 中尚未出现在 base 的词条追加到 negative_prompt。"""
        if not extra.strip():
            return base
        existing = {p.strip().lower() for p in base.split(",") if p.strip()}
        to_add = [
            p.strip()
            for p in extra.split(",")
            if p.strip() and p.strip().lower() not in existing
        ]
        if not to_add:
            return base
        sep = ", " if base.rstrip().endswith(",") or not base.strip() else ", "
        return base.rstrip().rstrip(",") + sep + ", ".join(to_add)

    def apply_multi_ref_transition_defaults(self, shots: List[Dict[str, Any]]) -> None:
        """多参考帧 shot：补全转场相关 negative；记录 prompt 转场描述不足警告。"""
        for s in shots:
            n = len(s.get("image_files") or [])
            if n < 2:
                continue
            neg = s.get("negative_prompt") or DEFAULT_NEGATIVE
            s["negative_prompt"] = self._merge_negative_terms(
                neg, MULTI_REF_TRANSITION_NEGATIVE
            )
            LTXResponseParser.warn_transition_prompt(
                s.get("prompt", ""),
                n,
                s.get("shot_id", ""),
            )

    def _stitch_playback_notes(self, stitch_mode: str) -> str:
        if stitch_mode == "trim_overlap":
            return (
                "裁剪余量模式：各 shot mp4 不可整段直接拼接；主 shot 尾段与 bridge "
                "首尾为可剪辑 handle，需在边界参考帧附近选取切点后再衔接。"
            )
        return (
            "直接拼接模式：按 export_playback_order 将各 shot 整段 mp4 顺序拼接；"
            "边界参考帧须贴近 shot 首尾（§13.1）。"
        )

    def _export_playback_notes(self, shots: List[Dict[str, Any]]) -> str:
        if not self.use_bridge_at_export():
            bridge_n = sum(1 for s in shots if s.get("shot_type") == "bridge")
            if bridge_n:
                return (
                    f"导出拼接跳过 {bridge_n} 个 bridge candidate（ltx_use_bridge_at_export=false）；"
                    "仅顺序拼接主 shot。若直接拼接突兀，可改用 bridge 或切换 trim_overlap。"
                )
        if self.use_bridge_at_export():
            return (
                "导出拼接包含全部 bridge candidate：shot1 + bridge + shot2 + …"
                "（v1.5 默认生成、默认使用；可用 --no-bridge-at-export 改为仅主 shot）。"
            )
        return "导出拼接顺序见 export_playback_order。"

    def _shot_included_in_export(self, shot: Dict[str, Any]) -> bool:
        if not self.parser.is_bridge_shot(shot):
            return True
        return self.use_bridge_at_export()

    def _build_playback_order(self, shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        order: List[Dict[str, Any]] = []
        for s in shots:
            order.append({
                "shot_id": s.get("shot_id", ""),
                "output_file": s.get("output_file", ""),
                "shot_type": s.get("shot_type", "main"),
                "is_bridge": bool(s.get("is_bridge")),
                "video_seconds": float(s.get("video_seconds", 0)),
                "image_files": list(s.get("image_files", [])),
                "bridges_between": s.get("bridges_between", ""),
                "bridge_id": s.get("bridge_id", ""),
                "transition_carrier": s.get("transition_carrier", ""),
                "can_skip_bridge": bool(s.get("can_skip_bridge", True)),
                "generated_by_default": bool(s.get("generated_by_default", False)),
                "optional_for_export": bool(s.get("is_bridge")),
                "included_in_export": self._shot_included_in_export(s),
            })
        return order

    def _build_export_playback_order(
        self, shots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return [
            item for item in self._build_playback_order(shots)
            if item.get("included_in_export")
        ]

    @staticmethod
    def _bridge_frame_pair(
        prev_main: Dict[str, Any],
        next_main: Dict[str, Any],
    ) -> tuple[str, str]:
        prev_files = list(prev_main.get("image_files") or [])
        next_files = list(next_main.get("image_files") or [])
        if not prev_files or not next_files:
            return "", ""
        tail = prev_files[-1]
        head = next_files[0]
        if tail == head and len(prev_files) >= 2:
            tail = prev_files[-2]
        elif tail == head and len(next_files) >= 2:
            head = next_files[1]
        return tail, head

    @staticmethod
    def _main_shot_index_from_id(shot_id: str) -> int:
        m = re.search(r"(\d+)", shot_id or "")
        return int(m.group(1)) if m else 0

    def _default_bridge_idxs(self) -> List[float]:
        if self.stitch_mode() == "trim_overlap":
            return [0.12, 0.82]
        return [0.05, 0.88]

    @staticmethod
    def _bridges_between_label(left_main: int, right_main: int) -> str:
        return f"shot_{left_main:02d}_shot_{right_main:02d}"

    @staticmethod
    def _bridge_tag(left_main: int, right_main: int) -> str:
        return f"bridge_{left_main:02d}_{right_main:02d}"

    def _synthesize_bridge_shot(
        self,
        prev_main: Dict[str, Any],
        next_main: Dict[str, Any],
        *,
        left_main_idx: int,
        right_main_idx: int,
        auto_inserted: bool = True,
    ) -> Dict[str, Any]:
        tail_file, head_file = self._bridge_frame_pair(prev_main, next_main)
        bridge_id = self._bridge_tag(left_main_idx, right_main_idx)
        bridges_between = self._bridges_between_label(left_main_idx, right_main_idx)
        notes = (
            prev_main.get("transition_notes", "")
            or next_main.get("transition_notes", "")
        )
        prompt = (
            "The sequence stays within the story shown by the reference frames. "
            "A controlled motion-wipe bridges the previous beat into the next: ambient light, "
            "camera movement, or the subject's continuing gesture sweeps across the lens, "
            "carrying motion momentum and a sustained audio hum smoothly from the prior state "
            "into the opening of the next segment without a hard cut."
        )
        if notes:
            prompt += f" Transition intent: {notes[:160]}."
        return {
            "image_files": [tail_file, head_file],
            "image_idxs": self._default_bridge_idxs(),
            "image_strengths": [0.92, 0.95],
            "video_seconds": float(LTX_BRIDGE_DEFAULT_SECONDS),
            "prompt": prompt,
            "negative_prompt": self._merge_negative_terms(
                DEFAULT_NEGATIVE, BRIDGE_STITCH_NEGATIVE
            ),
            "shot_function": "跨 shot 转场（bridge candidate）",
            "transition_notes": notes or "系统自动补齐 bridge；请确认转场载体",
            "shot_type": "bridge",
            "is_bridge": True,
            "stitch_mode": self.stitch_mode(),
            "bridges_between": bridges_between,
            "bridge_id": bridge_id,
            "transition_carrier": "",
            "can_skip_bridge": True,
            "generated_by_default": True,
            "auto_inserted_bridge": auto_inserted,
        }

    def ensure_bridge_candidates(self, shots: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """v1.5：多主 shot 时默认在相邻主 shot 之间补齐 bridge candidate。"""
        if not self.generate_bridge_candidates():
            return shots
        main_shots = [
            s for s in shots if not self.parser.is_bridge_shot(s)
        ]
        if len(main_shots) <= 1:
            return shots

        out: List[Dict[str, Any]] = []
        last_main_idx = 0
        main_idx = 0
        inserted = 0
        for s in shots:
            if self.parser.is_bridge_shot(s):
                s.setdefault("can_skip_bridge", True)
                s.setdefault("generated_by_default", True)
                s.setdefault("optional_for_export", True)
                out.append(s)
                continue
            main_idx += 1
            if last_main_idx > 0 and not self.parser.is_bridge_shot(out[-1]):
                prev_main = next(
                    x for x in reversed(out) if not self.parser.is_bridge_shot(x)
                )
                bridge = self._synthesize_bridge_shot(
                    prev_main,
                    s,
                    left_main_idx=last_main_idx,
                    right_main_idx=main_idx,
                    auto_inserted=True,
                )
                out.append(bridge)
                inserted += 1
                logger.warning(
                    "主 shot %02d → %02d 缺少 bridge candidate，已自动补齐 %s",
                    last_main_idx,
                    main_idx,
                    bridge.get("bridge_id"),
                )
            out.append(s)
            last_main_idx = main_idx
        if inserted:
            print(
                f"  v1.5：已自动补齐 {inserted} 个 bridge candidate（默认生成、导出可选）"
            )
        return out

    def collapse_duplicate_bridge_shots(
        self, shots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """同一对主 shot 之间只保留一个 bridge（VLM 修订叠加时可能重复）。"""
        out: List[Dict[str, Any]] = []
        main_idx = 0
        seen_pairs: set = set()
        for s in shots:
            if not self.parser.is_bridge_shot(s):
                main_idx += 1
                out.append(s)
                continue
            pair = (main_idx, main_idx + 1)
            if pair in seen_pairs:
                logger.warning(
                    "丢弃重复 bridge（shot_%02d ↔ shot_%02d）",
                    pair[0], pair[1],
                )
                continue
            seen_pairs.add(pair)
            s.setdefault(
                "bridges_between",
                self._bridges_between_label(pair[0], pair[1]),
            )
            out.append(s)
        return out

    def assign_shot_ids_and_paths(self, shots: List[Dict[str, Any]]) -> None:
        """主 shot 用 shot_XX；bridge 用 bridge_AA_BB（主 shot 序号，非帧号）。"""
        main_idx = 0
        for s in shots:
            if self.parser.is_bridge_shot(s):
                bid = s.get("bridge_id", "")
                m = re.match(r"shot_(\d+)_shot_(\d+)", s.get("bridges_between", ""))
                if m:
                    bid = f"bridge_{m.group(1)}_{m.group(2)}"
                elif main_idx > 0:
                    bid = f"bridge_{main_idx:02d}_{main_idx + 1:02d}"
                if not bid:
                    bid = "bridge_unknown"
                s["bridge_id"] = bid
                s["shot_id"] = bid
                s["output_file"] = f"case_ltx_{bid}.mp4"
                s["candidates_dir"] = f"case_ltx_{bid}_candidates"
            else:
                main_idx += 1
                s["shot_id"] = f"shot_{main_idx:02d}"
                s["output_file"] = f"case_ltx_shot_{main_idx:02d}.mp4"
                s["candidates_dir"] = f"case_ltx_shot_{main_idx:02d}_candidates"

    def user_long_shot_seconds(self) -> Optional[float]:
        """长 shot 模式下用户显式指定的单段主 shot 时长；未指定则返回 None（由 VLM 规划）。"""
        if not self.orch.state.get("long_shot_mode"):
            return None
        raw = self.orch.state.get("long_shot_seconds")
        return float(raw) if raw is not None else None

    @staticmethod
    def expected_main_shot_count(
        target_seconds: float, user_secs: float,
    ) -> int:
        """用户给定总时长与单段时长时，主 shot 数量（bridge 不计）。"""
        return max(1, round(float(target_seconds) / float(user_secs)))

    def main_shots_planned_seconds(self, shots: List[Dict[str, Any]]) -> float:
        return sum(
            float(s.get("video_seconds", 0))
            for s in shots
            if not self.parser.is_bridge_shot(s)
        )

    def collect_long_shot_plan_issues(
        self, shots: List[Dict[str, Any]],
    ) -> List[str]:
        """检查 VLM 规划是否满足用户指定的单段主 shot 时长与总时长预算（不修改 shots）。"""
        user_secs = self.user_long_shot_seconds()
        if user_secs is None:
            return []
        user_secs = min(float(user_secs), float(LTX_MAX_SHOT_SECONDS))
        target = self.orch.state.get("video_target_seconds")
        mains = [s for s in shots if not self.parser.is_bridge_shot(s)]
        issues: List[str] = []
        if target:
            target_f = float(target)
            expected = self.expected_main_shot_count(target_f, user_secs)
            main_sum = self.main_shots_planned_seconds(shots)
            if len(mains) != expected:
                issues.append(
                    f"主 shot 数量应为 **{expected}** 段（{target_f:.0f}s ÷ {user_secs:.0f}s，"
                    f"bridge 不计），实际 {len(mains)} 段"
                )
            if abs(main_sum - target_f) > 1.0:
                issues.append(
                    f"主 shot 时长之和为 {main_sum:.0f}s，应**等于**目标 {target_f:.0f}s"
                    f"（bridge 不计入该预算；禁止用额外短主 shot 如 8s 凑剧情）"
                )
        for s in mains:
            sid = s.get("shot_id") or "shot"
            secs = float(s.get("video_seconds", 0))
            if abs(secs - user_secs) > 0.5:
                issues.append(
                    f"{sid} 的 video_seconds={secs:.1f}，应为 {user_secs:.0f}"
                )
        return issues

    def validate_long_shot_plan(
        self, shots: List[Dict[str, Any]],
    ) -> List[str]:
        """打印长 shot 规划校验结果，返回问题列表（不修改 shots）。"""
        issues = self.collect_long_shot_plan_issues(shots)
        for msg in issues:
            print(f"  警告：长 shot 规划 — {msg}")
            logger.warning("long shot plan: %s", msg)
        return issues

    def _main_shot_frame_index_sets(
        self, shots: List[Dict[str, Any]],
    ) -> List[List[int]]:
        mains = [s for s in shots if not self.parser.is_bridge_shot(s)]
        out: List[List[int]] = []
        for m in mains:
            idxs = sorted({
                self.parser.frame_index_from_label(f)
                for f in (m.get("image_files") or [])
                if self.parser.frame_index_from_label(f) > 0
            })
            out.append(idxs)
        return out

    def collect_reference_frame_partition_issues(
        self,
        shots: List[Dict[str, Any]],
        total_frames: int,
    ) -> List[str]:
        """主 shot 应连续覆盖 frame01..frameN；bridge 仅边界重叠，不能独占中间帧。"""
        if total_frames <= 0:
            return []
        main_sets = self._main_shot_frame_index_sets(shots)
        if len(main_sets) < 2:
            return []
        issues: List[str] = []
        covered: set = set()
        for idxs in main_sets:
            covered.update(idxs)
        all_frames = set(range(1, total_frames + 1))
        missing = sorted(all_frames - covered)
        if missing:
            labels = ", ".join(f"frame{i:02d}" for i in missing)
            issues.append(
                f"主 shot 未覆盖 {labels}（这些帧仅在 bridge 或完全遗漏）；"
                f"主 shot 须连续覆盖 frame01–frame{total_frames:02d}"
            )
        for i, idxs in enumerate(main_sets, start=1):
            if len(idxs) >= 2 and idxs != list(range(idxs[0], idxs[-1] + 1)):
                labels = ", ".join(f"frame{x:02d}" for x in idxs)
                issues.append(
                    f"shot_{i:02d} 参考帧不连续（{labels}）；"
                    f"每个主 shot 内应使用连续帧段"
                )
        for i in range(len(main_sets) - 1):
            left, right = main_sets[i], main_sets[i + 1]
            if not left or not right:
                continue
            gap = right[0] - left[-1]
            if gap > 1:
                issues.append(
                    f"主 shot 之间跳帧：shot_{i:02d} 末帧 frame{left[-1]:02d} → "
                    f"shot_{i + 1:02d} 首帧 frame{right[0]:02d}；"
                    f"shot_{i + 1:02d} 应从 frame{left[-1] + 1:02d} 起"
                    f"包含后续参考帧（如 frame{left[-1] + 1:02d}–"
                    f"frame{total_frames:02d}）"
                )
            elif gap <= 0:
                issues.append(
                    f"shot_{i:02d} 与 shot_{i + 1:02d} 主 shot 参考帧重叠"
                    f"（frame{right[0]:02d}），重叠应仅由 bridge 承担"
                )
        return issues

    def validate_reference_frame_partition(
        self, shots: List[Dict[str, Any]], total_frames: int,
    ) -> List[str]:
        issues = self.collect_reference_frame_partition_issues(shots, total_frames)
        for msg in issues:
            print(f"  警告：参考帧切分 — {msg}")
            logger.warning("frame partition: %s", msg)
        return issues

    def _reference_frame_partition_directive(self, total_frames: int) -> str:
        if total_frames <= 1:
            return ""
        mains_hint = ""
        user_secs = self.user_long_shot_seconds()
        target = self.orch.state.get("video_target_seconds")
        if user_secs and target:
            n_main = self.expected_main_shot_count(float(target), user_secs)
            if n_main == 2 and total_frames >= 4:
                split = total_frames // 2
                mains_hint = (
                    f"（本 case 约 {n_main} 主 shot / {total_frames} 帧示例："
                    f"shot_01≈frame01–frame{split:02d}，"
                    f"shot_02≈frame{split + 1:02d}–frame{total_frames:02d}）"
                )
        return (
            "## 参考帧切分原则（多主 shot 时**必须连续覆盖**，bridge 不独占剧情帧）\n"
            f"- 共 {total_frames} 张参考帧 frame01–frame{total_frames:02d}，"
            f"各**主 shot** 须按剧情时间**连续**使用其中一段，合起来覆盖全部帧"
            f"{mains_hint}。\n"
            "- **禁止** shot_01 用 frame01–04 而 shot_02 从 frame06 开始、"
            "把 frame05 **只**放在 bridge 里；若 shot_01 止于 frame04，"
            "则 shot_02 的 `images` **必须从 frame05 起**包含余下帧"
            f"（如 frame05–frame{total_frames:02d}）。\n"
            "- bridge 仅用**相邻边界一对**重叠锚点（如 frame04+frame05）做过渡；"
            "bridge **不能替代**主 shot 对中间帧的覆盖。\n"
            "- micro-action bridge 可聚焦 frame04→05 动作，但 frame05 仍须出现在"
            "shot_02 的 `images` 列表中作为该主 shot 首帧或次帧。\n"
        )

    def validate_shot_capacity_plan(
        self, shots: List[Dict[str, Any]],
    ) -> List[str]:
        """打印 v1.6 参考帧容量校验结果，返回问题列表（不修改 shots）。"""
        issues = self.parser.collect_shot_capacity_issues(shots)
        for msg in issues:
            print(f"  警告：参考帧容量 — {msg}")
            logger.warning("shot capacity: %s", msg)
        return issues

    def apply_shot_capacity_safeguards(self, shots: List[Dict[str, Any]]) -> None:
        """v1.6：标注密度/容量 verdict，过密 shot 强化 anti-slideshow negative。"""
        overcrowded_neg = (
            "slideshow effect, repeated still image, freeze frame, static pose, "
            "still image sequence"
        )
        for s in shots:
            self.parser.annotate_shot_capacity(s)
            verdict = s.get("capacity_verdict", "")
            sid = s.get("shot_id") or s.get("bridge_id") or "shot"
            for msg in s.get("capacity_issues") or []:
                if verdict == "reject":
                    logger.warning("%s capacity reject: %s", sid, msg)
                elif verdict == "warn":
                    logger.warning("%s capacity warn: %s", sid, msg)
            if verdict == "reject":
                neg = s.get("negative_prompt") or DEFAULT_NEGATIVE
                s["negative_prompt"] = self._merge_negative_terms(
                    neg, overcrowded_neg,
                )

    def apply_semantic_action_safeguards(
        self,
        plan_response: str,
        shots: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """v1.7：解析语义清单、合并 capacity review、校验动作链与 prompt。"""
        semantic_inv = self.parser.parse_frame_semantic_inventory(plan_response)
        pair_relations = self.parser.parse_frame_pair_relations(plan_response)
        capacity_review = self.parser.parse_shot_capacity_review(plan_response)
        if capacity_review:
            self.parser.merge_capacity_review_into_shots(shots, capacity_review)
        for s in shots:
            self.parser.normalize_shot_type_fields(s)
            self.parser.warn_motion_first_prompt(
                s.get("prompt", ""),
                s.get("shot_id") or s.get("bridge_id", ""),
            )
        self.apply_shot_capacity_safeguards(shots)
        return {
            "frame_semantic_inventory": semantic_inv,
            "frame_pair_relations": pair_relations,
            "shot_capacity_review": capacity_review,
        }

    def validate_semantic_plan(
        self, plan_response: str, shots: List[Dict[str, Any]],
    ) -> List[str]:
        issues = self.parser.collect_semantic_planning_issues(
            plan_response, shots,
        )
        for msg in issues:
            print(f"  警告：语义动作链 — {msg}")
            logger.warning("semantic plan: %s", msg)
        return issues

    def annotate_bridge_metadata(self, shots: List[Dict[str, Any]]) -> None:
        """为 bridge shot 补全 v1.5/v1.7 元数据字段。"""
        for s in shots:
            if not self.parser.is_bridge_shot(s):
                continue
            self.parser.normalize_shot_type_fields(s)
            s.setdefault("can_skip_bridge", True)
            s.setdefault("generated_by_default", True)
            s.setdefault("optional_for_export", True)
            if not s.get("bridge_id") and s.get("bridges_between"):
                m = re.match(
                    r"shot_(\d+)_shot_(\d+)", s["bridges_between"],
                )
                if m:
                    s["bridge_id"] = f"bridge_{m.group(1)}_{m.group(2)}"
            if not s.get("bridge_id"):
                s["bridge_id"] = f"bridge_{s.get('shot_id', 'unknown')}"
            if not s.get("transition_carrier"):
                s["transition_carrier"] = self.parser.infer_transition_carrier(
                    s.get("prompt", ""),
                    s.get("transition_notes", ""),
                )

    def apply_bridge_stitch_safeguards(self, shots: List[Dict[str, Any]]) -> None:
        """v1.5：标注 bridge shot、按拼接模式校验边界 image_idxs、补全 negative。"""
        global_mode = self.stitch_mode()
        n = len(shots)
        for i, s in enumerate(shots):
            self.parser.normalize_shot_type_fields(s)
            s["stitch_mode"] = self.parser.normalize_stitch_mode(
                s.get("stitch_mode") or global_mode
            )

            neg = s.get("negative_prompt") or DEFAULT_NEGATIVE
            if self.parser.is_bridge_shot(s):
                s["negative_prompt"] = self._merge_negative_terms(
                    neg, BRIDGE_STITCH_NEGATIVE
                )
                self.parser.warn_bridge_shot_prompt(
                    s.get("prompt", ""), s.get("shot_id", ""),
                )
                secs = float(s.get("video_seconds", 0))
                if secs > 6:
                    logger.warning(
                        "%s bridge shot 时长 %.1fs 偏长，建议 3–5s（§8.1）",
                        s.get("shot_id", "bridge"), secs,
                    )

            prev_is_bridge = i > 0 and self.parser.is_bridge_shot(shots[i - 1])
            next_is_bridge = (
                i + 1 < n and self.parser.is_bridge_shot(shots[i + 1])
            )
            has_prev = i > 0
            has_next = i + 1 < n
            shot_type_label = (
                "bridge" if self.parser.is_bridge_shot(s) else "main"
            )
            self.parser.warn_boundary_idxs_for_stitch_mode(
                list(s.get("image_idxs") or []),
                len(s.get("image_files") or s.get("image_paths") or []),
                s["stitch_mode"],
                shot_type_label,
                s.get("shot_id", ""),
                has_prev=has_prev and (prev_is_bridge or not self.parser.is_bridge_shot(s)),
                has_next=has_next and (next_is_bridge or not self.parser.is_bridge_shot(s)),
            )
        self.annotate_bridge_metadata(shots)

    def apply_grounding_safeguards(self, shots: List[Dict[str, Any]]) -> None:
        """v1.2：合并越界抑制 negative、保守参数校正、漂移/越界日志警告。"""
        safe = self.grounded_safe_mode()
        for s in shots:
            sid = s.get("shot_id", "")
            neg = s.get("negative_prompt") or DEFAULT_NEGATIVE
            s["negative_prompt"] = self._merge_negative_terms(neg, GROUNDING_NEGATIVE)

            prompt = s.get("prompt", "")
            LTXResponseParser.warn_grounding_violations(prompt, sid)

            idxs = list(s.get("image_idxs") or [])
            strengths = list(s.get("image_strengths") or [])
            n = len(s.get("image_files") or [])

            if safe and strengths:
                adjusted, changed = LTXResponseParser.clamp_image_strengths_safe(
                    strengths, LTX_MIN_IMAGE_STRENGTH, LTX_AGGRESSIVE_STRENGTH_THRESHOLD,
                )
                if changed:
                    logger.info(
                        "%s image_strengths %s -> %s（safe mode 防漂移抬升）",
                        sid, strengths, adjusted,
                    )
                s["image_strengths"] = adjusted

            if safe and n >= 2:
                LTXResponseParser.warn_image_idxs_drift(idxs, n, sid)

            s["grounded_safe_mode"] = safe

    def _grounding_and_safe_mode_directive(self) -> str:
        if not self.grounded_safe_mode():
            return (
                "## 创意边界（文档 §21，建议遵守）\n"
                "prompt 须基于 idea、workflow_summary 与参考帧，避免无依据的新人物/新道具/新结局。\n"
            )
        return (
            "## 弱模型安全模式（文档 §21–§23，**已启用，必须遵守**）\n"
            "规划前按 Step A→G 组织推理，并在 Shot Plan 表之前**显式输出**三块：\n"
            "1) **Allowed Story Range**（仅列出 idea/summary/参考帧中可见的剧情状态、人物、道具、场景）；\n"
            "2) **Forbidden Expansion List**（本 case 禁止写入 prompt 的无参考元素，"
            "如三头六臂、新法器、升空换场景、新敌人等）；\n"
            "3) **Prompt Evidence Review**（简表：关键 prompt 元素 → 证据来源 idea/summary/帧）。\n"
            "prompt 规则：\n"
            "- **证据写作**：不得加入 idea、summary、参考帧均未支撑的剧情/形态/道具/地点/结局；\n"
            "- **允许扩展**：连续动作细节、运镜、转场载体、光效余波、音效（须服务已有帧状态）；\n"
            "- **收束帧之后**（image_idxs 末帧之后 10%~25%）：只写余波/呼吸/光效减弱/镜稳，"
            "**禁止**新变身、新武器、飞升、换场景、额外高潮；\n"
            "- 正向 prompt 建议含保守句："
            "`The sequence stays within the story shown by the reference frames...`\n"
            "- negative_prompt 须含 §21.6 越界抑制项（代码会自动补全，你也应写入 case 相关项）。\n"
            "参数规则（§22）：\n"
            "- image_strength 默认 **0.90~1.00**（角色/道具敏感用 0.92~1.00）；"
            "**禁止**无说明地使用 0.75~0.85 激进组合；\n"
            "- 长 shot 首张关键场景锚点：image_idxs[0] 优先 **0.0~0.05**；\n"
            "- 末张参考帧：image_idxs 末位 **0.82~0.90**，不要 1.0；\n"
            "- 15s 长 shot 剧情容量：约 4~5 个状态、3~4 个转场、1 条主动作链、1 个收束，"
            "禁止塞入过多新事件。\n"
        )

    def _bridge_and_stitch_directive(self) -> str:
        mode = self.stitch_mode()
        mode_label = (
            "Direct Concatenation（直接拼接）"
            if mode == "direct_concat"
            else "Trim-and-Overlap（裁剪余量）"
        )
        bridge_secs = LTX_BRIDGE_DEFAULT_SECONDS
        if mode == "direct_concat":
            idx_rules = (
                "- **边界帧（direct_concat，当前模式）**：主 shot 尾锚 **0.88–0.95**、"
                "bridge 首 **0.00–0.08**、bridge 尾 **0.85–0.95**、"
                "下一主 shot 首 **0.00–0.08**（§13.1.3）；\n"
                "- 后期将**整段 mp4 按 export_playback_order 直接拼接**；\n"
            )
        else:
            idx_rules = (
                "- **边界帧（trim_overlap，当前模式）**：主 shot 尾锚 **0.70–0.82** 留剪辑 handle、"
                "bridge 首 **0.08–0.18**、bridge 尾 **0.75–0.88**（§13.2.3）；\n"
                "- **禁止**简单整段拼接 shot1+bridge+shot2，须在边界帧附近裁切后再接；\n"
            )
        return (
            f"## 跨 Shot 转场与拼接模式（文档 v1.5 §7.4 / §12 / §14，**当前：{mode_label}**）\n"
            "### 问题背景\n"
            "单个 shot 内部流畅 ≠ 全片流畅：相邻主 shot 直接硬切时，边界参考帧状态可能从未被生成，"
            "衔接会突兀。\n"
            "### Bridge Shot（v1.5：默认生成、导出可选）\n"
            "**只要拆成多个主 shot，就必须为每对相邻主 shot 规划一个 bridge shot candidate**；"
            "不再由你判断“是否需要 bridge”。\n"
            "bridge 是**可选转场资产**：若主 shot 直接拼接已足够顺，用户可跳过 bridge；"
            "若直接拼接突兀，则使用 bridge。\n"
            "- **参考帧**：bridge 仅用边界重叠对（如 frame04+frame05）；"
            "边界后的帧（如 frame05）仍须列入下一主 shot 的 `images`，"
            "不得被 bridge 独占。\n"
            "```\n"
            "Main Shot 1: frame01 → frame02 → frame03\n"
            "Bridge 1-2:   frame03 → frame04  （3–5s，明确转场载体）\n"
            "Main Shot 2: frame04 → frame05 → frame06\n"
            "```\n"
            f"- bridge 推荐时长 **3–{bridge_secs + 1}s**（常用 {bridge_secs}s）；\n"
            "- **硬规则**：bridge 必须包含明确**转场载体**（light-wipe / motion-wipe / "
            "action bridge / trajectory bridge / camera bridge / audio bridge），"
            "否则与直接拼接无本质区别（§7.5）；\n"
            "- bridge prompt 描述转场过程，不承担完整新剧情（§15）；\n"
            "- bridge 推荐 `image_strengths = [0.92, 0.95]`；\n"
            "- Shot Plan 表用 **Bridge 1-2** 命名，Shot Function 标明「跨 shot 转场」，"
            "并简述 transition carrier；\n"
            f"{idx_rules}"
            "### Shot Plan 表列（v1.5）\n"
            "| Shot | Reference Images | Duration | image_idxs | Shot Function | "
            "Stitch Mode | Transition Notes |\n"
            f"- 每行 Stitch Mode 填 `{mode}`；Transition Notes 写明转场载体；\n"
            "- python 块建议：`shot_type = \"bridge\"` / `is_bridge = True` / "
            "`bridges_between = \"shot_01_shot_02\"` / `transition_carrier = \"light wipe\"` / "
            "`can_skip_bridge = True`；\n"
            "### 总时长\n"
            "插入 bridge 后可选：A) 总时长放宽（主 shot 保持 + bridge 额外）；"
            "B) 总时长不变则适度压缩主 shot（§8.2）。\n"
        )

    def _semantic_action_chain_directive(self) -> str:
        return (
            "## 语义密度与动作链解析（文档 v1.7 §1–§9，**规划核心，优先于机械数帧**）\n"
            "### 核心升级\n"
            "v1.6 限制参考帧**数量**；v1.7 要求先解析每张帧的**剧情功能**与相邻帧**关系**，再决定 shot 拆分。\n"
            "```\n"
            "错误：12s 最多 4–5 帧 → 机械取连续 4–5 张\n"
            "正确：先建 semantic inventory + action chain → 再决定合并/拆 micro-bridge\n"
            "```\n"
            "### Step 1：Reference Frame Semantic Parser（§2）\n"
            "规划前为每帧输出 `frame_semantic_inventory`（JSON 数组），字段含：\n"
            "`frame`, `semantic_stage`, `action_role`, `motion_direction`, "
            "`risk_level`, `suggested_use`\n"
            "语义阶段类型：establishing / preparation / manifestation / positioning / "
            "contact / boarding / lift_off / acceleration / breakthrough / ascent / "
            "departure / reaction / aftermath\n"
            "### Step 2：Action Chain Graph（§3）\n"
            "输出 `frame_pair_relations`（JSON），每对相邻帧标注：\n"
            "`pair`, `relation_type`, `reason`, `recommended_handling`\n"
            "关系类型：same_motion_chain | semantic_stage_shift | high_risk_pair | "
            "camera_jump | environment_shift | closure_shift\n"
            "### Step 3：High-Risk Pair → Micro-Action Bridge（§6–§7）\n"
            "脚踩载具、手触道具、握住武器、离地起飞等 **high_risk_pair** 必须优先拆成 "
            "**3–4s micro-action bridge**（2 帧，`shot_type = \"micro_action_bridge\"`），"
            "不要塞进主 shot 尾部。\n"
            "御剑推荐结构：\n"
            "```\n"
            "Shot1: frame01–04（召唤+定位）\n"
            "Micro-Bridge: frame04→05（脚踩剑，overlap anchor 复用 frame04/05）\n"
            "Shot2: frame05–09（连续 flight chain，5 帧可接受因同一动作链）\n"
            "```\n"
            "### Step 4：Semantic Event Density（§4）\n"
            f"- `semantic_event_density = major_semantic_stages / video_seconds`；"
            f"过高（>{LTX_MAX_SEMANTIC_EVENT_DENSITY}）须拆 shot 或 micro-bridge；\n"
            "- **12s + 5 帧**：仅当属于 **同一连续动作链**（continuous_motion_chain）才可接受；\n"
            "- 若 5 帧含 4+ 独立 semantic stages → **reject**，须重拆。\n"
            "### Step 5：Prompt Motion-First（§10）\n"
            "- prompt 主容量留给**可见动作曲线、运镜、转场载体、音频**；\n"
            "- 安全约束句压缩为一行或放 negative_prompt；\n"
            "- **禁止**逐帧 then...then... 罗列；须写成一条连续 motion sequence。\n"
            "### Bridge 类型（§9）\n"
            "- `transition` bridge：跨主 shot 视觉转场（light/motion wipe）；\n"
            "- `micro_action` bridge：单一高风险动作对（physical_contact）；\n"
            "python 块字段：`bridge_subtype`, `bridges_between_frames`, `transition_carrier`, "
            "`risk_reason`, `continuous_motion_chain`, `semantic_stage_count`, "
            "`action_chain_group`\n"
        )

    def _shot_capacity_directive(self) -> str:
        n_frames = len(self.collect_reference_frames())
        target = self.orch.state.get("video_target_seconds") or ""
        return (
            "## 单 Shot 参考帧密度与容量（文档 v1.6 + v1.7 §4，**与语义链联合判断**）\n"
            "### 核心问题\n"
            "**禁止**把 shot 当作「参考帧容器」：在 12s shot 中塞入 6 张参考帧会导致每帧仅 ~2s，"
            "动作与转场无法展开，成片像 **PPT 轮播**。\n"
            "```\n"
            "错误示例：12s shot + 6 张参考帧 + image_idxs 密排 [0.0,0.12,0.28,0.45,0.62,0.82]\n"
            "正确思路：时长先决定容量，参考帧数量必须服从容量\n"
            "```\n"
            "### Shot Capacity Rule（§2.1）\n"
            "| shot 时长 | 推荐参考帧 | 可接受上限 | 禁止 |\n"
            "|---|---:|---:|---:|\n"
            "| 4–6s | 1–2 | 2 | 3+ |\n"
            "| 7–9s | 2–3 | 3 | 4+ |\n"
            "| **10–12s** | **3–4** | **5** | **6+** |\n"
            "| 13–15s | 4–5 | 6 | 7+ |\n"
            "| 16–20s | 5–6 | 7 | 8+ |\n"
            "| 20s+ | 6–8 | 8 | 9+ |\n"
            f"- **12s 主 shot 最稳 4 帧；5 帧仅当 continuous_motion_chain=true**（v1.7 §5）；"
            f"6 帧直接 reject（§2.2）。\n"
            f"- density = 参考帧数 / video_seconds；推荐 **≤ {LTX_DENSITY_COMFORT_MAX}**，"
            f"**> {LTX_MAX_REFERENCE_DENSITY} 必须减帧或拆 shot**（§3.3）。\n"
            "### 规划优先级（§5）\n"
            "```\n"
            "Shot readability & motion continuity > reference frame coverage density\n"
            "```\n"
            "宁可 12s 只用 4 张参考帧 + bridge 分担边界，也不要 12s 硬塞 6 帧。\n"
        ) + (
            f"### 本 case 提示\n"
            f"参考帧共 **{n_frames}** 张，目标总时长 **{target}s**。"
            f"若拆成 2×12s 主 shot，每段主 shot 应约 **4–5 张**参考帧，"
            f"勿让单个 shot 承担过半帧数；用 **bridge** 减轻主 shot 拥挤度（§7 / §10）。\n"
            if n_frames and target
            else ""
        ) + (
            "### Shot Capacity Review（v1.7 §13.3，**独立 JSON，勿混入 Shot Plan 表**）\n"
            "```json\n"
            '"shot_capacity_review": [\n'
            '  {"shot_id": "shot_01", "video_seconds": 12, "reference_frame_count": 4,\n'
            '   "semantic_stage_count": 3, "reference_frame_density": 0.33,\n'
            '   "continuous_motion_chain": false, "semantic_density_verdict": "ok"}\n'
            "]\n"
            "```\n"
            "### image_idxs 呼吸空间（§4）\n"
            "- 禁止在 12s shot 中把多帧均匀密排为 `[0.0, 0.20, 0.40, 0.60, 0.80, 0.95]`；\n"
            "- 减帧后拉大间距，如 4 帧 / 12s：`[0.00, 0.28, 0.58, 0.88]`（§11.1）；\n"
            "- prompt 动作节点数量须与 shot 时间容量匹配，禁止逐帧点名式 then...then...（§9）。\n"
        )

    def _transition_planning_directive(self) -> str:
        return (
            "## Shot 内部转场细节（文档 §12.4 / §16，**多参考帧 shot 必遵**）\n"
            "- **禁止**只写 `then cuts to`、`the scene transitions to`、`the camera transitions to` "
            "等空泛切镜；模型会随机转场或幻灯片式跳变。\n"
            "- 每两个相邻参考帧之间，必须在英文 prompt 里写清**画面内可见的转场事件**：\n"
            "  光效遮挡（light-wipe）、物体运动遮挡（motion wipe）、轨迹匹配（match cut / "
            "follow the beam）、缩放连续（push in / contract inward）、动作桥（同一动作的"
            "前后状态）、音频桥（雷声/嗡鸣延续到下一段）。\n"
            "- 转场写法模板：`[载体] + [方向/动作] + [如何填满/扫过镜头] + "
            "`transition into` + [下一机位/景别]`。\n"
            "- 示例（正确）：`The petals fold inward and a golden flare rises into the lens "
            "as a controlled light-wipe transition into a low-angle close-up of his face.`\n"
            "- 三帧及以上或 10s+ 长 shot：prompt 中须覆盖**每一段**相邻参考帧的转场；"
            "Shot Plan 表 **Transition Notes** 列用中文简述每段转场载体（如「莲心金光 light wipe」）。\n"
            "- 长 shot / 多参考帧的 negative_prompt 须包含："
            "`abrupt unrelated scene cut, uncontrolled random transition, slideshow effect, "
            "repeated still image, freeze frame, chaotic camera rotation`（可与基础模板合并）。\n"
            "- 检查清单（§12.4.5）：转场载体来自画面已有元素；有方向；说明到达的新机位；"
            "保留动作动量，避免角色突然换姿。\n"
        )

    def _duration_first_planning_directive(self) -> str:
        """用户给定单段时长时：时长是输入约束，驱动参考帧/image_idxs/prompt 规划。"""
        user_secs = self.user_long_shot_seconds()
        if user_secs is None:
            return ""
        target = self.orch.state.get("video_target_seconds")
        budget_line = ""
        if target:
            expected = self.expected_main_shot_count(float(target), user_secs)
            budget_line = (
                f"总预算 **{float(target):.0f}s = {expected} 个主 shot × {user_secs:.0f}s**"
                f"（bridge 3–5s 另计、不计入 {float(target):.0f}s）。\n"
            )
        return (
            "## 规划顺序（**时长是用户给定的输入约束，不是最后填的数字**）\n"
            f"{budget_line}"
            "必须严格按以下顺序规划；**禁止**先定参考帧分配、剧情、转场与 `image_idxs`，"
            "最后再改 `video_seconds` 凑数：\n"
            f"1) **先锁定**每个主 shot 的 `video_seconds = {user_secs:.0f}`（不可为 8s 等其他值）；\n"
            f"2) 在 {user_secs:.0f}s 预算下决定主 shot **数量**与每段容纳的参考帧（服从容量/语义规则）；\n"
            f"3) 在 **{user_secs:.0f}s 时间轴**上规划 `image_idxs`（各参考帧在镜头内的时刻）；\n"
            f"4) 写 **prompt**：描述该 {user_secs:.0f}s 内的连续动作、运镜与转场，与 image_idxs 对齐；\n"
            "5) 填写 Shot Plan 表（Duration / image_idxs / Transition Notes 须与 python 块一致）。\n"
            "**系统不会事后修改 `video_seconds`**；若时长与参考帧节奏不匹配，"
            "须回到步骤 2–4 重新规划，而非只改时长字段。\n"
        )

    def _image_idxs_planning_directive(self) -> str:
        user_secs = self.user_long_shot_seconds()
        if self.grounded_safe_mode():
            first_anchor = (
                "- **长 shot / 多参考（safe mode）**：首张关键场景锚点优先 **0.0~0.05**"
                "以锁定场景（§22.2）；末张参考 **0.82~0.90**，尾段只写余波。\n"
            )
        else:
            first_anchor = (
                "- **单帧 + 锚点靠近开场**：优先 **0.10~0.20**（如 **0.15**），"
                "避免 0.0 硬锁。\n"
            )
        duration_first = ""
        if user_secs is not None:
            duration_first = (
                f"- **用户已锁定主 shot 时长 = {user_secs:.0f}s**："
                f"`image_idxs` 必须在该 **{user_secs:.0f}s 时间轴**上规划"
                f"（先有时长 → 再落参考帧时刻），"
                f"**禁止**套用其他秒数模板后再改 `video_seconds`。\n"
                f"- 四帧/{user_secs:.0f}s 示例间距可参考 "
                f"`[0.00, 0.28, 0.58, 0.88]`，但须按本 shot 动作节奏在 {user_secs:.0f}s 内重新计算，"
                f"不得机械复制。\n"
            )
        else:
            duration_first = (
                "- 未给定单段时长时：先根据剧情与参考帧数量决定 `video_seconds`，"
                "再在同一时间轴上规划 `image_idxs`。\n"
            )
        return (
            "## image_idxs 规划原则（**每个 shot 必须由你单独决定**，禁止统一套用固定值）\n"
            f"{duration_first}"
            "- `image_idxs` 使用 **0~1 的小数**表示参考图落在该 shot **已确定时长**时间轴上的位置，"
            "**不是**帧序号。\n"
            "- 参考图前后留出动作与运镜空间；避免无必要的 `[0.0, 1.0]` 首尾双锁。\n"
            f"{first_anchor}"
            "- **单帧 + 情绪/反应/收束**：常用 **0.30~0.45**。\n"
            "- **双帧连续动作**：如 `[0.08, 0.78]` 或 `[0.10, 0.72]`；"
            "三帧：`[0.10, 0.48, 0.82]`。\n"
            "- 参考帧数量须先服从 v1.6 Shot Capacity Rule，再决定 image_idxs 间距。\n"
            "- Shot Plan 表须说明每 shot 参考帧数量、容量理由与 **image_idxs 理由**。\n"
        )

    def _long_shot_directive(self) -> str:
        if not self.orch.state.get("long_shot_mode"):
            return (
                "## 分镜模式：标准多 shot\n"
                "将参考帧拆成多个独立 shot，每个 shot 使用单帧或少量多帧参考；"
                "通过剪辑拼接达到目标总时长。\n"
            )
        user_secs = self.user_long_shot_seconds()
        target = self.orch.state.get("video_target_seconds")
        if user_secs is not None:
            if target:
                expected = max(1, round(float(target) / user_secs))
                duration_rule = (
                    f"**用户已指定每段主 shot 时长为 {user_secs:.0f} 秒**"
                    f"（不得超过 {LTX_MAX_SHOT_SECONDS} 秒）。"
                    f"目标总时长 {target} 秒时，须规划 **恰好 {expected} 个主 shot**"
                    f"（每段 {user_secs:.0f}s，**之和 = {target}s**；"
                    f"bridge 3–5s **不计入** {target}s）。"
                    f"**禁止**新增第 {expected + 1} 个短主 shot（如 8s）；"
                    f"须先锁定 `video_seconds={user_secs:.0f}`，再据此规划参考帧、"
                    f"`image_idxs` 与 prompt；**系统不会事后修改时长**。\n"
                )
            else:
                duration_rule = (
                    f"**用户已指定每段主 shot 时长为 {user_secs:.0f} 秒**"
                    f"（不得超过 {LTX_MAX_SHOT_SECONDS} 秒）。"
                    f"每个主 shot 须**先**锁定 `video_seconds = {user_secs:.0f}`，"
                    f"再规划参考帧与 `image_idxs`；**系统不会事后修改时长**。\n"
                )
            sum_rule = (
                f"**硬性时长预算**：所有**主 shot** 的 `video_seconds` **之和必须等于 {target}s**；"
                f"bridge 3–5s **不计入**该 {target}s。"
                f"禁止新增额外短主 shot（如 8s）突破预算；多出来的剧情须合并进 "
                f"{user_secs:.0f}s 主 shot 或由 bridge 承担转场。\n"
            )
        else:
            duration_rule = (
                "**用户未指定单段时长**——每个主 shot 的 `video_seconds` 必须由你根据"
                "剧情节奏、参考帧数量、动作连续性与目标总时长**自行规划**"
                f"（单段建议 8~{LTX_MAX_SHOT_SECONDS} 秒整数，并在 Shot Plan 表的 "
                "Duration 列写明数值与「选择理由」）。"
                "**禁止**机械套用固定默认秒数；系统不会事后覆盖你的规划。\n"
            )
            sum_rule = "各主 shot 的 video_seconds 之和应接近用户指定的目标总时长。\n"
        return (
            "## 分镜模式：长 shot 多参考帧（用户已开启）\n"
            f"{duration_rule}"
            "在每个 shot 内用多张参考帧 + image_idxs 分布实现镜头内分镜切换；"
            "减少硬切数量，让 LTX 在单段内完成动作与机位过渡。\n"
            "**每个多参考帧 shot 的 prompt 必须按 §12.4 写清相邻帧之间的转场载体与路径**，"
            "不得依赖模型随机转场。\n"
            f"{sum_rule}"
        )

    def _extract_shot_plan_excerpt(self, plan_response: str, max_chars: int = 12000) -> str:
        """截取 Shot Plan 表附近文本，供补全 python 块重试使用。"""
        markers = (
            r"#\s*\*?\*?Shot Plan",
            r"Shot Plan Markdown",
            r"\|\s*Shot\s*\|",
        )
        start = 0
        for pat in markers:
            m = re.search(pat, plan_response, re.IGNORECASE)
            if m:
                start = m.start()
                break
        return plan_response[start : start + max_chars]

    async def _request_ltx_python_blocks_retry(
        self,
        plan_response: str,
        frame_map: Dict[str, str],
        target_seconds: float,
    ) -> str:
        """VLM 只给了 Markdown 表时，二次请求仅补 ```python``` shot 参数块。"""
        table_rows = self.parser.parse_shot_plan_table(plan_response)
        excerpt = self._extract_shot_plan_excerpt(plan_response)
        frame_lines = "\n".join(
            f'- {name}: "{path}"' for name, path in frame_map.items()
        )
        shot_hint = ""
        if table_rows:
            shot_hint = "\n".join(
                f"- {row.get('shot', i+1)}: refs={row.get('reference_images','')}, "
                f"duration={row.get('duration','')}, idxs={row.get('image_idxs','')}"
                for i, row in enumerate(table_rows)
            )
        user_secs = self.user_long_shot_seconds()
        duration_line = (
            f"目标总时长约 {target_seconds} 秒（仅计主 shot，bridge 另计）。\n"
        )
        if user_secs is not None:
            expected = self.expected_main_shot_count(target_seconds, user_secs)
            duration_line = (
                f"用户已锁定每段主 shot **{user_secs:.0f}s**，共 **{expected}** 段，"
                f"之和 = **{target_seconds:.0f}s**；须先有时长再写 image_idxs/prompt。\n"
            )
        retry_text = (
            "上一轮 LTX 规划**缺少可解析的 ```python``` shot 参数块**（仅有 Markdown 表不够）。\n"
            "请**只**输出每个 shot 一个 ```python``` 代码块，不要重复 Inventory、"
            "Forbidden List、长表或解释性散文。\n\n"
            f"{duration_line}\n"
            f"可用参考帧（images 必须用下列绝对路径）：\n{frame_lines}\n\n"
        )
        if shot_hint:
            retry_text += f"已规划的 Shot 摘要（按此逐条输出 python 块）：\n{shot_hint}\n\n"
        if excerpt.strip():
            retry_text += f"Shot Plan 节选：\n{excerpt}\n\n"
        example_secs = user_secs if user_secs is not None else 12.0
        retry_text += (
            "每个 ```python``` 块必须严格包含：\n"
            "images = [绝对路径, ...]\n"
            "image_idxs = [0.0, ...]\n"
            "image_strengths = [0.95, ...]\n"
            f"video_seconds = {example_secs:.0f}\n"
            'prompt = """英文 4-8 句，含 §12.4 转场"""\n'
            'negative_prompt = """转场抑制 + 越界抑制"""\n'
        )
        print("  [LTX] 未解析到 python 块，向 VLM 发起补全请求（仅 shot 参数）...")
        return await self.orch.vlm.chat_without_history(
            text=retry_text,
            temperature=0.3,
            max_tokens=16384,
        )

    async def _request_ltx_long_shot_constraint_retry(
        self,
        plan_response: str,
        issues: List[str],
        frame_map: Dict[str, str],
        target_seconds: float,
        user_secs: float,
    ) -> str:
        """用户指定单段时长但 VLM 规划不符时，请求其修订完整 Shot Plan + python 块。"""
        excerpt = self._extract_shot_plan_excerpt(plan_response)
        frame_lines = "\n".join(
            f'- {name}: "{path}"' for name, path in frame_map.items()
        )
        issue_lines = "\n".join(f"- {x}" for x in issues)
        frames_dir_placeholder = self.output_dir
        expected = self.expected_main_shot_count(target_seconds, user_secs)
        duration_hint = (
            f"- **硬性时长预算**：目标 {target_seconds:.0f}s = **恰好 {expected} 个主 shot "
            f"× {user_secs:.0f}s**（bridge 3–5s 另计、不计入 {target_seconds:.0f}s）；\n"
            f"- **禁止** 新增第 {expected + 1} 个主 shot（如 8s 尾段）；须把 frame 合并进 "
            f"现有 {user_secs:.0f}s 主 shot 或用 bridge 过渡；\n"
        )
        retry_text = (
            f"上一轮 LTX 规划**未满足用户给定的时长约束**"
            f"（单段主 shot {user_secs:.0f}s，总预算 {target_seconds:.0f}s 仅计主 shot）。\n\n"
            "检测到的问题：\n"
            f"{issue_lines}\n\n"
            "请**以时长为输入约束，从头重新规划**（Reference Inventory 可简写、Shot Plan 表、"
            "每个 shot 含 bridge 的 ```python``` 参数块），**禁止**保留原参考帧/image_idxs/prompt "
            "仅把 `video_seconds` 改成目标数字。\n"
            "修订顺序：\n"
            f"1) 锁定 {expected} 个主 shot，各 `video_seconds = {user_secs:.0f}`，之和 = {target_seconds:.0f}；\n"
            f"2) 在 {user_secs:.0f}s 内重新分配参考帧与 `image_idxs`；\n"
            "3) 重写 prompt 以匹配此时长内的动作节奏；\n"
            "4) bridge 另计 3–5s。\n\n"
            f"{duration_hint}"
            f"{self._duration_first_planning_directive()}\n"
            f"{self._long_shot_directive()}\n"
            f"可用参考帧（images 必须用下列绝对路径）：\n{frame_lines}\n\n"
            f"images 路径格式：f\"{frames_dir_placeholder}/case_final_frame01.png\"\n"
        )
        if excerpt.strip():
            retry_text += f"上一轮 Shot Plan 节选（供对照修订）：\n{excerpt}\n\n"
        print(
            f"  [LTX] 规划未满足用户时长约束（须以 {user_secs:.0f}s 为先验重新规划 shot），"
            "向 VLM 请求修订…"
        )
        return await self.orch.vlm.chat_without_history(
            text=retry_text,
            image_paths=list(frame_map.values()),
            temperature=0.4,
            max_tokens=16384,
        )

    async def _ensure_long_shot_duration_via_vlm(
        self,
        response: str,
        shots: List[Dict[str, Any]],
        frame_map: Dict[str, str],
        target: float,
        *,
        revision_title: str = "以时长为先验重新规划 shot",
    ) -> tuple[str, List[Dict[str, Any]], List[str]]:
        """用户指定单段时长时，请求 VLM 修订直至主 shot 之和等于目标（bridge 不计）。"""
        user_secs = self.user_long_shot_seconds()
        if user_secs is None:
            return response, shots, []
        issues = self.collect_long_shot_plan_issues(shots)
        if not issues:
            return response, shots, []
        constraint_retry = await self._request_ltx_long_shot_constraint_retry(
            response, issues, frame_map, float(target), user_secs,
        )
        print(constraint_retry[:2000])
        if len(constraint_retry) > 2000:
            print(f"... 时长修订回复共 {len(constraint_retry)} 字符")
        retry_shots = self.parser.parse_ltx_plan_response(
            constraint_retry, self.output_dir, default_negative=DEFAULT_NEGATIVE,
        )
        if not retry_shots:
            return response, shots, issues
        retry_shots = self._prepare_vlm_plan_shots(constraint_retry, retry_shots)
        retry_issues = self.collect_long_shot_plan_issues(retry_shots)
        if not retry_issues or len(retry_issues) < len(issues):
            shots = retry_shots
            response = (
                response
                + f"\n\n---\n## VLM 修订：{revision_title}\n\n"
                + constraint_retry
            )
            issues = retry_issues
        return response, shots, issues

    async def _request_ltx_frame_partition_retry(
        self,
        plan_response: str,
        issues: List[str],
        frame_map: Dict[str, str],
        total_frames: int,
    ) -> str:
        """主 shot 参考帧切分不连续时，请求 VLM 按 frame 顺序重新分配。"""
        excerpt = self._extract_shot_plan_excerpt(plan_response)
        frame_lines = "\n".join(
            f'- {name}: "{path}"' for name, path in frame_map.items()
        )
        issue_lines = "\n".join(f"- {x}" for x in issues)
        frames_dir_placeholder = self.output_dir
        retry_text = (
            "上一轮 LTX 规划**参考帧在主 shot 之间切分不正确**。\n\n"
            "检测到的问题：\n"
            f"{issue_lines}\n\n"
            "请**重新输出完整规划**（Shot Plan 表 + 每个 shot 的 ```python``` 块），要求：\n"
            f"- 共 {total_frames} 帧 frame01–frame{total_frames:02d}，"
            "**各主 shot 连续覆盖**，合起来不遗漏任何帧；\n"
            "- 若 shot_01 使用 frame01–04，则 shot_02 的 `images` **必须从 frame05 起**"
            f"包含 frame05–frame{total_frames:02d}（可含 5–9 全部），"
            "**禁止** shot_02 从 frame06 开始而把 frame05 只留给 bridge；\n"
            "- bridge 可用 frame04+frame05 做过渡锚点，但 frame05 仍须出现在 shot_02 的 "
            "`images` 中；\n"
            "- 同步调整各 shot 的 `image_idxs` 与 prompt。\n\n"
            f"{self._reference_frame_partition_directive(total_frames)}\n"
            f"{self._duration_first_planning_directive()}\n"
            f"可用参考帧：\n{frame_lines}\n\n"
            f"images 路径：f\"{frames_dir_placeholder}/case_final_frame01.png\"\n"
        )
        if excerpt.strip():
            retry_text += f"上一轮 Shot Plan 节选：\n{excerpt}\n\n"
        print("  [LTX] 主 shot 参考帧切分不连续，向 VLM 请求按帧序重新分配…")
        return await self.orch.vlm.chat_without_history(
            text=retry_text,
            image_paths=list(frame_map.values()),
            temperature=0.4,
            max_tokens=16384,
        )

    async def _ensure_frame_partition_via_vlm(
        self,
        response: str,
        shots: List[Dict[str, Any]],
        frame_map: Dict[str, str],
        total_frames: int,
        *,
        revision_title: str = "参考帧连续切分",
    ) -> tuple[str, List[Dict[str, Any]], List[str]]:
        issues = self.collect_reference_frame_partition_issues(
            shots, total_frames,
        )
        if not issues:
            return response, shots, []
        partition_retry = await self._request_ltx_frame_partition_retry(
            response, issues, frame_map, total_frames,
        )
        print(partition_retry[:2000])
        if len(partition_retry) > 2000:
            print(f"... 帧切分修订回复共 {len(partition_retry)} 字符")
        retry_shots = self.parser.parse_ltx_plan_response(
            partition_retry, self.output_dir, default_negative=DEFAULT_NEGATIVE,
        )
        if not retry_shots:
            return response, shots, issues
        retry_shots = self._prepare_vlm_plan_shots(partition_retry, retry_shots)
        retry_issues = self.collect_reference_frame_partition_issues(
            retry_shots, total_frames,
        )
        if not retry_issues or len(retry_issues) < len(issues):
            shots = retry_shots
            response = (
                response
                + f"\n\n---\n## VLM 修订：{revision_title}\n\n"
                + partition_retry
            )
            issues = retry_issues
        return response, shots, issues

    async def _request_ltx_capacity_retry(
        self,
        plan_response: str,
        issues: List[str],
        frame_map: Dict[str, str],
        target_seconds: float,
    ) -> str:
        """v1.6：参考帧过密时请求 VLM 减帧/拆 shot 并重写规划。"""
        excerpt = self._extract_shot_plan_excerpt(plan_response)
        frame_lines = "\n".join(
            f'- {name}: "{path}"' for name, path in frame_map.items()
        )
        issue_lines = "\n".join(f"- {x}" for x in issues)
        frames_dir_placeholder = self.output_dir
        user_secs = self.user_long_shot_seconds()
        duration_hint = ""
        if user_secs is not None:
            expected = self.expected_main_shot_count(target_seconds, user_secs)
            duration_hint = (
                f"- **硬性时长预算**：{target_seconds:.0f}s = **{expected} 个主 shot × "
                f"{user_secs:.0f}s**（bridge 另计）；减帧时**不得**新增第 {expected + 1} 个主 shot；\n"
            )
        retry_text = (
            "上一轮 LTX 规划**违反 v1.6 单 Shot 参考帧容量规则**，成片易产生 PPT/幻灯片感。\n\n"
            "检测到的问题：\n"
            f"{issue_lines}\n\n"
            "请**重新输出完整规划**（Reference Inventory 可简写、**Shot Capacity Review 表**、"
            "Shot Plan 表、每个 shot 含 bridge 的 ```python``` 参数块），要求：\n"
            "- **减少过密主 shot 的参考帧数量**，或把内容拆到更多主 shot / bridge；\n"
            f"- 12s 主 shot **禁止** 6+ 张参考帧；density 不得超过 {LTX_MAX_REFERENCE_DENSITY}；\n"
            "- 同步调整 `image_idxs` 间距（留出动作/转场呼吸空间）、`image_strengths`、"
            "prompt（写 motion sequence 而非逐帧轮播）；\n"
            f"{duration_hint}"
            "- **禁止**只改表内数字而不改 python 块。\n\n"
            f"{self._shot_capacity_directive()}\n"
            f"{self._bridge_and_stitch_directive()}\n"
            f"可用参考帧（images 必须用下列绝对路径）：\n{frame_lines}\n\n"
            f"images 路径格式：f\"{frames_dir_placeholder}/case_final_frame01.png\"\n"
        )
        if excerpt.strip():
            retry_text += f"上一轮 Shot Plan 节选（供对照修订）：\n{excerpt}\n\n"
        print(
            "  [LTX] 规划参考帧过密（v1.6），向 VLM 请求减帧/拆 shot 修订…"
        )
        return await self.orch.vlm.chat_without_history(
            text=retry_text,
            image_paths=list(frame_map.values()),
            temperature=0.4,
            max_tokens=16384,
        )

    async def _request_ltx_semantic_retry(
        self,
        plan_response: str,
        issues: List[str],
        frame_map: Dict[str, str],
        target_seconds: float,
    ) -> str:
        """v1.7：语义/动作链规划不合格时请求 VLM 重写（含 v1.6 容量约束）。"""
        excerpt = self._extract_shot_plan_excerpt(plan_response)
        frame_lines = "\n".join(
            f'- {name}: "{path}"' for name, path in frame_map.items()
        )
        issue_lines = "\n".join(f"- {x}" for x in issues)
        frames_dir_placeholder = self.output_dir
        user_secs = self.user_long_shot_seconds()
        duration_hint = ""
        if user_secs is not None:
            expected = self.expected_main_shot_count(target_seconds, user_secs)
            duration_hint = (
                f"- **硬性时长预算（语义修订不得突破）**：{target_seconds:.0f}s = "
                f"**恰好 {expected} 个主 shot × {user_secs:.0f}s**；bridge 另计；\n"
                f"- **禁止**为修语义新增第 {expected + 1} 个主 shot；"
                f"须**先**锁定 {user_secs:.0f}s，再在该时间轴内合并 frame / 写 image_idxs / prompt；\n"
                f"- **禁止**保留原剧情切分仅改 `video_seconds` 数字。\n"
            )
        retry_text = (
            "上一轮 LTX 规划**未通过 v1.7 语义动作链校验**"
            "（可能参考帧机械切分、缺少 micro-bridge、或语义阶段过密）。\n\n"
            "检测到的问题：\n"
            f"{issue_lines}\n\n"
            "请**重新输出完整规划**，必须包含：\n"
            "1) `frame_semantic_inventory` JSON 数组（每帧 semantic_stage / risk_level）；\n"
            "2) `frame_pair_relations` JSON 数组（相邻帧 relation_type / recommended_handling）；\n"
            "3) `shot_capacity_review` JSON（含 semantic_stage_count / continuous_motion_chain）；\n"
            "4) Shot Plan 表 + 每个 shot 的 ```python``` 参数块；\n"
            "5) **high_risk_pair**（脚踩剑/手触道具等）须拆 **micro_action_bridge**（3–4s，2 帧）；\n"
            "6) 12s + 5 帧仅当 continuous_motion_chain=true；prompt 写连续动作曲线。\n"
            f"{duration_hint}"
            "- **禁止**只改表不改 python 块。\n\n"
            f"{self._semantic_action_chain_directive()}\n"
            f"{self._shot_capacity_directive()}\n"
            f"{self._bridge_and_stitch_directive()}\n"
            f"可用参考帧：\n{frame_lines}\n\n"
            f"images 路径：f\"{frames_dir_placeholder}/case_final_frame01.png\"\n"
        )
        if excerpt.strip():
            retry_text += f"上一轮 Shot Plan 节选：\n{excerpt}\n\n"
        print("  [LTX] 规划未通过 v1.7 语义动作链校验，向 VLM 请求重写…")
        return await self.orch.vlm.chat_without_history(
            text=retry_text,
            image_paths=list(frame_map.values()),
            temperature=0.4,
            max_tokens=16384,
        )

    def _prepare_vlm_plan_shots(
        self,
        plan_response: str,
        shots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """合并 Shot 表并做规划期 safeguard（不改 video_seconds）。"""
        table_rows = self.parser.parse_shot_plan_table(plan_response)
        if table_rows:
            shots = self.parser.merge_shots_with_table(shots, table_rows)
        self.apply_multi_ref_transition_defaults(shots)
        self.apply_grounding_safeguards(shots)
        return shots

    def _resolve_shot_image_paths(self, shots: List[Dict[str, Any]]) -> None:
        for s in shots:
            resolved = []
            for fname in s.get("image_files", []):
                p = os.path.join(self.output_dir, fname)
                if not os.path.isfile(p):
                    for alt in (
                        fname.replace("final", "edit"),
                        fname.replace("final", "base"),
                    ):
                        ap = os.path.join(self.output_dir, alt)
                        if os.path.isfile(ap):
                            p = ap
                            break
                resolved.append(p)
            s["image_paths"] = resolved

    def _finalize_ltx_plan_shots(
        self, shots: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """bridge 去重/补齐、容量/拼接 safeguard、命名与参考图路径。"""
        shots = self.collapse_duplicate_bridge_shots(shots)
        shots = self.ensure_bridge_candidates(shots)
        self.apply_bridge_stitch_safeguards(shots)
        self.assign_shot_ids_and_paths(shots)
        self._resolve_shot_image_paths(shots)
        return shots

    async def step_ltx_plan(self, idea: str) -> None:
        target = self.orch.state.get("video_target_seconds")
        if not target:
            n_frames = len(self.collect_reference_frames())
            target = max(n_frames * 6, 20)
            self.orch.state["video_target_seconds"] = target

        ls_mode = self.orch.state.get("long_shot_mode")
        ls_secs = self.orch.state.get("long_shot_seconds")
        safe_hint = "开" if self.grounded_safe_mode() else "关"
        stitch = self.stitch_mode()
        stitch_label = (
            "直接拼接" if stitch == "direct_concat" else "裁剪余量"
        )
        if ls_mode and ls_secs is None:
            ls_hint = "长 shot=开（每段时长由 VLM 规划）"
        elif ls_mode:
            ls_hint = f"长 shot=开（用户指定每段约 {ls_secs}s）"
        else:
            ls_hint = "长 shot=关"
        print(
            f"\n[Step 6/7] LTX Shot 规划（目标总时长 {target}s，{ls_hint}，"
            f"拼接模式={stitch_label}，bridge=默认生成/导出可选，"
            f"证据约束 safe mode={safe_hint}）..."
        )

        frame_map = self.collect_reference_frames()
        frame_paths = list(frame_map.values())

        if not os.path.exists(LTX_WORKFLOW_DOC_PATH):
            raise FileNotFoundError(f"LTX 工作流文档不存在: {LTX_WORKFLOW_DOC_PATH}")

        with open(LTX_WORKFLOW_DOC_PATH, "r", encoding="utf-8") as f:
            ltx_doc = f.read()

        self.orch.vlm.set_system_prompt(ltx_doc)

        frames_dir_placeholder = self.output_dir
        user_secs_prompt = self.user_long_shot_seconds()
        if user_secs_prompt is not None:
            expected = self.expected_main_shot_count(float(target), user_secs_prompt)
            duration_note = (
                f"**目标总时长：{target} 秒（仅统计主 shot，bridge 不计）**。"
                f"用户指定每段主 shot **{user_secs_prompt:.0f}s**（**先验约束**）→ "
                f"应规划 **恰好 {expected} 个主 shot**，每段 {user_secs_prompt:.0f}s，**之和 = {target}s**。"
                f"须先锁定时长，再规划参考帧、`image_idxs`、剧情与 prompt；"
                f"bridge 3–5s 另计。**系统不会事后修改 video_seconds**。\n\n"
            )
        else:
            duration_note = (
                f"**目标总时长：{target} 秒**（各主 shot 的 video_seconds 之和应接近该值，"
                f"允许 ±2 秒误差；bridge 另计。）\n\n"
            )
        user_text = (
            f"请为以下项目规划 LTX 音视频 shot，并输出可执行的参数。\n\n"
            f"{self._build_inventory_text(frame_map)}\n"
            f"{duration_note}"
            f"{self._duration_first_planning_directive()}"
            f"{self._reference_frame_partition_directive(len(frame_map))}"
            f"{self._grounding_and_safe_mode_directive()}\n"
            f"{self._semantic_action_chain_directive()}\n"
            f"{self._shot_capacity_directive()}\n"
            f"{self._bridge_and_stitch_directive()}\n"
            f"{self._long_shot_directive()}\n"
            f"{self._image_idxs_planning_directive()}\n"
            f"{self._transition_planning_directive()}\n"
            f"**全局拼接模式 stitch_mode = `{stitch}`**（所有 shot 默认遵循此模式）。\n"
            f"**Bridge 策略（v1.5）**：`generate_bridge_candidates=True` — "
            "多主 shot 时**必须为每对相邻主 shot 生成 bridge candidate**，"
            "不得省略；bridge 含明确转场载体。最终导出是否使用 bridge 由用户决定"
            f"（当前默认 use_bridge_at_export={self.use_bridge_at_export()}）。\n\n"
            "硬性要求：\n"
            "1) 先输出 Reference Frame Inventory（含每帧 Story State / Camera / "
            "Motion Potential，须与参考帧画面一致，不得臆造）；\n"
            "   并输出 **frame_semantic_inventory** 与 **frame_pair_relations** JSON（v1.7 §13）；\n"
        )
        if self.grounded_safe_mode():
            user_text += (
                "2) 输出 Allowed Story Range、Forbidden Expansion List、"
                "Prompt Evidence Review（§21.3 / §23.1）；\n"
                "3) 输出 Shot Plan Markdown 表（列：Shot / Reference Images / Duration / "
            )
        else:
            user_text += (
                "2) 输出 Shot Plan Markdown 表（列：Shot / Reference Images / Duration / "
            )
        user_text += (
            "image_idxs / Shot Function / Stitch Mode / Transition Notes）；\n"
            "   多主 shot 时**必须**为相邻主 shot 规划 bridge candidate（默认生成，非可选规划项）；"
            "bridge 的 Transition Notes 须写明转场载体；Stitch Mode 列填 "
            f"`{stitch}`；Transition Notes **每条不超过 80 字**；\n"
            "4) **【必须、不可省略】每个 shot（含 bridge candidate）单独一个** ```python``` 代码块，"
            "变量名固定：images, image_idxs, image_strengths, video_seconds, "
            "prompt, negative_prompt；bridge 块另加 shot_type / bridge_subtype / "
            "transition_carrier / bridges_between_frames；主 shot 可加 continuous_motion_chain、"
            "semantic_stage_count、action_chain_group；"
            "**仅有 Markdown 表而无 python 块视为不合格**；\n"
            f"5) images 使用绝对路径：f\"{frames_dir_placeholder}/case_final_frame01.png\"；\n"
            "6) prompt 英文单段 4-8 句；不得包含无参考支撑的新剧情/形态/道具/结局；\n"
            "7) ≥2 参考图须写 §12.4 转场；收束帧之后只写余波（§22.4）；\n"
            "8) keyframe_interpolation 语义；negative 含转场 + 越界抑制；\n"
            "9) 不要输出无法解析的伪代码；**先写短表，再立刻写全部 python 块**。\n"
        )
        if user_secs_prompt is not None:
            user_text += (
                f"10) **时长先验**：每个主 shot 的 `video_seconds` 已锁定为 {user_secs_prompt:.0f}s；"
                f"`image_idxs` 与 prompt 必须按该时长规划，不得先按其他秒数设计再改时长。\n"
            )
        if idea:
            user_text = f"补充 idea：{idea}\n\n" + user_text

        response = await self.orch.vlm.chat_without_history(
            text=user_text,
            image_paths=frame_paths,
            temperature=0.5,
            max_tokens=16384,
        )

        self.orch._log("ltx_plan", {"response_preview": response[:2000]})
        print(response[:2500])
        if len(response) > 2500:
            print(f"... (共 {len(response)} 字符)")

        shots = self.parser.parse_ltx_plan_response(
            response, self.output_dir, default_negative=DEFAULT_NEGATIVE,
        )
        if not shots:
            retry_response = await self._request_ltx_python_blocks_retry(
                response, frame_map, float(target),
            )
            print(retry_response[:2000])
            if len(retry_response) > 2000:
                print(f"... 补全回复共 {len(retry_response)} 字符")
            shots = self.parser.parse_ltx_plan_response(
                retry_response, self.output_dir, default_negative=DEFAULT_NEGATIVE,
            )
            if shots:
                response = (
                    response
                    + "\n\n---\n## VLM 补全：shot ```python``` 参数块\n\n"
                    + retry_response
                )
        if not shots:
            table_rows = self.parser.parse_shot_plan_table(response)
            hint = (
                f"（已解析到 {len(table_rows)} 行 Shot 表，但无有效 python 块）"
                if table_rows
                else "（Shot 表与 python 块均未解析成功）"
            )
            raise RuntimeError(
                "未能从 LTX 规划回复中解析出任何 ```python``` shot 参数块。"
                f"请检查 VLM 输出格式是否符合文档 §15。{hint}"
            )

        shots = self._prepare_vlm_plan_shots(response, shots)
        user_secs = self.user_long_shot_seconds()
        response, shots, issues = await self._ensure_long_shot_duration_via_vlm(
            response, shots, frame_map, float(target),
            revision_title="以时长为先验重新规划 shot",
        )
        if issues:
            self.validate_long_shot_plan(shots)
            if user_secs is not None:
                print(
                    "  警告：规划仍与用户主 shot 时长预算不一致，"
                    "将使用当前 VLM 输出继续（未事后改 video_seconds）"
                )
        semantic_rejects = self.parser.collect_semantic_planning_issues(
            response, shots,
        )
        if semantic_rejects:
            semantic_retry = await self._request_ltx_semantic_retry(
                response, semantic_rejects, frame_map, float(target),
            )
            print(semantic_retry[:2000])
            if len(semantic_retry) > 2000:
                print(f"... 语义修订回复共 {len(semantic_retry)} 字符")
            retry_shots = self.parser.parse_ltx_plan_response(
                semantic_retry, self.output_dir, default_negative=DEFAULT_NEGATIVE,
            )
            if retry_shots:
                retry_shots = self._prepare_vlm_plan_shots(
                    semantic_retry, retry_shots,
                )
                retry_rejects = self.parser.collect_semantic_planning_issues(
                    semantic_retry, retry_shots,
                )
                if not retry_rejects or len(retry_rejects) < len(semantic_rejects):
                    shots = retry_shots
                    response = (
                        response
                        + "\n\n---\n## VLM 修订：v1.7 语义动作链优化\n\n"
                        + semantic_retry
                    )
                    semantic_rejects = retry_rejects
        if semantic_rejects:
            self.validate_semantic_plan(response, shots)
            print(
                "  警告：规划仍未通过语义动作链校验，将使用当前 VLM 输出继续"
                "（建议 --reset-ltx 后重跑规划）"
            )
        response, shots, issues = await self._ensure_long_shot_duration_via_vlm(
            response, shots, frame_map, float(target),
            revision_title="语义修订后按时长先验重规划",
        )
        if issues:
            self.validate_long_shot_plan(shots)
            if user_secs is not None:
                print(
                    "  警告：语义修订后主 shot 时长之和仍不等于目标，"
                    "将使用当前 VLM 输出继续（建议 --reset-ltx 重跑）"
                )
        n_frames = len(frame_map)
        response, shots, partition_issues = await self._ensure_frame_partition_via_vlm(
            response, shots, frame_map, n_frames,
            revision_title="参考帧连续切分",
        )
        if partition_issues:
            self.validate_reference_frame_partition(shots, n_frames)
            print(
                "  警告：主 shot 参考帧仍未连续覆盖全部帧，"
                "将使用当前 VLM 输出继续（建议 --reset-ltx 重跑）"
            )
        shots = self._finalize_ltx_plan_shots(shots)
        semantic_meta = self.apply_semantic_action_safeguards(response, shots)
        self.orch.state["ltx_frame_semantic_inventory"] = semantic_meta.get(
            "frame_semantic_inventory", [],
        )
        self.orch.state["ltx_frame_pair_relations"] = semantic_meta.get(
            "frame_pair_relations", [],
        )
        self.orch.state["ltx_shot_capacity_review"] = semantic_meta.get(
            "shot_capacity_review", [],
        )

        self.orch.state["ltx_plan_response"] = response
        self.orch.state["ltx_shots"] = shots
        self.orch.state["ltx_shots_done"] = []
        self.orch.state["ltx_planned_long_shot_seconds"] = (
            self.orch.state.get("long_shot_seconds")
        )
        self.orch.state["ltx_planned_stitch_mode"] = self.stitch_mode()
        self.orch.state["ltx_planned_generate_bridge"] = self.generate_bridge_candidates()
        self.orch.state["ltx_planned_use_bridge_at_export"] = self.use_bridge_at_export()
        self._save_shot_plan(response, shots)
        bridge_n = sum(1 for s in shots if self.parser.is_bridge_shot(s))
        main_n = sum(1 for s in shots if not self.parser.is_bridge_shot(s))
        micro_n = sum(
            1 for s in shots
            if s.get("shot_type") == "micro_action_bridge"
            or s.get("bridge_subtype") in ("micro_action", "physical_contact")
        )
        if bridge_n:
            micro_txt = f"（含 {micro_n} 个 micro-action bridge）" if micro_n else ""
            print(
                f"  含 {main_n} 个主 shot + {bridge_n} 个 bridge candidate{micro_txt}，"
                f"拼接模式：{stitch_label}（{stitch}）；"
                f"导出{'含' if self.use_bridge_at_export() else '不含'} bridge"
            )
        elif main_n > 1 and self.generate_bridge_candidates():
            print("  警告：多主 shot 但未生成 bridge candidate，请检查规划输出")
        overcrowded = [
            s for s in shots
            if s.get("capacity_verdict") == "reject"
            and not self.parser.is_bridge_shot(s)
        ]
        if overcrowded:
            print(
                f"  警告：{len(overcrowded)} 个主 shot 参考帧/语义密度仍不合格 "
                f"（v1.6 density>{LTX_MAX_REFERENCE_DENSITY} 或 v1.7 语义阶段过密），"
                "建议 --reset-ltx 后重跑规划"
            )
        self.orch.state["step"] = "ltx_generate"
        self.orch._save_state()
        print(f"  已解析 {len(shots)} 个 LTX shot，计划总时长约 "
              f"{sum(s['video_seconds'] for s in shots):.0f}s")
        print(f"  参数总结: {self.summary_file}")

    async def step_ltx_generate(self) -> None:
        shots: List[Dict] = self.orch.state.get("ltx_shots") or []
        if not shots and os.path.exists(self.plan_file):
            with open(self.plan_file, "r", encoding="utf-8") as f:
                shots = json.load(f).get("shots", [])
            self.orch.state["ltx_shots"] = shots

        if not shots:
            raise RuntimeError("无 LTX shot 计划，请先执行 ltx_plan 阶段。")

        num_candidates = int(
            self.orch.state.get("ltx_video_candidates", LTX_VIDEO_CANDIDATES)
        )
        done = set(self.orch.state.get("ltx_shots_done") or [])
        pending = [s for s in shots if s["shot_id"] not in done]

        max_parallel = int(
            self.orch.state.get("ltx_max_parallel", LTX_MAX_PARALLEL)
        )
        ltx_w, ltx_h = self.get_ltx_video_size()
        res_key = self.get_ltx_resolution_key()
        res_txt = (
            f"{res_key}（{LTX_RESOLUTION_LABELS[res_key]}）"
            if res_key in LTX_RESOLUTION_LABELS
            else f"{ltx_w}×{ltx_h}"
        )
        print(
            f"\n[Step 7/7] LTX 参考生视频（{len(pending)}/{len(shots)} 个 shot 待抽卡，"
            f"每 shot {num_candidates} 条候选、同时最多 {max_parallel} 路并行，"
            f"输出 {res_txt}，pipeline=keyframe_interpolation）..."
        )
        print(
            "  说明：候选保存在各 shot 的 *_candidates/ 目录；请人工挑选后可将选中文件"
            f"复制为 case_ltx_shot_XX.mp4（正式成片位，可选）。"
        )

        for shot in pending:
            sid = shot["shot_id"]
            candidates_dir = self.shot_candidates_dir(shot)
            os.makedirs(candidates_dir, exist_ok=True)
            prefix = shot["output_file"].replace(".mp4", "")

            existing = self.list_shot_video_candidates(candidates_dir)
            if len(existing) >= num_candidates:
                print(
                    f"  [skip] {sid} 已有 {len(existing)} 个候选 → {candidates_dir}/"
                )
                done.add(sid)
                self.orch.state["ltx_shots_done"] = sorted(done)
                self.save_ltx_shot_summary(shots)
                continue

            self.parser.normalize_shot_dict(shot)
            image_paths = [
                p for p in shot.get("image_paths", [])
                if p and os.path.isfile(p)
            ]
            if not image_paths:
                raise FileNotFoundError(
                    f"{sid} 无有效参考图: {shot.get('image_files')}"
                )
            n_img = len(image_paths)
            n_idx = len(shot.get("image_idxs") or [])
            n_str = len(shot.get("image_strengths") or [])
            if not (n_img == n_idx == n_str):
                raise ValueError(
                    f"{sid} 参考图参数长度不一致: images={n_img}, "
                    f"image_idxs={n_idx}, image_strengths={n_str}"
                )

            to_generate: List[tuple] = []
            for i in range(1, num_candidates + 1):
                save_path = os.path.join(
                    candidates_dir, f"{prefix}_candidate_{i:02d}.mp4"
                )
                if not os.path.isfile(save_path):
                    to_generate.append((i, save_path))

            if not to_generate:
                done.add(sid)
                continue

            video_secs = min(
                float(shot["video_seconds"]), float(LTX_MAX_SHOT_SECONDS),
            )

            print(
                f"  {sid}: 生成 {len(to_generate)} 条候选（同时最多 {max_parallel} 路） "
                f"({video_secs}s, {ltx_w}×{ltx_h}, {len(image_paths)} 张参考图) → "
                f"{os.path.basename(candidates_dir)}/"
            )

            sem = asyncio.Semaphore(max_parallel)

            async def _gen_one(candidate_idx: int, save_path: str) -> None:
                async with sem:
                    logger.info(
                        "LTX %s candidate %d %dx%d -> %s",
                        sid, candidate_idx, ltx_w, ltx_h, save_path,
                    )
                    await self.ltx.keyframe_interpolation_to_video(
                        prompt=shot["prompt"],
                        negative_prompt=shot.get("negative_prompt") or DEFAULT_NEGATIVE,
                        video_seconds=video_secs,
                        images=image_paths,
                        image_idxs=shot.get("image_idxs"),
                        image_strengths=shot.get("image_strengths"),
                        save_path=save_path,
                        width=ltx_w,
                        height=ltx_h,
                    )

            await asyncio.gather(
                *[_gen_one(idx, path) for idx, path in to_generate]
            )

            done.add(sid)
            self.orch.state["ltx_shots_done"] = sorted(done)
            final_candidates = self.list_shot_video_candidates(candidates_dir)
            self.orch._save_state()
            self.save_ltx_shot_summary(shots)
            print(
                f"  ✓ {sid} 候选已就绪 ({len(final_candidates)} 个): "
                f"{candidates_dir}/"
            )

        self.orch.state["step"] = "done"
        self.orch.summary["status"] = "已完成（LTX 候选已生成，待人工挑选成片）"
        self.orch._save_state()
        self.save_ltx_shot_summary(shots)
        print(f"  LTX shot 总结已更新: {self.summary_file}")
