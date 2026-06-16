#!/usr/bin/env python3
"""
多宫格分镜参考帧 + LTX 音视频生成工作流 — CLI 入口

参考帧阶段（v6.14）：资源库 → 基础帧 → 编辑帧（Screen-Content + Camera transformation 双段式，No-Keep，默认仅 base 参考）→ case_final_frameXX.png
LTX 阶段（v1.7）：语义动作链规划 + 参考帧容量 + Bridge/micro-bridge + 双拼接模式 → 每 shot 并行 N 条候选 → 人工挑选成片

断点续传：每次 run 会先扫描结果目录（资源/基础帧/编辑帧），将 step 前推到与磁盘一致的阶段再继续。
逐帧 prompt 即时写入 case 目录下的 frame_prompts.json（生成完成前可查看）。
"""

import argparse
import asyncio
import logging
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auto_storyboard.orchestrator import (
    WorkflowOrchestrator,
    RESOURCE_MODE_LABELS,
    PIPELINE_MODE_LABELS,
)
from auto_storyboard.config import (
    LTX_DEFAULT_HEIGHT,
    LTX_DEFAULT_RESOLUTION,
    LTX_DEFAULT_STITCH_MODE,
    LTX_DEFAULT_WIDTH,
    LTX_GENERATE_BRIDGE_CANDIDATES,
    LTX_USE_BRIDGE_AT_EXPORT,
    LTX_GROUNDED_SAFE_MODE,
    LTX_MAX_PARALLEL,
    LTX_RESOLUTION_LABELS,
    LTX_RESOLUTION_PRESETS,
    LTX_STITCH_MODES,
    LTX_VIDEO_CANDIDATES,
    PIPELINE_MODES,
    VLM_PROVIDER,
    get_vlm_model_pair,
    get_vlm_settings,
    resolve_ltx_resolution,
)

# ─────────────────────────────────────────────────────────────────────────────
# 参数说明（run.py --help 与下方一致）
# ─────────────────────────────────────────────────────────────────────────────
PARAMETER_GUIDE = """
参数说明
========

【创意与项目】
  --idea TEXT
      视频创意描述，传给 VLM 做分镜规划与 LTX prompt 规划。
      示例：--idea "哪吒从火焰莲花中复活，3D动漫风格"

  --case-name NAME
      项目名，同时作为输出子目录名（默认 seedance_fuke/qwen/<case-name>/）。
      断点续传、手动替换帧/视频都依赖此名称保持不变。
      示例：--case-name case_nezha

  --output-dir PATH
      输出根目录，默认为 seedance_fuke/qwen/。
      实际写入：<output-dir>/<case-name>/

【参考帧阶段】
  --grid N
      参考帧数量 N，范围 [2, 25]。常用 4 / 6 / 9。
      决定 case_base_frame01~NN、case_final_frame01~NN 的数量。
      示例：--grid 4

  --chars N
      强制人物资源数量；省略则由 VLM 根据 idea 自动决定。

  --scenes N
      强制场景资源数量；省略则由 VLM 自动决定。

  --opening {front,back,side,auto}
      首帧 case_base_frame01 的人物-镜头朝向偏好。
      front=正面/三分之二正面；back=背影；side=侧面；auto=VLM 中立判断。

  --resources {auto,upload,generate}
      资源库（人物/场景/道具参考图）来源。
      auto     = 目录已有 PNG 直接用，缺失再 AI 生成（默认）
      upload   = 仅使用用户放入目录的 case_char/scene/prop 图，不抽卡
      generate = 必须按 Step1 规划 prompt 全部 AI 生成

【全流程开关】
  --pipeline {frames,video,full}
      frames = 只跑参考帧工作流，到 case_final_frameXX.png 结束
      video  = 只跑 LTX（需目录里已有 final/edit/base 参考帧）
      full   = 参考帧完成后自动进入 LTX Shot 规划与生成

【LTX 音视频】（规划遵循 LTX音视频生成工作流指令文档_v1.7：语义动作链 + 参考帧容量 + bridge/micro-bridge + 双拼接模式 + §21 证据约束）
  --video-duration SEC
      目标成片总时长（秒）。LTX 规划阶段会拆成多个 shot 并分配时长。
      --pipeline video/full 时若未指定：video 默认 24s，full 默认 max(grid*6, 20)。

  --long-shot
      开启「长 shot」模式：用较少段、每段更长的视频（多参考帧内分镜），
      减少硬切。适合 30s 用 2 段长 shot 等场景。
      若未同时指定 --long-shot-seconds，则**每段时长由 LTX 规划 VLM 根据剧情决定**。

  --long-shot-seconds SEC
      （可选）长 shot 模式下**强制**每段目标时长；省略则由 VLM 在 Shot Plan 中
      为每个 shot 分别填写 video_seconds 及时长理由。仅在与 --long-shot 同用时生效。

  --ltx-candidates N
      每个 LTX shot 要生成的候选视频总条数（默认 5）。

  --ltx-parallel N
      同时向 LTX 服务提交的任务数上限（默认 3）。例如候选 5 条、并行 3 时，
      先跑 3 路，完成后再跑剩余 2 路，避免 5×15s 同时打满 GPU。

  --ltx-resolution {480p,576p,720p,1080p}
      LTX 输出清晰度预设（默认 1080p）。宽高均为 64 的倍数：
        480p  → 832×512
        576p  → 1024×576
        720p  → 1280×704
        1080p → 1920×1088（当前部署推荐）
      与 --ltx-width/--ltx-height 二选一；若同时指定，以预设为准。

  --ltx-width PX / --ltx-height PX
      自定义 LTX 输出宽高（须同时为 64 的倍数）。未指定时使用 --ltx-resolution 或默认 1080p。

  --ltx-stitch-mode {direct_concat,trim_overlap}
      跨 shot 拼接模式（默认 direct_concat）。
      direct_concat = 各 shot 整段 mp4 直接顺序拼接，边界帧须贴近 shot 首尾；
      trim_overlap  = 允许裁剪余量，主 shot 尾段作剪辑 handle，不可简单整段拼接。

  --no-generate-bridge
      关闭 v1.5 bridge candidate 默认生成（默认开启：多主 shot 时自动补齐相邻 bridge）。
      也可设环境变量 LTX_GENERATE_BRIDGE_CANDIDATES=0。

  --no-bridge-at-export
      导出拼接顺序不含 bridge（仅主 shot 直接拼接）；bridge 仍会生成供人工选用。
      也可设环境变量 LTX_USE_BRIDGE_AT_EXPORT=0。

  --reset-ltx
      清除本 case 的 LTX 规划与视频候选，从 Shot 规划（ltx_plan）重新跑；
      不删除 case_final_frameXX.png 等参考帧。仅用于 --pipeline video / full。

  --no-ltx-safe-mode
      关闭 v1.2 弱模型安全模式（默认开启：证据约束 prompt、保守 image_strength、越界 negative）。
      也可设环境变量 LTX_GROUNDED_SAFE_MODE=0。
      输出目录：<case-dir>/case_ltx_shot_01_candidates/
                case_ltx_shot_01_candidate_01.mp4 ~ _05.mp4
      工作流不自动 VLM 选片；请人工预览后，将选中文件复制为：
                case_ltx_shot_01.mp4  （正式成片位，可选）

输出总结文件（与参考帧 workflow_summary.json 对应）
  workflow_summary.json   全流程总结；含 ltx_shots 段（每 shot 的 prompt/参数摘要）
  ltx_shot_summary.json   LTX 专用总结：每个 shot 的 prompt、negative_prompt、
                          image_idxs、video_seconds、候选列表、选片状态等
  ltx_shot_plan.json      VLM 原始规划回复 + 可执行 shots 数组（供断点续传）

【其它】
  -v, --verbose
      打印 DEBUG 日志。

环境变量
  VLM_PROVIDER        VLM 后端：gitee（模力方舟，默认）| dashscope（阿里云）
  GITEE_AI_API_KEY    模力方舟 API Key（见 config.py）
  DASHSCOPE_API_KEY   阿里云 DashScope API Key（切回 dashscope 时使用）
  LTX_BASE_URL        LTX 服务地址（默认 http://10.42.1.2:8000）
  LTX_DEFAULT_RESOLUTION  默认清晰度预设：480p | 576p | 720p | 1080p（默认 1080p）
  LTX_STITCH_MODE              默认拼接模式：direct_concat | trim_overlap
  LTX_GENERATE_BRIDGE_CANDIDATES  是否默认生成 bridge candidate（默认 1）
  LTX_USE_BRIDGE_AT_EXPORT     导出拼接是否包含 bridge（默认 1）

常用命令示例
  # 仅 4 宫格参考帧
  python run.py --idea "哪吒复活" --grid 4 --pipeline frames

  # 全流程：4 帧 + 30s 视频，2 个长 shot
  python run.py --grid 4 --pipeline full --video-duration 30 --long-shot

  # 已有参考帧，只生成 LTX 候选
  python run.py --case-name case_nezha --pipeline video --video-duration 24

  # 每 shot 并行 3 条候选（默认 5 条）
  python run.py --case-name case_nezha --pipeline video --ltx-candidates 3
"""


def _grid_count(value: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"--grid 必须是整数，得到: {value!r}") from e
    if n < 2 or n > 25:
        raise argparse.ArgumentTypeError(f"--grid 必须在 [2, 25] 区间，得到: {n}")
    return n


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError(f"必须是正整数，得到: {value!r}") from e
    if n < 1 or n > 20:
        raise argparse.ArgumentTypeError(f"必须在 [1, 20] 区间，得到: {n}")
    return n


def main():
    parser = argparse.ArgumentParser(
        description="多宫格分镜参考帧 + LTX 音视频生成工作流",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=PARAMETER_GUIDE,
    )
    parser.add_argument(
        "--idea",
        type=str,
        default="小龙女召唤出一把飞剑，然后站上飞剑，御剑飞向远处。",
        help="视频创意描述（见下方「参数说明」）",
    )
    parser.add_argument(
        "--grid", type=_grid_count, default=6, metavar="N",
        help="参考帧数量 [2,25]（默认 6）",
    )
    parser.add_argument(
        "--chars", type=int, default=None,
        help="人物资源数量；省略则 VLM 自动决定",
    )
    parser.add_argument(
        "--scenes", type=int, default=None,
        help="场景资源数量；省略则 VLM 自动决定",
    )
    parser.add_argument(
        "--case-name", type=str, default="case_yujian_aliyun",
        help="项目名 / 输出子目录名（断点续传键）",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="输出根目录（默认 seedance_fuke/qwen/）",
    )
    parser.add_argument(
        "--opening", type=str, default="front",
        choices=["front", "back", "side", "auto"],
        help="首帧人物-镜头朝向：front/back/side/auto",
    )
    parser.add_argument(
        "--resources", type=str, default="auto",
        choices=["auto", "upload", "generate"],
        help="资源库来源：auto / upload / generate",
    )
    parser.add_argument(
        "--pipeline", type=str, default="frames",
        choices=list(PIPELINE_MODES),
        help="全流程：frames=仅参考帧 | video=仅LTX | full=参考帧+LTX",
    )
    parser.add_argument(
        "--video-duration", type=float, default=None, metavar="SEC",
        help="目标视频总时长（秒），用于 LTX Shot 规划",
    )
    parser.add_argument(
        "--long-shot", action="store_true",
        help="长 shot：少段长视频 + 多参考帧内分镜（见 --long-shot-seconds）",
    )
    parser.add_argument(
        "--long-shot-seconds", type=float, default=None, metavar="SEC",
        help=(
            "（可选）长 shot 每段固定秒数（先验约束）；VLM 须先锁定时长再规划 "
            "参考帧/image_idxs/剧情；省略则由 VLM 自行决定每段时长"
        ),
    )
    parser.add_argument(
        "--ltx-candidates", type=_positive_int, default=LTX_VIDEO_CANDIDATES,
        metavar="N",
        help=(
            f"每个 LTX shot 生成的候选视频总条数（默认 {LTX_VIDEO_CANDIDATES}）；"
            "写入 case_ltx_shot_XX_candidates/，需人工挑选成片"
        ),
    )
    parser.add_argument(
        "--ltx-parallel", type=_positive_int, default=LTX_MAX_PARALLEL,
        metavar="N",
        help=(
            f"同时提交给 LTX 服务的任务数上限（默认 {LTX_MAX_PARALLEL}）；"
            "与 --ltx-candidates 配合使用"
        ),
    )
    parser.add_argument(
        "--ltx-resolution",
        type=str,
        default=None,
        choices=list(LTX_RESOLUTION_PRESETS),
        metavar="PRESET",
        help=(
            f"LTX 清晰度预设（默认 {LTX_DEFAULT_RESOLUTION}，宽高须为 64 倍数）："
            + " | ".join(
                f"{k}={LTX_RESOLUTION_PRESETS[k][0]}×{LTX_RESOLUTION_PRESETS[k][1]}"
                for k in LTX_RESOLUTION_PRESETS
            )
        ),
    )
    parser.add_argument(
        "--ltx-width", type=_positive_int, default=None, metavar="PX",
        help=(
            f"LTX 输出宽度（须为 64 倍数；与 --ltx-height 成对使用，"
            f"否则用 --ltx-resolution，默认 {LTX_DEFAULT_WIDTH}）"
        ),
    )
    parser.add_argument(
        "--ltx-height", type=_positive_int, default=None, metavar="PX",
        help=(
            f"LTX 输出高度（须为 64 倍数；与 --ltx-width 成对使用，"
            f"否则用 --ltx-resolution，默认 {LTX_DEFAULT_HEIGHT}）"
        ),
    )
    parser.add_argument(
        "--ltx-stitch-mode",
        type=str,
        default=LTX_DEFAULT_STITCH_MODE,
        choices=list(LTX_STITCH_MODES),
        help=(
            "跨 shot 拼接模式（默认 direct_concat）："
            "direct_concat=整段直接拼接 | trim_overlap=裁剪余量后衔接"
        ),
    )
    parser.add_argument(
        "--no-generate-bridge", action="store_true",
        help=(
            "关闭 v1.5 bridge candidate 默认生成（默认开启；"
            f"config 默认={LTX_GENERATE_BRIDGE_CANDIDATES}）"
        ),
    )
    parser.add_argument(
        "--no-bridge-at-export", action="store_true",
        help=(
            "导出拼接顺序不含 bridge，仅主 shot；bridge 仍生成供选用（"
            f"config 默认 use_bridge={LTX_USE_BRIDGE_AT_EXPORT}）"
        ),
    )
    parser.add_argument(
        "--reset-ltx", action="store_true",
        help="清除 LTX 规划/候选缓存，从 Shot 规划阶段重新生成视频（保留参考帧）",
    )
    parser.add_argument(
        "--no-ltx-safe-mode", action="store_true",
        help="关闭 LTX v1.2 证据约束/弱模型安全模式（默认开启）",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出 DEBUG 日志",
    )
    parser.add_argument(
        "--vlm-provider",
        type=str,
        default=None,
        choices=["gitee", "dashscope"],
        help=f"VLM 后端（默认 {VLM_PROVIDER}，见 config.py / 环境变量 VLM_PROVIDER）",
    )
    parser.add_argument(
        "--regen-edit",
        type=str,
        default="",
        metavar="IDS",
        help=(
            "仅重跑指定编辑帧：逗号分隔编号或文件名，如 03,04 或 case_edit_frame03.png。"
            "会删除对应成片、清除缓存 prompt、归档旧候选目录为 *_candidates_old，"
            "并重新向 VLM 要 prompt 后抽卡（不会复用旧候选图）。"
        ),
    )

    args = parser.parse_args()

    if args.pipeline in ("video", "full"):
        try:
            resolve_ltx_resolution(
                preset=args.ltx_resolution,
                width=args.ltx_width,
                height=args.ltx_height,
            )
        except ValueError as e:
            parser.error(str(e))
        if args.ltx_resolution and (args.ltx_width is not None or args.ltx_height is not None):
            print("  提示：已指定 --ltx-resolution，将忽略 --ltx-width/--ltx-height")
    if args.reset_ltx and args.pipeline not in ("video", "full"):
        parser.error("--reset-ltx 仅适用于 --pipeline video 或 full")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.pipeline in ("video", "full") and args.video_duration is None:
        args.video_duration = 24.0 if args.pipeline == "video" else max(args.grid * 6, 20.0)

    case_name = args.case_name or args.idea[:20].strip().replace(" ", "_").replace("/", "_")

    vlm_provider = args.vlm_provider or VLM_PROVIDER
    text_model, vl_model = get_vlm_model_pair(vlm_provider)

    orchestrator = WorkflowOrchestrator(
        case_name=case_name,
        output_base_dir=args.output_dir,
        resource_mode=args.resources,
        vlm_provider=vlm_provider,
        reset_ltx=args.reset_ltx and args.pipeline in ("video", "full"),
    )

    if args.regen_edit.strip():
        ids = [
            x.strip()
            for x in re.split(r"[,;\s]+", args.regen_edit.strip())
            if x.strip()
        ]
        print(f"\n[regen-edit] 准备重跑编辑帧: {', '.join(ids)}")
        orchestrator.prepare_edit_frame_regeneration(ids)
        print()

    opening_label = {
        "front": "正面 / 三分之二正面",
        "back": "背影 / 三分之二背影",
        "side": "侧面 / 三分之二侧面",
        "auto": "中立判断（由 VLM 决定）",
    }[args.opening]

    print(f"{'='*60}")
    print("分镜参考帧 + LTX 音视频工作流")
    print(f"{'='*60}")
    print(f"  Idea:            {args.idea}")
    print(f"  帧数 N:           {args.grid}")
    print(f"  人物/场景:        {args.chars or '自动'} / {args.scenes or '自动'}")
    print(f"  开场朝向:         {opening_label}")
    print(f"  资源库:           {args.resources} — {RESOURCE_MODE_LABELS[args.resources]}")
    print(f"  全流程模式:       {args.pipeline} — {PIPELINE_MODE_LABELS[args.pipeline]}")
    if args.pipeline in ("video", "full"):
        print(f"  目标视频时长:     {args.video_duration}s")
        if args.long_shot:
            if args.long_shot_seconds is not None:
                ls_txt = f"开（用户指定每段 {args.long_shot_seconds}s）"
            else:
                ls_txt = "开（每段时长由 VLM 规划）"
        else:
            ls_txt = "关"
        print(f"  长 shot 模式:     {ls_txt}")
        _ltx_w, _ltx_h, _ltx_res = resolve_ltx_resolution(
            preset=args.ltx_resolution,
            width=args.ltx_width,
            height=args.ltx_height,
        )
        _res_label = LTX_RESOLUTION_LABELS.get(_ltx_res, f"{_ltx_w}×{_ltx_h}")
        print(
            f"  LTX 每 shot 候选: {args.ltx_candidates} 条"
            f"（同时最多 {args.ltx_parallel} 路，输出 {_ltx_res} {_res_label}，人工选片）"
        )
        stitch_label = (
            "直接拼接（direct_concat）"
            if args.ltx_stitch_mode == "direct_concat"
            else "裁剪余量（trim_overlap）"
        )
        print(f"  LTX 拼接模式:     {stitch_label}")
        gen_bridge = not args.no_generate_bridge
        use_bridge = not args.no_bridge_at_export
        print(
            f"  LTX bridge:       "
            f"{'默认生成' if gen_bridge else '不生成'} / "
            f"导出{'含' if use_bridge else '不含'}"
        )
        safe_on = LTX_GROUNDED_SAFE_MODE and not args.no_ltx_safe_mode
        print(f"  LTX 证据约束:     {'开（v1.2 safe mode）' if safe_on else '关'}")
    if text_model == vl_model:
        print(f"  VLM 后端:         {vlm_provider} ({text_model})")
    else:
        print(f"  VLM 后端:         {vlm_provider}")
        print(f"    文本模型:       {text_model}")
        print(f"    视觉模型:       {vl_model}")
    print(f"  项目名:           {case_name}")
    print(f"  输出目录:         {orchestrator.output_dir}")
    print(f"  当前断点:         {orchestrator.state.get('step', 'init')}")
    print(f"{'='*60}")
    print("完整参数说明: python run.py --help")
    print(f"{'='*60}")

    asyncio.run(
        orchestrator.run(
            idea=args.idea,
            num_characters=args.chars,
            num_scenes=args.scenes,
            grid_n=args.grid,
            opening_orientation=args.opening,
            resource_mode=args.resources,
            pipeline_mode=args.pipeline,
            video_target_seconds=args.video_duration,
            long_shot_mode=args.long_shot,
            long_shot_seconds=args.long_shot_seconds if args.long_shot else None,
            ltx_video_candidates=args.ltx_candidates,
            ltx_max_parallel=args.ltx_parallel,
            ltx_grounded_safe_mode=(
                False if args.no_ltx_safe_mode else None
            ),
            ltx_width=args.ltx_width,
            ltx_height=args.ltx_height,
            ltx_resolution=args.ltx_resolution,
            ltx_stitch_mode=args.ltx_stitch_mode,
            ltx_generate_bridge_candidates=(
                False if args.no_generate_bridge else None
            ),
            ltx_use_bridge_at_export=(
                False if args.no_bridge_at_export else None
            ),
        )
    )


if __name__ == "__main__":
    main()
