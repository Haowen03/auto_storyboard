"""
QwenVLClient: 通过 aiohttp 调用 OpenAI 兼容 VLM 接口（DashScope / Gitee 模力方舟），
支持 thinking 模型、base64 图片输入、多轮对话管理。
"""

import asyncio
import copy
import aiohttp
import base64
import json
import logging
import re
from typing import List, Optional, Dict, Any

from .config import (
    GITEE_MAX_VL_IMAGES,
    GITEE_VL_CONTEXT_LIMIT,
    get_vlm_model_pair,
    get_vlm_settings,
)

logger = logging.getLogger(__name__)

_MAX_TOKENS_CAP_RE = re.compile(
    r"maximum context length is (\d+).*?(\d+) input tokens",
    re.IGNORECASE | re.DOTALL,
)
_INPUT_TOO_LONG_RE = re.compile(
    r"your request has (\d+) input tokens",
    re.IGNORECASE,
)

_VL_COMPACT_SYSTEM = (
    "你是分镜参考帧工作流的视觉审查与多模态规划专家（v6.8/v6.14）。"
    "严格完成用户当前任务；先前文本阶段的规划摘要见对话历史。"
    "若存在「镜头多样性运行账本」，必须据此为当前帧选择**不同于已用帧**的特殊镜头，"
    "不得退回 neutral medium 平淡中景。"
)

VL_IMAGE_TOKEN_ESTIMATE = 900  # 每张 base64 图在 VL 上下文中的粗估 token
# Gitee VL 压缩时优先保留的结构化补充（如镜头多样性账本），不被普通摘要挤掉
VL_COMPACT_SUPPLEMENT_MAX_CHARS = 4500


def _extract_message_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image_url":
                parts.append("[image]")
        return "\n".join(parts)
    return ""


def _count_images_in_message(msg: Dict[str, Any]) -> int:
    content = msg.get("content", "")
    if not isinstance(content, list):
        return 0
    return sum(1 for item in content if item.get("type") == "image_url")


def _estimate_message_tokens(messages: List[Dict[str, Any]]) -> int:
    total_chars = 0
    image_count = 0
    for msg in messages:
        total_chars += len(_extract_message_text(msg))
        image_count += _count_images_in_message(msg)
    return total_chars // 3 + image_count * VL_IMAGE_TOKEN_ESTIMATE


def _strip_all_images(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Gitee 文本模型不接受历史多模态消息，发送前将图片替换为占位说明。"""
    out = copy.deepcopy(messages)
    for msg in out:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        omitted = sum(1 for item in content if item.get("type") == "image_url")
        if not omitted:
            continue
        new_content = [
            {
                "type": "text",
                "text": f"[已省略 {omitted} 张历史图片以适配 Gitee 文本模型]",
            }
        ]
        for item in content:
            if item.get("type") != "image_url":
                new_content.append(item)
        msg["content"] = new_content
    return out


def _cap_images_per_user_message(
    messages: List[Dict[str, Any]],
    max_images: int,
) -> List[Dict[str, Any]]:
    """Gitee VL 单条 user 消息最多 max_images 张图。"""
    if max_images <= 0:
        return messages
    out = copy.deepcopy(messages)
    for msg in out:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        image_idxs = [
            i for i, item in enumerate(content) if item.get("type") == "image_url"
        ]
        if len(image_idxs) <= max_images:
            continue
        drop = set(image_idxs[max_images:])
        dropped = len(drop)
        new_content = [item for i, item in enumerate(content) if i not in drop]
        new_content.insert(
            0,
            {
                "type": "text",
                "text": (
                    f"[已省略 {dropped} 张图片：Gitee 单次请求每条消息最多 "
                    f"{max_images} 张图]"
                ),
            },
        )
        msg["content"] = new_content
    return out


def cap_pick_context_images(
    candidate_paths: List[str],
    extra_paths: List[str],
    max_images: Optional[int],
    *,
    prioritize_extras: bool = False,
) -> tuple[List[str], int, str]:
    """
    限制评选任务的传图数量。返回 (paths, num_candidates_sent, omission_note)。
    prioritize_extras=False：优先保留全部候选，省略尾部参考图（基础帧评选）。
    prioritize_extras=True：为参考图腾位，可缩减候选数（编辑帧评选需附 base）。
    """
    if max_images is None or max_images <= 0:
        paths = list(candidate_paths) + [
            p for p in extra_paths if p and p not in candidate_paths
        ]
        return paths, len(candidate_paths), ""

    candidates = list(candidate_paths)
    extras = [p for p in extra_paths if p and p not in candidates]
    note = ""

    if prioritize_extras and extras:
        max_cands = max(1, max_images - len(extras))
        if len(candidates) > max_cands:
            note = (
                f"Gitee API 单次最多 {max_images} 张图："
                f"仅前 {max_cands}/{len(candidate_paths)} 张候选参与评选，"
                f"并附 {len(extras)} 张参考图。"
            )
            candidates = candidates[:max_cands]
        paths = candidates + extras
        return paths, len(candidates), note

    if len(candidates) + len(extras) <= max_images:
        return candidates + extras, len(candidates), ""

    kept_extras = extras[: max(0, max_images - len(candidates))]
    omitted = len(extras) - len(kept_extras)
    if omitted:
        note = (
            f"Gitee API 单次最多 {max_images} 张图："
            f"{omitted} 张参考图未传入，请依据生成 prompt 判断场景/人物一致性。"
        )
    return candidates + kept_extras, len(candidates), note


def _cap_max_tokens_from_context_error(error_text: str, requested: int) -> Optional[int]:
    """从 Gitee 等 400 错误中解析剩余输出 token 预算。"""
    m = _MAX_TOKENS_CAP_RE.search(error_text)
    if not m:
        return None
    ctx_len, input_tokens = int(m.group(1)), int(m.group(2))
    remaining = ctx_len - input_tokens - 64  # 留少量安全边距
    if remaining < 256:
        return None
    capped = min(requested, remaining)
    return capped if capped < requested else None


def encode_image_to_base64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def make_image_content(image_path: str) -> Dict[str, Any]:
    b64 = encode_image_to_base64(image_path)
    suffix = image_path.rsplit(".", 1)[-1].lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(suffix, "image/png")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


class QwenVLClient:
    """管理与 VLM 后端的多轮对话，维护完整 messages 历史。"""

    THINKING_MODEL_KEYWORDS = ("thinking",)

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider: str | None = None,
    ):
        if provider or api_key is None or base_url is None or model is None:
            resolved_key, resolved_url, resolved_model, resolved_provider = (
                get_vlm_settings(provider)
            )
            self.api_key = api_key or resolved_key
            self.base_url = (base_url or resolved_url).rstrip("/")
            self.model = model or resolved_model
            self.provider = resolved_provider
            text_model, vl_model = get_vlm_model_pair(provider)
            self.text_model = text_model
            self.vl_model = vl_model
        else:
            self.api_key = api_key
            self.base_url = base_url.rstrip("/")
            self.model = model
            self.provider = "custom"
            self.text_model = model
            self.vl_model = model
        self.messages: List[Dict[str, Any]] = []
        self.compact_supplements: List[str] = []
        logger.info(
            "VLM provider=%s text_model=%s vl_model=%s",
            self.provider,
            self.text_model,
            self.vl_model,
        )

    def _supports_thinking_for(self, model: str) -> bool:
        return any(kw in model.lower() for kw in self.THINKING_MODEL_KEYWORDS)

    def _pick_model(self, image_paths: Optional[List[str]] = None) -> str:
        if image_paths:
            return self.vl_model
        return self.text_model

    def _needs_vl_compact(self, model: str) -> bool:
        return self.vl_model != self.text_model and model == self.vl_model

    def set_compact_supplements(self, *sections: str) -> None:
        """Gitee 双模型 VL 压缩时注入的高优先级结构化上下文（如镜头多样性账本）。"""
        self.compact_supplements = [s.strip() for s in sections if s and s.strip()]

    def clear_compact_supplements(self) -> None:
        self.compact_supplements = []

    def _build_protected_supplement_blob(self) -> str:
        if not self.compact_supplements:
            return ""
        blob = "\n\n".join(self.compact_supplements)
        if len(blob) <= VL_COMPACT_SUPPLEMENT_MAX_CHARS:
            return blob
        return (
            blob[:VL_COMPACT_SUPPLEMENT_MAX_CHARS]
            + "\n\n[...镜头多样性账本尾部已截断；请以表中已列帧为准...]"
        )

    def _compact_messages_for_vl(
        self,
        messages: List[Dict[str, Any]],
        compact_level: int = 0,
    ) -> List[Dict[str, Any]]:
        """双模型时 VL 只有 32K：去掉巨型 system 文档，保留规划摘要 + 当前带图请求。"""
        if len(messages) <= 1:
            return copy.deepcopy(messages)

        last = copy.deepcopy(messages[-1])
        history = messages[1:-1]
        supplement_blob = self._build_protected_supplement_blob()

        context_parts: List[str] = []
        if compact_level >= 2:
            for msg in history:
                if msg.get("role") == "user":
                    text = _extract_message_text(msg)
                    if text:
                        context_parts.append(f"### Idea / 首轮输入\n{text[:1200]}")
                    break
        elif compact_level >= 1:
            best = ""
            for msg in history:
                if msg.get("role") == "assistant":
                    text = _extract_message_text(msg)
                    if len(text) > len(best):
                        best = text
            if best:
                # 优先从长规划中抽取镜头相关段落，避免整段截断丢多样性约束
                camera_excerpt = self._extract_camera_plan_excerpt(best)
                if camera_excerpt:
                    context_parts.append(
                        f"### 规划中的镜头多样性要点\n{camera_excerpt}"
                    )
                context_parts.append(
                    f"### 文本阶段规划摘要\n{best[:8000]}"
                )
        else:
            for msg in history:
                role = msg.get("role", "")
                text = _extract_message_text(msg)
                if not text:
                    continue
                label = "User" if role == "user" else "Assistant"
                context_parts.append(f"### {label}\n{text}")

        context_blob = "\n\n".join(context_parts)
        budget_tokens = GITEE_VL_CONTEXT_LIMIT - 4096  # 留给输出与图片余量
        last_tokens = _estimate_message_tokens([last])
        max_context_chars = max(2000, (budget_tokens - last_tokens) * 3)
        # 账本优先：摘要预算 = 总预算 − 账本长度
        summary_budget = max(
            1200,
            max_context_chars - len(supplement_blob) - (80 if supplement_blob else 0),
        )
        if len(context_blob) > summary_budget:
            context_blob = (
                context_blob[:summary_budget]
                + "\n\n[...先前对话摘要已截断；镜头多样性以运行账本为准...]"
            )

        merged_blob = supplement_blob
        if context_blob.strip():
            merged_blob = (
                f"{supplement_blob}\n\n{context_blob}"
                if supplement_blob
                else context_blob
            )

        compact: List[Dict[str, Any]] = [
            {"role": "system", "content": _VL_COMPACT_SYSTEM},
        ]
        if merged_blob.strip():
            compact.append(
                {
                    "role": "user",
                    "content": f"## 先前文本阶段对话摘要\n{merged_blob}",
                }
            )
            compact.append(
                {
                    "role": "assistant",
                    "content": (
                        "已阅读摘要与镜头多样性账本。请根据当前任务指令与图片继续，"
                        "并确保本帧镜头与已用帧有明显区分。"
                    ),
                }
            )
        compact.append(last)
        return compact

    @staticmethod
    def _extract_camera_plan_excerpt(plan_text: str) -> str:
        """从长规划回复中抽取镜头多样性相关行，供 level>=1 压缩时保留。"""
        if not plan_text:
            return ""
        keywords = (
            "planned_edit_camera", "镜头", "camera", "close-up", "wide", "overhead",
            "low-angle", "OTS", "diversity", "多样性", "neutral medium", "edit_level",
            "story_function", "E0", "case_edit_frame",
        )
        lines: List[str] = []
        for line in plan_text.splitlines():
            low = line.lower()
            if any(k.lower() in low for k in keywords):
                lines.append(line.rstrip())
        if not lines:
            return ""
        excerpt = "\n".join(lines)
        return excerpt[:6000]

    def _prepare_payload_messages(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        compact_level: int = 0,
    ) -> List[Dict[str, Any]]:
        if self._needs_vl_compact(model):
            payload = self._compact_messages_for_vl(messages, compact_level)
            est = _estimate_message_tokens(payload)
            logger.info(
                "VL compact level=%d: %d msgs -> %d msgs, ~%d tokens (limit %d)",
                compact_level,
                len(messages),
                len(payload),
                est,
                GITEE_VL_CONTEXT_LIMIT,
            )
        else:
            payload = copy.deepcopy(messages)

        if self.provider == "gitee":
            if model == self.text_model:
                payload = _strip_all_images(payload)
            else:
                payload = _cap_images_per_user_message(payload, GITEE_MAX_VL_IMAGES)
        return payload

    def set_system_prompt(self, text: str):
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = {"role": "system", "content": text}
        else:
            self.messages.insert(0, {"role": "system", "content": text})

    def add_user_message(self, text: str, image_paths: Optional[List[str]] = None):
        content: List[Dict[str, Any]] = []
        if image_paths:
            for p in image_paths:
                content.append(make_image_content(p))
        content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})

    def add_assistant_message(self, text: str):
        self.messages.append({"role": "assistant", "content": text})

    async def chat(
        self,
        text: str,
        image_paths: Optional[List[str]] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        enable_thinking: Optional[bool] = None,
    ) -> str:
        """发送一轮对话，返回模型最终回复文本（不含 thinking 部分）。"""
        self.add_user_message(text, image_paths)
        model = self._pick_model(image_paths)
        if enable_thinking is None:
            enable_thinking = self._supports_thinking_for(model)
        reply = await self._call_api(
            temperature, max_tokens, enable_thinking, model=model
        )
        self.add_assistant_message(reply)
        return reply

    async def chat_without_history(
        self,
        text: str,
        image_paths: Optional[List[str]] = None,
        system_prompt: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        enable_thinking: Optional[bool] = None,
    ) -> str:
        """单次调用，不影响主对话历史。用于独立的图片评选任务。"""
        model = self._pick_model(image_paths)
        if enable_thinking is None:
            enable_thinking = self._supports_thinking_for(model)
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        content: List[Dict[str, Any]] = []
        if image_paths:
            for p in image_paths:
                content.append(make_image_content(p))
        content.append({"type": "text", "text": text})
        messages.append({"role": "user", "content": content})

        reply = await self._call_api_with_messages(
            messages, temperature, max_tokens, enable_thinking, model=model
        )
        return reply

    async def _call_api(
        self,
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        model: Optional[str] = None,
    ) -> str:
        return await self._call_api_with_messages(
            self.messages,
            temperature,
            max_tokens,
            enable_thinking,
            model=model or self.text_model,
        )

    async def _call_api_with_messages(
        self,
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        model: Optional[str] = None,
    ) -> str:
        resolved_model = model or self.text_model
        payload = self._prepare_payload_messages(messages, resolved_model, compact_level=0)
        return await self._stream_chat_completion(
            messages,
            payload,
            temperature,
            max_tokens,
            enable_thinking,
            model=resolved_model,
            retried=False,
            compact_level=0,
        )

    async def _stream_chat_completion(
        self,
        full_messages: List[Dict[str, Any]],
        messages: List[Dict[str, Any]],
        temperature: float,
        max_tokens: int,
        enable_thinking: bool,
        model: str,
        retried: bool,
        compact_level: int,
    ) -> str:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Gitee 等后端默认开启 thinking，需显式关闭才能得到 content 字段
        body["enable_thinking"] = bool(enable_thinking)

        final_content = []
        reasoning_content = []
        chunk_count = 0
        first_chunk_logged = False

        timeout = aiohttp.ClientTimeout(total=600)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=body) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    if resp.status == 400:
                        capped = _cap_max_tokens_from_context_error(
                            error_text, max_tokens
                        )
                        if capped is not None and not retried:
                            logger.warning(
                                "max_tokens=%d 超出上下文余量，自动降为 %d 后重试",
                                max_tokens,
                                capped,
                            )
                            return await self._stream_chat_completion(
                                full_messages,
                                messages,
                                temperature,
                                capped,
                                enable_thinking,
                                model=model,
                                retried=True,
                                compact_level=compact_level,
                            )
                        if (
                            _INPUT_TOO_LONG_RE.search(error_text)
                            and self._needs_vl_compact(model)
                            and compact_level < 2
                        ):
                            next_level = compact_level + 1
                            logger.warning(
                                "VL 输入超长，提升压缩等级 %d -> %d 后重试",
                                compact_level,
                                next_level,
                            )
                            payload = self._prepare_payload_messages(
                                full_messages, model, compact_level=next_level
                            )
                            return await self._stream_chat_completion(
                                full_messages,
                                payload,
                                temperature,
                                max_tokens,
                                enable_thinking,
                                model=model,
                                retried=retried,
                                compact_level=next_level,
                            )
                    raise RuntimeError(
                        f"VLM API error ({self.provider}) {resp.status}: {error_text}"
                    )

                async for line in resp.content:
                    line = line.decode("utf-8").strip()
                    if not line.startswith("data: "):
                        continue
                    data_str = line[len("data: "):]
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    chunk_count += 1
                    if not first_chunk_logged:
                        logger.debug("First chunk keys: %s", list(chunk.keys()))
                        first_chunk_logged = True

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})

                    rc = delta.get("reasoning_content", "")
                    if rc:
                        reasoning_content.append(rc)

                    c = delta.get("content", "")
                    if c:
                        final_content.append(c)

        reply = "".join(final_content)
        thinking = "".join(reasoning_content)

        if thinking:
            logger.debug("VLM thinking length: %d chars", len(thinking))

        if not reply and thinking:
            logger.warning(
                "content is empty but reasoning_content has %d chars, "
                "falling back to reasoning_content as reply", len(thinking)
            )
            reply = thinking

        if not reply:
            logger.error(
                "VLM returned empty response. model=%s, enable_thinking=%s, "
                "chunks_received=%d", self.model, enable_thinking, chunk_count
            )
            raise RuntimeError(
                f"VLM returned empty response (model={self.model}, "
                f"chunks={chunk_count}, enable_thinking={enable_thinking})"
            )

        logger.info("VLM reply length: %d chars", len(reply))
        return reply

    def get_messages_snapshot(self) -> List[Dict[str, Any]]:
        """返回当前 messages 的深拷贝，用于日志记录。"""
        return copy.deepcopy(self.messages)

    def trim_old_frame_images(self, keep_last_n: int = 2):
        """优化上下文长度：只保留最近 N 帧的图片，更早的帧图片替换为文本描述。"""
        frame_msg_indices = []
        for i, msg in enumerate(self.messages):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                has_frame_image = False
                for item in msg["content"]:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if "case_frame" in text and ("已生成" in text or "上传" in text or "选择" in text):
                            has_frame_image = True
                            break
                if has_frame_image:
                    frame_msg_indices.append(i)

        if len(frame_msg_indices) <= keep_last_n:
            return

        indices_to_trim = frame_msg_indices[:-keep_last_n]
        for idx in indices_to_trim:
            msg = self.messages[idx]
            if isinstance(msg["content"], list):
                new_content = []
                for item in msg["content"]:
                    if item.get("type") == "image_url":
                        new_content.append({
                            "type": "text",
                            "text": "[图片已省略以节省上下文]",
                        })
                    else:
                        new_content.append(item)
                msg["content"] = new_content
