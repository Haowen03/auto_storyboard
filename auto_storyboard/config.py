import os

# VLM 后端：gitee=模力方舟 | dashscope=阿里云 DashScope
# 也可通过环境变量 VLM_PROVIDER 或 run.py --vlm-provider 切换
VLM_PROVIDER = os.environ.get("VLM_PROVIDER", "gitee").lower()

# ── Gitee 模力方舟（双模型：文本 64K + 视觉 32K，见 Token 包）
# 资源包: https://moark.com/serverless-api/packages/1492?model=Qwen3.5-27B&package=1492
GITEE_AI_API_KEY = os.environ.get(
    "GITEE_AI_API_KEY", "your_gitee_api_key"
)
GITEE_AI_BASE_URL = os.environ.get("GITEE_AI_BASE_URL", "https://ai.gitee.com/v1")
# 纯文本 / 长上下文（64K）：分镜规划、prompt 生成、LTX 文本规划等
GITEE_TEXT_MODEL = os.environ.get("GITEE_TEXT_MODEL", "Qwen3.5-27B")
# 多模态（32K）：资源审查、候选图评选、带参考图的编辑 prompt
GITEE_VL_MODEL = os.environ.get("GITEE_VL_MODEL", "Qwen3-VL-30B-A3B-Instruct")
# 单模型回退（取消下行注释并设 GITEE_DUAL_MODEL=0 可只用 VL）
GITEE_VLM_MODEL = os.environ.get("GITEE_VL_MODEL", GITEE_VL_MODEL)
GITEE_DUAL_MODEL = os.environ.get("GITEE_DUAL_MODEL", "1") not in ("0", "false", "False")
# Gitee VL 模型上下文上限（用于发送前压缩历史，避免 Step1 长文档撑爆 32K）
GITEE_VL_CONTEXT_LIMIT = int(os.environ.get("GITEE_VL_CONTEXT_LIMIT", "32768"))
# Gitee VL 单次请求最多传入图片数（含候选+参考）；超出会 400
GITEE_MAX_VL_IMAGES = int(os.environ.get("GITEE_MAX_VL_IMAGES", "5"))
# GITEE_VL_MODEL = os.environ.get("GITEE_VL_MODEL", "Qwen2.5-VL-32B-Instruct")
# GITEE_TEXT_MODEL = os.environ.get("GITEE_TEXT_MODEL", "Qwen3.5-35B-A3B")

# ── DashScope 阿里云（切回时设 VLM_PROVIDER=dashscope 或 --vlm-provider dashscope）
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "your_dashscope_api_key")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# DASHSCOPE_VLM_MODEL = "qwen-plus-2025-07-28"
DASHSCOPE_VLM_MODEL = "qwen3-vl-235b-a22b-thinking"

_VLM_PROFILES = {
    "gitee": (GITEE_AI_API_KEY, GITEE_AI_BASE_URL, GITEE_VLM_MODEL),
    "dashscope": (DASHSCOPE_API_KEY, DASHSCOPE_BASE_URL, DASHSCOPE_VLM_MODEL),
}


def get_vlm_settings(provider=None):
    """返回 (api_key, base_url, model, provider_name)。"""
    name = (provider or VLM_PROVIDER).lower()
    if name not in _VLM_PROFILES:
        raise ValueError(
            f"VLM_PROVIDER 必须是 {tuple(_VLM_PROFILES)} 之一，得到: {name!r}"
        )
    key, url, model = _VLM_PROFILES[name]
    return key, url, model, name


def get_vlm_model_pair(provider=None):
    """返回 (text_model, vl_model)；dashscope 两者相同。"""
    name = (provider or VLM_PROVIDER).lower()
    key, url, model, _ = get_vlm_settings(name)
    if name == "gitee" and GITEE_DUAL_MODEL:
        return GITEE_TEXT_MODEL, GITEE_VL_MODEL
    return model, model


VLM_API_KEY, VLM_BASE_URL, VLM_MODEL, _ACTIVE_VLM_PROVIDER = get_vlm_settings()

QWEN_IMAGE_BASE_URL = os.environ.get("QWEN_IMAGE_BASE_URL", "http://127.0.0.1:9000")

DEFAULT_IMAGE_HEIGHT = 720
DEFAULT_IMAGE_WIDTH = 1280
DEFAULT_NUM_INFERENCE_STEPS = 50

RESOURCE_CANDIDATES = 3
FRAME_CANDIDATES = 5
EDIT_FRAME_CANDIDATES = 5
SHOT_FRAME_CANDIDATES = EDIT_FRAME_CANDIDATES

MAX_REGEN_ATTEMPTS = 1

# LTX 参考图插帧生视频（下游阶段，见 LTX音视频生成工作流指令文档_v1.5）
LTX_BASE_URL = os.environ.get("LTX_BASE_URL", "http://127.0.0.1:8000")
LTX_FRAME_RATE = 24
# LTX 服务要求 width / height 均为 64 的倍数
LTX_DIM_MULTIPLE = 64

# 可选清晰度预设（均为 64 倍数、约 16:9；1080p 为当前部署实测可用）
LTX_RESOLUTION_PRESETS: dict[str, tuple[int, int]] = {
    "480p": (832, 512),       # 13×64 × 8×64
    "576p": (1024, 576),      # 16×64 × 9×64
    "720p": (1280, 704),      # 20×64 × 11×64
    "1080p": (1920, 1088),    # 30×64 × 17×64（默认）
}
LTX_RESOLUTION_LABELS: dict[str, str] = {
    "480p": "832×512（约 480p）",
    "576p": "1024×576（约 576p）",
    "720p": "1280×704（约 720p）",
    "1080p": "1920×1088（约 1080p，推荐）",
}
LTX_DEFAULT_RESOLUTION = os.environ.get("LTX_DEFAULT_RESOLUTION", "1080p")
if LTX_DEFAULT_RESOLUTION not in LTX_RESOLUTION_PRESETS:
    LTX_DEFAULT_RESOLUTION = "1080p"

_dw, _dh = LTX_RESOLUTION_PRESETS[LTX_DEFAULT_RESOLUTION]
LTX_DEFAULT_WIDTH = int(os.environ.get("LTX_DEFAULT_WIDTH", str(_dw)))
LTX_DEFAULT_HEIGHT = int(os.environ.get("LTX_DEFAULT_HEIGHT", str(_dh)))
LTX_NUM_INFERENCE_STEPS = 15
# 长 shot 未指定 --long-shot-seconds 时由 VLM 规划每段时长，无代码层默认值
LTX_MAX_SHOT_SECONDS = 15
LTX_VIDEO_CANDIDATES = 5  # 每个 shot 目标候选条数（与 MetaXViMax test.py NUM_PARALLEL 一致）
LTX_MAX_PARALLEL = 3  # 同时向 LTX 服务提交的任务上限（避免 5×15s 打满 GPU）

# 全流程模式：frames=仅参考帧 | video=仅 LTX 视频 | full=参考帧+LTX
PIPELINE_MODES = ("frames", "video", "full")
PIPELINE_MODE_LABELS = {
    "frames": "仅参考帧生成",
    "video": "仅 LTX 音视频生成（需已有参考帧）",
    "full": "参考帧 + LTX 音视频全流程",
}

LTX_WORKFLOW_DOC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "LTX音视频生成工作流指令文档_v1.7_语义密度与动作链解析优化.md",
)

# v1.4 / v1.5 跨 shot 拼接模式：direct_concat=整段 mp4 直接拼接 | trim_overlap=允许裁剪余量
LTX_STITCH_MODES = ("direct_concat", "trim_overlap")
LTX_DEFAULT_STITCH_MODE = os.environ.get("LTX_STITCH_MODE", "direct_concat")
if LTX_DEFAULT_STITCH_MODE not in LTX_STITCH_MODES:
    LTX_DEFAULT_STITCH_MODE = "direct_concat"
LTX_BRIDGE_DEFAULT_SECONDS = 4  # bridge shot 推荐 3–5 秒（文档 §8.1）
LTX_BRIDGE_MIN_SECONDS = 3
LTX_BRIDGE_MAX_SECONDS = 5

# v1.6 单 shot 参考帧密度（reference_frame_density = 帧数 / video_seconds）
# 文档 §3.3：0.29–0.36 推荐；>0.42 过密须减帧或拆 shot
LTX_DENSITY_COMFORT_MAX = float(os.environ.get("LTX_DENSITY_COMFORT_MAX", "0.36"))
LTX_MAX_REFERENCE_DENSITY = float(os.environ.get("LTX_MAX_REFERENCE_DENSITY", "0.42"))

# v1.7 语义阶段密度（semantic_event_density = major_semantic_stages / video_seconds）
# 12s 内 4+ 独立语义阶段且非连续动作链时应拆 shot / micro-bridge（§4）
LTX_MAX_SEMANTIC_EVENT_DENSITY = float(
    os.environ.get("LTX_MAX_SEMANTIC_EVENT_DENSITY", "0.33")
)

# v1.5：生成与使用 bridge 分离（文档 §14）
LTX_GENERATE_BRIDGE_CANDIDATES = os.environ.get(
    "LTX_GENERATE_BRIDGE_CANDIDATES", "1"
) not in ("0", "false", "False")
LTX_USE_BRIDGE_AT_EXPORT = os.environ.get(
    "LTX_USE_BRIDGE_AT_EXPORT", "1"
) not in ("0", "false", "False")

# v1.2 弱模型安全模式：证据约束 prompt、保守 image_strength / image_idxs
LTX_GROUNDED_SAFE_MODE = os.environ.get("LTX_GROUNDED_SAFE_MODE", "1") not in (
    "0",
    "false",
    "False",
)
LTX_MIN_IMAGE_STRENGTH = 0.88  # 低于此值在 safe mode 下自动抬升（§22.1）
LTX_AGGRESSIVE_STRENGTH_THRESHOLD = 0.85  # 低于此值记录激进测试警告


def is_valid_ltx_dimensions(width: int, height: int) -> bool:
    m = LTX_DIM_MULTIPLE
    return width > 0 and height > 0 and width % m == 0 and height % m == 0


def format_ltx_resolution_help() -> str:
    lines = [f"须为 {LTX_DIM_MULTIPLE} 的倍数。预设："]
    for key in LTX_RESOLUTION_PRESETS:
        w, h = LTX_RESOLUTION_PRESETS[key]
        lines.append(f"  {key}: {w}×{h} — {LTX_RESOLUTION_LABELS.get(key, '')}")
    return "\n".join(lines)


def resolve_ltx_resolution(
    *,
    preset: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> tuple[int, int, str]:
    """解析 LTX 输出尺寸，返回 (width, height, resolution_key)。"""
    if preset:
        key = preset.strip().lower()
        if key not in LTX_RESOLUTION_PRESETS:
            valid = ", ".join(LTX_RESOLUTION_PRESETS)
            raise ValueError(
                f"ltx_resolution 必须是 [{valid}] 之一，得到: {preset!r}\n"
                f"{format_ltx_resolution_help()}"
            )
        w, h = LTX_RESOLUTION_PRESETS[key]
        return w, h, key
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("自定义尺寸须同时指定 width 与 height")
        w, h = int(width), int(height)
        if not is_valid_ltx_dimensions(w, h):
            raise ValueError(
                f"LTX 尺寸 {w}×{h} 非法：width/height 均须为 {LTX_DIM_MULTIPLE} 的倍数。\n"
                f"{format_ltx_resolution_help()}"
            )
        return w, h, f"{w}x{h}"
    return (
        LTX_RESOLUTION_PRESETS[LTX_DEFAULT_RESOLUTION][0],
        LTX_RESOLUTION_PRESETS[LTX_DEFAULT_RESOLUTION][1],
        LTX_DEFAULT_RESOLUTION,
    )
