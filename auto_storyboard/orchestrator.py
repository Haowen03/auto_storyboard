"""
WorkflowOrchestrator: 多宫格分镜参考帧自动生成的主编排逻辑。
串联 VLM 规划 -> 资源库构建 -> 逐帧生成循环，支持断点续传。
"""

import asyncio
import json
import os
import shutil
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional

from .vlm_client import QwenVLClient, cap_pick_context_images
from .image_generator import ImageGenerator
from .response_parser import ResponseParser, InitPlanResult
from .config import (
    GITEE_MAX_VL_IMAGES,
    RESOURCE_CANDIDATES,
    FRAME_CANDIDATES,
    EDIT_FRAME_CANDIDATES,
    MAX_REGEN_ATTEMPTS,
    DEFAULT_IMAGE_HEIGHT,
    DEFAULT_IMAGE_WIDTH,
    PIPELINE_MODES,
    PIPELINE_MODE_LABELS,
)
from .ltx_workflow import LTXWorkflowExtension

logger = logging.getLogger(__name__)

WORKFLOW_DOC_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "多宫格分镜参考帧生成工作流v6.14_完整版.md",
)

# v6.8：资源依赖调度（替代 v6.7「最小参考原则」——禁止默认只参考上一帧）
V68_REFERENCE_RESOLVER_RULES = """\
v6.8 Reference Resolver（资源依赖调度）硬性规则：
- 每帧生成前必须先判断：本帧 visible entities、new entities（相对上一帧）、hero product 是否清晰可见，再决定 reference_images。
- 默认公式：上一帧（空间/剧情连续） + 本帧新出现或身份敏感实体对应资源图；**禁止**默认只写 Use case_base_frameXX 而忽略新人物/新道具资源。
- 新人物首次出现（上一帧中不存在）：必须引用对应 case_char_xx（或 clean / with_prop 变体），不能仅靠文字描述生成。
- 新道具/核心商品首次进入画面：必须引用 case_prop_xx（或 verified 组合资源图）。
- hero product（广告核心商品）：在 product shot / 持枪 / 发射 / 击中 / 反应等清晰可见帧，必须引用 case_prop_xx，或上一帧已稳定且形状正确；若上一帧道具变形则必须重新挂 case_prop_xx 纠偏。
- 人物从小比例背景变为中景/表情焦点：应重新补充该人物标准资源图。
- 参考图预算（建议最多 2 张）：1)上一帧 2)新出现实体资源 3)hero product 4)焦点人物 5)场景；frame01 无上一帧 → scene + 主人物；若同时新人物+新道具且名额不足，优先组合资源图 case_char_xx_with_prop_xx。
- 英文 prompt 中必须写 Use <真实文件名>，且与 reference_images 列表一致。
"""

V66_BASE_RULES = """\
v6.7 ADDITIONAL HARD RULES (topology & continuity; base-frame stage unchanged):
- Each base frame must define one Scene Core: the key visible information center of this frame.
- Each base frame must state the Camera-Side Continuity Track relative to the previous base frame: same camera side, gradual side transition, or justified side switch.
- Each base frame must state the topology between camera, scene core, and character:
  1) Camera -> Scene Core -> Character for front / three-quarter front staging;
  2) Camera -> Character -> Scene Core for back / three-quarter back staging;
  3) Character beside Scene Core, both visible, for side / three-quarter side staging.
- Key anchors must be visible, not merely present. The character must not block the Scene Core unless the story explicitly requires it.
- Final image-generation prompts must be compact: one English paragraph, usually 5-7 sentences, keeping only reference usage, camera, topology, story state, key visible anchors, and 3-6 critical negative constraints.
"""

V66_BASE_PICK_OVERLAY = """\
v6.8 TOP PRIORITIES FOR BASE FRAME SELECTION:
0. Reference Dependency Correctness (v6.8): if prompt requires new character/prop/hero product refs, reject candidates that ignore resource identity.
1. New Entity / Hero Product Consistency: new characters and hero props must match assigned resource images.
2. Camera-Side Continuity: adjacent base frames should keep the same camera side or change side only with a clear story reason.
3. Scene Core Topology Accuracy: verify the visible order among camera, scene core, and character matches the prompt.
4. Anchor Visibility Accuracy: key anchors must be clearly visible, not hidden behind the character.
5. Prompt Compression: reject bloated prompts that caused conflicting composition.
These v6.8 checks are evaluated before ordinary visual beauty.
"""

# 候选评选输出格式（避免「其余张不合格」被误判为「整批不合格」而无谓重抽）
PICK_OUTPUT_FORMAT_RULE = """\
## 评选输出格式（必须遵守，避免误触发整批重抽）
- 只要有 **至少 1 张** 基本可用的候选，回复 **必须以** `选择第X张，原因：` 开头（X 为 1 到候选总数）。
- 可以在原因中逐张说明「第1张…不足」「第2张…淘汰」，但这表示 **其他张不行**，不是「整批都不行」。
- **禁止**在已有可用候选时使用 `全部候选均不合格` / `所有候选都不合格` / `均不合格` 作为总结（除非 X 张真的全部不可用）。
- 仅当 **0 张** 候选能勉强使用时，才用：`全部不合格，原因：...，建议：...`
"""

# v6.7 + v6.9 + v6.10：第二阶段「全帧镜头再设计」+ 平衡式提示（抑制局部细节过拟合）
V610_BALANCED_EDIT_RULES = """\
v6.10 Balanced Cinematic Prompting（§22，在 v6.9 镜头多样性之上）:
- **Camera diversity required; detail overfitting forbidden; narrative readability preserved.**
- **One primary visual focus** per edit frame; **2–3 subtle supporting details** max — they must not compete with the story beat.
- **Detail budget** (per prompt): 1 camera type + 1 visual focus + 2 continuity anchors + 2–3 supporting details + 2–3 negative constraints. No microscopic detail checklist.
- **Avoid overfit phrasing**: `fingerprint texture`, `glowing energy bridge`, `magnifying the contact`, `macro insert shot`, `isolate the tactile detail`; bare `Blur the background` without naming recognizable anchors.
- **Insert shot grading (narrative boards)**: prefer `foreground-detail composition` or `close-up detail shot`; reserve `macro insert shot` only for explicit product/texture micro-detail needs.
- **Context anchors**: special camera ≠ remove context; keep face/costume, scene edge, action relation, or atmosphere **recognizable** (softened background OK).
- **Background softening**: if shallow DOF, specify which anchors stay readable (e.g. flame lotus, circular altar edge, storm lighting).
- **Negative compression**: ~3 items — no neutral medium; no story-beat change; no identity/prop drift. Prefer positive framing over `Do not show the entire character body clearly`.
"""

# v6.11：再编辑表层 prompt — 只描述画面内容与镜头转换（§16 / §23）
V611_SURFACE_EDIT_RULES = """\
v6.11 Re-edit Surface Prompt（§16 / §23，在 v6.10 之上）:
- **Base-only image reference**: `actual_reference_images = [case_base_frame0x.png]` by default; char/prop/scene resources are for planning & pick review, **not** default extra image inputs or surface prompt filenames.
- **Mention only images actually sent** to the image editing model (§23.2).
- **Two-layer strategy**: rich structured constraints stay in the edit plan; the **surface prompt** is a compact visual description for the image model.
- **Do NOT default into surface prompt**: `Use case_char_xx.png`, `Use case_prop_xx.png`, long `preserve...` chains, `do not change A/B/C/D`, full identity/prop texture checklists, resource dependency reasoning.
- **Prompt Constraint ≠ Evaluation Criterion** (§23.6): identity/prop/scene checks belong in candidate pick, not necessarily in generation prompt when base already contains them.
- Optional anti-medium cue only; avoid stacking negatives.
"""

# v6.12：画面块 vs 镜头块职责分离（§24）
V612_LAYER_SEPARATION_RULES = """\
v6.12 Edit Prompt Layer Separation（§24，在 v6.11 之上）:
- **Story state block** (Use… as the direct source for the same story state of …): only inherited plot state — who, pose, prop/action result — **NOT** how to shoot.
- **Camera transformation block**: the **only** place for shot type, visual center, foreground/background, DOF, proximity, lighting-on-local, framing emotion.
- **Do NOT use `Show …` with camera language** in the story state block.
- **Story state blacklist**: close-up/insert/wide, focused on, visual center, close proximity, foreground/background, softly visible/blurred/DOF, capturing the tension, subtle reflection on skin/metal, magnifying, tight framing, strong focus on (before Camera transformation).
- **Duplicate emphasis rejection**: if hand/ring/face focus appears in both blocks, remove from story state — keep only in Camera transformation.
"""

# v6.13：再编辑表层 prompt 不输出显式 Keep 句（§25，v6.14 保留）
V613_NO_KEEP_SURFACE_RULES = """\
v6.13 No-Keep Surface Prompt（§25，在 v6.12 之上）:
- **Do NOT output standalone Keep / Preserve / Clearly show / Reframe the same moment without altering sentences** in edit surface prompts.
- **Keep 信息仅用于规划表与候选评选**；生成 prompt 只写一次 story state 继承 + Camera transformation。
- **Forbidden**: `Keep A, B, and C present in the frame.` or element checklists between story state and Camera transformation.
"""

# v6.14 §16：Story-State / Screen-Content + Camera Transformation 双段式
V614_DUAL_BLOCK_RULES = """\
v6.14 Dual-Block Edit Surface Prompt（§16，在 v6.13 之上）:
- **Block 1 — Story-State / Screen-Content**: `Use case_base_frame0x.png as the direct source for the exact story state of [character + action + pose + prop state + scene state].`
  - 自然交代这帧画面正在呈现什么（姿态、动作、道具状态、场景状态）。
  - **禁止** Keep / Preserve / Clearly show；**禁止** close-up、focused on、foreground/background、softly visible、close proximity、subtle reflection 等镜头用语。
- **Block 2 — Camera Transformation**: `Camera transformation: create a [shot type] with strong focus on [visual emphasis].`
  - **唯一**负责镜头类型、视觉焦点、强调细节；可选轻量上下文（如 lotus still visible as context）。
- **v6.14 标准模板（一段式输出）**:
  `Generate case_edit_frame0x.png: Use case_base_frame0x.png as the direct source for the exact story state of [screen content]. Camera transformation: create a [shot type] with strong focus on [visual emphasis]. [Style].`
- **Edit Prompt Compiler**（§18）：规划表提供 story_state / camera_type / visual_focus 时，按模板编译，不抄 Keep 清单。
"""

V67_EDIT_PICK_OVERLAY = """\
v6.7 + v6.9 + v6.10 + v6.11 + v6.12 + v6.13 + v6.14 TOP PRIORITIES FOR EDIT-FRAME (lens redesign) SELECTION:
- **Camera transformation vs base is primary**: each edit frame needs clear shot-scale, angle, side, composition, focus, or depth delta (§9.2). Relighting-only is NOT enough.
- **v6.9 Neutral medium shot = failure**: a neutral eye-level medium shot that only polishes the base medium shot is invalid unless it adds low-angle, OTS, foreground occlusion, strong depth, or product-focused composition.
- **v6.10 Narrative readability = failure**: close-up/insert that becomes a局部展示图 — story beat unreadable, no character/scene/action anchor, or texture/spark/fingerprint dominates the frame.
- **Story function → camera first** (§21.2): awakening beats need face/eye close-up; detail beats need close-up detail / foreground-detail (not macro insert checklist); climax needs low-angle hero — not another full-body medium.
- **Nine-grid fallback** (§21.5): if the planned special camera fails, prefer another special camera (close-up / foreground-detail / overhead / low-angle / OTS), NOT reverting to base medium framing.
- **Story state preservation**: identity, pose result, object states, scene layout, lighting direction locked to base.
- **At most one** light-polish row in N frames; even that row needs a visible camera/design shift.
- **v6.11 Prompt Constraint ≠ Evaluation Criterion**: check identity/prop/scene in pick even when the surface prompt did not list every preserve item.
- **v6.12 Layer separation**: reject candidates driven by story-block camera leaks (oversized hand/prop, lost face/scene anchors); pick should match Camera transformation intent, not duplicated Show-block framing.
- **v6.13 No-Keep**: evaluate story-state preservation in pick using planning `keep` fields; **do not expect** a `Keep … present in the frame` sentence in the surface prompt.
- **v6.14 Dual-block**: pick 时对照 Camera transformation 是否落实规划镜头；story block 应可读叙事，不应含镜头词泄漏。
""" + V610_BALANCED_EDIT_RULES + "\n" + V611_SURFACE_EDIT_RULES + "\n" + V612_LAYER_SEPARATION_RULES + "\n" + V613_NO_KEEP_SURFACE_RULES + "\n" + V614_DUAL_BLOCK_RULES

# v6.9 §21.4 + v6.10 §22.4 九宫格默认「目标 edit 镜头」（frame_idx 1-based）
_V69_NINE_GRID_CAMERAS: Dict[int, str] = {
    1: "high-angle wide / foreground-depth wide (establish, stronger than base medium)",
    2: "face close-up / extreme close-up of eyes (awakening — NOT full-body medium)",
    3: "close-up detail shot / foreground-detail composition (hand-ring contact, keep face & lotus readable)",
    4: "side trajectory shot / diagonal action composition",
    5: "reaction close-up / over-the-shoulder reaction",
    6: "overhead shot / high-angle spatial relation",
    7: "low-angle hero shot / silhouette against sky",
    8: "foreground-background composition / over-the-shoulder",
    9: "wide aftermath / symbolic close-up / hero-object foreground",
}

_V69_NINE_GRID_STORY: Dict[int, str] = {
    1: "Establish / environment",
    2: "Awakening / realization",
    3: "Detail activation",
    4: "Motion begins",
    5: "Reaction / impact",
    6: "Spatial escalation",
    7: "Climax / hero reveal",
    8: "Result processing",
    9: "Closure / aftermath",
}

_V610_OVERFIT_PROMPT_RE = re.compile(
    r"fingerprint\s+texture|glowing\s+energy\s+bridge|magnifying\s+the\s+contact|"
    r"macro\s+insert|isolate\s+the\s+tactile|"
    r"blur\s+the\s+background(?!\s+while)",
    re.IGNORECASE,
)

_V610_OVERFIT_PLAN_RE = re.compile(
    r"\bhand\s+insert\b(?!\s+with)|fingerprint|能量桥|微观|macro\s+insert|"
    r"magnify(?:ing)?\s+the\s+contact",
    re.IGNORECASE,
)

_V611_BLOATED_SURFACE_PROMPT_RE = re.compile(
    r"case_char_\d+|case_prop_\d+|case_scene_\d+|"
    r"preserve\s+every|strict\s+reference\s+to\s+case_|"
    r"Use\s+case_(?:char|prop|scene)_",
    re.IGNORECASE,
)

_IDENTITY_CORRECTION_EDIT_RE = re.compile(
    r"identity\s+restor|prop\s+correction|纠偏|shape\s+already\s+wrong|"
    r"restore\s+identity|身份纠偏|道具纠偏|pure\s+identity\s+restoration",
    re.IGNORECASE,
)

# v6.12 §24.4：story state 块（Camera transformation 之前）不得含镜头用语
_V612_CONTENT_BLOCK_INTRUSION_RE = re.compile(
    r"\bclose\s+proximity\b|\bclose-up\b|\bclose\s+up\b|\binsert\s+shot\b|"
    r"\bextreme\s+close\b|\bmacro\b|\bwide\s+shot\b|\boverhead\s+shot\b|"
    r"\bfocused\s+on\b|\bas\s+the\s+visual\s+center\b|\bvisual\s+center\b|"
    r"\bin\s+the\s+foreground\b|\bin\s+the\s+background\b|"
    r"\bforeground\b|\bbackground\b|"
    r"\bsoftly\s+visible\b|\bsoftly\s+recognizable\b|\bblurred\b|"
    r"\bshallow\s+depth\b|\bdepth\s+of\s+field\b|"
    r"\bcapturing\s+the\s+tension\b|\bsubtle\s+(?:light\s+)?reflection\b|"
    r"\bmagnifying\b|\boccupying\s+the\s+frame\b|\btight\s+framing\b|"
    r"\bwith\s+strong\s+focus\b|\bstrong\s+focus\s+on\b|"
    r"\bisolate\b|\bisolating\b",
    re.IGNORECASE,
)

# v6.13 §25.2：表层 prompt 中的显式 Keep / Preserve 句
_V613_KEEP_SURFACE_SENTENCE_RE = re.compile(
    r"\b(?:Keep|Preserve|Clearly show|Reframe the same moment without altering)\b",
    re.IGNORECASE,
)

_CAMERA_TRANSFORM_SPLIT_RE = re.compile(
    r"(Camera\s+transformation\s*:)",
    re.IGNORECASE,
)

_SPECIAL_CAMERA_RE = re.compile(
    r"close-up|insert|overhead|low-angle|over-the-shoulder|extreme close-up|"
    r"foreground-depth|foreground-detail|close-up detail|high-angle wide|"
    r"eye extreme|diagonal action|silhouette|"
    r"face close-up|hero shot|OTS",
    re.IGNORECASE,
)

# ──────── 资源图评选指令 ────────
RESOURCE_PICK_SYSTEM_PROMPT = """\
你是一位专业的动画角色设计与场景设计评审专家。
你的任务是从多张候选图中挑选最适合作为"资源库锁定图"的一张。
资源库锁定图的作用是为后续所有分镜帧提供一致性参考，因此必须严格符合以下标准。

## 人物参考图 (case_char_xx.png) 评选标准（v6.7 §3.1）
1. **单人物、单姿势、单视角**：一张图里只能一个人、一个自然站姿、一个确定视角；禁止多视角并排、禁止 character sheet、禁止同图重复多人
2. 全身构图：从头到脚完整入镜；禁止半身裁切
3. 体态完整性（一票否决）：四肢自然，手指数量正确，无穿模断裂
4. 视角写法：优先 **front three-quarter view only** 或 **front view only**；若 prompt 写「front and three-quarter 同时可见」类双视角句 ⇒ 候选易为多视图拼接，应判不合格或降权
5. 身份特征清晰：脸型、发型、服装、武器位置（若背在背上须写清 shoulder/handle 等，避免手持多姿势）一目了然
6. 背景干净：白底或浅色底，无场景无特效
7. 排除项：多人物、多姿势、多视角、畸形、模糊面部、现代元素

## 场景参考图 (case_scene_xx.png) 评选标准
1. 空间结构完整：展示场景的整体布局（门窗、家具、道具位置）
2. 无人物：画面中最好不出现人物
3. 光源与氛围：光线方向明确、时间感/天气感一致、氛围符合描述
4. 关键地标清晰：主要空间元素（桌椅/石台/树木等）辨识度高
5. 可复用性：后续帧可以从该图中继承空间布局而不产生矛盾

## 道具参考图 (case_prop_xx.png) 评选标准
1. 外形、材质、纹理清晰
2. 背景干净
3. 大小比例合理
4. 特殊状态（发光、碎裂等）清楚展现

## 输出格式（必须严格遵守，二选一）

{PICK_OUTPUT_FORMAT_RULE}

如果有至少一张基本合格：
选择第X张，原因：<简要说明为什么这张最符合上述标准，以及其他候选的主要不足>

如果所有候选图都存在严重缺陷（如人物截断缺肢、面部严重变形、构图完全不符合要求），不要勉强选择，请回复：
全部不合格，原因：<所有候选的共性严重问题>，建议：<可先用中文或条列写修改方向；若方便也可直接给出以 `Generate <资源文件名>:` 开头的完整英文一段式 prompt。仅中文建议时系统会自动合并到上一版英文 prompt 并请求模型改写，但直接给出英文 Generate 行可减少一轮调用。>
""".format(PICK_OUTPUT_FORMAT_RULE=PICK_OUTPUT_FORMAT_RULE)

# 资源 prompt 改写（评选「全部不合格」后，把中文/条列意见合并进英文 Generate 行）
RESOURCE_PROMPT_REVISE_SYSTEM_PROMPT = """\
你是文生图 prompt 工程师，负责在**保留上一轮英文 prompt 中的身份、服饰、场景类型、风格与否定约束**的前提下，
根据评审给出的「原因」和「修改意见」（可能为中文或条列），产出**新的一行**可直接提交给文生图 API 的说明。

硬性要求：
1. 输出**只包含一行**正文，不要 Markdown、不要代码围栏、不要前后缀说明。
2. 这一行必须严格以英文 `Generate <资源文件名>:` 开头；`<资源文件名>` 与用户消息中给出的文件名**逐字一致**（例如 case_char_01.png）。
3. 冒号后为**英文**主体：把修改意见落实为具体画面约束（如 full body head-to-toe、plain white background、3D animation style 等）；可用简短英文短语表达原中文里的负向约束（如 no cropped legs, no half-body framing），但不要发明与上一轮矛盾的新角色身份或新场景类型。
4. 若修改意见与上一轮信息冲突，以修改意见为准，但仍保持资源类型一致（人物参考仍是**单人物、单姿势、单视角**全身立绘，不要变成叙事插画或多视图设定图）。
"""

# ──────── 第一阶段：基础剧情帧评选指令 ────────
BASE_FRAME_PICK_SYSTEM_PROMPT = V66_BASE_PICK_OVERLAY + """\
你是一位专业的动画分镜质检专家。
当前阶段：v6.5 第一阶段——稳定剧情基础帧（case_base_frame0x.png）。
你的任务是从多张候选基础帧中挑选最适合作为"剧情骨架参考"的一张。

基础帧的核心使命不是最炫的画面，而是后续所有派生镜头都要参考它，
所以稳定 > 视觉冲击；剧情清楚 > 镜头复杂。

v6.5 的核心强调：
- Scene Composition Accuracy（v6.5 §9.1 第2优先级）：候选必须继承 case_scene_01.png 的原始空间结构、
  主视角、关键物体位置、光源方向和真实锚点——不能只继承氛围而丢失构图。
- No Invented Anchor（v6.5 §9.1 第3优先级）：候选不能包含场景参考图中不存在的入口、门槛、台阶、
  门框、石柱、窗户、桌子、石台等新场景结构。发明新锚点等同于场景一致性失败。
- Frame Count Fit（v6.5 §9.1 第1优先级）：候选必须能落在当前 N 帧规划的当前剧情节点上；
  如果用户选择 N=5/6 等任意帧模式，候选不能因为剧情冗余或缺失关键因果而被选中。
- Subject–Camera Relation（人物正面/背面/侧面/三分之二面对镜头）必须严格匹配 prompt；
  prompt 写了 back view / his back faces the camera 时，候选必须真的是背影；
  prompt 写了 front view / three-quarter front view 时，候选必须真的能看到正脸/正面服饰；
  人物面向目标物 ≠ 人物背对镜头，不要被画面"漂亮"误导。
- Opening Orientation Reasonableness（仅 case_base_frame01.png）：
  开场朝向必须服务于开场叙事意图（介绍人物/介绍空间/进入动作/道具线索/悬念），
  不要因为"背影更稳"就默认选背影；当 prompt 明确要求正面/三分之二正面时，
  必须挑出真正的正面候选，背影候选直接不合格。
- Reference Stability：候选应承袭上一帧的镜头尺度、人物大小和可见面；
  应是 wide shot 却被某张候选拉近成中近景/特写，等同于参考链漂移，应排除。

## 评选维度（按优先级排序）

### 0. Frame Count Fit（v6.5 §9.1 首位维度）
- 候选是否准确表现 N 帧规划中当前帧的剧情节点
- 候选不能让规划中"开场" / "推进" / "关键动作" / "收束"等节点错位
- 候选不能与前后帧叙事重复（短帧数模式下尤其严格）

### 0.5 Scene Composition Accuracy（v6.5 §9.1 一票否决）
- 候选是否继承了 case_scene_01.png 的原始空间结构、主视角、关键物体位置和光源方向
- 仅继承"氛围"但构图/桌子位置/窗户位置/书架布局被重新生成 ⇒ 排除
- 尤其 case_base_frame01.png：场景参考图是空间主参考，候选必须保持其原始构图

### 0.6 No Invented Anchor（v6.5 §6.3 一票否决）
- 候选是否包含了场景参考图中不存在的入口、门槛、台阶、门框、石柱、窗户、桌子、石台等新结构
- 凭空生成的新场景元素等同于"发明锚点"，直接排除
- prompt 中写了 "Do not create a new doorway / entrance threshold / stone steps" 等约束时更要严格检查

### 1. 人物体态完整性（一票否决）
- 四肢比例自然，手臂无拉伸/扭曲/多余关节，手指数量正确
- 身体无明显穿模、断裂、关节异常
- 即使 prompt 契合度很高，只要存在明显畸形就排除

### 2. Subject–Camera Relation 准确性（一票否决）
- prompt 写 back view / his back faces the camera ⇒ 候选必须真的是背影，露出正脸即不合格
- prompt 写 front view / facing the camera ⇒ 候选必须看到正脸 / 正面服饰，背影候选不合格
- prompt 写 three-quarter back / three-quarter front / side view ⇒ 候选必须保持对应可见面
- 当帧是 case_base_frame01.png 且 prompt 明确写明了开场朝向时，
  候选的朝向必须严格匹配；不要用"背影更稳定"的理由替换掉 prompt 指定的正面/侧面候选
- 哪怕画面更好看，只要 Subject–Camera 错就排除

### 3. 场景环境一致性（一票否决）
- 地面纹理、空间结构、建筑细节、光源方向是否与 case_scene / 上一基础帧一致
- 凭空多出新道具、月亮窗户、散落卷轴、前景柱子等元素都判为不合格

### 4. 基础镜头准确性（一票否决）
- 必须是确定的 wide shot 或 medium shot
- 镜头尺度应与上一基础帧保持一致（应 wide 就 wide，应 medium 就 medium）
- 候选图被拉近成特写、俯视、过肩、背面遮挡、极端低角度、大幅旋转、强烈前景遮挡 ⇒ 排除

### 5. Reference Stability（参考稳定性）
- 候选图的人物大小、人物比例、所处位置应该承袭上一帧
- 特别警惕：因为重复引用 case_char_01.png 而导致镜头被拉近、人物正脸被强行展示、构图被破坏的候选
- 应保持上一帧已经建立的镜头距离和人物可见面

### 6. Prompt 契合度（剧情状态准确）
- 画面主体、动作结果、视线、道具状态是否与当前剧情节点匹配
- 不应出现的元素（prompt 中"Do not"的部分）确实没有出现

### 7. 人物一致性（与资源图对比）
- 发型、服装款式与配色、武器/饰品是否与 case_char 一致

### 8. Subject–Object Relation 准确性
- 人物面向/背向/侧对目标物是否正确
- 注意：这与 Subject–Camera Relation 是两件事，不能混淆

### 9. 锚点定位准确性（Anchor Accuracy）
- 用脚/鞋与地面纹理、台阶、石柱底座等具体可见锚点判断人物落点

### 10. 后续参考稳定性（Future Usability）
- 这一帧能否清楚地为后续基础帧 / 派生镜头提供稳定参考
- 主要身体结构是否足够清楚（不要被复杂镜头切掉关键信息）

## 选择原则
- 七条一票否决：场景构图准确、无发明锚点、人物体态完整、Subject–Camera 关系、场景一致、基础镜头、参考稳定性
- 当 prompt 明确写了 back view / his back faces the camera 时，
  候选中只有真正的背影才能入选；其余露脸、转正、侧脸的都必须排除，哪怕画面更"炫"
- 派生镜头会在第二阶段单独生成，不要在基础帧阶段就追求镜头多样性

## 输出格式（必须严格遵守，二选一）

""" + PICK_OUTPUT_FORMAT_RULE + """

如果有至少一张基本合格：
选择第X张，原因：<说明该候选的优势，及其他候选的主要不足，必须点明各候选的 Subject–Camera 关系（正面/背面/侧面/三分之二）>

如果所有候选都存在严重缺陷（朝向错误 / 场景不一致 / 人物畸形 / 基础镜头被破坏 / 剧情状态完全不符），请回复：
全部不合格，原因：<所有候选的共性严重问题>，建议：<对生成 prompt 的具体修改建议，例如"补上 his back faces the camera 与 do not show his face"或"移除 case_char_01.png 的引用避免镜头被拉近">
"""


# ──────── 第二阶段：全帧镜头再设计 —— 编辑帧评选指令（v6.7） ────────
EDIT_FRAME_PICK_SYSTEM_PROMPT = V67_EDIT_PICK_OVERLAY + """\
你是一位专业的电影镜头设计与分镜质检专家。
当前阶段：**v6.13 第二阶段——全帧镜头再设计**（默认仅 case_base_frame0x.png；story state 继承一次 + Camera transformation；**不**输出 Keep 句；不是轻微修图/补光）。

你的任务是从多张候选编辑帧中挑选最适合作为"最终分镜帧"的一张。

v6.7 的核心约定：
- 不论 N 是 4、5、6 还是 9，第二阶段都对所有基础帧做再编辑；
- **主目标是镜头再设计**（景别/机位/镜头侧/构图重点/视觉重点/节奏），补光、修瑕、统一色调只是附带；
- 剧情状态、人物位置、动作结果、道具状态、场景结构必须严格继承基础帧；
- 不能让编辑后变成另一 beat；
- 编辑强度由规划表决定，但必须能在画面上看到相对 base 的 **camera delta**（见工作流文档 §9.2）。

## 评选维度（按 v6.7 §13 优先级）

### 1. Camera Transformation Accuracy（相对 base 的镜头变化，一票否决）
- 与基础帧相比，是否出现**明确**的景别/角度/机位侧/构图/焦点/纵深之一的变化？
- 若与 base **同景别 + 同机位 + 同构图**、仅更亮更锐 ⇒ 视为镜头再设计失败（除非规划明确本帧为整组唯一轻量 polish 且仍有可核查的微调）。
- **v6.9**：普通平视中景、与 base 几乎相同的全身中景叙事 ⇒ **一律淘汰**（觉醒/反应/高潮节点尤其不能用中景平铺）。

### 2. Story State Preservation（一票否决）
- 人物身份、位置、动作结果是否与基础帧一致
- 道具/能量/特效状态是否与基础帧一致
- 场景结构、光源方向、地面纹理是否承自基础帧

### 3. Camera Diversity Value
- 是否为整组最终分镜提供了**新的镜头类型或构图价值**（而非重复 base 的视觉句子）。

### 4. Subject/Object Continuity
- 人物、道具、场景与基础帧一致，无凭空新增角色或道具。

### 5. Narrative Readability（v6.10 §22.5，一票否决）
- 观众能否读出当前剧情节点？特殊镜头不能变成「局部纹理/火花/指纹展示图」。
- close-up / insert 是否仍保留角色、场景或动作锚点（脸、莲花、祭坛边缘、手-道具关系等）？

### 6. Detail Overfit Risk（v6.10 §22.11）
- 指纹、纹路、能量火花、触点是否被画成装饰性主元素？
- 多个局部细节是否互相抢主角、导致主体权重混乱？

### 7. Visible Detail Quality
- 主视觉焦点是否清楚；支持性细节是否克制（≤2–3 个），未压过叙事。

### 8. Context Anchor Preservation（v6.10 §22.6）
- 关键锚点仍可识别，Scene Core 可读；背景虚化后同一场景仍可辨认。

### 9. 人物体态完整性（一票否决）
- 四肢比例自然，手指数量正确，无明显畸形。

### 10. 人物身份与服装一致性
- 与 case_char / 基础帧一致。

## 与 v6.5 Level 标签的对应（规划表仍用 Level 1–4）
- Level 3 / Level 4：应出现**显著的**镜头再设计（medium→insert、wide→strong wide、加入 OTS / high-angle / low-angle 等），且不得破坏剧情状态。
- Level 1 / Level 2：**整组 N 帧中最多一行**；即使如此也必须在画面中看到**可描述的**机位/构图微调，禁止「仅 cleanup / improve lighting」作为唯一策略。

## 选择原则
- **若候选与 base 几乎一样，即使更干净也不优先**（除非本帧被规划为整组唯一的轻量 polish 且仍有可见 delta）。
- 体态完整性、剧情状态、可追溯性仍是底线。
- 在底线通过的候选中，优先选 **camera delta 明显 + 剧情可读 + 细节未过拟合** 的一张。
- **漂亮局部展示图但读不出剧情节点 ⇒ 淘汰**（v6.10）。
- **v6.11**：生成 prompt 未写明的身份/道具/场景项，仍可在评选中检查；**不要求** surface prompt 堆叠所有 preserve 项。
- **v6.13**：规划表「必须保持」用于**候选评选**；surface prompt 不应含 `Keep … present in the frame`。若候选丢失 keep 项，在评选理由中说明，而非要求 prompt 补 Keep 句。

## 输出格式（必须严格遵守，二选一）

如果有至少一张基本合格：
选择第X张，原因：<说明优势；必须点明每张候选相对 base 的镜头变化是否成立>

如果所有候选都存在严重缺陷，请回复：
全部不合格，原因：<…>，建议：<…>
"""

# 旧别名，向后兼容（部分历史 import 仍指向 SHOT_FRAME_PICK_SYSTEM_PROMPT）
SHOT_FRAME_PICK_SYSTEM_PROMPT = EDIT_FRAME_PICK_SYSTEM_PROMPT

RESOURCE_MODES = ("auto", "upload", "generate")
RESOURCE_MODE_LABELS = {
    "auto": "自动（有图用上传，缺图再生成）",
    "upload": "用户上传（不调用文生图生成资源库）",
    "generate": "模型生成（按规划 prompt 抽卡生成资源库）",
}

# 对外 re-export，供 run.py 使用
__all__ = ["WorkflowOrchestrator", "RESOURCE_MODE_LABELS", "PIPELINE_MODE_LABELS"]


class WorkflowOrchestrator:
    def __init__(
        self,
        case_name: str,
        output_base_dir: str = None,
        resource_mode: str = "auto",
        vlm_provider: str = None,
        reset_ltx: bool = False,
    ):
        if resource_mode not in RESOURCE_MODES:
            raise ValueError(
                f"resource_mode 必须是 {RESOURCE_MODES} 之一，得到: {resource_mode!r}"
            )
        self.case_name = case_name
        self.resource_mode = resource_mode
        if output_base_dir is None:
            output_base_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "qwen"
            )
        self.output_dir = os.path.join(output_base_dir, case_name)
        os.makedirs(self.output_dir, exist_ok=True)

        self.vlm = QwenVLClient(provider=vlm_provider)
        self.img_gen = ImageGenerator()
        self.parser = ResponseParser()

        self.state_file = os.path.join(self.output_dir, "workflow_state.json")
        self.log_file = os.path.join(self.output_dir, "workflow_log.jsonl")
        self.summary_file = os.path.join(self.output_dir, "workflow_summary.json")
        self.frame_prompts_file = os.path.join(self.output_dir, "frame_prompts.json")
        self.frame_prompts: Dict[str, Any] = {
            "case_name": case_name,
            "frames": {},
        }

        self.summary: Dict = {
            "case_name": case_name,
            "status": "未完成",
            "overview": "",
            "storyboard_plan": "",
            "resources": {},
            "base_frames": {},
            "edit_plan": [],
            "edit_frames": {},
            "final_frames": {},
            "ltx_shots": {},
        }

        self.state: Dict = {
            "step": "init",
            "resources_done": [],
            "base_frames_done": [],
            "edit_frames_done": [],
            "current_frame_index": 0,
            "total_frames": 0,
            "init_plan_response": "",
            "pending_frame_prompt": "",
            "edit_plan_response": "",
            "edit_plan": [],
        }

        self.resource_files: Dict[str, str] = {}
        self.base_frame_files: Dict[str, str] = {}
        self.edit_frame_files: Dict[str, str] = {}
        self.ltx_ext = LTXWorkflowExtension(self)
        self._reset_ltx_on_load = reset_ltx

        self._load_state()

    async def run(
        self,
        idea: str,
        num_characters: Optional[int] = None,
        num_scenes: Optional[int] = None,
        grid_n: int = 9,
        opening_orientation: str = "auto",
        resource_mode: Optional[str] = None,
        pipeline_mode: str = "frames",
        video_target_seconds: Optional[float] = None,
        long_shot_mode: bool = False,
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
    ):
        """opening_orientation: 'front' / 'back' / 'side' / 'auto'
        指定 case_base_frame01.png 的人物-镜头朝向；'auto' 时由 VLM 中立判断。
        resource_mode: 'upload' | 'generate' | 'auto'（见 RESOURCE_MODE_LABELS）。
        """
        if resource_mode is not None:
            if resource_mode not in RESOURCE_MODES:
                raise ValueError(
                    f"resource_mode 必须是 {RESOURCE_MODES} 之一，得到: {resource_mode!r}"
                )
            self.resource_mode = resource_mode

        self._load_state()
        self.state["resource_mode"] = self.resource_mode
        from .config import LTX_MAX_PARALLEL, LTX_VIDEO_CANDIDATES
        self.ltx_ext.apply_runtime_config(
            pipeline_mode=pipeline_mode,
            video_target_seconds=video_target_seconds,
            long_shot_mode=long_shot_mode,
            long_shot_seconds=long_shot_seconds,
            ltx_video_candidates=ltx_video_candidates or LTX_VIDEO_CANDIDATES,
            ltx_max_parallel=ltx_max_parallel if ltx_max_parallel is not None else LTX_MAX_PARALLEL,
            ltx_grounded_safe_mode=ltx_grounded_safe_mode,
            ltx_width=ltx_width,
            ltx_height=ltx_height,
            ltx_resolution=ltx_resolution,
            ltx_stitch_mode=ltx_stitch_mode,
            ltx_generate_bridge_candidates=ltx_generate_bridge_candidates,
            ltx_use_bridge_at_export=ltx_use_bridge_at_export,
        )
        self.sync_workflow_from_disk(advance_step=True, announce=True)
        if pipeline_mode in ("video", "full"):
            self.ltx_ext.sync_step_from_disk()
        self._save_state()

        run_frames = pipeline_mode in ("frames", "full")
        run_video = pipeline_mode in ("video", "full")

        if run_video and pipeline_mode == "video":
            self.ltx_ext.ensure_video_pipeline_prerequisites()

        if run_frames:
            if self.state["step"] == "init":
                await self._step_init_plan(
                    idea, num_characters, num_scenes, grid_n, opening_orientation,
                )

            if self.state["step"] == "resources":
                await self._step_build_resources()

            if self.state["step"] == "resource_review":
                await self._step_resource_review(grid_n, opening_orientation)

            if self.state["step"] == "base_frames":
                await self._step_generate_base_frames()

            if self.state["step"] == "edit_plan":
                await self._step_select_edit_plan()

            if self.state["step"] == "edit_frames":
                await self._step_generate_edit_frames()

            if self.state["step"] == "final_assembly":
                self._step_final_assembly()

        if run_video:
            if pipeline_mode == "video":
                self.ltx_ext.sync_step_from_disk()
            if self.state["step"] == "ltx_plan":
                await self.ltx_ext.step_ltx_plan(idea)
            if self.state["step"] == "ltx_generate":
                await self.ltx_ext.step_ltx_generate()

        if self.state["step"] == "done":
            if run_video and self.state.get("ltx_shots"):
                self.summary["status"] = "已完成（含 LTX 视频）"
            else:
                self.summary["status"] = "已完成"
            self._save_summary()

        logger.info("=== Workflow complete! Output: %s ===", self.output_dir)
        print(f"\n{'='*60}")
        if run_video and self.state.get("ltx_shots_done"):
            print(f"工作流完成！参考帧与 LTX 候选视频已保存到: {self.output_dir}")
            print("  LTX：请在各 case_ltx_shot_XX_candidates/ 中人工选片，")
            print("  可将选中文件复制为 case_ltx_shot_XX.mp4 作为正式成片。")
        elif run_frames:
            print(f"工作流完成！参考帧已保存到: {self.output_dir}")
        else:
            print(f"工作流完成！输出目录: {self.output_dir}")
        print(f"全流程总结: {self.summary_file}")
        if run_video and (
            self.state.get("ltx_shots")
            or os.path.exists(self.ltx_ext.summary_file)
        ):
            print(f"LTX shot 总结: {self.ltx_ext.summary_file}")
        if os.path.exists(self.frame_prompts_file):
            print(f"逐帧 prompt（可提前查看）: {self.frame_prompts_file}")
        print(f"{'='*60}")

    async def _step_init_plan(
        self,
        idea: str,
        num_characters: Optional[int],
        num_scenes: Optional[int],
        grid_n: int,
        opening_orientation: str = "auto",
    ):
        mode_label = "9宫格核心模式" if grid_n == 9 else "任意帧数衍生模式"
        print(
            f"\n[Step 1/5] 初始化规划（v6.5 {mode_label} N={grid_n}：稳定基础帧 → 全帧再编辑）..."
        )
        logger.info(
            "Step 1: Init plan for idea='%s', grid=%d, opening=%s",
            idea, grid_n, opening_orientation,
        )

        with open(WORKFLOW_DOC_PATH, "r", encoding="utf-8") as f:
            workflow_doc = f.read()

        self.vlm.set_system_prompt(workflow_doc)

        user_input = f"idea：{idea}。"
        if num_characters is not None:
            user_input += f" {num_characters}人物"
        if num_scenes is not None:
            user_input += f"{num_scenes}场景"
        else:
            user_input += " 单人物单场景"
        is_classic_9 = (grid_n == 9)
        is_square_grid = grid_n in (4, 9, 16, 25)
        grid_label = f"{grid_n}宫格参考" if is_square_grid else f"{grid_n} 帧参考序列"
        mode_phrase = (
            "v6.5 9宫格核心模式：严格沿用 v6.3 标准 9 宫格逻辑，不启用任意帧压缩"
            if is_classic_9 else
            f"v6.5 任意帧数衍生模式：从标准 9 beat 骨架压缩或扩展得到 {grid_n} 帧"
        )
        user_input += (
            f" {grid_label}"
            f"（{mode_phrase}；两阶段范式：先稳定基础 {grid_n} 帧，再对所有基础帧逐张再编辑）"
        )
        opening_block = self._build_opening_directive(opening_orientation)

        # 帧数模式相关说明
        if is_classic_9:
            mode_block = (
                "## 帧数模式（v6.5 §3.1：9宫格核心保底）\n"
                "用户使用 N=9 标准 9 宫格模式，必须严格沿用 v6.3 的标准 9 宫格节奏："
                "B1=建立人物与场景 / B2=进入行动位置 / B3=开始关键动作 / "
                "B4=接近或触碰目标 / B5=目标物首次变化 / B6=力量或变化升级 / "
                "B7=结果显现 / B8=人物处理结果或离开 / B9=收束或余波。\n"
                "**禁止启用任意帧压缩逻辑**，规划表第二列直接填 B1~B9。\n"
            )
            beat_constraint = (
                f"- **N=9 9宫格核心模式**：规划表的「对应9宫格beat」列必须严格填写 B1~B9，"
                f"每帧一个 beat，不允许合并或跳过；如果某帧不符合标准 9 beat 节奏，应调整剧情节点描述而不是合并 beat。"
            )
        else:
            mode_block = (
                "## 帧数模式（v6.5 §3.2 / §3.3：任意帧数衍生）\n"
                f"用户使用 N={grid_n} 任意帧数模式，**不要重新发明节奏**。\n"
                "请先在内部建立标准 9 beat 骨架："
                "B1=建立人物与场景 / B2=进入行动位置 / B3=开始关键动作 / "
                "B4=接近或触碰目标 / B5=目标物首次变化 / B6=力量或变化升级 / "
                "B7=结果显现 / B8=人物处理结果或离开 / B9=收束或余波。\n"
                f"然后**按工作流文档 §3.3 的标准映射**把 9 beat 压缩成 {grid_n} 帧：\n"
                "- N=4：01=B1, 02=B3+B4, 03=B5+B6+B7, 04=B8+B9；\n"
                "- N=5：01=B1, 02=B2+B3, 03=B4+B5, 04=B6+B7, 05=B8+B9；\n"
                "- N=6：01=B1, 02=B2, 03=B3+B4, 04=B5+B6, 05=B7, 06=B8+B9；\n"
                "- N=7：01=B1, 02=B2, 03=B3, 04=B4+B5, 05=B6, 06=B7, 07=B8+B9；\n"
                "- N=8：建议合并 B8+B9 或 B3+B4；\n"
                "- N>9：在 B3/B4/B5/B6 中细分，不要重复开场或收束。\n"
                "如果当前 N 不在上述列表，可在 9 beat 骨架内自由合并/细分，但**每一行都必须能写出对应 beat**。\n"
                "**严禁出现帧数与用户指定 N 不一致的规划表**（v6.5 §17.1）。\n"
            )
            beat_constraint = (
                f"- **N={grid_n} 任意帧数模式**：规划表的「对应9宫格beat」列必须为每帧标注其对应的 beat 或合并 beat"
                f"（如 B1 / B2+B3 / B5+B6+B7 等），**整组 {grid_n} 行必须覆盖 B1~B9 的关键因果链**；"
                "若用户 idea 中没有某个 beat（如没有收束），可在该列写 N/A 并简要说明。"
            )

        user_input += (
            "\n\n请按照 v6.5 工作流文档输出：\n"
            f"{self._build_resource_mode_directive()}\n"
            "1) 视频概述；\n"
            f"2) **帧数选择小节**：明确写出『参考帧数量：N={grid_n}』『选择理由：……』"
            f"『模式：{('9宫格核心模式' if is_classic_9 else '任意帧衍生模式')}』；\n"
            "3) 人物、场景、道具数量；\n"
            "4) 资源库构建（每个资源用英文一段式 prompt；**人物 case_char 必须遵守 v6.7 §3.1**："
            "single character, single pose, single view, full body, plain background；"
            "推荐用 `front three-quarter view only` 或 `front view only`，"
            "禁止 `front view and three-quarter front view are clearly visible` 等多视角句式；"
            "武器若在背上须写清 strapped across back / handle near shoulder，避免手持多姿势）；"
            "场景图要包含可复用锚点）；\n"
            f"5) **初步**基础剧情 {grid_n} 帧规划表，**列顺序固定为 9 列**："
            "基础帧 / 对应9宫格beat / 剧情节点 / 基础镜头 / 开场或镜头意图 / 位置锚点 / 朝向关系 / 画面重点 / 连续性变化；\n"
            "   （注意：此规划表是初步版本——资源图生成并选定后，会进行 v6.5 §6「资源图内容审查」，"
            "届时会基于场景参考图中真实可见的锚点修订规划表和首帧 prompt。但初步规划仍然要认真写，给后续审查提供起点。）\n"
            "6) 单独写一小节『case_base_frame01.png 开场意图与朝向选择』，说明开场镜头叙事功能和最终选定的 Subject–Camera Relation；\n"
            "7) 第一基础帧的英文一段式生成 prompt（格式：Generate case_base_frame01.png: ...）。\n\n"
            f"{mode_block}\n"
            f"{opening_block}\n"
            f"{V66_BASE_RULES}\n"
            "硬性约束（v6.5 第一阶段——基础帧阶段重点）：\n"
            f"- **用户指定总帧数为 {grid_n}**：规划表必须严格输出 {grid_n} 行，"
            f"对应 case_base_frame01.png ~ case_base_frame{grid_n:02d}.png；"
            f"绝不允许多写或少写，也不允许默认回到 9 帧。\n"
            f"{beat_constraint}\n"
            "- 命名必须用 case_base_frame0x.png；第二阶段会自动产出 case_edit_frame0x.png，"
            "因此第一阶段每张 base frame 都要做好"
            "「适合作为再编辑输入」"
            "的稳定性，不要写复杂镜头；\n"
            "- 每个基础帧的 camera layer 只能是 wide shot 或 medium shot 中的一个，且不能用 'wide shot or medium shot'、'wide / medium' 这类选择式表达；\n"
            "- 基础帧禁止使用 close-up / insert shot / overhead view / over-the-shoulder / Dutch angle / extreme low-angle / complex camera rotation / heavy foreground blocking；"
            "（这些镜头留给第二阶段 **v6.7 全帧镜头再设计**）；\n"
            "- prompt 中禁止使用 distance / closer / farther / near / slightly / partially visible 等抽象词，必须用脚/鞋/手与场景锚点的具体可见关系表达位置；\n"
            "- **必须显式写明 Subject–Camera Relation**：每帧 camera 描述里都要写明人物相对镜头是 back view / front view / side view / three-quarter front / three-quarter back 中的哪一种；\n"
            "  人物面向目标物 ≠ 人物背对镜头，两件事必须分别写。\n"
            "  如果当前帧应为背影，必须加上 'his back faces the camera' + 'Do not show his face, do not rotate the camera to the front side'；\n"
            "  如果当前帧应为正面/三分之二正面，必须加上 'facing the camera' / 'three-quarter front view'，并明确要看到 facial structure、collar、front robe pattern 等正面细节；\n"
            f"{V68_REFERENCE_RESOLVER_RULES}\n"
            "- v6.8 规划表必须为每一帧写出：可见人物/道具、新出现实体、hero product 是否可见、禁止出现实体；并为每帧规划 reference_images（不可默认仅上一帧）。\n"
            "- v6.7 规划表必须为每一帧写出 Scene Core、Camera-Side Track、Topology、Anchor Visibility Budget；最终英文生成 prompt 只保留必要约束，禁止把规划分析全文复制进 prompt。\n"
            "- 只输出第一基础帧 prompt，不要一次性输出全部基础帧 prompt。"
        )

        response = await self.vlm.chat(user_input)

        self._log("init_plan", {"user_input": user_input, "response": response})
        print("\n--- VLM 初始化规划 ---")
        print(response[:2000])
        if len(response) > 2000:
            print(f"... (共 {len(response)} 字符)")

        plan = self.parser.parse_init_plan(response)

        self.state["init_plan_response"] = response
        self.state["total_frames"] = grid_n
        self.state["pending_frame_prompt"] = plan.first_frame_prompt

        resource_prompts = {}
        for name, prompt in plan.resource_prompts.items():
            resource_prompts[name] = prompt
        self.state["resource_prompts"] = resource_prompts
        if not resource_prompts:
            logger.warning(
                "Step1 未解析到任何 resource_prompts（无 Generate case_char/scene/prop 行），"
                "Step2 auto 模式将跳过资源库生成"
            )
            print(
                "  ⚠ 警告：未从规划回复中解析到资源库 prompt，"
                "人物/场景/道具图将不会自动生成（请检查 VLM 输出格式）"
            )
        else:
            print(
                f"  已解析资源库 prompt {len(resource_prompts)} 项: "
                f"{', '.join(sorted(resource_prompts.keys()))}"
            )

        self.summary["idea"] = idea
        self.summary["grid"] = grid_n
        self.summary["resource_mode"] = self.resource_mode
        self.summary["overview"] = plan.overview
        self.summary["storyboard_plan"] = plan.storyboard_plan
        for rname, rprompt in plan.resource_prompts.items():
            self.summary["resources"][rname] = {
                "generation_prompt": rprompt,
                "chosen_candidate": None,
                "pick_reason": "",
                "file": rname,
            }
        self._save_summary()

        self.state["step"] = "resources"
        self._save_state()

    _RESOURCE_FILENAME_RE = re.compile(
        r"^case_(?:char|scene|prop|style)_\d+(?:_[a-z0-9_]+)*\.png$",
        re.IGNORECASE,
    )
    _RESOURCE_STUB_RE = re.compile(
        r"^(case_(?:char|scene|prop|style)_\d+)",
        re.IGNORECASE,
    )
    _BASE_FRAME_RE = re.compile(r"^case_base_frame(\d+)\.png$", re.IGNORECASE)
    _EDIT_FRAME_RE = re.compile(r"^case_edit_frame(\d+)\.png$", re.IGNORECASE)
    _FINAL_FRAME_RE = re.compile(r"^case_final_frame(\d+)\.png$", re.IGNORECASE)

    def _is_resource_library_file(self, filename: str) -> bool:
        return bool(self._RESOURCE_FILENAME_RE.match(filename))

    def _scan_resource_files_on_disk(self) -> Dict[str, str]:
        """扫描输出目录中用户上传或已生成的资源库图片（不含 base/edit 帧）。"""
        found: Dict[str, str] = {}
        if not os.path.isdir(self.output_dir):
            return found
        for fname in sorted(os.listdir(self.output_dir)):
            if not self._is_resource_library_file(fname):
                continue
            path = os.path.join(self.output_dir, fname)
            if os.path.isfile(path):
                found[fname] = path
        return found

    def _resolve_resource_alias(self, planned_name: str) -> Optional[str]:
        """规划文件名在磁盘上的实际对应文件（如 clean 变体 → 用户上传的无后缀版）。"""
        direct = os.path.join(self.output_dir, planned_name)
        if os.path.isfile(direct):
            return planned_name
        m = self._RESOURCE_STUB_RE.match(planned_name)
        if not m:
            return None
        stub = m.group(1).lower()
        disk = self._scan_resource_files_on_disk()
        if not disk:
            return None
        # 优先精确 stub：case_char_01.png
        exact = f"{stub}.png"
        lower_map = {k.lower(): k for k in disk}
        if exact in lower_map:
            return lower_map[exact]
        # 同编号任意变体：case_char_01_clean / case_char_01_with_prop ...
        candidates = sorted(
            k for k in disk if k.lower().startswith(stub + "_") or k.lower() == exact
        )
        return candidates[0] if candidates else None

    def _scan_numbered_frames_on_disk(
        self, pattern: re.Pattern,
    ) -> Dict[str, str]:
        found: Dict[str, str] = {}
        if not os.path.isdir(self.output_dir):
            return found
        for fname in sorted(os.listdir(self.output_dir)):
            if not pattern.match(fname):
                continue
            path = os.path.join(self.output_dir, fname)
            if os.path.isfile(path):
                found[fname] = path
        return found

    def _infer_total_frames_from_disk(self) -> int:
        max_idx = int(self.state.get("total_frames") or 0)
        for pattern in (self._BASE_FRAME_RE, self._EDIT_FRAME_RE, self._FINAL_FRAME_RE):
            for fname in self._scan_numbered_frames_on_disk(pattern):
                m = pattern.match(fname)
                if m:
                    max_idx = max(max_idx, int(m.group(1)))
        return max_idx

    def _sync_frame_lists_from_disk(self, total: int) -> None:
        """将磁盘上已有的 base/edit/final 帧登记到 state 与内存索引。"""
        if total <= 0:
            return
        for i in range(1, total + 1):
            for kind, store, done_key in (
                ("base", self.base_frame_files, "base_frames_done"),
                ("edit", self.edit_frame_files, "edit_frames_done"),
            ):
                if kind == "base":
                    name = f"case_base_frame{i:02d}.png"
                else:
                    name = f"case_edit_frame{i:02d}.png"
                path = os.path.join(self.output_dir, name)
                if os.path.isfile(path):
                    store[name] = path
                    done = self.state.setdefault(done_key, [])
                    if name not in done:
                        done.append(name)
        for fname, path in self._scan_numbered_frames_on_disk(self._FINAL_FRAME_RE).items():
            self.summary.setdefault("final_frames", {})[fname] = {
                "file": fname,
                "source": "disk",
            }

    def _disk_resources_ready_for_pipeline(self) -> bool:
        disk = self._scan_resource_files_on_disk()
        if not disk and not self.state.get("resources_done"):
            return False
        prompts = self.state.get("resource_prompts") or {}
        if not prompts:
            return bool(disk)
        for planned in prompts:
            if not self._resolve_resource_alias(planned):
                return False
        return True

    def _all_base_frames_on_disk(self, total: int) -> bool:
        if total <= 0:
            return False
        return all(
            os.path.isfile(os.path.join(self.output_dir, f"case_base_frame{i:02d}.png"))
            for i in range(1, total + 1)
        )

    def _all_edit_frames_on_disk(self, total: int) -> bool:
        if total <= 0:
            return False
        return all(
            os.path.isfile(os.path.join(self.output_dir, f"case_edit_frame{i:02d}.png"))
            for i in range(1, total + 1)
        )

    def _register_resource_aliases_from_plan(self) -> int:
        """把规划名映射到磁盘实际文件，便于后续 prompt 引用解析。"""
        linked = 0
        prompts = self.state.get("resource_prompts") or {}
        for planned in prompts:
            actual = self._resolve_resource_alias(planned)
            if not actual:
                continue
            path = os.path.join(self.output_dir, actual)
            if not os.path.isfile(path):
                continue
            self.resource_files[planned] = path
            self.resource_files[actual] = path
            if planned not in self.state.setdefault("resources_done", []):
                self.state["resources_done"].append(planned)
            if actual not in self.state["resources_done"]:
                self.state["resources_done"].append(actual)
            self._register_disk_resource(
                actual,
                path,
                source="user_upload" if actual != planned else "existing",
                generation_prompt=prompts.get(planned, ""),
            )
            linked += 1
        return linked

    def _reconcile_step_from_disk(self) -> str:
        """根据磁盘产物将 step 前推到可继续的最晚阶段（仅前进、不后退）。"""
        step_order = [
            "init", "resources", "resource_review", "base_frames",
            "edit_plan", "edit_frames", "final_assembly",
            "ltx_plan", "ltx_generate", "done",
        ]
        cur = self.state.get("step", "init")
        try:
            cur_idx = step_order.index(cur)
        except ValueError:
            cur_idx = 0

        def set_at_least(target: str) -> None:
            nonlocal cur_idx
            try:
                t_idx = step_order.index(target)
            except ValueError:
                return
            if t_idx > cur_idx:
                cur_idx = t_idx

        total = self._infer_total_frames_from_disk()
        if total > 0:
            self.state["total_frames"] = total

        if self.state.get("init_plan_response") and cur == "init":
            set_at_least("resources")

        if self._disk_resources_ready_for_pipeline():
            set_at_least("resource_review")
            if self._all_base_frames_on_disk(total):
                # 基础帧已齐：跳过 resource_review / base_frames 的重复生成
                set_at_least("edit_plan")
                if self.state.get("edit_plan"):
                    set_at_least("edit_frames")
                if self._all_edit_frames_on_disk(total):
                    set_at_least("final_assembly")
            elif self.state.get("resource_review_response") and self.state.get(
                "pending_frame_prompt",
            ):
                set_at_least("base_frames")

        pipeline = self.state.get("pipeline_mode", "frames")
        if pipeline in ("video", "full"):
            frames = self.ltx_ext.collect_reference_frames()
            if frames and cur_idx >= step_order.index("final_assembly"):
                if self.state.get("ltx_shots"):
                    done = self.state.get("ltx_shots_done") or []
                    if len(done) >= len(self.state["ltx_shots"]):
                        set_at_least("done")
                    else:
                        set_at_least("ltx_generate")
                else:
                    set_at_least("ltx_plan")

        return step_order[cur_idx]

    def sync_workflow_from_disk(
        self,
        *,
        advance_step: bool = True,
        announce: bool = True,
    ) -> Dict[str, int]:
        """续跑前解析结果目录：资源 / 基础帧 / 编辑帧 → 更新 state 与 step。"""
        stats = {
            "resources_disk": 0,
            "resources_linked": 0,
            "base_frames_disk": 0,
            "edit_frames_disk": 0,
        }
        disk_res = self._scan_resource_files_on_disk()
        stats["resources_disk"] = len(disk_res)
        for name, path in disk_res.items():
            self.resource_files[name] = path
            if name not in self.state.setdefault("resources_done", []):
                self.state["resources_done"].append(name)
        stats["resources_linked"] = self._register_resource_aliases_from_plan()
        self._sync_resources_summary_from_disk()

        total = self._infer_total_frames_from_disk()
        if total > 0:
            self.state["total_frames"] = total
            self._sync_frame_lists_from_disk(total)
            stats["base_frames_disk"] = sum(
                1 for i in range(1, total + 1)
                if os.path.isfile(
                    os.path.join(self.output_dir, f"case_base_frame{i:02d}.png")
                )
            )
            stats["edit_frames_disk"] = sum(
                1 for i in range(1, total + 1)
                if os.path.isfile(
                    os.path.join(self.output_dir, f"case_edit_frame{i:02d}.png")
                )
            )

        old_step = self.state.get("step", "init")
        if advance_step:
            self.state["step"] = self._reconcile_step_from_disk()
            self._save_state()
            self._save_summary()

        if announce and (
            stats["resources_disk"]
            or stats["base_frames_disk"]
            or stats["edit_frames_disk"]
            or old_step != self.state.get("step")
        ):
            print(
                f"  [磁盘同步] 资源 {stats['resources_disk']} 张"
                f"（规划映射 {stats['resources_linked']}）"
                f"，基础帧 {stats['base_frames_disk']}/{total or '?'}"
                f"，编辑帧 {stats['edit_frames_disk']}/{total or '?'}"
            )
            if advance_step and old_step != self.state.get("step"):
                print(f"  [磁盘同步] step: {old_step} → {self.state['step']}")

        return stats

    def _register_disk_resource(
        self,
        name: str,
        path: str,
        *,
        source: str = "user_upload",
        generation_prompt: str = "",
    ):
        self.resource_files[name] = path
        if name not in self.state.setdefault("resources_done", []):
            self.state["resources_done"].append(name)
        entry = self.summary.setdefault("resources", {}).setdefault(name, {})
        entry.setdefault("file", name)
        entry["source"] = source
        if generation_prompt:
            entry["generation_prompt"] = generation_prompt
        elif not entry.get("generation_prompt"):
            entry["generation_prompt"] = ""
        entry.setdefault("chosen_candidate", None)
        entry.setdefault("pick_reason", "")
        if source == "user_upload" and not entry.get("pick_reason"):
            entry["pick_reason"] = "用户上传或已存在，跳过 AI 生成"

    def _sync_resources_summary_from_disk(self):
        """将磁盘上已有资源图同步到 summary.resources（修复用户上传后 resources 为空）。"""
        for name, path in self._scan_resource_files_on_disk().items():
            prompts = self.state.get("resource_prompts", {})
            gen_prompt = prompts.get(name, "")
            if name in self.summary.get("resources", {}):
                self.summary["resources"][name].setdefault("file", name)
                if not self.summary["resources"][name].get("generation_prompt") and gen_prompt:
                    self.summary["resources"][name]["generation_prompt"] = gen_prompt
                self.summary["resources"][name].setdefault(
                    "source",
                    "generated" if gen_prompt else "user_upload",
                )
            else:
                source = "generated" if name in prompts else "user_upload"
                self._register_disk_resource(
                    name, path, source=source, generation_prompt=gen_prompt,
                )
        cards_by_file = {
            c.get("file"): c
            for c in self.state.get("resource_cards", [])
            if isinstance(c, dict) and c.get("file")
        }
        for name, card in cards_by_file.items():
            if name in self.summary.get("resources", {}):
                self.summary["resources"][name]["resource_card"] = card
        self._save_summary()

    def _format_resource_registry_brief(self) -> str:
        """供 VLM 主对话使用的资源库摘要。"""
        cards = self.state.get("resource_cards", [])
        if not cards:
            names = sorted(self.resource_files.keys())
            if not names:
                return "（资源卡片尚未解析）"
            return "已登记资源文件：\n" + "\n".join(f"- {n}" for n in names)
        lines = ["已解析资源卡片（v6.8 Resource Registry）："]
        for card in cards:
            if not isinstance(card, dict):
                continue
            fname = card.get("file", "?")
            etype = card.get("resource_type", card.get("entity_id", ""))
            hero = " [hero_product]" if card.get("hero_product") else ""
            traits = card.get("visual_traits") or card.get("display_name") or ""
            if isinstance(traits, list):
                traits = ", ".join(str(t) for t in traits[:6])
            lines.append(f"- {fname} ({etype}{hero}): {traits}")
        return "\n".join(lines)

    def _effective_resource_mode(self) -> str:
        return self.state.get("resource_mode", self.resource_mode)

    def _build_resource_mode_directive(self) -> str:
        mode = self._effective_resource_mode()
        if mode == "upload":
            return (
                "## 资源库来源（当前：upload / 用户自行上传）\n"
                f"请将资源图预先放入项目目录（如 {self.output_dir}/）。\n"
                "工作流**不会**为 case_char / case_scene / case_prop 调用文生图抽卡。\n"
                "第 4 节必须列出**完整资源文件名清单**（如 case_char_01.png、case_scene_01.png），"
                "每项附英文画面规格说明；可写 `Generate case_xxx.png:` 行作为上传参考规格，"
                "但系统只认磁盘上已存在的 PNG 文件。\n"
            )
        if mode == "generate":
            return (
                "## 资源库来源（当前：generate / 模型生成）\n"
                "资源库由工作流根据第 4 节英文 Generate prompt 自动抽卡生成。\n"
                "必须为每个 case_char / case_scene / case_prop 写出完整、可执行的 Generate 行。\n"
                "不要假设用户会预先上传资源图。\n"
            )
        return (
            "## 资源库来源（当前：auto / 自动）\n"
            "输出目录中已有的 case_char / case_scene / case_prop 视为用户上传并直接使用；"
            "规划中有但磁盘缺失的文件再由模型按 Generate prompt 生成。\n"
        )

    def _register_scanned_disk_resources(
        self,
        disk_resources: Dict[str, str],
        resource_prompts: Dict[str, str],
    ):
        if not disk_resources:
            return
        mode = self._effective_resource_mode()
        print(f"  扫描到 {len(disk_resources)} 个资源库文件")
        for name, path in disk_resources.items():
            if mode == "upload":
                source = "user_upload"
            elif name in resource_prompts:
                source = "existing"
            else:
                source = "user_upload"
            self._register_disk_resource(
                name,
                path,
                source=source,
                generation_prompt=resource_prompts.get(name, ""),
            )
        self._sync_resources_summary_from_disk()
        self._save_state()
        self._save_summary()

    def _validate_upload_mode_resources(
        self,
        disk_resources: Dict[str, str],
        resource_prompts: Dict[str, str],
    ):
        if resource_prompts:
            missing = sorted(
                n for n in resource_prompts if not self._resolve_resource_alias(n)
            )
            if missing:
                raise RuntimeError(
                    "资源库模式为 upload（用户上传），但以下规划资源文件不存在：\n"
                    + "\n".join(f"  - {n}" for n in missing)
                    + f"\n请将 PNG 放入：{self.output_dir}/"
                )
        elif not disk_resources:
            raise RuntimeError(
                "资源库模式为 upload（用户上传），但输出目录中未找到任何 "
                "case_char / case_scene / case_prop / case_style 资源图。\n"
                f"请放入：{self.output_dir}/"
            )

    async def _step_build_resources(self):
        mode = self._effective_resource_mode()
        print(
            f"\n[Step 2/5] 资源库构建（模式: {mode} — "
            f"{RESOURCE_MODE_LABELS.get(mode, mode)}）..."
        )
        resource_prompts: Dict[str, str] = dict(self.state.get("resource_prompts", {}))
        disk_resources = self._scan_resource_files_on_disk()

        # ── upload：仅扫描登记，禁止 AI 生成 ──
        if mode == "upload":
            self._register_scanned_disk_resources(disk_resources, resource_prompts)
            self._validate_upload_mode_resources(disk_resources, resource_prompts)
            print("  upload 模式：已登记用户资源，进入资源卡片解析与审查")
            self.state["step"] = "resource_review"
            self._save_state()
            return

        # ── generate：必须按规划 prompt 生成（磁盘已有则跳过该文件）──
        if mode == "generate":
            if not resource_prompts:
                raise RuntimeError(
                    "资源库模式为 generate（模型生成），但 Step1 未解析出任何 "
                    "resource_prompts。请检查 init 规划是否包含 Generate case_char/scene/prop 行。"
                )
            self._register_scanned_disk_resources(disk_resources, resource_prompts)
            names_to_generate = [
                name for name in resource_prompts
                if not self._resolve_resource_alias(name)
            ]
            if not names_to_generate:
                print("  generate 模式：规划资源均已存在，跳过抽卡")
                self.state["step"] = "resource_review"
                self._save_state()
                return
            print(f"  generate 模式：将为 {len(names_to_generate)} 个资源调用文生图")
            for name in names_to_generate:
                await self._generate_single_resource(name, resource_prompts[name])
            self.state["step"] = "resource_review"
            self._save_state()
            return

        # ── auto：有则用上传，缺则生成 ──
        self._register_scanned_disk_resources(disk_resources, resource_prompts)

        names_to_generate = [
            name for name in resource_prompts
            if not self._resolve_resource_alias(name)
        ]

        if not resource_prompts and not disk_resources:
            logger.warning("No resource prompts and no resource files on disk; skipping resource build")
            self.state["step"] = "resource_review"
            self._save_state()
            return

        if not names_to_generate:
            print("  auto 模式：资源已齐全，跳过 AI 生成")
            self.state["step"] = "resource_review"
            self._save_state()
            return

        print(f"  auto 模式：补生成 {len(names_to_generate)} 个缺失资源")
        for name in names_to_generate:
            await self._generate_single_resource(name, resource_prompts[name])
        self.state["step"] = "resource_review"
        self._save_state()

    async def _generate_single_resource(self, name: str, prompt: str):
        """为单个资源文件执行抽卡 + VLM 评选（原 _step_build_resources 内联逻辑）。"""
        final_path = os.path.join(self.output_dir, name)
        if os.path.exists(final_path):
            self.resource_files[name] = final_path
            if name not in self.state["resources_done"]:
                self.state["resources_done"].append(name)
                self._save_state()
            print(f"  [skip] {name} 已存在，跳过生成")
            return

        initial_resource_prompt = prompt
        current_prompt = prompt
        candidates_dir = os.path.join(
            self.output_dir, name.replace(".png", "_candidates")
        )
        pre_existing = self._find_existing_candidates(candidates_dir)

        for regen_round in range(MAX_REGEN_ATTEMPTS + 1):
            if regen_round == 0 and pre_existing:
                candidates = pre_existing
                print(f"  发现 {name} 已有 {len(candidates)} 张候选图，跳过生成直接选择...")
            else:
                print(f"  生成 {name} ({RESOURCE_CANDIDATES} 张候选"
                      f"{f', 第 {regen_round+1} 轮' if regen_round > 0 else ''})...")
                logger.info("Generating resource: %s (round %d)", name, regen_round + 1)

                gen_prompt_clean = self.parser.extract_generation_prompt(current_prompt)

                prefix = name.replace(".png", "")
                if regen_round > 0:
                    prefix = f"{prefix}_r{regen_round+1}"
                candidates = await self.img_gen.generate_candidates(
                    prompt=gen_prompt_clean,
                    save_dir=candidates_dir,
                    filename_prefix=prefix,
                    num_candidates=RESOURCE_CANDIDATES,
                )

            print(f"  VLM 挑选最佳...")
            pick_text = self._build_resource_pick_prompt(
                name, current_prompt, len(candidates)
            )
            pick_response = await self.vlm.chat_without_history(
                text=pick_text,
                image_paths=candidates,
                system_prompt=RESOURCE_PICK_SYSTEM_PROMPT,
            )

            pick_result = self.parser.parse_image_pick(pick_response, len(candidates))

            if pick_result.all_rejected:
                print(f"  {name} 全部候选不合格: {pick_result.reason}")
                self._log("resource_all_rejected", {
                    "resource": name,
                    "round": regen_round + 1,
                    "reason": pick_result.reason,
                    "suggestion": pick_result.suggestion,
                })
                if regen_round >= MAX_REGEN_ATTEMPTS:
                    print(f"  {name} 达到最大重试轮数，强制使用第 1 张")
                    pick_result.chosen_index = 1
                    pick_result.all_rejected = False
                else:
                    revised = self.parser.extract_generate_line_for_target(
                        pick_response, name,
                    )
                    if revised:
                        current_prompt = revised
                        print("  使用 VLM 回复中的修订英文 Generate 行重新生成...")
                    elif (
                        pick_result.suggestion
                        and self.parser.looks_like_image_generation_prompt(
                            pick_result.suggestion, name,
                        )
                    ):
                        current_prompt = pick_result.suggestion.strip()
                        print("  采纳 VLM 建议中的完整 Generate prompt，重新生成...")
                    elif pick_result.suggestion or pick_result.reason:
                        print(
                            "  将评选原因/修改意见提交 VLM，基于上一版英文 prompt 合并改写..."
                        )
                        merged = await self._revise_resource_prompt_from_feedback(
                            resource_name=name,
                            previous_prompt_line=current_prompt,
                            review_reason=pick_result.reason or "",
                            review_suggestion=pick_result.suggestion or "",
                            pick_response_full=pick_response,
                        )
                        if merged:
                            current_prompt = merged
                            print("  已根据反馈生成修订版英文 Generate 行，重新抽卡...")
                        else:
                            logger.warning(
                                "VLM 改写未得到可用的 Generate %s 行，回退为初始资源 prompt",
                                name,
                            )
                            current_prompt = initial_resource_prompt
                    else:
                        current_prompt = initial_resource_prompt
                    continue

            chosen_idx = pick_result.chosen_index
            if chosen_idx < 1 or chosen_idx > len(candidates):
                chosen_idx = 1

            chosen_path = candidates[chosen_idx - 1]
            final_path = os.path.join(self.output_dir, name)
            shutil.copy2(chosen_path, final_path)
            self.resource_files[name] = final_path

            print(f"  {name} -> 选择第 {chosen_idx} 张")
            self._log("resource_pick", {
                "resource": name,
                "round": regen_round + 1,
                "candidates": candidates,
                "chosen": chosen_idx,
                "reason": pick_response[:200],
            })

            if name in self.summary["resources"]:
                self.summary["resources"][name]["chosen_candidate"] = chosen_idx
                self.summary["resources"][name]["pick_reason"] = pick_response.strip()
                self.summary["resources"][name]["source"] = "generated"
                if regen_round > 0:
                    self.summary["resources"][name]["regen_rounds"] = regen_round + 1
                    self.summary["resources"][name]["final_prompt"] = current_prompt
            else:
                self.summary["resources"][name] = {
                    "generation_prompt": current_prompt,
                    "chosen_candidate": chosen_idx,
                    "pick_reason": pick_response.strip(),
                    "file": name,
                    "source": "generated",
                }
            self._save_summary()
            break

        if name not in self.state["resources_done"]:
            self.state["resources_done"].append(name)
        self._save_state()

    async def _step_resource_review(
        self, grid_n: int = 9, opening_orientation: str = "auto",
    ):
        """v6.5 §6 资源图内容审查与剧情落点校准。

        在资源库选定后、基础帧生成前，把实际选中的资源图片送给 VLM，
        让它基于真实可见的锚点重新校准剧情落点、修订基础帧规划表、
        给出第一帧 prompt。
        """
        total = self.state.get("total_frames", grid_n) or grid_n
        is_classic_9 = (total == 9)
        mode_tag = "9宫格核心模式" if is_classic_9 else f"N={total} 任意帧数衍生模式"
        print(f"\n[Step 2.5/5] 资源库扫描解析 + 内容审查（v6.8 §3–§6：{mode_tag}）...")

        # 收集已选/用户上传的资源图
        resource_paths: list = []
        resource_names: list = []
        for name, path in self._scan_resource_files_on_disk().items():
            self.resource_files[name] = path
        for name in sorted(self.resource_files.keys()):
            path = self.resource_files[name]
            if os.path.exists(path):
                resource_paths.append(path)
                resource_names.append(name)
        if not resource_paths:
            for name in sorted(self.state.get("resources_done", [])):
                path = os.path.join(self.output_dir, name)
                if os.path.exists(path):
                    resource_paths.append(path)
                    resource_names.append(name)
                    self.resource_files[name] = path

        self._sync_resources_summary_from_disk()

        if not resource_paths:
            logger.warning("No resource images found; skipping resource review")
            self._register_resources_in_conversation()
            self.state["step"] = "base_frames"
            self._save_state()
            return

        # v6.8 §3：资源卡片解析（用户上传也必须解析，不能跳过）
        if not self.state.get("resource_cards"):
            idea_brief = self.summary.get("idea", "")[:800]
            card_list = "\n".join(
                f"- 第 {i+1} 张：{n}" for i, n in enumerate(resource_names)
            )
            parse_text = (
                f"以下是资源库中的全部图片（按顺序传入），包含用户上传与 AI 生成：\n{card_list}\n\n"
                f"视频 idea 摘要：{idea_brief}\n\n"
                "## v6.8 §3.3 资源卡片解析任务\n"
                "请**仔细观察每张图**，为每个文件输出结构化 Resource Card（JSON 数组），字段包括：\n"
                "file, resource_type (character/scene/prop/style), entity_id, display_name, "
                "identity_sensitive, hero_product (bool), visual_traits (array), "
                "usable_for (array), risk_notes (array), must_reference_when_first_appears, "
                "must_reference_when_visible（如适用）。\n"
                "人物图标注 clean / with_prop 等 state_tags；场景图列出 scene_anchors；"
                "核心广告商品必须 hero_product=true。\n\n"
                "输出格式：先写简短中文说明，再附 ```json [...] ``` 代码块（仅 JSON 数组）。"
            )
            parse_response = await self.vlm.chat_without_history(
                text=parse_text,
                image_paths=resource_paths,
                system_prompt=(
                    "你是 v6.8 资源库解析专家。必须基于真实画面填写资源卡片，"
                    "不要臆造图中不存在的细节。"
                ),
            )
            cards = self.parser.parse_resource_cards(parse_response)
            if cards:
                self.state["resource_cards"] = cards
                self.summary["resource_registry"] = cards
                print(f"  已解析 {len(cards)} 张资源卡片")
                self._sync_resources_summary_from_disk()
                self._save_summary()
            else:
                logger.warning("Resource card parse returned empty; review will still proceed")
            self._log("resource_card_parse", {
                "resources": resource_names,
                "response": parse_response[:4000],
                "cards_count": len(cards),
            })

        # 检测是否已有缓存
        cached = self.state.get("resource_review_response", "")
        cached_prompt = self.state.get("pending_frame_prompt", "")
        if cached and cached_prompt:
            prompt_block = self.parser.extract_generation_block(
                cached_prompt, "case_base_frame01.png"
            )
            if prompt_block:
                self.state["pending_frame_prompt"] = prompt_block
                self._save_state()
            print("  发现已有资源审查结果，跳过重新询问 VLM")
            self._register_resources_in_conversation()
            self.state["step"] = "base_frames"
            self._save_state()
            return

        opening_block = self._build_opening_directive(opening_orientation)
        scene_names = [n for n in resource_names if "scene" in n]
        char_names = [n for n in resource_names if "char" in n]
        prop_names = [n for n in resource_names if "prop" in n]

        scene_ref = scene_names[0] if scene_names else "case_scene_01.png"
        char_ref = char_names[0] if char_names else "case_char_01.png"

        if char_names:
            char_section = (
                f"分析 {', '.join(char_names)} 的：人物可见面（正面/侧面/背面）、"
                f"服装、发型、配饰、体型，列出适合后续继承的具体细节。\n\n"
            )
        else:
            char_section = "（无人物参考图）\n\n"

        if scene_names:
            scene_section = (
                f"分析 {', '.join(scene_names)} 的：\n"
                f"- 主视角和构图（俯视/平视/仰视，从哪个方向看）\n"
                f"- 真实存在的可定位元素（桌子、窗户、书架、地面纹理、台阶、门口、石柱、平台等）\n"
                f"- 光源方向和氛围\n"
                f"- **必须逐一列出，不能遗漏**\n\n"
            )
        else:
            scene_section = "（无场景参考图）\n\n"

        if prop_names:
            prop_section = f"分析 {', '.join(prop_names)} 的：形状、材质、大小、发光状态。\n\n"
        else:
            prop_section = "（无道具参考图）\n\n"

        file_list = "\n".join(f"- 第 {i+1} 张：{n}" for i, n in enumerate(resource_names))

        registry_brief = self._format_resource_registry_brief()
        review_text = (
            f"资源库已全部就绪（含用户上传），以下图片按顺序传入：\n{file_list}\n\n"
            f"{registry_brief}\n\n"
            f"## v6.8 §5–§6 资源图内容审查、Frame Entity Plan 与剧情落点校准\n\n"
            f"请**先仔细观察以上资源图**，然后输出以下审查结果：\n\n"
            f"### 1. 人物参考可用信息\n{char_section}"
            f"### 2. 场景参考可用锚点\n{scene_section}"
            f"### 3. 道具参考可用信息\n{prop_section}"
            f"### 4. 不可用/禁止使用的场景锚点\n"
            f"列出用户 idea 或初步规划中提到的、但**在以上场景参考图中并不存在**的结构。\n"
            f"例如：如果场景图中没有入口、门槛、台阶、石柱，则后续 base frame prompt **禁止写这些结构**。\n\n"
            f"### 5. 剧情落点调整\n"
            f"根据真实锚点，说明初步规划中哪些人物位置/剧情起点需要调整。\n"
            f"例如：如果初步规划写了\u201c人物站在门槛内侧\u201d但场景图没有门口，应改为\u201c人物已站在中央桌旁\u201d。\n\n"
            f"### 6. Frame Entity Plan（v6.8 §6，每帧实体状态表）\n"
            f"表格列：Frame / Story State / Visible Characters / Visible Props / New Entities / "
            f"Hero Product Visible / Focus / Forbidden / Planned reference_images\n"
            f"**必须 {total} 行**；Planned reference_images 须遵守 Reference Resolver，"
            f"不可默认仅上一帧。\n\n"
            f"### 7. 修订后的基础帧 {total} 帧规划表\n"
            f"**列顺序固定为 9 列**：基础帧 / 对应9宫格beat / 剧情节点 / 基础镜头 / "
            f"开场或镜头意图 / 位置锚点 / 朝向关系 / 画面重点 / 连续性变化\n"
            f"**规划表必须严格 {total} 行**，所有位置锚点必须来自上述审查中确认存在的真实锚点。\n\n"
            f"{V66_BASE_RULES}\n"
            f"{V68_REFERENCE_RESOLVER_RULES}\n"
            f"### v6.6 Additional Resource Review Output\n"
            f"- For each base frame, name the Scene Core that must remain visible.\n"
            f"- For each base frame, define the Camera-Side Track relative to the previous frame.\n"
            f"- For each base frame, define topology as Camera -> Scene Core -> Character, Camera -> Character -> Scene Core, or Character beside Scene Core.\n"
            f"- For each base frame, give a short Anchor Visibility Budget: which core anchors must be clearly visible and not blocked.\n\n"
            f"{opening_block}\n"
            f"### 8. case_base_frame01.png 开场意图与朝向选择\n"
            f"说明开场镜头叙事功能和最终选定的 Subject–Camera Relation。\n\n"
            f"### 9. 第一基础帧 Reference Resolver + prompt\n"
            f"先给出 frame01 的 reference_images JSON（含 reference_reason），"
            f"再输出 case_base_frame01.png 的英文一段式生成 prompt（格式：Generate case_base_frame01.png: ...）。\n"
            f"prompt 中的 Use 文件名必须与 reference_images 一致。\n\n"
            f"**v6.8 / v6.5 关键约束**：\n"
            f"- case_base_frame01.png 必须以 {scene_ref} "
            f"作为 **primary composition reference**（空间主参考），优先继承场景构图、主视角、关键锚点位置和光源方向；\n"
            f"- 人物身份从 {char_ref} 继承，但不能让角色参考图主导构图；\n"
            f"- prompt 中**禁止出现上述「不可用锚点」中列出的场景结构**（如场景图没有门口就不能写 entrance threshold）；\n"
            f"- 如果用户 idea 需要的场景元素在场景图中不存在，把人物落点改到已有锚点附近（桌旁、窗边、书架间等）；\n"
            f"- camera 只能是 wide shot 或 medium shot 中的一个（不能用选择式表达）；\n"
            f"- 必须显式写明 Subject–Camera Relation（back view / front view / side view / three-quarter 等）。"
        )

        response = await self.vlm.chat(review_text, image_paths=resource_paths)

        self._log("resource_review", {
            "resources": resource_names,
            "response": response[:3000],
        })
        self.state["resource_review_response"] = response

        print("\n--- VLM 资源图内容审查 ---")
        print(response[:2000])
        if len(response) > 2000:
            print(f"... (共 {len(response)} 字符)")

        # Keep only the actual image-generation prompt in state; the resource
        # review report belongs in summary["resource_review"].
        revised_prompt = self.parser.extract_generation_block(
            response, "case_base_frame01.png"
        )
        if revised_prompt:
            self.state["pending_frame_prompt"] = revised_prompt
            print(f"  已获取修订后的 case_base_frame01.png prompt（{len(revised_prompt)} 字符）")
        else:
            logger.warning("Resource review did not produce a revised first frame prompt; keeping init plan prompt")

        self.summary["resource_review"] = {
            "resources_reviewed": resource_names,
            "review_response": response[:5000],
        }
        self._save_summary()

        self._register_resources_in_conversation()
        self.state["step"] = "base_frames"
        self._save_state()

    def _register_resources_in_conversation(self):
        """将资源库图片展示给 VLM 主对话，建立视觉记忆。"""
        resource_paths = []
        resource_names = []
        for name in sorted(self.resource_files.keys()):
            path = self.resource_files[name]
            if os.path.exists(path):
                resource_paths.append(path)
                resource_names.append(name)

        if resource_paths:
            registry = self._format_resource_registry_brief()
            text = (
                "资源库已构建并解析完毕（含用户上传资源）：\n"
                + "\n".join(f"- {n}" for n in resource_names)
                + f"\n\n{registry}\n\n"
                + f"{V68_REFERENCE_RESOLVER_RULES}\n"
                "v6.7: edit frames must **redesign the camera** vs base; base frames lock story continuity.\n"
                "资源图内容审查与 Frame Entity Plan 已完成（v6.8 §5–§6）。"
                "接下来逐帧生成基础帧：每帧生成前必须运行 Reference Resolver，"
                "新人物/新道具/hero product 必须引用对应资源图，禁止默认只参考上一帧。"
            )
            self.vlm.add_user_message(text, resource_paths)
            self.vlm.add_assistant_message(
                "资源库确认完毕，资源图内容审查已完成。"
                "场景真实锚点已锁定，准备开始第一阶段基础帧生成。"
            )

    async def _step_generate_base_frames(self):
        """v6.0 第一阶段：稳定剧情九宫格基础帧（case_base_frame0x.png）。
        每帧使用唯一确定的 wide shot 或 medium shot，禁止复杂镜头。"""
        print("\n[Step 3/5] 第一阶段：逐帧生成基础剧情九宫格...")
        total = self.state["total_frames"]

        for done_name in self.state["base_frames_done"]:
            path = os.path.join(self.output_dir, done_name)
            if os.path.exists(path):
                self.base_frame_files[done_name] = path

        for frame_idx in range(total):
            frame_num = frame_idx + 1
            frame_name = f"case_base_frame{frame_num:02d}.png"

            final_path = os.path.join(self.output_dir, frame_name)
            if os.path.exists(final_path):
                self.base_frame_files[frame_name] = final_path
                if frame_name not in self.state["base_frames_done"]:
                    self.state["base_frames_done"].append(frame_name)
                print(f"  [skip] {frame_name} 已存在，跳过")
                self.state["current_frame_index"] = frame_idx + 1
                self.state["pending_frame_prompt"] = ""
                self._save_state()
                continue

            prefix = frame_name.replace(".png", "")
            candidates_dir = os.path.join(
                self.output_dir, f"{prefix}_candidates"
            )
            pre_existing = self._find_existing_candidates(candidates_dir)

            print(f"\n  --- {frame_name} ({frame_num}/{total})"
                  f"{' [发现已有候选图，直接选择]' if pre_existing else ''} ---")

            original_prompt = self._get_frame_prompt(frame_name, kind="base")
            if not original_prompt:
                prev_frame = f"case_base_frame{frame_num-1:02d}.png" if frame_num > 1 else None
                if prev_frame and self._resolve_single_path(prev_frame):
                    print(f"  从 VLM 获取 {frame_name} 的生成 prompt...")
                    original_prompt = await self._request_frame_prompt(
                        frame_name, prev_frame, kind="base",
                    )
                if not original_prompt:
                    logger.error("No prompt available for %s", frame_name)
                    raise RuntimeError(
                        f"Missing frame prompt for {frame_name}, "
                        f"无法从 summary/state 恢复且无法从 VLM 获取"
                    )
            self.state["pending_frame_prompt"] = original_prompt

            success = False
            for attempt in range(MAX_REGEN_ATTEMPTS + 1):
                if attempt == 0 and pre_existing:
                    candidates = pre_existing
                    print(f"  使用已有 {len(candidates)} 张候选图...")
                else:
                    gen_prompt = self.parser.extract_generation_prompt(original_prompt)
                    ref_names = self.parser.extract_reference_images(original_prompt)
                    ref_paths = self._resolve_reference_paths(ref_names)

                    print(f"  生成 {FRAME_CANDIDATES} 张候选 (参考图: {ref_names})"
                          f"{f' [重新抽卡 第{attempt+1}轮]' if attempt > 0 else ''}...")
                    if attempt > 0:
                        cand_prefix = f"{prefix}_r{attempt+1}"
                    else:
                        cand_prefix = prefix
                    candidates = await self.img_gen.generate_candidates(
                        prompt=gen_prompt,
                        save_dir=candidates_dir,
                        filename_prefix=cand_prefix,
                        num_candidates=FRAME_CANDIDATES,
                        images=ref_paths if ref_paths else None,
                    )

                ref_names = self.parser.extract_reference_images(original_prompt)

                print(f"  VLM 评选最佳基础帧...")

                extra_ref_paths = []
                ref_label_map = {}
                for rn in ref_names:
                    rp = self._resolve_single_path(rn)
                    if rp and rp not in extra_ref_paths:
                        extra_ref_paths.append(rp)

                max_vl_images = (
                    GITEE_MAX_VL_IMAGES
                    if self.vlm.provider == "gitee"
                    else None
                )
                context_images, num_pick, pick_omit_note = cap_pick_context_images(
                    candidates,
                    extra_ref_paths,
                    max_vl_images,
                    prioritize_extras=False,
                )
                for rn in ref_names:
                    rp = self._resolve_single_path(rn)
                    if rp and rp in context_images:
                        ref_label_map[rn] = context_images.index(rp) + 1

                pick_text = self._build_base_frame_pick_prompt(
                    frame_name, original_prompt, num_pick,
                    ref_names, ref_label_map,
                )
                if pick_omit_note:
                    pick_text += f"\n\n**传图说明**：{pick_omit_note}"

                pick_response = await self.vlm.chat_without_history(
                    text=pick_text,
                    image_paths=context_images,
                    system_prompt=BASE_FRAME_PICK_SYSTEM_PROMPT,
                )

                pick_result = self.parser.parse_image_pick(pick_response, num_pick)

                if pick_result.all_rejected:
                    print(f"  {frame_name} 全部候选不合格: {pick_result.reason}")
                    self._log("base_frame_all_rejected", {
                        "frame": frame_name,
                        "attempt": attempt + 1,
                        "reason": pick_result.reason,
                        "suggestion": pick_result.suggestion,
                    })
                    print(f"  使用相同 prompt 重新抽卡...")
                    if attempt >= MAX_REGEN_ATTEMPTS:
                        print(f"  达到最大重试次数，强制使用第 1 张")
                    else:
                        continue

                chosen_idx = pick_result.chosen_index
                if chosen_idx < 1 or chosen_idx > num_pick:
                    chosen_idx = 1

                chosen_path = candidates[chosen_idx - 1]
                final_path = os.path.join(self.output_dir, frame_name)
                shutil.copy2(chosen_path, final_path)
                self.base_frame_files[frame_name] = final_path

                print(f"  {frame_name} 选择第 {chosen_idx} 张，提交质检...")

                self.vlm.trim_old_frame_images(keep_last_n=3)

                review_text = (
                    f"已生成 {frame_name}（附图）。"
                    f"请按 v6.2 第一阶段质检规则进行检查：\n"
                    f"1. **人物体态完整性**：四肢比例是否正常？手臂/手指有无畸形？\n"
                    f"2. **Subject–Camera Relation**：候选画面是否真的是 prompt 写明的视角"
                    f"（背影/正面/侧面/三分之二）？应背对镜头却露出正脸是严重错误。\n"
                    f"3. **场景环境一致性**：地面纹理、空间结构、光照是否与资源库场景图一致？\n"
                    f"4. **基础镜头准确性**：是否为唯一确定的 wide shot 或 medium shot？"
                    f"是否避免了特写/俯视/过肩/极端低角度等复杂镜头？\n"
                    f"5. **Reference Stability**：镜头尺度、人物大小、可见面是否与上一基础帧保持一致，"
                    f"没有因为引用 case_char_01.png 被拉近或被强行转正？\n"
                    f"6. 人物一致性、锚点定位、后续参考稳定性。\n\n"
                    f"如果通过，先给出下一帧 {f'case_base_frame{frame_num+1:02d}.png' if frame_num < total else 'N/A'} "
                    f"的 Reference Resolver（reference_images + 简短 reason），"
                    f"再给出英文一段式 prompt（格式：Generate case_base_frameXX.png: ...）。\n"
                    f"{V68_REFERENCE_RESOLVER_RULES}\n"
                    f"- **显式写 Subject–Camera Relation**；\n"
                    f"- 若下一帧有新人物/新道具/hero product 首次或清晰可见，必须在 prompt 中 Use 对应资源文件名；\n"
                    f"- camera 只能写一个确定的 wide shot 或 medium shot；\n"
                    f"- 不使用 closer / farther / distance / slightly 等抽象词。\n"
                    f"如果不通过，简要说明问题即可。"
                )
                review_response = await self.vlm.chat(
                    review_text, image_paths=[final_path]
                )

                review_result = self.parser.parse_frame_review(review_response, frame_kind="base")

                self._log("base_frame_review", {
                    "frame": frame_name,
                    "attempt": attempt + 1,
                    "chosen_candidate": chosen_idx,
                    "passed": review_result.passed,
                    "review_response": review_response[:500],
                })

                if review_result.passed:
                    print(f"  {frame_name} 质检通过!")
                    self._update_base_frame_summary(
                        frame_name, original_prompt, chosen_idx,
                        pick_response, "通过", attempt + 1,
                    )
                    self.state["base_frames_done"].append(frame_name)
                    self.state["current_frame_index"] = frame_idx + 1
                    self.state["pending_frame_prompt"] = review_result.next_frame_prompt
                    self._save_state()
                    success = True
                    break
                else:
                    print(f"  {frame_name} 质检未通过 (attempt {attempt+1}): {review_result.issues}")
                    print(f"  使用相同 prompt 重新抽卡...")

                    if attempt >= MAX_REGEN_ATTEMPTS:
                        print(f"  {frame_name} 达到最大重试次数，使用当前最佳结果继续")
                        self._update_base_frame_summary(
                            frame_name, original_prompt, chosen_idx,
                            pick_response, f"未通过(强制使用): {review_result.issues}",
                            attempt + 1,
                        )
                        self.state["base_frames_done"].append(frame_name)
                        self.state["current_frame_index"] = frame_idx + 1

                        if frame_idx < total - 1:
                            next_frame = f"case_base_frame{frame_num+1:02d}.png"
                            next_prompt_text = (
                                f"{frame_name} 已使用当前最佳结果（存在一些瑕疵）。"
                                f"请先给出 {next_frame} 的 Reference Resolver（reference_images），"
                                f"再给出英文一段式生成 prompt（格式：Generate {next_frame}: ...）。\n"
                                f"{V68_REFERENCE_RESOLVER_RULES}\n"
                                f"- camera 必须是唯一确定的 wide shot 或 medium shot；\n"
                                f"- 必须显式写 Subject–Camera Relation；\n"
                                f"- 新出现实体必须引用资源图，不可仅参考 {frame_name}；\n"
                                f"- 不使用 closer / farther / distance / slightly 等抽象词。"
                            )
                            next_response = await self.vlm.chat(next_prompt_text)
                            for pat in [
                                r"(Generate\s+case_base_frame\d+\.png\s*:.+?)(?=\n\n|\Z)",
                                r"(生成\s*case_base_frame\d+\.png[：:].+?)(?=\n\n|\Z)",
                            ]:
                                gen_match = re.search(pat, next_response, re.DOTALL | re.IGNORECASE)
                                if gen_match:
                                    self.state["pending_frame_prompt"] = gen_match.group(1).strip()
                                    break

                        self._save_state()
                        success = True

            if not success:
                raise RuntimeError(f"Failed to generate {frame_name}")

        print(f"\n[Step 3/5] 基础九宫格全部完成: {len(self.state['base_frames_done'])}/{total}")
        self.state["step"] = "edit_plan"
        self.state["pending_frame_prompt"] = ""
        self._save_state()

    # ──────────────────────────────────────────────────────────
    # 第二阶段：让 VLM 基于完整基础帧组输出全帧再编辑计划 (v6.5)
    # ──────────────────────────────────────────────────────────

    def _v69_planned_camera_for_frame(self, frame_idx: int, total: int) -> str:
        """v6.9 §21.4：按帧位给出默认特殊镜头（兜底/重试用）。"""
        if total == 9 and frame_idx in _V69_NINE_GRID_CAMERAS:
            return _V69_NINE_GRID_CAMERAS[frame_idx]
        if total == 4:
            m4 = {
                1: "foreground-depth wide / high-angle establishing",
                2: "extreme close-up of face and eyes (awakening)",
                3: "low-angle action / side trajectory shot",
                4: "low-angle hero shot / wide aftermath",
            }
            return m4.get(frame_idx, "insert shot / low-angle hero")
        pos = frame_idx / max(total, 1)
        if frame_idx == 1:
            return "foreground-depth wide / stronger establishing"
        if frame_idx == total:
            return "wide aftermath / symbolic close-up"
        if pos <= 0.35:
            return "face close-up / extreme close-up of eyes"
        if pos <= 0.55:
            return "close-up detail shot / foreground-detail composition"
        if pos <= 0.75:
            return "overhead / high-angle spatial / low-angle action"
        return "low-angle hero / reaction close-up / OTS"

    def _v69_story_function_for_frame(self, frame_idx: int, total: int) -> str:
        if total == 9 and frame_idx in _V69_NINE_GRID_STORY:
            return _V69_NINE_GRID_STORY[frame_idx]
        if total == 4:
            return {
                1: "Establish",
                2: "Awakening / realization",
                3: "Motion / energy",
                4: "Closure / hero",
            }.get(frame_idx, "Story beat")
        if frame_idx == 1:
            return "Establish"
        if frame_idx == total:
            return "Closure"
        if frame_idx == 2:
            return "Awakening / realization"
        return "Action / escalation"

    def _v69_edit_camera_fallback(self, frame_idx: int, total: int, attempt: int) -> str:
        """v6.9 §21.5：抽卡失败后的备选特殊镜头（attempt 从 1 起为第 2 次尝试）。"""
        ladders: Dict[int, List[str]] = {
            2: [
                "extreme close-up of eyes opening",
                "side close-up of face with flame foreground",
                "low-angle close-up from lotus edge",
                "overhead close-up inside flame lotus",
            ],
            3: [
                "close-up detail shot with hand and ring, face and lotus readable",
                "foreground-detail composition on wrist and prop",
                "symbol close-up with flame bokeh and soft scene anchors",
            ],
            7: [
                "low-angle hero silhouette",
                "low-angle full-body against lightning",
                "foreground lotus petal with hero behind",
            ],
        }
        if frame_idx in ladders:
            alts = ladders[frame_idx]
            return alts[min(attempt - 1, len(alts) - 1)]
        generic = [
            "close-up detail shot with clear focal detail and readable context",
            "overhead composition",
            "low-angle hero framing",
            "over-the-shoulder with depth",
            "foreground-depth medium close-up",
        ]
        return generic[min(attempt - 1, len(generic) - 1)]

    def _prompt_needs_v610_refresh(self, prompt: str) -> bool:
        """v6.10：缓存的 edit prompt 若含微观细节清单或过强 insert 用语，需重写。"""
        if not prompt:
            return False
        return bool(_V610_OVERFIT_PROMPT_RE.search(prompt))

    def _prompt_needs_v611_refresh(self, prompt: str) -> bool:
        """v6.11：表层 prompt 含未实际输入的资源文件名或过长 preserve/do-not 链，需重写。"""
        if not prompt:
            return False
        if _V611_BLOATED_SURFACE_PROMPT_RE.search(prompt):
            return True
        if len(re.findall(r"\bpreserve\b", prompt, re.I)) >= 3:
            return True
        if len(re.findall(r"\bdo\s+not\b", prompt, re.I)) >= 4:
            return True
        return False

    def _split_story_state_and_camera(self, body: str) -> tuple:
        """将 edit prompt 正文拆为 (story_state, camera_block)。
        camera_block 含 `Camera transformation:` 标签及其后内容。
        注意：带捕获组的 split 会产出 [pre, label, post] 三段。
        """
        parts = _CAMERA_TRANSFORM_SPLIT_RE.split(body, maxsplit=1)
        if len(parts) >= 3:
            return parts[0].strip(), (parts[1] + parts[2]).strip()
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
        return body.strip(), ""

    def _extract_edit_content_block(self, prompt: str) -> str:
        """v6.12/v6.13：提取 Camera transformation 之前的 story state 块。"""
        if not prompt:
            return ""
        body = prompt
        gen = re.search(
            r"Generate\s+case_edit_frame\d+\.png\s*:\s*",
            prompt,
            re.I,
        )
        if gen:
            body = prompt[gen.end():]
        pre, _ = self._split_story_state_and_camera(body)
        return pre

    def _prompt_needs_v612_refresh(self, prompt: str) -> bool:
        """v6.12：画面块侵入镜头职责（特写/焦点/近距离/光影等），需按 §24 重写。"""
        if not prompt:
            return False
        content = self._extract_edit_content_block(prompt)
        if not content:
            return False
        if _V612_CONTENT_BLOCK_INTRUSION_RE.search(content):
            return True
        # 旧版 v6.11/v6.12 常用 Show 或 Keep 元素清单
        if re.search(r"\bShow\b", content, re.I) and re.search(
            r"\b(?:hand|ring|finger|eye|face)\b",
            content,
            re.I,
        ) and re.search(
            r"\b(?:close|focus|proximity|reflection|tension|foreground|background)\b",
            content,
            re.I,
        ):
            return True
        return False

    def _prompt_needs_v613_refresh(self, prompt: str) -> bool:
        """v6.13：表层 prompt 含显式 Keep/Preserve 保持句，需重写。"""
        if not prompt:
            return False
        if not _V613_KEEP_SURFACE_SENTENCE_RE.search(prompt):
            return False
        # Camera transformation 内的 while keeping ... recognizable 允许
        body = prompt
        gen = re.search(
            r"Generate\s+case_edit_frame\d+\.png\s*:\s*",
            prompt,
            re.I,
        )
        if gen:
            body = prompt[gen.end():]
        pre_camera, _ = self._split_story_state_and_camera(body)
        return bool(_V613_KEEP_SURFACE_SENTENCE_RE.search(pre_camera))

    def _prompt_is_truncated_at_camera_transform(self, prompt: str) -> bool:
        """Camera transformation: 后无镜头描述（常见于 v6.13 sanitize 误删）。"""
        if not prompt:
            return False
        if re.search(r"Camera\s+transformation\s*:\s*$", prompt, re.I):
            return True
        _, camera = self._split_story_state_and_camera(
            re.sub(
                r"^Generate\s+case_edit_frame\d+\.png\s*:\s*",
                "",
                prompt,
                count=1,
                flags=re.I,
            )
        )
        return bool(
            re.match(r"Camera\s+transformation\s*:\s*$", camera, re.I)
        )

    def _prompt_needs_edit_refresh(self, prompt: str) -> bool:
        return (
            self._prompt_needs_v610_refresh(prompt)
            or self._prompt_needs_v611_refresh(prompt)
            or self._prompt_needs_v612_refresh(prompt)
            or self._prompt_needs_v613_refresh(prompt)
            or self._prompt_is_truncated_at_camera_transform(prompt)
        )

    def _edit_allows_extra_resource_refs(self, edit_strategy: str, fixes: str) -> bool:
        blob = f"{edit_strategy} {fixes}"
        return bool(_IDENTITY_CORRECTION_EDIT_RE.search(blob))

    def _strip_v613_keep_sentences(self, prompt: str) -> str:
        """v6.13：移除 story state 与 Camera transformation 之间的 Keep/Preserve 句。"""
        if not prompt:
            return prompt
        body = prompt
        prefix = ""
        gen = re.search(
            r"(Generate\s+case_edit_frame\d+\.png\s*:\s*)",
            prompt,
            re.I,
        )
        if gen:
            prefix = gen.group(1)
            body = prompt[gen.end():]
        pre, rest = self._split_story_state_and_camera(body)
        for pat in (
            r"\s*Keep\b[^.]*?\.\s*",
            r"\s*Preserve\b[^.]*?\.\s*",
            r"\s*Clearly show\b[^.]*?\.\s*",
            r"\s*Reframe the same moment without altering\b[^.]*?\.\s*",
        ):
            pre = re.sub(pat, " ", pre, flags=re.IGNORECASE)
        pre = re.sub(r"\s{2,}", " ", pre).strip()
        if rest:
            out = f"{prefix}{pre} {rest}".strip()
        else:
            out = f"{prefix}{pre}".strip()
        return re.sub(r"\s{2,}", " ", out).strip()

    def _sanitize_edit_surface_prompt(self, prompt: str) -> str:
        """v6.11/v6.13：移除未默认输入的资源图引用与显式 Keep 句。"""
        if not prompt:
            return prompt
        out = re.sub(
            r"\s*Use\s+case_(?:char|prop|scene)_\d+\.png[^.;]*[.;]?",
            " ",
            prompt,
            flags=re.IGNORECASE,
        )
        out = re.sub(
            r"\s*and\s+use\s+case_(?:char|prop|scene)_\d+\.png[^.;]*[.;]?",
            " ",
            out,
            flags=re.IGNORECASE,
        )
        out = re.sub(r"\s{2,}", " ", out).strip()
        return self._strip_v613_keep_sentences(out)

    def _default_edit_style(self) -> str:
        idea = (self.summary.get("idea") or self.state.get("idea") or "").strip()
        if "3D" in idea or "动漫" in idea or "animation" in idea.lower():
            return "Cinematic Chinese fantasy 3D animation style."
        return "Cinematic high-quality animation style."

    def _normalize_v614_camera_type(self, planned_camera: str) -> str:
        p = (planned_camera or "").strip()
        if not p:
            return "single-character cinematic shot"
        pl = p.lower()
        if re.search(r"face\s+close|eye\s+close|extreme\s+close", pl):
            return "single-character face close-up"
        if re.search(r"wrist|hand", pl):
            return "single-character wrist close-up"
        if re.search(r"seated|sitting", pl):
            return "single-character seated close-up"
        if re.search(r"low-angle|low angle", pl):
            return "single-character low-angle wide shot"
        if re.search(r"hero|wide", pl):
            return "single-character heroic wide shot"
        if re.search(r"action", pl):
            return "single-character action close-up"
        if re.search(r"close-up|close up|insert|detail|foreground", pl):
            return "single-character close-up detail shot"
        if re.search(r"overhead|high-angle|high angle", pl):
            return "single-character overhead shot"
        if re.search(r"over-the-shoulder|ots", pl):
            return "single-character over-the-shoulder shot"
        if not p.lower().startswith("single-character"):
            return f"single-character {p}"
        return p

    def _derive_v614_fields_from_plan_entry(self, entry: Dict) -> Dict[str, str]:
        """从 edit plan 行推导 v6.14 Edit Prompt Compiler 字段（§18）。"""
        story_state = (entry.get("story_state") or "").strip()
        camera_type = (entry.get("camera_type") or "").strip()
        visual_focus = (entry.get("visual_focus") or "").strip()
        style = (entry.get("style") or "").strip() or self._default_edit_style()

        planned = entry.get("planned_edit_camera", "")
        fixes = entry.get("fixes", "")
        keep = entry.get("keep", "")
        story_fn = entry.get("story_function", "")

        if not camera_type:
            camera_type = self._normalize_v614_camera_type(planned)

        if not visual_focus:
            visual_focus = fixes or planned or story_fn or "the planned narrative focus for this beat"

        if not story_state:
            for candidate in (fixes, keep):
                if candidate and len(candidate) > 8:
                    story_state = candidate
                    break
            if not story_state and story_fn:
                story_state = f"the story beat for {story_fn} as shown in the base frame"
            if not story_state:
                story_state = "the exact story state shown in the base frame"

        # 第一段不得含镜头黑名单词（粗清理）
        if _V612_CONTENT_BLOCK_INTRUSION_RE.search(story_state):
            story_state = re.sub(
                r"\b(?:close-up|close up|focused on|foreground|background|"
                r"softly visible|close proximity|subtle reflection)\b[^.;,]*",
                "",
                story_state,
                flags=re.IGNORECASE,
            )
            story_state = re.sub(r"\s{2,}", " ", story_state).strip(" ,.;")

        return {
            "story_state": story_state,
            "camera_type": camera_type,
            "visual_focus": visual_focus,
            "style": style,
        }

    def _compile_edit_prompt_v614(
        self,
        edit_name: str,
        base_name: str,
        story_state: str,
        camera_type: str,
        visual_focus: str,
        style: str = "",
    ) -> str:
        """v6.14 §16.5 / §18.2 Edit Prompt Compiler。"""
        style = (style or self._default_edit_style()).strip()
        if style and not style.endswith("."):
            style += "."
        ss = story_state.strip().rstrip(".")
        ct = camera_type.strip().rstrip(".")
        vf = visual_focus.strip().rstrip(".")
        return (
            f"Generate {edit_name}: Use {base_name} as the direct source for the "
            f"exact story state of {ss}. Camera transformation: create a {ct} "
            f"with strong focus on {vf}. {style}"
        )

    def _finalize_edit_surface_prompt(
        self,
        raw: str,
        edit_name: str,
        base_name: str,
        plan_entry: Optional[Dict] = None,
        edit_strategy: str = "",
        fixes: str = "",
    ) -> str:
        """清洗 VLM 输出；若 Camera 块缺失则用 v6.14 Compiler 兜底。"""
        if not raw:
            raw = ""
        if not self._edit_allows_extra_resource_refs(edit_strategy, fixes):
            raw = self._sanitize_edit_surface_prompt(raw)
        raw = re.sub(
            r"\bthe same story state of\b",
            "the exact story state of",
            raw,
            flags=re.IGNORECASE,
        )
        if self._prompt_is_truncated_at_camera_transform(raw) and plan_entry:
            fields = self._derive_v614_fields_from_plan_entry(plan_entry)
            logger.warning(
                "%s prompt truncated at Camera transformation; using v6.14 compiler",
                edit_name,
            )
            return self._compile_edit_prompt_v614(
                edit_name,
                base_name,
                fields["story_state"],
                fields["camera_type"],
                fields["visual_focus"],
                fields["style"],
            )
        return raw.strip()

    def _enrich_edit_plan_entry_v614(self, entry: Dict) -> Dict:
        fields = self._derive_v614_fields_from_plan_entry(entry)
        entry.update(fields)
        return entry

    def _edit_refs_for_generation(
        self,
        base_name: str,
        base_path: str,
        ref_names: List[str],
        edit_strategy: str = "",
        fixes: str = "",
    ) -> tuple:
        """v6.11：默认仅 base frame；身份/道具纠偏时允许规划中的额外资源图。"""
        if base_name not in ref_names:
            ref_names = [base_name] + list(ref_names)
        if self._edit_allows_extra_resource_refs(edit_strategy, fixes):
            paths = self._resolve_reference_paths(ref_names)
            names = [n for n in ref_names if self._resolve_single_path(n)]
            if paths:
                return paths, names
        if base_path and os.path.exists(base_path):
            return [base_path], [base_name]
        paths = self._resolve_reference_paths([base_name])
        return (paths if paths else ([base_path] if base_path else [])), [base_name]

    def _edit_plan_needs_v610_refresh(
        self, entries: List[Dict], total: int,
    ) -> bool:
        """v6.10：规划表若默认 hand insert / 细节清单式策略，需按 §22 重规划。"""
        if not entries:
            return False
        for e in entries:
            blob = " ".join([
                e.get("planned_edit_camera", ""),
                e.get("edit_strategy", ""),
                e.get("fixes", ""),
            ])
            if _V610_OVERFIT_PLAN_RE.search(blob):
                return True
            if re.search(
                r"\bhand\s+insert\b",
                e.get("planned_edit_camera", ""),
                re.I,
            ) and not re.search(
                r"close-up detail|foreground-detail",
                e.get("planned_edit_camera", ""),
                re.I,
            ):
                return True
        return False

    def _build_v69_edit_plan_constraints(self, total: int) -> str:
        """v6.9 §21 + v6.10 §22 注入编辑规划请求的硬约束块。"""
        strict_medium = (
            "用户 idea 若含「复活/苏醒/觉醒」类剧情：**第 2 帧（及相邻觉醒节点）"
            "的 edit 必须是面部/眼睛特写**，禁止继续全身中景平铺。\n"
            "开场帧（E1）可用加强版 wide；**E2–E8 默认禁止普通平视中景**"
            "（除非带低角度/过肩/前景遮挡/强纵深/商品前景等强镜头属性）。\n"
        )
        nine_grid = ""
        if total == 9:
            nine_grid = (
                "\n### v6.9 九宫格镜头覆盖预算（N=9，§21.4）\n"
                "- neutral medium shot **≤ 1**；带强镜头设计的 medium close-up **≤ 2**；\n"
                "- close-up / insert / overhead / high-angle / low-angle / OTS / foreground-depth **≥ 6**；\n"
                "- 最终 9 帧至少 **5 种**不同 camera label；\n"
                "- 参考映射：E1 establish+wide → E2 face/eye close-up → E3 close-up detail / foreground-detail → "
                "E4 trajectory → E5 reaction → E6 overhead → E7 low-angle hero → "
                "E8 foreground-depth → E9 aftermath wide/symbolic。\n"
            )
        elif total == 4:
            nine_grid = (
                "\n### v6.9 四帧模式（§21.4 压缩）\n"
                "- frame01：加强 establishing wide / foreground-depth（可相对 base medium 升格）；\n"
                "- frame02：**必须** face close-up / extreme close-up of eyes（觉醒切镜，禁止全身中景）；\n"
                "- frame03：close-up detail / foreground-detail / low-angle action（禁止 macro hand insert）；\n"
                "- frame04：low-angle hero / wide aftermath；\n"
                "- **四帧中 neutral medium 最多 0–1 帧**。\n"
            )
        v610_block = (
            "\n## v6.10 平衡式电影化提示（§22，与 §21 同时满足）\n"
            "- **镜头要强、细节要弱**：每帧 1 个主视觉焦点 + 2–3 个支持性细节；"
            "禁止把指纹/纹路/火花/触点/能量桥/虚化背景全部堆进「需要修复/增强」。\n"
            "- **叙事分镜默认** `close-up detail shot` 或 `foreground-detail composition`；"
            "慎用 `hand insert shot` / `macro insert`；E3 类道具互动帧须保留脸/场景锚点可读。\n"
            "- 「必须保持」须含 **2 个上下文锚点**（如火焰莲花、祭坛边缘、人物脸）；\n"
            "- 「编辑策略」勿写 magnifying / fingerprint / isolate tactile / blur background alone。\n"
        )
        v611_block = (
            "\n## v6.11 表层 Prompt 策略（§16 / §23，规划层 vs 生成层分离）\n"
            "- 规划表「必须保持」「需要修复/增强」用于 **VLM 规划与候选评选**；"
            "**不要**要求逐条写进最终英文 Generate prompt。\n"
            "- 默认 **仅引用** 对应 `case_base_frame0x.png`（§13.2 base-only）。\n"
            "- 禁止在规划「编辑策略」中默认要求 `Use case_char_xx` / `Use case_prop_xx` 进入表层 prompt；"
            "仅当 base 身份/道具已错且本轮为纠偏时例外。\n"
        )
        v612_block = (
            "\n## v6.12 Prompt 分层解耦（§24，编译逐帧 Generate prompt 时强制）\n"
            "- **Story state 块**（Use … same story state of …）：仅剧情状态；"
            "**禁止** close-up、focused on、close proximity、foreground/background、"
            "softly visible、capturing tension、subtle reflection 等镜头用语。\n"
            "- **镜头块**（Camera transformation）：唯一负责景别、视觉中心、前后景、景深、局部光效。\n"
            "- 同一焦点不得在 story state 与镜头块重复。\n"
        )
        v613_block = (
            "\n## v6.13 No-Keep 表层 Prompt（§25）\n"
            "- **禁止**在最终 prompt 中输出 `Keep … present in the frame` / `Preserve …` / "
            "`Clearly show …` 等独立保持句。\n"
            "- 规划表「必须保持」**仅**供候选评选；不要逐条抄进 Generate prompt。\n"
        )
        v614_block = (
            "\n## v6.14 双段式表层 Prompt + Edit Prompt Compiler（§16 / §18）\n"
            "- **第一段**（Screen-Content）：`Use base as the direct source for the exact story state of …`"
            " — 自然写清这帧画面内容（姿态/动作/道具/场景状态），**不要** Keep 清单，**不要**写镜头词。\n"
            "- **第二段**（Camera）：`Camera transformation: create a [shot type] with strong focus on [emphasis].`\n"
            "- 规划表建议补充三列（供 Compiler）：**story_state 英文句** | **camera_type** | **visual_focus**；"
            "若已给出则逐帧 Generate 必须严格按 Compiler 模板输出。\n"
            "- 标准模板见文档 §16.5 / §17 哪吒示例组。\n"
        )
        return (
            "\n## v6.9 镜头多样性硬约束（§21，违反需整表重写）\n"
            "- **基础帧可以普通（wide/medium 叙事）；编辑帧必须电影化**，不能九张中景平淡图；\n"
            "- 规划前先做 **Story Function → Camera Resolver**（§21.2），"
            "在表中写明「剧情功能」与「目标 edit 镜头」；\n"
            "- 「目标 edit 镜头」必须是特殊镜头标签（close-up / foreground-detail / overhead / "
            "low-angle / OTS / foreground-depth 等），禁止只写 cinematic reframe；\n"
            "- 「编辑策略」必须写清 **Base Camera → Edit Camera** 与 delta，"
            "且 Edit Camera **不能**仍是与 base 相同的 neutral medium；\n"
            f"{strict_medium}"
            f"{nine_grid}"
            f"{v610_block}"
            f"{v611_block}"
            f"{v612_block}"
            f"{v613_block}"
            f"{v614_block}"
            "- 失败回退时换**另一个特殊镜头**，不能退回 base 中景构图（§21.5）。\n"
        )

    def _edit_plan_needs_v69_refresh(
        self, entries: List[Dict], total: int,
    ) -> bool:
        """旧规划若几乎全是 medium 叙事，强制按 v6.9 重规划。"""
        if not entries or total < 2:
            return False
        special = 0
        for e in entries:
            blob = " ".join([
                e.get("edit_strategy", ""),
                e.get("planned_edit_camera", ""),
                e.get("story_function", ""),
            ])
            if _SPECIAL_CAMERA_RE.search(blob):
                special += 1
        # frame2 觉醒特写：4 帧以上时第 2 帧必须有特写类镜头
        if total >= 2:
            e2 = next(
                (x for x in entries if "frame02" in x.get("base_frame", "")),
                None,
            )
            if e2:
                b2 = e2.get("planned_edit_camera", "") + e2.get("edit_strategy", "")
                if not re.search(
                    r"close-up|insert|extreme|eye|face",
                    b2,
                    re.I,
                ):
                    return True
        return special < max(2, total - 1)

    async def _step_select_edit_plan(self):
        print("\n[Step 4/5] 第二阶段：v6.14 全帧镜头再设计规划...")
        total = self.state["total_frames"]

        # 把完整的基础九宫格图片送给 VLM 看
        base_frame_paths: List[str] = []
        base_frame_names: List[str] = []
        for i in range(1, total + 1):
            name = f"case_base_frame{i:02d}.png"
            path = os.path.join(self.output_dir, name)
            if os.path.exists(path):
                base_frame_paths.append(path)
                base_frame_names.append(name)

        if not base_frame_paths:
            logger.warning("No base frames found, skipping edit plan stage")
            self.state["step"] = "edit_frames"
            self._save_state()
            return

        cached_response = self.state.get("edit_plan_response", "")
        cached_entries = self.state.get("edit_plan", [])

        # 若缓存来自 v6.0/v6.2 (含 transform/transform_type)，则视为无效，重新询问
        looks_like_v62 = any(
            ("transform" in e or "transform_type" in e or "shot_frame" in e)
            and "edit_level" not in e
            for e in cached_entries
        ) if cached_entries else False
        # 若缓存是 v6.3 的 6 列规划（没有 beat_mapping），保留 cache 但补 beat 兜底
        if cached_entries and not looks_like_v62:
            for e in cached_entries:
                if "beat_mapping" not in e:
                    base = e.get("base_frame", "")
                    num_match = re.search(r"\d+", base)
                    if num_match:
                        idx = int(num_match.group(0))
                        e["beat_mapping"] = self._fallback_beat_mapping(idx, total)

        needs_v69 = self._edit_plan_needs_v69_refresh(cached_entries, total)
        needs_v610 = self._edit_plan_needs_v610_refresh(cached_entries, total)
        if (
            cached_response and cached_entries and not looks_like_v62
            and not needs_v69 and not needs_v610
        ):
            print("  发现已有全帧再编辑计划，跳过重新询问 VLM")
            response = cached_response
            entries_data = cached_entries
        else:
            if needs_v69 and cached_entries:
                print("  检测到旧版平淡中景再编辑计划，按 v6.9 重新规划...")
            elif needs_v610 and cached_entries:
                print("  检测到 v6.9 细节清单式再编辑计划，按 v6.10 重新规划...")
            diversity_budget = self._build_diversity_budget(total)
            is_classic_9 = (total == 9)
            min_cameras = 3 if total >= 5 else 2
            min_levels = 2

            if is_classic_9:
                mode_note = (
                    "## 帧数模式（v6.5 §3.1：9宫格核心保底）\n"
                    "当前 N=9，沿用 v6.3 标准 9 宫格预算，规划表 beat 列直接填 B1~B9。\n"
                )
            else:
                mode_note = (
                    "## 帧数模式（v6.5 §3.2 / §9.2：任意帧数衍生）\n"
                    f"当前 N={total}，请按工作流文档 §9.2 的镜头多样性预算分配编辑强度；"
                    "短帧数中每一帧都承担关键剧情功能，**不要乱用复杂镜头**。"
                    "规划表 beat 列填写每帧对应的 9 beat 或合并 beat（如 B1 / B3+B4 / B5+B6+B7）。\n"
                )

            request_text = (
                f"基础 {total} 帧全部生成完毕（case_base_frame01.png ~ case_base_frame{total:02d}.png），"
                "请按 v6.14 第二阶段流程（v6.9 镜头多样性 + v6.10 细节预算 + v6.11 base-only + §24 分层 + §16 双段式 prompt）：\n"
                f"{V67_EDIT_PICK_OVERLAY}\n"
                "1) 对整组基础帧做整体质检（人物一致 / 场景一致 / 道具状态 / 剧情可读性 / 帧数是否符合 N）；\n"
                "2) **v6.9：第二阶段是「全帧镜头再设计」**；基础帧可用 medium/wide 锁剧情，"
                "**编辑帧必须换成特殊电影镜头**，禁止整组 neutral medium；\n"
                "2b) **v6.10**：每帧仅 1 个主视觉焦点，支持性细节 ≤3，须保留上下文锚点可读，"
                "禁止 hand insert + 微观细节清单式规划；\n"
                "2c) **v6.11**：规划层可详细，默认仅引用对应 base frame，勿把资源图写进表层 prompt；\n"
                "2d) **v6.14**：逐帧 Generate = Screen-Content（exact story state of …）+ "
                "Camera transformation（create … with strong focus on …）；"
                "**禁止** Keep 清单；keep 列仅用于候选评选；"
                "「需要修复/增强」列请写**英文画面内容句**（供 Compiler 作 story_state），"
                "「目标 edit 镜头」列写 camera_type；\n"
                "3) 为每张 base 先定 **剧情功能（story function）** 与 **目标 edit 镜头（planned edit camera）**，"
                "再定 Level 与编辑策略；\n"
                f"4) 输出完整的全帧再编辑规划表，**共 {total} 行**（每一帧一行，不要多也不要少）。\n\n"
                f"{mode_note}\n"
                f"{self._build_v69_edit_plan_constraints(total)}\n"
                "规划表必须为 markdown 表格，**列顺序固定为 9 列**（v6.9）：\n"
                "| 基础帧 | 对应 beat | 剧情功能 | 目标 edit 镜头 | 基础帧诊断 | "
                "编辑强度 | 编辑策略 | 需要修复/增强 | 必须保持 |\n"
                "字段约定：\n"
                f"- 「基础帧」：写实际文件名 case_base_frame0x.png（01 ~ {total:02d}）；\n"
                "- 「对应 beat」：标准 9 beat 中的一个或合并 beat（如 B1 / B2+B3 / B5+B6+B7）；"
                "若用户 idea 中缺少某 beat，可写 N/A 并简要说明；\n"
                "- 「剧情功能」：Establish / Awakening / Detail activation / Motion / Reaction / "
                "Spatial escalation / Climax / Closure 等（§21.2）；\n"
                "- 「目标 edit 镜头」：必须写特殊镜头（如 face close-up、close-up detail shot、"
                "foreground-detail、overhead、low-angle hero），**禁止** neutral medium、"
                "叙事分镜中的 `hand insert shot` / `macro insert`，或「与 base 相同中景」；\n"
                "- 「基础帧诊断」：用一两句说明该基础帧的画面状况、轻微瑕疵或电影感不足；\n"
                "- 「编辑强度」：必须是 Level 1 / Level 2 / Level 3 / Level 4 之一（v6.7 与规划语义对齐）：\n"
                "    * **整组 N 行中，Level 1 与 Level 2 合计不得超过 1 行**（唯一允许的轻量 polish 行，§9.3）；"
                "且该行仍须在「编辑策略」写明**可验证的机位/构图微调**（略高/略低机位、前景桌面加强、主体偏三分线等），"
                "禁止把 `cinematic polish` / `cleanup` / `improve lighting` 作为唯一策略句；\n"
                "    * Level 3 = moderate camera reframe（**必须**写清 Base Camera → Edit Camera 与 delta 类型："
                "如 medium→medium close-up、wide→stronger wide、eye-level→subtle high-angle 等）\n"
                "    * Level 4 = strong cinematic transformation（insert / close-up / overhead / low-angle / OTS 等，"
                "同样写清 Base→Edit 与 delta）\n"
                "    * 若误标为 Level 1/2 但超过 1 行，整表视为不合格需重写；\n"
                "- 「编辑策略」：**必须**用简短中文或英文写清 **Base Camera → Edit Camera**、**camera delta 类型**（景别/角度/机位侧/构图/焦点/纵深之一或多项），"
                "并说明视觉重点变化；禁止只写「增强氛围」而无镜头变化；\n"
                "- 「需要修复/增强」：列出本帧要在再编辑里改善的具体点；\n"
                "- 「必须保持」：列出本帧绝对不能改变的剧情/位置/朝向/道具状态。\n\n"
                "## 镜头多样性硬约束（v6.9 §21 + v6.7 §10，违反任何一条都需要返工）\n"
                f"- **不允许全部 {total} 帧使用同一种镜头**；**不允许 ≥{max(1, total-1)} 帧都是 neutral medium**；\n"
                f"- **除至多 1 行 Level 1/2 外，其余必须为 Level 3/4**；且须含 **≥{min_cameras} 种**特殊 camera label；\n"
                f"- 整组须 **≥{min_levels} 种 Level** 且含 Level 3 或 Level 4；\n"
                "- 觉醒/睁眼/苏醒节点 → **face close-up / eye extreme close-up**（Level 4）；\n"
                "- 轻量 polish **最多 1 行**，且仍有可见机位变化，不能是中景补光。\n\n"
                f"{diversity_budget}\n\n"
                f"你已经收到全部基础帧图片（按 case_base_frame01~{total:02d} 顺序传入），"
                f"请综合判断后给出完整规划表，**确保恰好 {total} 帧每一行都被填写**。"
            )

            self._sync_gitee_compact_supplements()
            self.vlm.trim_old_frame_images(keep_last_n=0)
            response = await self.vlm.chat(request_text, image_paths=base_frame_paths)

            self._log("edit_plan", {
                "request": request_text[:300],
                "response": response[:2000],
            })

            print("\n--- VLM 全帧再编辑规划 ---")
            print(response[:2000])
            if len(response) > 2000:
                print(f"... (共 {len(response)} 字符)")

            plan_result = self.parser.parse_edit_plan(response)
            entries_data = [
                {
                    "base_frame": e.base_frame,
                    "edit_frame": e.edit_frame,
                    "final_frame": e.final_frame,
                    "beat_mapping": e.beat_mapping,
                    "story_function": e.story_function,
                    "planned_edit_camera": e.planned_edit_camera,
                    "diagnosis": e.diagnosis,
                    "edit_level": e.edit_level,
                    "edit_strategy": e.edit_strategy,
                    "fixes": e.fixes,
                    "keep": e.keep,
                }
                for e in plan_result.entries
            ]

            # 补齐表格缺失的基础帧。
            # 按位置启发式分配 Level/策略，保留一定多样性，避免兜底导致整组规划塌缩成 Level 2。
            covered = {e["base_frame"] for e in entries_data}
            for i in range(1, total + 1):
                name = f"case_base_frame{i:02d}.png"
                if name in covered:
                    continue
                level, strategy, story_fn, planned_cam = self._fallback_edit_assignment(
                    i, total,
                )
                beat = self._fallback_beat_mapping(i, total)
                entries_data.append({
                    "base_frame": name,
                    "edit_frame": f"case_edit_frame{i:02d}.png",
                    "final_frame": f"case_final_frame{i:02d}.png",
                    "beat_mapping": beat,
                    "story_function": story_fn,
                    "planned_edit_camera": planned_cam,
                    "diagnosis": "VLM 规划表未覆盖此帧（v6.9 自动兜底特殊镜头）",
                    "edit_level": level,
                    "edit_strategy": strategy,
                    "fixes": "落实目标特殊镜头，避免 neutral medium 回退",
                    "keep": "保持基础帧的剧情状态与人物位置、朝向、道具状态",
                })

            # 按编号排序
            entries_data.sort(key=lambda e: int(re.search(r"\d+", e["base_frame"]).group(0)))

        for e in entries_data:
            self._enrich_edit_plan_entry_v614(e)

        self.state["edit_plan_response"] = response
        self.state["edit_plan"] = entries_data
        self.summary["edit_plan"] = entries_data
        self._save_summary()

        level_counts = {}
        for e in entries_data:
            lvl = e.get("edit_level", "")
            level_counts[lvl] = level_counts.get(lvl, 0) + 1
        print(f"  全帧再编辑计划: 共 {len(entries_data)} 帧，编辑强度分布 {level_counts}")

        self.state["step"] = "edit_frames"
        self._save_state()

    # ──────────────────────────────────────────────────────────
    # 第三阶段：对每张基础帧都生成对应的 case_edit_frame0x.png (v6.3)
    # ──────────────────────────────────────────────────────────

    async def _step_generate_edit_frames(self):
        print("\n[Step 4/5] 第二阶段：逐帧生成 case_edit_frame0x.png（v6.14 双段式表层 prompt）...")
        plan: List[Dict] = self.state.get("edit_plan", [])
        for entry in plan:
            self._enrich_edit_plan_entry_v614(entry)

        if not plan:
            print("  没有再编辑计划，跳过")
            self.state["step"] = "final_assembly"
            self._save_state()
            return

        for done_name in self.state.get("edit_frames_done", []):
            path = os.path.join(self.output_dir, done_name)
            if os.path.exists(path):
                self.edit_frame_files[done_name] = path

        for entry in plan:
            base_name = entry["base_frame"]
            edit_name = entry.get("edit_frame") or self._derive_edit_name(base_name)
            edit_level = entry.get("edit_level", "Level 3")
            edit_strategy = entry.get("edit_strategy", "camera reframe per v6.9 plan")
            fixes = entry.get("fixes", "")
            keep = entry.get("keep", "")
            beat_mapping = entry.get("beat_mapping", "")
            story_function = entry.get("story_function", "")
            planned_camera = entry.get("planned_edit_camera", "")
            base_idx = 0
            m_idx = re.search(r"\d+", base_name)
            if m_idx:
                base_idx = int(m_idx.group(0))
            if not story_function and base_idx:
                story_function = self._v69_story_function_for_frame(base_idx, len(plan))
            if not planned_camera and base_idx:
                planned_camera = self._v69_planned_camera_for_frame(base_idx, len(plan))
            entry["story_function"] = story_function
            entry["planned_edit_camera"] = planned_camera
            entry["edit_frame"] = edit_name

            final_path = os.path.join(self.output_dir, edit_name)
            cached_prompt = self._get_edit_prompt(edit_name)
            prompt_stale = bool(
                cached_prompt and self._prompt_needs_edit_refresh(cached_prompt)
            )
            if os.path.exists(final_path) and not prompt_stale:
                self.edit_frame_files[edit_name] = final_path
                if edit_name not in self.state["edit_frames_done"]:
                    self.state["edit_frames_done"].append(edit_name)
                print(f"  [skip] {edit_name} 已存在")
                self._save_state()
                continue
            if os.path.exists(final_path) and prompt_stale:
                print(
                    f"  [v6.14] {edit_name} 已存在但缓存 prompt 需按双段式/Compiler 规则重写，将重新抽卡"
                )
                if edit_name in self.state.get("edit_frames_done", []):
                    self.state["edit_frames_done"].remove(edit_name)
                edit_summary = self.summary.get("edit_frames", {}).get(edit_name)
                if isinstance(edit_summary, dict):
                    edit_summary.pop("generation_prompt", None)

            base_path = self._resolve_single_path(base_name)
            if not base_path:
                logger.warning("Base frame %s missing, skipping edit frame %s", base_name, edit_name)
                continue

            prefix = edit_name.replace(".png", "")
            candidates_dir = os.path.join(self.output_dir, f"{prefix}_candidates")
            fp_meta = self.frame_prompts.get("frames", {}).get(edit_name, {})
            pending_regen = fp_meta.get("status") == "pending_regeneration"
            pre_existing = (
                []
                if pending_regen
                else self._find_existing_candidates(candidates_dir)
            )

            print(
                f"\n  --- {edit_name} "
                f"（基于 {base_name}，{edit_level} / {edit_strategy}）"
                f"{' [regen：将重新抽卡]' if pending_regen else ''}"
                f"{' [发现已有候选图]' if pre_existing and not pending_regen else ''} ---"
            )

            edit_prompt = cached_prompt or self._get_edit_prompt(edit_name)
            if edit_prompt and self._prompt_needs_edit_refresh(edit_prompt):
                reason = []
                if self._prompt_needs_v610_refresh(edit_prompt):
                    reason.append("细节过拟合用语")
                if self._prompt_needs_v611_refresh(edit_prompt):
                    reason.append("表层 prompt 含资源引用/约束堆叠")
                if self._prompt_needs_v612_refresh(edit_prompt):
                    reason.append("story state 块侵入镜头描述(§24)")
                if self._prompt_needs_v613_refresh(edit_prompt):
                    reason.append("含显式 Keep/Preserve 句(§25)")
                if self._prompt_is_truncated_at_camera_transform(edit_prompt):
                    reason.append("Camera transformation 块缺失/被截断")
                print(
                    f"  [v6.14] {edit_name} 缓存 prompt 需重写（{', '.join(reason)}），"
                    "将重新向 VLM 请求 v6.14 双段式表层 prompt..."
                )
                edit_prompt = ""
            if not edit_prompt:
                print(f"  从 VLM 获取 {edit_name} 的再编辑 prompt（v6.14 双段式）...")
                self._sync_gitee_compact_supplements(edit_name)
                edit_prompt = await self._request_edit_prompt(
                    edit_name=edit_name,
                    base_name=base_name,
                    edit_level=edit_level,
                    edit_strategy=edit_strategy,
                    fixes=fixes,
                    keep=keep,
                    base_path=base_path,
                    story_function=story_function,
                    planned_edit_camera=planned_camera,
                    plan_entry=entry,
                )
                if not edit_prompt:
                    v614 = self._derive_v614_fields_from_plan_entry(entry)
                    print(f"  VLM 未返回有效 prompt，改用 v6.14 Edit Prompt Compiler...")
                    edit_prompt = self._compile_edit_prompt_v614(
                        edit_name,
                        base_name,
                        v614["story_state"],
                        v614["camera_type"],
                        v614["visual_focus"],
                        v614["style"],
                    )
                if not edit_prompt:
                    raise RuntimeError(f"Missing edit prompt for {edit_name}")

            if not self._edit_allows_extra_resource_refs(edit_strategy, fixes):
                edit_prompt = self._sanitize_edit_surface_prompt(edit_prompt)

            self._record_edit_prompt_snapshot(
                edit_name,
                base_name,
                edit_prompt,
                status="generating_candidates",
                edit_strategy=edit_strategy,
                fixes=fixes,
                extra={
                    "story_function": story_function,
                    "planned_edit_camera": planned_camera,
                    "edit_level": edit_level,
                    "edit_strategy": edit_strategy,
                },
            )

            success = False
            for attempt in range(MAX_REGEN_ATTEMPTS + 1):
                if attempt == 0 and pre_existing:
                    candidates = pre_existing
                    print(f"  使用已有 {len(candidates)} 张候选图...")
                else:
                    edit_prompt = self._sanitize_edit_surface_prompt(edit_prompt)
                    gen_prompt = self.parser.extract_generation_prompt(edit_prompt)
                    ref_names = self.parser.extract_reference_images(edit_prompt)
                    ref_paths, ref_names = self._edit_refs_for_generation(
                        base_name,
                        base_path,
                        ref_names,
                        edit_strategy=edit_strategy,
                        fixes=fixes,
                    )

                    print(
                        f"  生成 {EDIT_FRAME_CANDIDATES} 张候选 (参考图: {ref_names}, v6.11 base-only)"
                        f"{f' [重新抽卡 第{attempt+1}轮]' if attempt > 0 else ''}..."
                    )
                    cand_prefix = prefix if attempt == 0 else f"{prefix}_r{attempt+1}"
                    candidates = await self.img_gen.generate_candidates(
                        prompt=gen_prompt,
                        save_dir=candidates_dir,
                        filename_prefix=cand_prefix,
                        num_candidates=EDIT_FRAME_CANDIDATES,
                        images=ref_paths if ref_paths else None,
                    )

                print(f"  VLM 评选最佳编辑帧...")
                extra_base = [base_path] if base_path else []
                max_vl_images = (
                    GITEE_MAX_VL_IMAGES
                    if self.vlm.provider == "gitee"
                    else None
                )
                context_images, num_pick, pick_omit_note = cap_pick_context_images(
                    candidates,
                    extra_base,
                    max_vl_images,
                    prioritize_extras=True,
                )
                ref_label_map = {}
                if base_path and base_path in context_images:
                    ref_label_map[base_name] = context_images.index(base_path) + 1

                pick_text = self._build_edit_frame_pick_prompt(
                    edit_name, base_name, edit_level, edit_strategy, fixes, keep,
                    edit_prompt, num_pick, ref_label_map,
                    beat_mapping=beat_mapping,
                    story_function=story_function,
                    planned_edit_camera=planned_camera,
                )
                if pick_omit_note:
                    pick_text += f"\n\n**传图说明**：{pick_omit_note}"

                pick_response = await self.vlm.chat_without_history(
                    text=pick_text,
                    image_paths=context_images,
                    system_prompt=EDIT_FRAME_PICK_SYSTEM_PROMPT,
                )

                pick_result = self.parser.parse_image_pick(pick_response, num_pick)

                if pick_result.all_rejected:
                    print(f"  {edit_name} 全部候选不合格: {pick_result.reason}")
                    self._log("edit_frame_all_rejected", {
                        "frame": edit_name,
                        "attempt": attempt + 1,
                        "reason": pick_result.reason,
                    })
                    if attempt >= MAX_REGEN_ATTEMPTS:
                        print(f"  达到最大重试次数，强制使用第 1 张")
                    else:
                        fb_cam = self._v69_edit_camera_fallback(
                            base_idx or 1, len(plan), attempt + 1,
                        )
                        print(f"  v6.14 回退：换备选特殊镜头 → {fb_cam}")
                        edit_prompt = await self._request_edit_prompt(
                            edit_name=edit_name,
                            base_name=base_name,
                            edit_level=edit_level,
                            edit_strategy=edit_strategy,
                            fixes=fixes,
                            keep=keep,
                            base_path=base_path,
                            story_function=story_function,
                            planned_edit_camera=fb_cam,
                            fallback_camera=fb_cam,
                            plan_entry=entry,
                        )
                        if not edit_prompt:
                            continue
                        continue

                chosen_idx = pick_result.chosen_index
                if chosen_idx < 1 or chosen_idx > num_pick:
                    chosen_idx = 1

                chosen_path = candidates[chosen_idx - 1]
                shutil.copy2(chosen_path, final_path)
                self.edit_frame_files[edit_name] = final_path
                if pending_regen:
                    fp_done = self.frame_prompts.setdefault("frames", {}).get(edit_name)
                    if isinstance(fp_done, dict):
                        fp_done["status"] = "picked"

                print(f"  {edit_name} 选择第 {chosen_idx} 张")
                self._log("edit_frame_pick", {
                    "frame": edit_name,
                    "base": base_name,
                    "edit_level": edit_level,
                    "edit_strategy": edit_strategy,
                    "attempt": attempt + 1,
                    "chosen": chosen_idx,
                    "reason": pick_response[:300],
                })

                _, ref_names = self._edit_refs_for_generation(
                    base_name,
                    base_path,
                    self.parser.extract_reference_images(edit_prompt),
                    edit_strategy=edit_strategy,
                    fixes=fixes,
                )
                self.summary["edit_frames"][edit_name] = {
                    "base_frame": base_name,
                    "story_function": story_function,
                    "planned_edit_camera": planned_camera,
                    "edit_level": edit_level,
                    "edit_strategy": edit_strategy,
                    "fixes": fixes,
                    "keep": keep,
                    "generation_prompt": edit_prompt,
                    "reference_images": ref_names,
                    "chosen_candidate": chosen_idx,
                    "pick_reason": pick_response.strip(),
                    "attempts": attempt + 1,
                    "file": edit_name,
                }
                self._record_edit_prompt_snapshot(
                    edit_name,
                    base_name,
                    edit_prompt,
                    status="picked",
                    edit_strategy=edit_strategy,
                    fixes=fixes,
                    extra={
                        "chosen_candidate": chosen_idx,
                        "story_function": story_function,
                        "planned_edit_camera": planned_camera,
                    },
                    quiet=True,
                )
                self._save_summary()

                if edit_name not in self.state["edit_frames_done"]:
                    self.state["edit_frames_done"].append(edit_name)
                self._save_state()
                success = True
                break

            if not success:
                raise RuntimeError(f"Failed to generate {edit_name}")

        self.state["step"] = "final_assembly"
        self._save_state()

    # ──────────────────────────────────────────────────────────
    # 第四阶段：v6.5 — 默认用 edit frame 作为最终帧，缺失回退到 base frame
    # ──────────────────────────────────────────────────────────

    def _step_final_assembly(self):
        print("\n[Step 5/5] 最终分镜整理（v6.5：默认全部使用 case_edit_frame0x.png）...")
        plan: List[Dict] = self.state.get("edit_plan", [])
        total = self.state["total_frames"]

        if not plan:
            # 没有再编辑计划，所有基础帧 1:1 映射为最终帧（异常回退）
            plan = [
                {
                    "base_frame": f"case_base_frame{i:02d}.png",
                    "edit_frame": f"case_edit_frame{i:02d}.png",
                    "final_frame": f"case_final_frame{i:02d}.png",
                    "diagnosis": "",
                    "edit_level": "",
                    "edit_strategy": "无再编辑计划",
                    "fixes": "",
                    "keep": "",
                }
                for i in range(1, total + 1)
            ]

        final_mapping = {}
        for entry in plan:
            base_name = entry["base_frame"]
            num_match = re.search(r"\d+", base_name)
            if not num_match:
                continue
            idx = int(num_match.group(0))
            final_name = entry.get("final_frame") or f"case_final_frame{idx:02d}.png"
            edit_name = entry.get("edit_frame") or f"case_edit_frame{idx:02d}.png"

            edit_path = os.path.join(self.output_dir, edit_name)
            base_path = os.path.join(self.output_dir, base_name)

            if os.path.exists(edit_path):
                source_path = edit_path
                source_label = edit_name
                used_edit = True
            elif os.path.exists(base_path):
                # v6.5 §17.3：编辑帧缺失时允许回退到 base 作为最终帧（异常情况）
                source_path = base_path
                source_label = base_name
                used_edit = False
                logger.warning(
                    "Edit frame %s missing, falling back to %s for final frame",
                    edit_name, base_name,
                )
            else:
                logger.warning("Neither edit nor base frame exists for %s", final_name)
                continue

            target_path = os.path.join(self.output_dir, final_name)
            shutil.copy2(source_path, target_path)
            final_mapping[final_name] = {
                "source": source_label,
                "edit_level": entry.get("edit_level", ""),
                "edit_strategy": entry.get("edit_strategy", ""),
                "used_edit_frame": used_edit,
            }
            print(f"  {final_name}  <-  {source_label}"
                  f"{' (fallback to base)' if not used_edit else ''}")

        self.summary["final_frames"] = final_mapping
        self._save_summary()
        if self.state.get("pipeline_mode", "frames") in ("full",):
            self.state["step"] = "ltx_plan"
            print("  参考帧阶段完成，下一步将进入 LTX Shot 规划")
        else:
            self.state["step"] = "done"
        self._save_state()

    # ──────────────────────────────────────────────────────────
    # 各类辅助：summary 更新 / 评选 prompt / shot prompt 请求
    # ──────────────────────────────────────────────────────────

    def _fallback_edit_assignment(
        self, frame_idx: int, total: int,
    ) -> tuple:
        """VLM 规划表漏帧时的 v6.9 兜底：特殊镜头 + story function。"""
        story = self._v69_story_function_for_frame(frame_idx, total)
        planned = self._v69_planned_camera_for_frame(frame_idx, total)
        if total <= 0:
            return (
                "Level 4", f"{story} | medium → {planned}",
                story, planned,
            )
        if frame_idx == 2 or "Awakening" in story:
            level = "Level 4"
        elif frame_idx in (1, total):
            level = "Level 3"
        else:
            level = "Level 4"
        strategy = (
            f"{story} | Base medium/wide → Edit {planned} | "
            f"shot scale + angle delta; forbid neutral eye-level medium"
        )
        return (level, strategy, story, planned)

    def _build_diversity_budget(self, total: int) -> str:
        """根据总帧数动态生成「镜头多样性预算」提示，给 VLM 参考用于全帧再编辑规划。

        - N=4/5/6/9：使用 v6.5 §9.2 给出的明确预算（保证短帧数也有可执行参考，
          且 N=9 时与 v6.3 标准 9 宫格预算等价，符合『9宫格逻辑不变』承诺）。
        - 其他 N：按 9 帧比例 scale，并指出预算只是建议。
        """
        if total <= 0:
            return ""

        # v6.5 §9.2 明确预算（4 / 5 / 6 / 9）
        explicit_budgets = {
            4: (
                "v6.9 四帧：E1 foreground-depth wide；**E2 必须 face/eye extreme close-up**；"
                "E3 insert/low-angle action；E4 low-angle hero/aftermath；neutral medium ≤1。\n"
                "（v6.7：至少 2 帧 camera delta；最多 1 帧轻量 polish）"
            ),
            5: (
                "v6.7 §10：**至少 3 帧**有明确 camera delta；"
                "含 1 帧 wide/establishing reframe、1 帧 medium close-up、1 帧 insert/close-up/OTS/low-angle；"
                "**最多 1 帧**轻微 polish。\n"
                "（旧摘要：约 2 帧 wide；约 1–2 帧 medium；约 1 帧强镜头；Level 4 最多 2 帧）"
            ),
            6: (
                "v6.7 §10：**至少 4 帧**有明确 camera delta；"
                "1–2 帧 wide 或 stronger wide；1–2 帧 medium / medium close-up；"
                "1 帧 insert / close-up；1 帧 overhead / OTS / low-angle；**最多 1 帧**轻微 polish。\n"
                "（旧摘要：约 2 wide + 2 medium + 1 insert + 1 OTS/overhead/low-angle）"
            ),
            9: (
                "v6.9 §21.4：**neutral medium ≤1**；特殊镜头（close-up/insert/overhead/low-angle/OTS）**≥6**；"
                "E2 必须 face/eye close-up；E7 low-angle hero；至少 5 种 camera label。\n"
                "（v6.7：至少 6 帧 camera delta；最多 1 帧轻量 polish）"
            ),
        }

        if total in explicit_budgets:
            budget_text = explicit_budgets[total]
            note_extra = ""
            if total == 9:
                note_extra = "（这就是 v6.3 标准 9 宫格预算）"
            elif total <= 6:
                note_extra = "（短帧数中每一帧都承担关键剧情功能，**不要乱用复杂镜头**）"
            return (
                f"镜头多样性预算（**v6.9 §21 + v6.7 §10**，共 {total} 帧{note_extra}）：{budget_text}。\n"
                f"**禁止**整组 neutral medium 平淡叙事；**不允许**仅补光/锐化而无镜头变化。"
            )

        # 其他 N（如 7、8、10–25）按比例 scale
        blocks = [
            ("wide / strong wide shot（空间交代、开场或收束）", 0.30),
            ("medium / medium close-up（人物动作、视线、姿态）", 0.30),
            ("insert shot / close-up（手、道具、接触点的关键细节）", 0.18),
            ("overhead / high-angle view（俯视布局或能量扩散）", 0.11),
            ("low-angle / over-the-shoulder view（仰视力量感或凝视目标物）", 0.11),
        ]

        remaining = total
        parts: List[str] = []
        for i, (name, ratio) in enumerate(blocks):
            if remaining <= 0:
                break
            if i == len(blocks) - 1:
                n = remaining
            else:
                n = max(0, round(total * ratio))
                n = min(n, remaining)
            if n <= 0:
                continue
            parts.append(f"约 {n} 帧使用 {name}")
            remaining -= n

        budget_text = "；".join(parts)
        return (
            f"镜头多样性预算（共 {total} 帧，可以±1 浮动）：{budget_text}。"
            f"须满足 **v6.7 §10**：多数帧相对 base 有明确 camera delta；**最多 1 帧**可为轻量 polish。"
            f"注意：以上是建议比例；**不允许**全部帧仅有调色修瑕而无镜头再设计。"
        )

    def _extract_camera_label_from_prompt(self, prompt: str) -> str:
        """从 v6.14 表层 prompt 的 Camera transformation 块提取镜头标签。"""
        if not prompt:
            return ""
        m = re.search(
            r"Camera\s+transformation\s*:\s*create\s+(?:a\s+)?(.+?)"
            r"(?:\.\s+(?:Cinematic|Use )|\.\s*$)",
            prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:160]
        m2 = _SPECIAL_CAMERA_RE.search(prompt)
        if m2:
            return m2.group(0).strip()
        return ""

    def _camera_diversity_bucket(self, camera_label: str) -> str:
        """将镜头描述归并为多样性统计桶。"""
        low = (camera_label or "").lower()
        if not low:
            return "unknown"
        if re.search(r"face|eye|extreme\s+close", low):
            return "face/eye close-up"
        if re.search(r"insert|detail|foreground-detail|wrist|hand|finger", low):
            return "detail/insert"
        if re.search(r"overhead|high-angle|high angle", low):
            return "overhead/high-angle"
        if re.search(r"low-angle|low angle|hero", low):
            return "low-angle/hero"
        if re.search(r"over-the-shoulder|\bots\b", low):
            return "OTS"
        if re.search(r"wide|establish|foreground-depth", low):
            return "wide/establishing"
        if re.search(r"side|trajectory|diagonal", low):
            return "side/trajectory"
        if re.search(r"reaction", low):
            return "reaction close-up"
        if re.search(r"neutral.*medium|eye-level medium", low):
            return "neutral medium"
        if re.search(r"close-up|close up", low):
            return "close-up"
        if re.search(r"medium", low):
            return "medium"
        return camera_label[:48] or "unknown"

    def _edit_prompt_for_ledger(self, edit_name: str) -> str:
        fp = self.frame_prompts.get("frames", {}).get(edit_name, {})
        if isinstance(fp, dict):
            p = fp.get("generation_prompt", "")
            if p:
                return p
        ef = self.summary.get("edit_frames", {}).get(edit_name, {})
        if isinstance(ef, dict):
            return ef.get("generation_prompt", "") or ""
        return ""

    def _build_edit_plan_camera_baseline(self, total: int) -> str:
        """编辑计划阶段：无已定帧时给出默认目标镜头基线。"""
        rows: List[str] = []
        for i in range(1, total + 1):
            cam = self._v69_planned_camera_for_frame(i, total)
            story = self._v69_story_function_for_frame(i, total)
            rows.append(f"- E{i:02d} ({story}): {cam}")
        return "\n".join(rows)

    def _build_camera_diversity_ledger(self, current_edit_name: str = "") -> str:
        """
        结构化镜头多样性账本：Gitee VL 压缩时优先注入，避免丢失去重上下文。
        供逐帧 edit prompt 与 VL compact 共用。
        """
        plan: List[Dict] = self.state.get("edit_plan", [])
        total = len(plan)
        if total <= 0:
            return ""

        current_idx = 0
        if current_edit_name:
            m_cur = re.search(r"\d+", current_edit_name)
            if m_cur:
                current_idx = int(m_cur.group(0))

        done_set = set(self.state.get("edit_frames_done", []))
        lines = [
            "## 镜头多样性运行账本（v6.9 §21 + v6.7 §10）",
            self._build_diversity_budget(total),
            "",
            "### 各帧镜头台账（✓=已定稿 →=当前帧 ·=待生成）",
            "| 帧 | 剧情功能 | 规划目标镜头 | 已定表层镜头 |",
            "|---|---|---|---|",
        ]

        used_buckets: List[str] = []
        planned_buckets: List[str] = []
        current_planned = ""

        for entry in plan:
            base_name = entry.get("base_frame", "")
            edit_name = entry.get("edit_frame") or self._derive_edit_name(base_name)
            m_idx = re.search(r"\d+", edit_name)
            idx = int(m_idx.group(0)) if m_idx else 0
            story_fn = entry.get("story_function", "") or (
                self._v69_story_function_for_frame(idx, total) if idx else ""
            )
            planned = entry.get("planned_edit_camera", "") or (
                self._v69_planned_camera_for_frame(idx, total) if idx else ""
            )
            if planned:
                planned_buckets.append(self._camera_diversity_bucket(planned))

            if edit_name == current_edit_name:
                current_planned = planned
            status = "→" if edit_name == current_edit_name else ("✓" if edit_name in done_set else "·")

            actual = ""
            if edit_name in done_set or self._edit_prompt_for_ledger(edit_name):
                actual = self._extract_camera_label_from_prompt(
                    self._edit_prompt_for_ledger(edit_name)
                )
                if actual:
                    used_buckets.append(self._camera_diversity_bucket(actual))

            def _cell(s: str, n: int) -> str:
                s = (s or "—").replace("|", "/").strip()
                return s if len(s) <= n else s[: n - 1] + "…"

            lines.append(
                f"| {status} E{idx:02d} | {_cell(story_fn, 28)} | "
                f"{_cell(planned, 42)} | {_cell(actual, 36)} |"
            )

        unique_used = list(dict.fromkeys(used_buckets))
        unique_planned = list(dict.fromkeys(planned_buckets))
        neutral_used = sum(1 for b in used_buckets if b == "neutral medium")
        lines.extend([
            "",
            f"### 已定稿镜头类型（{len(used_buckets)} 帧）："
            + (", ".join(unique_used) if unique_used else "（尚无）"),
            f"### 全片规划镜头类型（去重 {len(unique_planned)} 种）："
            + (", ".join(unique_planned) if unique_planned else "—"),
        ])
        if neutral_used >= 1 and total >= 5:
            lines.append(
                f"- 警告：已定稿中 neutral medium 已出现 {neutral_used} 次；"
                f"全片 **最多 1 帧** 允许轻量 polish，其余须特殊镜头。"
            )
        if current_edit_name and current_planned:
            lines.extend([
                "",
                f"### 当前任务 {current_edit_name}",
                f"- **必须**落实规划镜头：{current_planned}",
                "- **禁止**退回与 base 同景别的 neutral eye-level medium shot。",
                "- 须与上表「已定表层镜头」列已有类型形成**可见区分**（scale/angle/side/focus/depth 至少一项）。",
            ])
            if unique_used:
                repeat = [b for b in unique_used if used_buckets.count(b) >= 2]
                if repeat:
                    lines.append(
                        f"- 已重复使用的类型 {', '.join(repeat)}：当前帧请换用其他特殊镜头。"
                    )
        return "\n".join(lines)

    def _sync_gitee_compact_supplements(self, current_edit_name: str = "") -> None:
        """Gitee 双模型：向 VL 客户端注入镜头多样性账本（DashScope 不注入）。"""
        if self.vlm.provider != "gitee" or self.vlm.text_model == self.vlm.vl_model:
            self.vlm.clear_compact_supplements()
            return
        ledger = self._build_camera_diversity_ledger(current_edit_name)
        if ledger:
            self.vlm.set_compact_supplements(ledger)
        else:
            total = (
                self.state.get("total_frames")
                or self.summary.get("grid")
                or len(self.state.get("edit_plan", []))
            )
            if total:
                baseline = (
                    "## 镜头多样性运行账本（规划阶段基线）\n"
                    + self._build_diversity_budget(int(total))
                    + "\n\n### v6.9 默认目标镜头\n"
                    + self._build_edit_plan_camera_baseline(int(total))
                )
                self.vlm.set_compact_supplements(baseline)
            else:
                self.vlm.clear_compact_supplements()

    def _fallback_beat_mapping(self, frame_idx: int, total: int) -> str:
        """VLM 规划表漏帧时的兜底 beat 映射（v6.5 §3.3）。"""
        standard_maps = {
            4: {1: "B1", 2: "B3+B4", 3: "B5+B6+B7", 4: "B8+B9"},
            5: {1: "B1", 2: "B2+B3", 3: "B4+B5", 4: "B6+B7", 5: "B8+B9"},
            6: {1: "B1", 2: "B2", 3: "B3+B4", 4: "B5+B6", 5: "B7", 6: "B8+B9"},
            7: {1: "B1", 2: "B2", 3: "B3", 4: "B4+B5", 5: "B6", 6: "B7", 7: "B8+B9"},
            8: {
                1: "B1", 2: "B2", 3: "B3", 4: "B4",
                5: "B5", 6: "B6", 7: "B7", 8: "B8+B9",
            },
            9: {i: f"B{i}" for i in range(1, 10)},
        }
        if total in standard_maps and frame_idx in standard_maps[total]:
            return standard_maps[total][frame_idx]
        if total <= 0:
            return ""
        # 大于 9 的情况按位置粗略映射到 B1~B9
        beat_idx = max(1, min(9, round((frame_idx / total) * 9)))
        return f"B{beat_idx}"

    def _build_opening_directive(self, opening_orientation: str) -> str:
        """根据用户选择的开场朝向，给 VLM 生成对应中文指令块。"""
        norm = (opening_orientation or "auto").strip().lower()
        # 一些常见别名
        if norm in ("frontal", "正面", "front-view", "front_view"):
            norm = "front"
        elif norm in ("rear", "behind", "背面", "背影", "back-view"):
            norm = "back"
        elif norm in ("profile", "侧面", "side-view"):
            norm = "side"
        elif norm in ("", "neutral", "中立", "自动"):
            norm = "auto"

        if norm == "front":
            return (
                "## 开场朝向（用户已指定：正面/三分之二正面）\n"
                "case_base_frame01.png 必须采用 front view 或 three-quarter front view，"
                "重点交代人物身份、面部、胸前服饰、桌面/手部线索等正面信息。\n"
                "- camera 描述中必须出现 'front view' 或 'three-quarter front view'；\n"
                "- prompt 必须说明能清楚看到 facial structure / collar / front robe pattern / hand position 等正面细节；\n"
                "- 不允许把第一帧画成背影或大面积背面遮挡。\n"
                "- 在『开场意图与朝向选择』小节中说明这个朝向如何服务于开场叙事。\n"
            )
        if norm == "back":
            return (
                "## 开场朝向（用户已指定：背影/三分之二背影）\n"
                "case_base_frame01.png 必须采用 back view 或 three-quarter back view，"
                "重点引导观众沿人物视线进入空间、看到远处目标物。\n"
                "- camera 描述中必须出现 'full-body back view' 或 'three-quarter back view'；\n"
                "- 必须加上 'his back faces the camera' + 'Do not show his face, do not rotate the camera to the front side'；\n"
                "- prompt 重点描述背面可见细节（back of hairstyle、rear shoulder line、back-view robe folds、boots 等），"
                "并写明 eyes / nose / mouth / facial expression 不可见；\n"
                "- 在『开场意图与朝向选择』小节中说明这个朝向如何服务于开场叙事。\n"
            )
        if norm == "side":
            return (
                "## 开场朝向（用户已指定：侧面/三分之二侧面）\n"
                "case_base_frame01.png 必须采用 side view 或 three-quarter side view，"
                "重点表现人物进入、跨过门槛、沿桌边移动等运动方向。\n"
                "- camera 描述中必须出现 'side view' 或 'three-quarter side view'；\n"
                "- prompt 必须写明哪一侧肩膀靠近镜头、身体朝哪个方向，以及侧面轮廓的服装/发型细节；\n"
                "- 不允许把第一帧画成完整正面肖像或完整背影。\n"
                "- 在『开场意图与朝向选择』小节中说明这个朝向如何服务于开场叙事。\n"
            )

        return (
            "## 开场朝向（用户未指定，按 v6.2 朝向中立原则自行判断）\n"
            "case_base_frame01.png 没有默认朝向偏好，**绝对不要因为之前的稳定性考虑就机械地选背影**。\n"
            "请按下列流程决定：\n"
            "1. 先判断开场镜头的叙事功能（介绍人物 / 介绍空间 / 进入动作 / 道具线索 / 悬念建立）；\n"
            "2. 再在以下五种 Subject–Camera Relation 中选择最匹配的一种：\n"
            "   - back view：人物刚进入未知空间，需要带观众沿视线看向远处目标物；\n"
            "   - three-quarter back view：背影 + 部分轮廓，强调空间纵深但仍保留人物侧脸/服装边；\n"
            "   - front view：开场重点是展示人物身份/正面气质/桌面正前方线索；\n"
            "   - three-quarter front view：人物已就位，既要看到脸/服饰，又要保留远处目标作为背景锚点；\n"
            "   - side view / three-quarter side view：表现人物跨入、行进或沿场景边缘移动；\n"
            "3. 在『开场意图与朝向选择』小节中先写中文 reasoning（你为什么选这个朝向），"
            "再给出最终选定值；最终 prompt 里只写选定的朝向词，不要写解释性语句。\n"
            "禁止仅因为『背影更稳定』就选择背影；如果场景核心是人物或桌面线索，请果断选择正面/三分之二正面。\n"
        )

    def _derive_edit_name(self, base_name: str) -> str:
        """case_base_frame03.png → case_edit_frame03.png"""
        num_match = re.search(r"\d+", base_name)
        if not num_match:
            return base_name.replace("case_base_frame", "case_edit_frame")
        return f"case_edit_frame{int(num_match.group(0)):02d}.png"

    def _derive_shot_name(self, base_name: str) -> str:
        """旧版命名（v6.0/v6.2），保留用于兼容旧 state。"""
        num_match = re.search(r"\d+", base_name)
        if not num_match:
            return base_name.replace("case_base_frame", "case_shot_frame")
        return f"case_shot_frame{int(num_match.group(0)):02d}.png"

    def _update_base_frame_summary(
        self,
        frame_name: str,
        generation_prompt: str,
        chosen_candidate: int,
        pick_reason: str,
        review_result: str,
        attempts: int,
    ):
        generation_prompt = (
            self.parser.extract_generation_block(generation_prompt, frame_name)
            or generation_prompt
        )
        ref_names = self.parser.extract_reference_images(generation_prompt)
        self.summary["base_frames"][frame_name] = {
            "generation_prompt": generation_prompt,
            "reference_images": ref_names,
            "chosen_candidate": chosen_candidate,
            "pick_reason": pick_reason.strip(),
            "review_result": review_result,
            "attempts": attempts,
            "file": frame_name,
        }
        self._upsert_frame_prompt(
            frame_name,
            stage="base",
            generation_prompt=generation_prompt,
            reference_images=ref_names,
            status="picked",
            extra={
                "chosen_candidate": chosen_candidate,
                "attempts": attempts,
            },
            quiet=True,
        )
        self._save_summary()

    def _build_resource_pick_prompt(
        self, resource_name: str, gen_prompt: str, num_candidates: int
    ) -> str:
        res_type = "人物参考图"
        if "scene" in resource_name:
            res_type = "场景参考图"
        elif "prop" in resource_name:
            res_type = "道具参考图"

        return (
            f"## 任务：从 {num_candidates} 张候选图中选出最佳的{res_type}\n\n"
            f"资源名称：{resource_name}\n"
            f"生成时使用的 prompt：{gen_prompt}\n\n"
            f"按顺序传入的图片分别是第 1 张到第 {num_candidates} 张候选图。\n\n"
            f"请对每张候选图逐一评估，然后选出最符合{res_type}评选标准的一张。\n"
            f"{PICK_OUTPUT_FORMAT_RULE}\n"
            f"输出格式：选择第X张，原因：..."
        )

    async def _revise_resource_prompt_from_feedback(
        self,
        resource_name: str,
        previous_prompt_line: str,
        review_reason: str,
        review_suggestion: str,
        pick_response_full: str,
    ) -> str:
        """评选全部不合格时，把原因/修改意见交给 VLM，在上一版英文 prompt 上改写出一行新的 Generate。"""
        user = (
            f"资源文件名（Generate 行必须使用该文件名）：{resource_name}\n\n"
            f"## 上一轮用于文生图的完整 prompt（含 Generate 前缀亦可）\n{previous_prompt_line}\n\n"
            f"## 评选原因\n{review_reason or '(未单独解析)'}\n\n"
            f"## 修改意见（可能为中文或条列）\n{review_suggestion or '(见下方原文)'}\n\n"
            f"## 评选模型完整回复（供交叉参考，勿逐字翻译无关内容）\n{pick_response_full[:6000]}"
        )
        reply = await self.vlm.chat_without_history(
            text=user,
            system_prompt=RESOURCE_PROMPT_REVISE_SYSTEM_PROMPT,
            temperature=0.35,
            max_tokens=2048,
        )
        self._log("resource_prompt_revise", {
            "resource": resource_name,
            "response": reply[:2500],
        })
        line = self.parser.extract_generate_line_for_target(reply, resource_name)
        if line:
            return line.strip()
        block = self.parser.extract_generation_block(reply, resource_name)
        return block.strip() if block else ""

    def _build_base_frame_pick_prompt(
        self,
        frame_name: str,
        gen_prompt: str,
        num_candidates: int,
        ref_names: List[str],
        ref_label_map: Dict[str, int],
    ) -> str:
        ref_desc = ""
        if ref_names:
            ref_lines = []
            for rn in ref_names:
                if rn in ref_label_map:
                    ref_lines.append(f"- {rn}（传入的第 {ref_label_map[rn]} 张图）")
                else:
                    ref_lines.append(f"- {rn}")
            ref_desc = (
                "\n\n参考图说明（附在候选图之后）：\n"
                + "\n".join(ref_lines)
                + "\n请将候选帧与这些参考图进行一致性对比。"
            )

        total = self.state.get("total_frames", 0) or 0
        frame_count_note = ""
        if total > 0:
            frame_count_note = (
                f"（当前 N={total}"
                f"{'，9宫格核心模式' if total == 9 else '，任意帧数衍生模式'}）"
            )

        return (
            f"## 任务：v6.5 第一阶段——从 {num_candidates} 张候选基础帧中选出最佳的 {frame_name} {frame_count_note}\n\n"
            f"{V66_BASE_PICK_OVERLAY}\n"
            f"生成时使用的一段式 prompt：\n{gen_prompt}\n"
            f"\n按顺序传入的前 {num_candidates} 张图片是候选基础帧（第 1 张到第 {num_candidates} 张）。"
            f"{ref_desc}\n\n"
            f"评选维度（八条一票否决，请先逐张判断再做选择）：\n"
            f"0. **Frame Count Fit（v6.5 §9.1，一票否决）**：候选是否准确表现 N={total} 帧规划中"
            f"当前帧 {frame_name} 的剧情节点？是否会与前后帧叙事重复或缺失关键因果？\n"
            f"0.5 **Scene Composition Accuracy（v6.5 §9.1，一票否决）**："
            f"候选是否继承了 case_scene 的原始空间结构、主视角、关键物体位置和光源方向？"
            f"仅继承\u201c氛围\u201d但构图被重新生成（桌子位置变了、窗户消失了等）⇒ 排除。\n"
            f"0.6 **No Invented Anchor（v6.5 §6.3，一票否决）**："
            f"候选是否包含了场景参考图中不存在的入口、门槛、台阶、门框、石柱等新场景结构？"
            f"prompt 中写了 'Do not create...' 的元素如果出现在候选中 ⇒ 排除。\n"
            f"1. **人物体态完整性（一票否决）**：四肢比例是否正常？手臂/手指有无畸形？\n"
            f"2. **Subject–Camera Relation（一票否决）**：先逐一标注每张候选属于"
            f"「正面 / 三分之二正面 / 侧面 / 三分之二背面 / 背面」中的哪一种，"
            f"再与 prompt 描述比对。prompt 写 back view 就只接受真正的背影，露出正脸/转正即不合格。\n"
            f"3. **场景环境一致性（一票否决）**：地面纹理、空间结构、光照、家具位置是否与"
            f" case_scene 或上一基础帧一致？凭空多出的元素（如月亮/卷轴/前景柱子）也不合格。\n"
            f"4. **基础镜头准确性（一票否决）**：是否为 prompt 指定的 wide shot 或 medium shot？"
            f"是否避免了特写/俯视/过肩/极端低角度等复杂镜头？\n"
            f"5. **Reference Stability（一票否决）**：镜头尺度和人物大小是否承袭上一帧？"
            f"是否因为重复引用 case_char_01.png 把镜头拉近或把人物强行转正？\n"
            f"6. Prompt 契合度：画面主体、可见元素、动作、视线是否匹配\n"
            f"7. 人物一致性：与 case_char 资源图对比发型、服装、配饰\n"
            f"8. Subject–Object Relation：人物面向/背向目标物是否正确（与 Subject–Camera 是两件事）\n"
            f"9. 锚点定位：人物脚/鞋与地面锚点的位置关系是否正确\n"
            f"10. 后续参考稳定性：是否适合作为下一帧基础剧情参考\n\n"
            f"{PICK_OUTPUT_FORMAT_RULE}\n"
            f"输出格式：选择第X张，原因：...（理由中必须点明每张候选的 Subject–Camera 视角与 Frame Count Fit）"
        )

    def _build_edit_frame_pick_prompt(
        self,
        edit_name: str,
        base_name: str,
        edit_level: str,
        edit_strategy: str,
        fixes: str,
        keep: str,
        edit_prompt: str,
        num_candidates: int,
        ref_label_map: Dict[str, int],
        beat_mapping: str = "",
        story_function: str = "",
        planned_edit_camera: str = "",
    ) -> str:
        base_label = ref_label_map.get(base_name)
        base_desc = f"基础帧 {base_name}"
        if base_label:
            base_desc += f"（传入的第 {base_label} 张图）"

        beat_line = f"对应 beat：{beat_mapping}（v6.5 §3）\n" if beat_mapping else ""
        v69_line = ""
        if story_function or planned_edit_camera:
            v69_line = (
                f"v6.13 剧情功能：{story_function or '（见规划表）'}\n"
                f"v6.13 目标 edit 镜头：{planned_edit_camera or '（见规划表）'}\n"
            )

        return (
            f"## 任务：v6.13 第二阶段——从 {num_candidates} 张候选编辑帧中选出最佳的 {edit_name}\n\n"
            f"{V67_EDIT_PICK_OVERLAY}\n"
            f"对应基础帧：{base_desc}\n"
            f"{beat_line}{v69_line}"
            f"规划编辑强度：{edit_level}\n"
            f"规划编辑策略：{edit_strategy}\n"
            f"需要修复/增强：{fixes or '（规划未指定）'}\n"
            f"必须保持：{keep or '（基础帧的剧情状态、人物位置、道具状态、场景结构）'}\n\n"
            f"生成时使用的一段式 prompt：\n{edit_prompt}\n"
            f"\n按顺序传入的前 {num_candidates} 张图片是候选编辑帧，第 {num_candidates+1} 张是对应基础帧（用于对比）。\n\n"
            f"评选维度（**先评镜头变化，再评剧情锁定**；前 3 项为底线）：\n"
            f"1. **Camera Transformation vs {base_name}（一票否决）**："
            f"是否出现可描述的景别/角度/机位侧/构图/焦点/纵深变化？与 base 同构图仅更亮 ⇒ 淘汰；"
            f"**普通平视中景叙事 ⇒ 淘汰**（v6.9）。\n"
            f"2. **剧情可读性（v6.10 一票否决）**：能否读出当前剧情节点？只剩局部纹理/火花/指纹、无人物或场景锚点 ⇒ 淘汰。\n"
            f"3. **细节过拟合（v6.10）**：指纹/纹路/火花是否抢过叙事？多个局部细节是否权重混乱？\n"
            f"   **v6.11**：身份/道具/场景一致性仍须检查，即使生成 prompt 未逐条写出 preserve。\n"
            f"4b. **v6.13**：生成 prompt 不应含 Keep 句；评选时用规划表 keep 字段检查连续性；"
            f"若 story state 块写了 close proximity / focused on / reflection 等，"
            f"候选易手部或道具过放大、丢失脸/场景锚点 ⇒ 降权或淘汰。\n"
            f"4. **剧情状态保留（一票否决）**：人物位置、动作结果、道具状态、特效形态、光源是否与基础帧一致？\n"
            f"5. **与基础帧可追溯（一票否决）**：能否看出由 {base_name} 再编辑而来？\n"
            f"6. **人物体态完整性（一票否决）**：四肢与手指是否正常、无畸形？\n"
            f"7. **规划落实**：「{edit_level} / {edit_strategy}」是否落实；须有 camera delta，且保留 ≥2 上下文锚点。\n"
            f"8. **瑕疵修复**：「{fixes or '…'}」是否改善且不引入新错。\n"
            f"9. 主视觉焦点是否清楚、支持性细节是否克制？\n"
            f"10. 背面/OTS 是否错误露正脸？人物身份与服装是否与基础帧/case_char 一致？\n\n"
            f"{PICK_OUTPUT_FORMAT_RULE}\n"
            f"输出格式：选择第X张，原因：...（必须点明每张候选对规划镜头策略的实现情况，"
            f"以及对剧情状态/基础帧锚点的保留情况）"
        )

    async def _request_edit_prompt(
        self,
        edit_name: str,
        base_name: str,
        edit_level: str,
        edit_strategy: str,
        fixes: str,
        keep: str,
        base_path: str,
        story_function: str = "",
        planned_edit_camera: str = "",
        fallback_camera: str = "",
        plan_entry: Optional[Dict] = None,
    ) -> str:
        """让 VLM 给出 case_edit_frame0x.png 的 v6.14 双段式英文表层 prompt。"""
        self.vlm.trim_old_frame_images(keep_last_n=1)
        target_cam = fallback_camera or planned_edit_camera
        compiler_hint = ""
        if plan_entry:
            v614 = self._derive_v614_fields_from_plan_entry(plan_entry)
            compiler_hint = (
                f"\n**v6.14 Edit Prompt Compiler 参考（须融入最终一段式 prompt，不要另起标题）**：\n"
                f"- story_state: {v614['story_state']}\n"
                f"- camera_type: {v614['camera_type']}\n"
                f"- visual_focus: {v614['visual_focus']}\n"
            )
        cam_block = ""
        if target_cam:
            cam_block = (
                f"\n**v6.14 本帧目标镜头（写入 Camera transformation 第二段）**：{target_cam}\n"
                f"剧情功能：{story_function or '（按规划）'}\n"
                "禁止写成 neutral eye-level medium shot；禁止与 base 同景别同构图；"
                "禁止 Keep 清单；镜头类型与强调细节**只**写在 Camera transformation 块。\n"
            )
        if fallback_camera:
            cam_block += (
                f"\n（上一轮候选均为平淡中景/无镜头变化/细节过拟合，"
                f"本次**必须**改用备选镜头：{fallback_camera}）\n"
            )
        extra_ref_note = ""
        if self._edit_allows_extra_resource_refs(edit_strategy, fixes):
            extra_ref_note = (
                "\n**例外**：本轮规划标明身份/道具纠偏，可在 prompt 中额外引用必要资源图，"
                "且须与实际会输入模型的参考图一致。\n"
            )
        else:
            extra_ref_note = (
                f"\n**v6.14 硬规则**：本轮图像参考**仅** `{base_name}`；"
                "**禁止**在 prompt 中出现 `case_char_xx` / `case_prop_xx` / `case_scene_xx`；"
                "**禁止**输出 `Keep … present in the frame` 等显式保持句。\n"
            )
        internal_block = (
            f"\n### 规划层字段（仅供你理解，**不要**逐条抄进最终 prompt，尤其不要把「必须保持」转成 Keep 句）\n"
            f"- 需要修复/增强：{fixes or '（按规划）'}\n"
            f"- 必须保持（**仅用于候选评选**）：{keep or '（基础帧已有剧情状态）'}\n"
        )
        diversity_ledger = self._build_camera_diversity_ledger(edit_name)
        ledger_block = f"{diversity_ledger}\n\n" if diversity_ledger else ""
        request_text = (
            f"{ledger_block}"
            f"现在进入 **v6.14 第二阶段：全帧镜头再设计** 的逐帧英文 **表层** prompt 阶段。\n"
            f"{V614_DUAL_BLOCK_RULES}\n"
            f"{V613_NO_KEEP_SURFACE_RULES}\n"
            f"{V612_LAYER_SEPARATION_RULES}\n"
            f"{V611_SURFACE_EDIT_RULES}\n"
            f"{V610_BALANCED_EDIT_RULES}\n"
            f"目标编辑帧：{edit_name}\n"
            f"对应基础帧：{base_name}（附图，也是**唯一**默认图像参考）\n"
            f"规划编辑强度：{edit_level}\n"
            f"规划编辑策略：{edit_strategy}\n"
            f"{internal_block}"
            f"{cam_block}{compiler_hint}{extra_ref_note}\n"
            f"请输出 {edit_name} 的**英文一段式**生成 prompt（格式：`Generate {edit_name}: ...`）。\n"
            f"**必须**按 v6.14 §16.5 双段式模板（一段内写完，不要分标题）：\n"
            f"1) **Story-State / Screen-Content**：`Use {base_name} as the direct source for the exact story state of ...` "
            f"— 用一句自然英文交代这帧画面正在呈现什么（人物姿态、动作、道具状态、场景状态）；"
            f"**不要** Keep/Preserve 清单；**不要**写 close-up / focused on / foreground 等镜头词；\n"
            f"2) **Camera Transformation**：`Camera transformation: create a [shot type] with strong focus on [visual emphasis].` "
            f"— **所有**镜头类型与强调细节只写在这里；可选 `with the glowing lotus still visible as context`；\n"
            f"3) 结尾加风格句，如 `Cinematic Chinese fantasy 3D animation style.`\n"
            f"- 禁止 `Use case_char` / `Use case_prop`（除非上方例外）；"
            f"禁止 fingerprint / energy bridge（v6.10）；"
            f"禁止 `Keep ... present in the frame`。\n"
            f"\n**§16.4 反例（禁止）**：\n"
            f"`Keep Nezha's seated posture, face, red sash ... present in the frame.`\n"
            f"**§17.2 正例（睁眼特写）**：\n"
            f"`Generate case_edit_frame05.png: Use case_base_frame05.png as the direct source for the exact story state of Nezha lying on the glowing lotus after the Universe Ring has fitted onto his right wrist. Camera transformation: create a single-character face close-up with strong focus on Nezha's awakened eyes and calm expression. Cinematic Chinese fantasy 3D animation style.`\n"
        )
        self._sync_gitee_compact_supplements(edit_name)
        image_paths = [base_path] if base_path and os.path.exists(base_path) else None
        response = await self.vlm.chat(request_text, image_paths=image_paths)

        for pat in [
            r"(Generate\s+case_edit_frame\d+\.png\s*:.+?)(?=\n\n|\Z)",
            r"(生成\s*case_edit_frame\d+\.png[：:].+?)(?=\n\n|\Z)",
        ]:
            gen_match = re.search(pat, response, re.DOTALL | re.IGNORECASE)
            if gen_match:
                raw = self._finalize_edit_surface_prompt(
                    gen_match.group(1).strip(),
                    edit_name,
                    base_name,
                    plan_entry=plan_entry,
                    edit_strategy=edit_strategy,
                    fixes=fixes,
                )
                if self._prompt_needs_v612_refresh(raw):
                    logger.warning(
                        "VLM edit prompt content block still has camera intrusion for %s",
                        edit_name,
                    )
                if self._prompt_needs_edit_refresh(raw) and plan_entry:
                    logger.warning(
                        "VLM edit surface prompt needs refresh for %s; trying compiler",
                        edit_name,
                    )
                    v614 = self._derive_v614_fields_from_plan_entry(plan_entry)
                    raw = self._compile_edit_prompt_v614(
                        edit_name,
                        base_name,
                        v614["story_state"],
                        v614["camera_type"],
                        v614["visual_focus"],
                        v614["style"],
                    )
                self._record_edit_prompt_snapshot(
                    edit_name,
                    base_name,
                    raw,
                    status="prompt_ready",
                    edit_strategy=edit_strategy,
                    fixes=fixes,
                    extra={
                        "story_function": story_function,
                        "planned_edit_camera": planned_edit_camera or fallback_camera,
                        "edit_level": edit_level,
                    },
                )
                return raw
        return ""

    def _get_edit_prompt(self, edit_name: str) -> str:
        fp_entry = self.frame_prompts.get("frames", {}).get(edit_name, {})
        if isinstance(fp_entry, dict):
            st = fp_entry.get("status", "")
            prompt = fp_entry.get("generation_prompt", "")
            if prompt and st not in ("pending_regeneration",):
                return prompt
        edit_summary = self.summary.get("edit_frames", {})
        if edit_name in edit_summary:
            prompt = edit_summary[edit_name].get("generation_prompt", "")
            if prompt:
                return prompt
        return ""

    def _find_existing_candidates(self, candidates_dir: str) -> List[str]:
        """扫描候选目录，返回已有的候选图片路径列表（按文件名排序）。
        若同时存在首轮与 _rN_ 重抽候选，仅返回首轮，避免混选。
        """
        if not os.path.isdir(candidates_dir):
            return []
        all_files = sorted(
            f
            for f in os.listdir(candidates_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg"))
        )
        primary = [f for f in all_files if not re.search(r"_r\d+_", f)]
        use_files = primary if primary else all_files
        return [os.path.join(candidates_dir, f) for f in use_files]

    def _get_frame_prompt(self, frame_name: str, kind: str = "base") -> str:
        """尝试从 summary 或 state 中恢复某一帧的生成 prompt。
        kind 可选 'base' / 'edit' / 'shot'（旧版）。
        """
        key_map = {"base": "base_frames", "edit": "edit_frames", "shot": "shot_frames"}
        key = key_map.get(kind, "base_frames")
        frames_summary = self.summary.get(key, {})
        if frame_name in frames_summary:
            prompt = frames_summary[frame_name].get("generation_prompt", "")
            if prompt:
                return self.parser.extract_generation_block(prompt, frame_name) or prompt
        pending = self.state.get("pending_frame_prompt", "")
        return self.parser.extract_generation_block(pending, frame_name) or pending

    async def _request_frame_prompt(
        self, frame_name: str, prev_frame_name: str, kind: str = "base",
    ) -> str:
        """让 VLM 基于前一帧生成当前帧的 prompt。"""
        prev_path = self._resolve_single_path(prev_frame_name)
        if not prev_path:
            prev_path = os.path.join(self.output_dir, prev_frame_name)

        self.vlm.trim_old_frame_images(keep_last_n=3)

        if kind == "base":
            request_text = (
                f"当前进度：{prev_frame_name} 已完成（附图）。"
                f"请给出下一基础帧 {frame_name} 的英文一段式生成 prompt"
                f"（格式：Generate {frame_name}: ...）。\n"
                f"{V66_BASE_RULES}\n"
                f"{V68_REFERENCE_RESOLVER_RULES}\n"
                f"{self._format_resource_registry_brief()}\n"
                f"v6.8 硬性要求：\n"
                f"- 先判断 {frame_name} 相对 {prev_frame_name} 的 new entities 与 hero product，再列出 reference_images；\n"
                f"- camera 必须是唯一确定的 wide shot 或 medium shot；\n"
                f"- 必须显式写 Subject–Camera Relation；\n"
                f"- 新人物/新道具首次出现必须 Use 对应 case_char / case_prop（或 clean/with_prop 变体）；\n"
                f"- 场景锚点只能来自资源图或上一帧真实可见元素；\n"
                f"- 禁止 distance / closer / farther / slightly 等抽象词；prompt 短而有效，含正确 Use 文件名。"
            )
        else:
            request_text = (
                f"当前进度：{prev_frame_name} 已完成（附图）。"
                f"请给出下一帧 {frame_name} 的英文一段式生成 prompt"
                f"（格式：Generate {frame_name}: ...）。"
            )
        image_paths = [prev_path] if prev_path and os.path.exists(prev_path) else None
        response = await self.vlm.chat(request_text, image_paths=image_paths)

        kind_token_map = {
            "base": "base_frame",
            "edit": "edit_frame",
            "shot": "shot_frame",
        }
        kind_token = kind_token_map.get(kind, "frame")
        for pat in [
            rf"(Generate\s+case_{kind_token}\d+\.png\s*:.+?)(?=\n\n|\Z)",
            rf"(生成\s*case_{kind_token}\d+\.png[：:].+?)(?=\n\n|\Z)",
        ]:
            gen_match = re.search(pat, response, re.DOTALL | re.IGNORECASE)
            if gen_match:
                prompt_text = gen_match.group(1).strip()
                ref_names = self.parser.extract_reference_images(prompt_text)
                self._upsert_frame_prompt(
                    frame_name,
                    stage="base",
                    generation_prompt=prompt_text,
                    reference_images=ref_names,
                    status="prompt_ready",
                )
                return prompt_text
        return ""

    def _resolve_reference_paths(self, ref_names: List[str]) -> List[str]:
        paths = []
        for name in ref_names:
            p = self._resolve_single_path(name)
            if p:
                paths.append(p)
        return paths

    def _resolve_single_path(self, name: str) -> Optional[str]:
        if name in self.resource_files:
            return self.resource_files[name]
        if name in self.base_frame_files:
            return self.base_frame_files[name]
        if name in self.edit_frame_files:
            return self.edit_frame_files[name]
        candidate = os.path.join(self.output_dir, name)
        if os.path.exists(candidate):
            return candidate
        if self._is_resource_library_file(name):
            alias = self._resolve_resource_alias(name)
            if alias:
                path = os.path.join(self.output_dir, alias)
                if os.path.isfile(path):
                    self.resource_files[name] = path
                    return path
        return None

    def _load_frame_prompts(self) -> None:
        if not os.path.exists(self.frame_prompts_file):
            return
        try:
            with open(self.frame_prompts_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict) and isinstance(saved.get("frames"), dict):
                self.frame_prompts = saved
                self.frame_prompts.setdefault("case_name", self.case_name)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load frame_prompts.json: %s", e)

    def _backfill_frame_prompts_from_summary(self) -> None:
        """旧案例：把 summary 里已有 prompt 补进 frame_prompts（不覆盖已有条目）。"""
        frames = self.frame_prompts.setdefault("frames", {})
        for name, data in self.summary.get("base_frames", {}).items():
            if not isinstance(data, dict):
                continue
            prompt = data.get("generation_prompt", "")
            if not prompt or frames.get(name, {}).get("generation_prompt"):
                continue
            self._upsert_frame_prompt(
                name,
                stage="base",
                generation_prompt=prompt,
                reference_images=data.get("reference_images", []),
                status="picked" if data.get("chosen_candidate") else "prompt_ready",
                extra={"chosen_candidate": data.get("chosen_candidate")},
                quiet=True,
            )
        for name, data in self.summary.get("edit_frames", {}).items():
            if not isinstance(data, dict):
                continue
            prompt = data.get("generation_prompt", "")
            if not prompt or frames.get(name, {}).get("generation_prompt"):
                continue
            self._record_edit_prompt_snapshot(
                name,
                data.get("base_frame", ""),
                prompt,
                status="picked" if data.get("chosen_candidate") else "prompt_ready",
                edit_strategy=data.get("edit_strategy", ""),
                fixes=data.get("fixes", ""),
                extra={
                    "chosen_candidate": data.get("chosen_candidate"),
                    "story_function": data.get("story_function"),
                    "planned_edit_camera": data.get("planned_edit_camera"),
                },
                quiet=True,
            )

    def _save_frame_prompts(self) -> None:
        self.frame_prompts["case_name"] = self.case_name
        self.frame_prompts["updated_at"] = time.time()
        with open(self.frame_prompts_file, "w", encoding="utf-8") as f:
            json.dump(self.frame_prompts, f, ensure_ascii=False, indent=2)

    def _normalize_edit_frame_name(self, name: str) -> str:
        name = name.strip()
        if re.fullmatch(r"\d{1,2}", name):
            return f"case_edit_frame{int(name):02d}.png"
        if not name.endswith(".png"):
            if re.search(r"case_edit_frame\d+", name, re.I):
                return name if name.endswith(".png") else f"{name}.png"
            num = re.search(r"\d+", name)
            if num:
                return f"case_edit_frame{int(num.group(0)):02d}.png"
        return name

    def _normalize_base_frame_name(self, name: str) -> str:
        name = name.strip()
        if re.fullmatch(r"\d{1,2}", name):
            return f"case_base_frame{int(name):02d}.png"
        if not name.endswith(".png") and re.search(r"\d+", name):
            num = re.search(r"\d+", name)
            return f"case_base_frame{int(num.group(0)):02d}.png"
        return name

    def _upsert_frame_prompt(
        self,
        frame_file: str,
        *,
        stage: str,
        generation_prompt: str = "",
        reference_images: Optional[List[str]] = None,
        status: str = "prompt_ready",
        base_frame: str = "",
        extra: Optional[Dict[str, Any]] = None,
        quiet: bool = False,
    ) -> None:
        """VLM 产出 prompt 后立即写入 frame_prompts.json，便于生成完成前查看。"""
        ref_names = list(reference_images or [])
        ref_paths = [
            p for p in self._resolve_reference_paths(ref_names) if p
        ]
        entry: Dict[str, Any] = {
            "stage": stage,
            "file": frame_file,
            "status": status,
            "generation_prompt": generation_prompt,
            "reference_images": ref_names,
            "reference_paths": ref_paths,
            "updated_at": time.time(),
        }
        if base_frame:
            entry["base_frame"] = base_frame
        if extra:
            entry.update(extra)
        self.frame_prompts.setdefault("frames", {})[frame_file] = entry
        self._save_frame_prompts()
        if not quiet:
            print(f"  [frame_prompts] 已保存 {frame_file} → {self.frame_prompts_file}")

    def _record_edit_prompt_snapshot(
        self,
        edit_name: str,
        base_name: str,
        edit_prompt: str,
        *,
        status: str = "prompt_ready",
        edit_strategy: str = "",
        fixes: str = "",
        extra: Optional[Dict[str, Any]] = None,
        quiet: bool = False,
    ) -> None:
        if not edit_prompt:
            return
        _, ref_names = self._edit_refs_for_generation(
            base_name,
            self._resolve_single_path(base_name) or "",
            self.parser.extract_reference_images(edit_prompt),
            edit_strategy=edit_strategy,
            fixes=fixes,
        )
        meta = dict(extra or {})
        self._upsert_frame_prompt(
            edit_name,
            stage="edit",
            generation_prompt=edit_prompt,
            reference_images=ref_names,
            status=status,
            base_frame=base_name,
            extra=meta,
            quiet=quiet,
        )

    def prepare_edit_frame_regeneration(self, edit_names: Iterable[str]) -> List[str]:
        """清除指定编辑帧的成片/状态/缓存 prompt，便于按 v6.14 重新要 prompt 并抽卡。"""
        normalized: List[str] = []
        for raw in edit_names:
            edit_name = self._normalize_edit_frame_name(raw)
            normalized.append(edit_name)
            final_path = os.path.join(self.output_dir, edit_name)
            if os.path.isfile(final_path):
                try:
                    os.remove(final_path)
                    print(f"  [regen] 已删除成片 {edit_name}")
                except OSError as e:
                    logger.warning("Cannot remove %s: %s", final_path, e)
            final_name = edit_name.replace("case_edit_frame", "case_final_frame")
            final_assembly_path = os.path.join(self.output_dir, final_name)
            if os.path.isfile(final_assembly_path):
                try:
                    os.remove(final_assembly_path)
                    print(f"  [regen] 已删除最终帧 {final_name}")
                except OSError as e:
                    logger.warning("Cannot remove %s: %s", final_assembly_path, e)
            self.summary.get("final_frames", {}).pop(final_name, None)
            done = self.state.setdefault("edit_frames_done", [])
            if edit_name in done:
                done.remove(edit_name)
            self.edit_frame_files.pop(edit_name, None)
            ef = self.summary.setdefault("edit_frames", {}).get(edit_name)
            if isinstance(ef, dict):
                ef.pop("generation_prompt", None)
                ef.pop("chosen_candidate", None)
                ef.pop("pick_reason", None)
                ef["status"] = "pending_regeneration"
            fp = self.frame_prompts.setdefault("frames", {}).get(edit_name)
            if isinstance(fp, dict):
                fp["status"] = "pending_regeneration"
                fp.pop("generation_prompt", None)
            else:
                self.frame_prompts.setdefault("frames", {})[edit_name] = {
                    "stage": "edit",
                    "file": edit_name,
                    "status": "pending_regeneration",
                }

            prefix = edit_name.replace(".png", "")
            candidates_dir = os.path.join(self.output_dir, f"{prefix}_candidates")
            if os.path.isdir(candidates_dir):
                archive_dir = f"{candidates_dir}_old"
                if os.path.isdir(archive_dir):
                    shutil.rmtree(archive_dir)
                os.rename(candidates_dir, archive_dir)
                print(
                    f"  [regen] 已归档旧候选目录 → {os.path.basename(archive_dir)}"
                )

        if normalized:
            step = self.state.get("step", "")
            if step in ("final_assembly", "done", "ltx_plan", "ltx_generate"):
                self.state["step"] = "edit_frames"
            elif step not in ("edit_frames", "edit_plan"):
                self.state["step"] = "edit_frames"
            self._save_frame_prompts()
            self._save_summary()
            self._save_state()
            print(
                f"  [regen] 已标记重生成: {', '.join(normalized)}；"
                f"续跑后将重新向 VLM 要 prompt 并写入 {self.frame_prompts_file}"
            )
        return normalized

    def _log(self, event_type: str, data: Dict):
        """追加一条日志到 workflow_log.jsonl（每行一条 JSON，不覆盖历史）。"""
        entry = {
            "timestamp": time.time(),
            "event": event_type,
            "data": data,
        }
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Failed to write log: %s", e)

    def _save_summary(self):
        """保存结构化的全流程总结，供人阅读。"""
        self._sanitize_summary_prompts()
        try:
            with open(self.summary_file, "w", encoding="utf-8") as f:
                json.dump(self.summary, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to write summary: %s", e)

    def _sanitize_summary_prompts(self):
        """Keep generation_prompt fields as pure image prompts, not planning reports."""
        for section in ("base_frames", "edit_frames"):
            frames = self.summary.get(section, {})
            if not isinstance(frames, dict):
                continue
            for frame_name, data in frames.items():
                if not isinstance(data, dict):
                    continue
                prompt = data.get("generation_prompt", "")
                prompt_block = self.parser.extract_generation_block(prompt, frame_name)
                if prompt_block:
                    data["generation_prompt"] = prompt_block
                else:
                    data["generation_prompt"] = self.parser.sanitize_prompt_text(
                        data.get("generation_prompt", "")
                    )
                data["reference_images"] = self.parser.extract_reference_images(
                    data.get("generation_prompt", "")
                )
        for rname, rdata in self.summary.get("resources", {}).items():
            if not isinstance(rdata, dict):
                continue
            gp = rdata.get("generation_prompt", "")
            if gp:
                rdata["generation_prompt"] = self.parser.sanitize_prompt_text(gp)

    def _save_state(self):
        serializable_state = {}
        for k, v in self.state.items():
            serializable_state[k] = v

        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(serializable_state, f, ensure_ascii=False, indent=2)
        logger.debug("State saved: step=%s", self.state["step"])

    def _load_state(self):
        if os.path.exists(self.summary_file):
            with open(self.summary_file, "r", encoding="utf-8") as f:
                saved_summary = json.load(f)
            # v6.0/v6.2 → v6.3 字段迁移
            if "shot_plan" in saved_summary and "edit_plan" not in saved_summary:
                saved_summary["edit_plan"] = saved_summary.pop("shot_plan")
            if "shot_frames" in saved_summary and "edit_frames" not in saved_summary:
                saved_summary["edit_frames"] = saved_summary.pop("shot_frames")
            self.summary.update(saved_summary)
            self._sanitize_summary_prompts()

        self._load_frame_prompts()
        self._backfill_frame_prompts_from_summary()

        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)

            # 旧版字段名兼容
            if "frames_done" in saved and "base_frames_done" not in saved:
                saved["base_frames_done"] = saved["frames_done"]
            if "shot_frames_done" in saved and "edit_frames_done" not in saved:
                saved["edit_frames_done"] = saved["shot_frames_done"]
            if "shot_plan" in saved and "edit_plan" not in saved:
                saved["edit_plan"] = saved["shot_plan"]
            if "shot_plan_response" in saved and "edit_plan_response" not in saved:
                saved["edit_plan_response"] = saved["shot_plan_response"]
            # 旧 step 名 → 新 step 名（"frames" 为历史 bug：从未被 run() 识别，会导致假完成）
            old_to_new_step = {
                "shot_plan": "edit_plan",
                "shot_frames": "edit_frames",
                "frames": "resource_review",
            }
            if saved.get("step") in old_to_new_step:
                saved["step"] = old_to_new_step[saved["step"]]
            self.state.update(saved)
            if self.state.get("resource_mode") in RESOURCE_MODES:
                self.resource_mode = self.state["resource_mode"]

            # 从 init 缓存补全 resource_prompts（解析器升级后可识别当时未识别的格式）
            init_resp = self.state.get("init_plan_response", "")
            if init_resp and not self.state.get("resource_prompts"):
                recalc = self.parser.parse_init_plan(init_resp)
                if recalc.resource_prompts:
                    self.state["resource_prompts"] = dict(recalc.resource_prompts)
                    self._save_state()
                    logger.info(
                        "从 init_plan_response 补全 resource_prompts（%d 项）",
                        len(recalc.resource_prompts),
                    )
                    print(
                        f"  已从历史规划补全资源库 prompt {len(recalc.resource_prompts)} 项: "
                        f"{', '.join(sorted(recalc.resource_prompts.keys()))}"
                    )

            # 已跑到后续步但磁盘上无任何资源图：回到 resources 生成
            disk_res = self._scan_resource_files_on_disk()
            if (
                self.state.get("resource_prompts")
                and not self.state.get("resources_done")
                and not disk_res
                and self.state.get("step") in (
                    "resource_review", "base_frames", "edit_plan", "edit_frames", "final_assembly",
                )
            ):
                self.state["step"] = "resources"
                logger.info(
                    "resources_done 为空且无磁盘资源，已将 step 设为 resources"
                )

            pending = self.state.get("pending_frame_prompt", "")
            if pending:
                cleaned = self.parser.extract_generation_block(
                    pending, "case_base_frame01.png"
                ) or self.parser.sanitize_prompt_text(pending)
                if cleaned != pending:
                    self.state["pending_frame_prompt"] = cleaned

            logger.info("Resumed from state: step=%s", self.state["step"])
            print(f"  从断点恢复: step={self.state['step']}, "
                  f"base_frames_done={self.state.get('base_frames_done', [])}, "
                  f"edit_frames_done={self.state.get('edit_frames_done', [])}")

            for name in self.state.get("resources_done", []):
                path = os.path.join(self.output_dir, name)
                if os.path.exists(path):
                    self.resource_files[name] = path

            for name, path in self._scan_resource_files_on_disk().items():
                self.resource_files[name] = path
                if name not in self.state.get("resources_done", []):
                    self.state["resources_done"].append(name)
            self._sync_resources_summary_from_disk()

            for name in self.state.get("base_frames_done", []):
                path = os.path.join(self.output_dir, name)
                if os.path.exists(path):
                    self.base_frame_files[name] = path

            for name in self.state.get("edit_frames_done", []):
                path = os.path.join(self.output_dir, name)
                if os.path.exists(path):
                    self.edit_frame_files[name] = path

            if self.state.get("pipeline_mode") in ("video", "full"):
                if self._reset_ltx_on_load:
                    self.ltx_ext.reset_ltx_phase()
                else:
                    self.ltx_ext.sync_step_from_disk()
                    if os.path.exists(self.ltx_ext.plan_file) or os.path.exists(
                        self.ltx_ext.summary_file
                    ):
                        self.ltx_ext.sync_ltx_summary_from_disk()

            recoverable_steps = (
                "resources", "resource_review", "base_frames",
                "edit_plan", "edit_frames", "final_assembly",
                "ltx_plan", "ltx_generate",
            )
            if self.state["step"] in recoverable_steps:
                workflow_doc_path = WORKFLOW_DOC_PATH
                if os.path.exists(workflow_doc_path):
                    with open(workflow_doc_path, "r", encoding="utf-8") as f:
                        self.vlm.set_system_prompt(f.read())

                init_resp = self.state.get("init_plan_response", "")
                if init_resp:
                    self.vlm.add_user_message("[恢复上下文] 之前的初始化规划请求")
                    self.vlm.add_assistant_message(init_resp)

                if self.state["step"] in (
                    "resource_review", "base_frames",
                    "edit_plan", "edit_frames", "final_assembly",
                ) and self.resource_files:
                    self._register_resources_in_conversation()

                if self.state["step"] in ("ltx_plan", "ltx_generate"):
                    ltx_resp = self.state.get("ltx_plan_response", "")
                    if ltx_resp:
                        self.vlm.add_user_message("[恢复上下文] LTX Shot 规划")
                        self.vlm.add_assistant_message(ltx_resp)
