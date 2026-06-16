"""
ResponseParser: 从 Qwen3-VL 的自然语言响应中结构化提取各类信息。
包括：资源库 prompt、分镜规划、帧生成 prompt、图片选择结果、质检结果等。
"""

import re
import json
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 与 orchestrator._RESOURCE_FILENAME_RE 对齐（允许 case_char_01_clean.png 等后缀）
_RESOURCE_FILE_RE = r"case_(?:char|scene|prop|style)_\d+(?:_[a-z0-9_]+)*\.png"


@dataclass
class InitPlanResult:
    overview: str = ""
    characters: List[str] = field(default_factory=list)
    scenes: List[str] = field(default_factory=list)
    resource_prompts: Dict[str, str] = field(default_factory=dict)
    storyboard_plan: str = ""
    first_frame_prompt: str = ""
    raw_response: str = ""


@dataclass
class FrameReviewResult:
    passed: bool = False
    issues: str = ""
    next_frame_prompt: str = ""
    regen_prompt: str = ""
    raw_response: str = ""


@dataclass
class ImagePickResult:
    chosen_index: int = -1
    all_rejected: bool = False
    reason: str = ""
    suggestion: str = ""
    raw_response: str = ""


@dataclass
class ShotPlanEntry:
    """v6.0/v6.2 第二阶段镜头变换计划中的一行（已被 v6.3 EditPlanEntry 取代，保留用于向后兼容）。"""
    base_frame: str = ""           # e.g. case_base_frame03.png
    transform: bool = False
    transform_type: str = ""       # e.g. close-up / insert shot / overhead view ...
    reason: str = ""
    final_frame: str = ""          # 最终统一命名（case_final_frameXX.png）
    shot_frame: str = ""           # 派生帧文件名（case_shot_frameXX.png），仅 transform=True 时存在


@dataclass
class ShotPlanResult:
    entries: List[ShotPlanEntry] = field(default_factory=list)
    raw_response: str = ""


@dataclass
class EditPlanEntry:
    """v6.3/v6.4/v6.9/v6.10/v6.11/v6.12/v6.13 第二阶段全帧再编辑计划中的一行。"""
    base_frame: str = ""           # 对应基础帧 case_base_frame0x.png
    edit_frame: str = ""           # 对应编辑帧 case_edit_frame0x.png
    final_frame: str = ""          # 最终帧 case_final_frame0x.png
    beat_mapping: str = ""         # v6.4 新增：对应 9 beat（如 "B1" / "B3+B4" / "B5+B6+B7"），N=9 时为 B1~B9
    story_function: str = ""       # v6.9：剧情功能（Establish / Awakening / …）
    planned_edit_camera: str = ""  # v6.9：目标特殊 edit 镜头
    diagnosis: str = ""            # 基础帧诊断
    edit_level: str = ""           # Level 1 / Level 2 / Level 3 / Level 4
    edit_strategy: str = ""        # cleanup / cinematic polish / moderate reframe / strong cinematic transformation 等
    fixes: str = ""                # 需要修复/增强的内容
    keep: str = ""                 # 必须保持不变的内容


@dataclass
class EditPlanResult:
    entries: List[EditPlanEntry] = field(default_factory=list)
    raw_response: str = ""


class ResponseParser:

    @staticmethod
    def _strip_markdown_fence(text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:\w+)?\n", "", text)
            text = re.sub(r"\n```\s*$", "", text)
        return text

    @staticmethod
    def _register_resource_prompt(
        result: InitPlanResult, name: str, body: str,
    ) -> None:
        body = body.strip()
        if not body:
            return
        if not re.match(
            r"^(?:Generate|Regenerate|生成|重生成)\s+case_",
            body,
            re.IGNORECASE,
        ):
            block = ResponseParser.extract_generation_block(body, name)
            body = block if block else f"Generate {name}: {body}"
        if name not in result.resource_prompts:
            result.resource_prompts[name] = body
            if "char" in name:
                result.characters.append(name)
            elif "scene" in name:
                result.scenes.append(name)

    @staticmethod
    def parse_init_plan(response: str) -> InitPlanResult:
        response = ResponseParser._strip_markdown_fence(response)
        result = InitPlanResult(raw_response=response)

        overview_match = re.search(
            r"##\s*视频概述\s*\n(.*?)(?=\n##\s|\Z)", response, re.DOTALL
        )
        if overview_match:
            result.overview = overview_match.group(1).strip()

        _case_heading = rf"({_RESOURCE_FILE_RE})(?:\s*\([^)]*\))?"

        for m in re.finditer(
            rf"###\s*{_case_heading}\s*\n(.*?)(?=\n###\s|\n##\s|\Z)",
            response,
            re.DOTALL | re.IGNORECASE,
        ):
            ResponseParser._register_resource_prompt(
                result, m.group(1), m.group(2),
            )

        # 兼容 #### 人物资源生成 prompt（case_char_01_clean.png）
        for m in re.finditer(
            rf"####[^\n]*[（(]{_case_heading}[）)]\s*\n+(.*?)(?=\n####\s|\n###\s|\n##\s|\Z)",
            response,
            re.DOTALL | re.IGNORECASE,
        ):
            ResponseParser._register_resource_prompt(
                result, m.group(1), m.group(2),
            )

        # 兼容 **case_char_01.png** 或 **case_char_01.png (别名)** + 引用块 `> ...`（可无 Generate 前缀）
        for m in re.finditer(
            rf"\*\*{_case_heading}\*\*\s*\n>\s*(.+?)(?=\n\n\*\*case_|\n##\s|\Z)",
            response,
            re.DOTALL | re.IGNORECASE,
        ):
            ResponseParser._register_resource_prompt(
                result, m.group(1), m.group(2),
            )

        # 兜底：扫描所有 Generate case_char/scene/prop 行（VLM 常用此格式）
        for m in re.finditer(
            rf"(Generate\s+({_RESOURCE_FILE_RE})\s*:.+?)"
            r"(?=\n\n|Generate\s+case_|####\s|###\s|##\s|\Z)",
            response,
            re.DOTALL | re.IGNORECASE,
        ):
            ResponseParser._register_resource_prompt(
                result, m.group(2), m.group(1),
            )

        plan_match = re.search(
            r"##\s*(?:第一阶段[：:]?\s*)?(?:基础)?(?:剧情)?(?:\d+)?(?:宫格)?(?:帧)?分镜?规划\s*\n(.*?)(?=\n##\s|\Z)",
            response, re.DOTALL,
        )
        if plan_match:
            result.storyboard_plan = plan_match.group(1).strip()

        prompt_match = re.search(
            r"##\s*(?:当前(?:基础)?帧生成|当前基础帧)\s*[Pp]rompt\s*\n(.*?)(?=\n##\s|\Z)",
            response, re.DOTALL,
        )
        if prompt_match:
            result.first_frame_prompt = prompt_match.group(1).strip()

        if not result.first_frame_prompt:
            for pat in [
                r"(Generate\s+case_base_frame01\.png\s*:.+?)(?=\n\n|\Z)",
                r"(生成\s*case_base_frame01\.png[：:].+?)(?=\n\n|\Z)",
                r"(Generate\s+case_frame01\.png\s*:.+?)(?=\n\n|\Z)",
                r"(生成\s*case_frame01\.png[：:].+?)(?=\n\n|\Z)",
            ]:
                gen_match = re.search(pat, response, re.DOTALL | re.IGNORECASE)
                if gen_match:
                    result.first_frame_prompt = gen_match.group(1).strip()
                    break

        return result

    @staticmethod
    def _parse_chosen_index(response: str, num_candidates: int) -> int:
        """从 VLM 回复中解析选中的候选序号（1-based），未识别则返回 -1。"""
        json_match = re.search(r'\{[^}]*"chosen"\s*:\s*(\d+)[^}]*\}', response)
        if json_match:
            idx = int(json_match.group(1))
            if 1 <= idx <= num_candidates:
                return idx

        # 优先匹配明确结论句，避免被文中「第 N 张……不合格」干扰
        priority_patterns = [
            r"(?:因此|综上|结论|最终)[：:，,]?\s*(?:\*\*)?(?:选择|推荐|挑选|采用)\s*第\s*(\d+)\s*张",
            r"(?:\*\*)?(?:选择|推荐|挑选|采用)\s*第\s*(\d+)\s*张[，,]?\s*原因",
            r"(?:\*\*)?(?:选择|推荐|挑选|采用)\s*第\s*(\d+)\s*张",
            r"(?:最佳|最优|最合适)(?:的)?(?:是)?\s*第\s*(\d+)\s*张",
            r"第\s*(\d+)\s*张.{0,100}?(?:唯一|最佳|最合适|入选|当选|完全合规|完全满足|推荐使用)",
            r"第\s*(\d+)\s*张候选.{0,300}?(?:完全合规|是唯一满足|是唯一.{0,40}?(?:合格|候选))",
        ]
        for pat in priority_patterns:
            m = re.search(pat, response, re.IGNORECASE | re.DOTALL)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= num_candidates:
                    return idx

        patterns = [
            r"(?:candidate|image|图片?)\s*[_#]?\s*(\d+)\s*(?:最|是|为)",
            r"(\d+)\s*(?:号|张|幅).*?(?:最合适|最好|最佳|推荐)",
        ]
        for pat in patterns:
            m = re.search(pat, response, re.IGNORECASE)
            if m:
                idx = int(m.group(1))
                if 1 <= idx <= num_candidates:
                    return idx

        return -1

    @staticmethod
    def _salvage_winner_index(response: str, num_candidates: int) -> int:
        """从「逐张点评、其余不合格」式长文中抢救出实际胜出的候选序号。"""
        if num_candidates <= 0:
            return -1
        scores = [0] * (num_candidates + 1)
        open_m = re.match(
            r"^\*{0,2}第\s*(\d+)\s*张候选.{0,500}?"
            r"(?:完全合规|是唯一满足|是唯一.{0,60}?(?:合格|候选))",
            response.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if open_m:
            n0 = int(open_m.group(1))
            if 1 <= n0 <= num_candidates:
                scores[n0] += 6
        for n in range(1, num_candidates + 1):
            if re.search(
                rf"(?:选择|推荐|挑选|采用)\s*第\s*{n}\s*张",
                response,
                re.IGNORECASE,
            ):
                scores[n] += 5
            if re.search(
                rf"第\s*{n}\s*张.{0,160}?"
                r"(?:唯一|最佳|最合适|入选|当选|完全合规|完全满足|推荐使用|✅)",
                response,
                re.IGNORECASE | re.DOTALL,
            ):
                scores[n] += 3
            if re.search(
                rf"第\s*{n}\s*张.{0,220}?(?:淘汰|→\s*\*\*淘汰\*\*)",
                response,
                re.IGNORECASE | re.DOTALL,
            ):
                scores[n] -= 3
            if re.search(
                rf"第\s*{n}\s*张.{0,120}?❌",
                response,
                re.IGNORECASE | re.DOTALL,
            ):
                scores[n] -= 1
        best = max(range(1, num_candidates + 1), key=lambda i: scores[i])
        if scores[best] >= 3:
            return best
        return -1

    @staticmethod
    def _is_explicit_batch_rejection(response: str) -> bool:
        """仅当 VLM 明确声明「整批候选均不可用」时为 True（非逐张点评里的不合格）。"""
        text = response.strip()
        if not text:
            return False
        head = text[:500]
        if re.search(
            r"(?m)^(?:\*\*)?(?:全部|所有)\s*候选?\s*(?:均|都)?\s*不合格",
            head,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"(?m)^(?:\*\*)?全部不合格\s*[，,:]",
            head,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"(?m)^没有(?:任何)?(?:一张)?(?:合格|可用|符合)(?:的)?(?:候选|图片|图)",
            head,
            re.IGNORECASE,
        ):
            return True
        if re.search(
            r"(?m)^(?:五|四|三|两|\d+)\s*张候选?\s*(?:均|都)\s*不合格\s*$",
            head,
            re.IGNORECASE,
        ):
            return True
        return False

    @staticmethod
    def parse_image_pick(response: str, num_candidates: int) -> ImagePickResult:
        """从 VLM 回复中解析它选择了第几张图，或者判定全部不合格。"""
        result = ImagePickResult(raw_response=response)

        chosen = ResponseParser._parse_chosen_index(response, num_candidates)
        if chosen <= 0:
            chosen = ResponseParser._salvage_winner_index(response, num_candidates)
            if chosen > 0:
                logger.info(
                    "Image pick: salvaged winner candidate %d from comparative review",
                    chosen,
                )

        if chosen > 0:
            result.chosen_index = chosen
            if ResponseParser._is_explicit_batch_rejection(response):
                logger.warning(
                    "Pick response contains batch-reject wording but candidate %d "
                    "was selected; treating as partial reject, not full regen",
                    chosen,
                )
            return result

        if ResponseParser._is_explicit_batch_rejection(response):
            result.all_rejected = True
            reason_match = re.search(
                r"原因[：:]\s*(.+?)(?:[，,]建议|$)", response, re.DOTALL
            )
            if reason_match:
                result.reason = reason_match.group(1).strip()
            suggestion_match = re.search(
                r"建议[：:]\s*(.+?)$", response, re.DOTALL
            )
            if suggestion_match:
                result.suggestion = suggestion_match.group(1).strip()
            if not result.reason:
                result.reason = response.strip()[:500]
            return result

        logger.warning(
            "Could not parse image pick from response, defaulting to candidate 1"
        )
        result.chosen_index = 1
        return result

    @staticmethod
    def parse_frame_review(response: str, frame_kind: str = "base") -> FrameReviewResult:
        """质检结果解析。
        frame_kind: "base" / "edit" / "shot"，决定要识别的下一帧 prompt 命名前缀。
        """
        result = FrameReviewResult(raw_response=response)

        kind_token_map = {
            "base": "base_frame",
            "edit": "edit_frame",
            "shot": "shot_frame",
        }
        kind_token = kind_token_map.get(frame_kind, "base_frame")
        next_prompt_patterns = [
            rf"(Generate\s+case_{kind_token}\d+\.png\s*:.+?)(?=\n\n|\Z)",
            rf"(生成\s*case_{kind_token}\d+\.png[：:].+?)(?=\n\n|\Z)",
        ]
        regen_prompt_patterns = [
            rf"(Regenerate\s+case_{kind_token}\d+\.png\s*:.+?)(?=\n\n|\Z)",
            rf"((?:重生成)\s*case_{kind_token}\d+\.png[：:].+?)(?=\n\n|\Z)",
        ]

        for pat in next_prompt_patterns:
            gen_match = re.search(pat, response, re.DOTALL | re.IGNORECASE)
            if gen_match:
                result.next_frame_prompt = gen_match.group(1).strip()
                break

        for pat in regen_prompt_patterns:
            regen_match = re.search(pat, response, re.DOTALL | re.IGNORECASE)
            if regen_match:
                result.regen_prompt = regen_match.group(1).strip()
                break

        # Gitee 等弱模型常：标题写「未通过」、正文/结论写「均通过」——结论优先
        strong_pass = [
            r"结论[^。\n]{0,40}(?:均通过|全部通过|全部合格)",
            r"所有质检项均通过",
            r"(?:第一阶段)?质检结果[：:]\s*通过",
            r"case_(?:base_|edit_|shot_)?frame\d+\.png\s*质检结果[：:]\s*通过",
            r"当前(?:基础|编辑|派生)?帧通过",
            r"(?:基础|编辑|派生)帧通过",
            r"质检通过",
            r"(?:合格|通过)\s*[。.\s]*\n+\s*(?:---|\*\*Next Frame)",
            r"\*\*Next Frame:\s*case_",
            r"\*\*下一步\*\*[：:]\s*生成\s*`?case_",
        ]
        strong_fail = [
            r"(?:第一阶段)?质检结果[：:]\s*未通过",
            r"需要重生成",
            r"[Rr]egenerate\s+case_(?:base_|edit_|shot_)?frame",
            r"不合格",
        ]

        passed = False
        for pat in strong_pass:
            if re.search(pat, response, re.IGNORECASE | re.DOTALL):
                passed = True
                break

        if result.next_frame_prompt and not result.regen_prompt:
            passed = True

        if passed:
            for pat in strong_fail:
                if re.search(pat, response, re.IGNORECASE):
                    # 标题「未通过」但结论已判定通过时，以结论为准
                    if re.search(
                        r"结论[^。\n]{0,40}(?:均通过|全部通过)|所有质检项均通过",
                        response,
                    ):
                        break
                    passed = False
                    break

        if not passed and not result.regen_prompt:
            for pat in strong_fail:
                if re.search(pat, response, re.IGNORECASE):
                    passed = False
                    break
            else:
                if re.search(r"问题是", response):
                    passed = False

        result.passed = passed

        if not result.passed:
            for pat in (
                r"问题是[：:]?\s*(.+?)(?:\n|。)",
                r"问题说明[：:]?\s*\n+(.+?)(?:\n\n|\*\*结论\*\*)",
                r"未通过[^。\n]*[：:]\s*(.+?)(?:\n\n|\Z)",
            ):
                issue_match = re.search(pat, response, re.DOTALL)
                if issue_match:
                    text = issue_match.group(1).strip()
                    if text and not re.match(
                        r"[\s\d.*\-]*人物体态", text[:30]
                    ):
                        result.issues = text[:500]
                        break
                    if text:
                        result.issues = "见 VLM 回复（标题与正文不一致）"
                        break

        return result

    @staticmethod
    def extract_reference_images(prompt: str) -> List[str]:
        """从一段式 prompt 中提取所有引用的参考图文件名。"""
        prompt = ResponseParser.extract_generation_block(prompt) or prompt
        pattern = r"(case_(?:char|scene|prop|base_frame|edit_frame|shot_frame|final_frame|frame)\w*\.png)"
        matches = re.findall(pattern, prompt)
        seen = set()
        unique = []
        for m in matches:
            if m not in seen:
                seen.add(m)
                unique.append(m)

        gen_match = re.match(
            r"(?:Generate|Regenerate|生成|重生成)\s+(case_(?:base_frame|edit_frame|shot_frame|final_frame|frame)\d+\.png)",
            prompt, re.IGNORECASE,
        )
        if gen_match:
            target = gen_match.group(1)
            unique = [f for f in unique if f != target]

        return unique

    @staticmethod
    def sanitize_prompt_text(text: str) -> str:
        """去掉 VLM 残留的 Markdown 代码围栏，避免传入图像 API。"""
        if not text:
            return ""
        text = text.strip()
        text = re.sub(r"\n?```\s*$", "", text)
        text = re.sub(r"^```(?:text|json)?\s*\n?", "", text, flags=re.IGNORECASE)
        return text.strip()

    @staticmethod
    def extract_generation_prompt(prompt_text: str) -> str:
        """从带前缀的 prompt 中提取纯生成 prompt。
        去掉 'Generate/Regenerate case_XXX.png:' 或中文等价前缀，
        返回剩余部分作为图像生成 prompt。
        支持 case_base_frame、case_edit_frame、case_shot_frame、case_final_frame、
        case_frame、case_char、case_scene、case_prop 前缀。
        """
        prompt_text = ResponseParser.extract_generation_block(prompt_text) or prompt_text
        text = re.sub(
            r"^(?:Generate|Regenerate|生成|重生成)\s+case_(?:base_frame|edit_frame|shot_frame|final_frame|frame|char|scene|prop)\w*\.png\s*[：:]\s*",
            "", prompt_text, flags=re.IGNORECASE,
        )
        return ResponseParser.sanitize_prompt_text(text)

    @staticmethod
    def parse_resource_cards(response: str) -> List[Dict]:
        """从 VLM 回复中解析 v6.8 资源卡片（JSON 数组或逐条对象）。"""
        if not response:
            return []
        cards: List[Dict] = []
        for block in re.finditer(
            r"```(?:json)?\s*(\[.*?\])\s*```", response, re.DOTALL | re.IGNORECASE
        ):
            try:
                data = json.loads(block.group(1))
                if isinstance(data, list) and data:
                    return [c for c in data if isinstance(c, dict)]
            except json.JSONDecodeError:
                continue
        for block in re.finditer(
            r"```(?:json)?\s*(\{.*?\})\s*```", response, re.DOTALL | re.IGNORECASE
        ):
            try:
                data = json.loads(block.group(1))
                if isinstance(data, dict) and data.get("file"):
                    cards.append(data)
            except json.JSONDecodeError:
                continue
        if cards:
            return cards
        for mobj in re.finditer(
            r'\{\s*"file"\s*:\s*"(case_(?:char|scene|prop|style)[^"]+\.png)"[^}]*\}',
            response,
            re.DOTALL,
        ):
            try:
                cards.append(json.loads(mobj.group(0)))
            except json.JSONDecodeError:
                continue
        return cards

    @staticmethod
    def extract_generation_block(text: str, target_name: Optional[str] = None) -> str:
        """Return only the image-generation prompt block from a mixed planning response."""
        if not text:
            return ""
        target = re.escape(target_name) if target_name else r"case_(?:base_frame|edit_frame|shot_frame|final_frame|frame|char|scene|prop)\w*\.png"
        patterns = [
            rf"(Generate\s+{target}\s*:.+?)(?=\n\s*\n|\n##\s|\n###\s|\Z)",
            rf"(Regenerate\s+{target}\s*:.+?)(?=\n\s*\n|\n##\s|\n###\s|\Z)",
            rf"(鐢熸垚\s*{target}[锛:：].+?)(?=\n\s*\n|\n##\s|\n###\s|\Z)",
            rf"(閲嶇敓鎴?\s*{target}[锛:：].+?)(?=\n\s*\n|\n##\s|\n###\s|\Z)",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.DOTALL | re.IGNORECASE)
            if match:
                return ResponseParser.sanitize_prompt_text(match.group(1))
        return ""

    @staticmethod
    def looks_like_image_generation_prompt(text: str, filename: str) -> bool:
        """是否为可直接替换 current_prompt 的一段式 Generate 头（含正确文件名）。"""
        if not text or not filename:
            return False
        esc = re.escape(filename)
        return bool(
            re.match(
                rf"^\s*(?:Generate|Regenerate|生成|重生成)\s+{esc}\s*[：:]",
                text,
                re.IGNORECASE | re.DOTALL,
            )
        )

    @staticmethod
    def extract_generate_line_for_target(text: str, filename: str) -> str:
        """从评选回复全文里提取 `Generate <filename>: …` 一段（避免把中文「建议」整段当作文生图 prompt）。"""
        if not text or not filename:
            return ""
        esc = re.escape(filename)
        starter = re.compile(
            rf"(?:Generate|Regenerate|生成|重生成)\s+{esc}\s*[：:]",
            re.IGNORECASE,
        )
        positions = [m.start() for m in starter.finditer(text)]
        if not positions:
            return ""
        best = ""
        for i, start in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(text)
            chunk = text[start:end].strip()
            lines_out: List[str] = []
            for ln in chunk.splitlines():
                stripped = ln.strip()
                if re.match(r"^(?:原因|建议|选择|备注)[：:]", stripped):
                    break
                lines_out.append(ln)
            chunk = "\n".join(lines_out).strip()
            if len(chunk) > len(best):
                best = chunk
        return best

    # ──────────────────────────────────────────────────────────
    # v6.3 第二阶段：全帧再编辑计划解析
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def parse_edit_plan(response: str) -> EditPlanResult:
        """解析 VLM 给出的全帧再编辑规划表，兼容 v6.9（9 列）、v6.4（7 列）、v6.3（6 列）。

        v6.9 列顺序：
        | 基础帧 | 对应 beat | 剧情功能 | 目标 edit 镜头 | 基础帧诊断 | 编辑强度 | 编辑策略 | 需要修复/增强 | 必须保持 |

        v6.4 §9.3 列顺序：
        | 基础帧 | 对应 beat | 基础帧诊断 | 编辑强度 | 编辑策略 | 需要修复/增强 | 必须保持 |
        """
        result = EditPlanResult(raw_response=response)

        row_pattern_9 = re.compile(
            r"^\s*\|\s*(case_base_frame\d+\.png)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|",
            re.MULTILINE,
        )
        row_pattern_7 = re.compile(
            r"^\s*\|\s*(case_base_frame\d+\.png)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|",
            re.MULTILINE,
        )
        row_pattern_6 = re.compile(
            r"^\s*\|\s*(case_base_frame\d+\.png)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|"
            r"\s*([^|\n]*?)\s*\|",
            re.MULTILINE,
        )

        # 用第一行匹配数量较多的那种列布局作为正式格式
        matches_9 = list(row_pattern_9.finditer(response))
        matches_7 = list(row_pattern_7.finditer(response))
        matches_6 = list(row_pattern_6.finditer(response))

        seen = set()

        if len(matches_9) >= max(len(matches_7), len(matches_6), 1) and matches_9:
            for m in matches_9:
                base = m.group(1).strip()
                if base in seen:
                    continue
                seen.add(base)
                result.entries.append(ResponseParser._build_edit_entry(
                    base,
                    m.group(2).strip(),
                    m.group(5).strip(),
                    m.group(6).strip(),
                    m.group(7).strip(),
                    m.group(8).strip(),
                    m.group(9).strip(),
                    story_function=m.group(3).strip(),
                    planned_edit_camera=m.group(4).strip(),
                ))
        elif len(matches_7) >= len(matches_6) and matches_7:
            for m in matches_7:
                base = m.group(1).strip()
                if base in seen:
                    continue
                seen.add(base)
                beat_mapping = m.group(2).strip()
                diagnosis = m.group(3).strip()
                edit_level_cell = m.group(4).strip()
                edit_strategy = m.group(5).strip()
                fixes = m.group(6).strip()
                keep = m.group(7).strip()
                result.entries.append(ResponseParser._build_edit_entry(
                    base, beat_mapping, diagnosis,
                    edit_level_cell, edit_strategy, fixes, keep,
                ))
        else:
            for m in matches_6:
                base = m.group(1).strip()
                if base in seen:
                    continue
                seen.add(base)
                diagnosis = m.group(2).strip()
                edit_level_cell = m.group(3).strip()
                edit_strategy = m.group(4).strip()
                fixes = m.group(5).strip()
                keep = m.group(6).strip()
                result.entries.append(ResponseParser._build_edit_entry(
                    base, "", diagnosis,
                    edit_level_cell, edit_strategy, fixes, keep,
                ))

        return result

    @staticmethod
    def _build_edit_entry(
        base: str,
        beat_mapping: str,
        diagnosis: str,
        edit_level_cell: str,
        edit_strategy: str,
        fixes: str,
        keep: str,
        story_function: str = "",
        planned_edit_camera: str = "",
    ) -> EditPlanEntry:
        level_match = re.search(r"Level\s*([1-4])", edit_level_cell, re.IGNORECASE)
        edit_level = f"Level {level_match.group(1)}" if level_match else edit_level_cell

        num_match = re.search(r"\d+", base)
        idx = int(num_match.group(0)) if num_match else 0
        edit_frame = f"case_edit_frame{idx:02d}.png" if idx else ""
        final_frame = f"case_final_frame{idx:02d}.png" if idx else ""

        return EditPlanEntry(
            base_frame=base,
            edit_frame=edit_frame,
            final_frame=final_frame,
            beat_mapping=beat_mapping,
            story_function=story_function,
            planned_edit_camera=planned_edit_camera,
            diagnosis=diagnosis,
            edit_level=edit_level,
            edit_strategy=edit_strategy,
            fixes=fixes,
            keep=keep,
        )

    # ──────────────────────────────────────────────────────────
    # v6.0/v6.2 第二阶段：镜头变换计划解析（已废弃，保留兼容旧 state）
    # ──────────────────────────────────────────────────────────

    @staticmethod
    def parse_shot_plan(response: str) -> ShotPlanResult:
        """解析 VLM 给出的镜头变换表。

        预期 markdown 表格示例（列顺序固定）：
        | 基础帧 | 是否变换 | 变换类型 | 选择理由 | 最终使用 |
        |---|---|---|---|---|
        | case_base_frame01.png | 否 | 保持基础 wide shot | …… | case_final_frame01.png |
        | case_base_frame02.png | 是 | medium close-up | …… | case_shot_frame02.png |
        """
        result = ShotPlanResult(raw_response=response)

        # 收集所有引用了 case_base_frame 的表格行
        row_pattern = re.compile(
            r"^\s*\|\s*(case_base_frame\d+\.png)\s*\|"
            r"\s*([^|\n]+?)\s*\|"
            r"\s*([^|\n]+?)\s*\|"
            r"\s*([^|\n]+?)\s*\|"
            r"\s*([^|\n]+?)\s*\|",
            re.MULTILINE,
        )

        seen = set()
        for m in row_pattern.finditer(response):
            base = m.group(1).strip()
            if base in seen:
                continue
            seen.add(base)

            transform_word = m.group(2).strip()
            transform_type = m.group(3).strip()
            reason = m.group(4).strip()
            final_cell = m.group(5).strip()

            transform = bool(re.search(r"是|yes|y\b|true|变换|transform", transform_word, re.IGNORECASE))

            shot_frame = ""
            final_frame = ""
            shot_match = re.search(r"(case_shot_frame\d+\.png)", final_cell, re.IGNORECASE)
            if shot_match:
                shot_frame = shot_match.group(1)
                transform = True

            final_match = re.search(r"(case_final_frame\d+\.png)", final_cell, re.IGNORECASE)
            if final_match:
                final_frame = final_match.group(1)
            else:
                num_match = re.search(r"\d+", base)
                if num_match:
                    final_frame = f"case_final_frame{int(num_match.group(0)):02d}.png"

            if transform and not shot_frame:
                num_match = re.search(r"\d+", base)
                if num_match:
                    shot_frame = f"case_shot_frame{int(num_match.group(0)):02d}.png"

            result.entries.append(ShotPlanEntry(
                base_frame=base,
                transform=transform,
                transform_type=transform_type,
                reason=reason,
                final_frame=final_frame,
                shot_frame=shot_frame,
            ))

        return result
