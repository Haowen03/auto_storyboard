"""
LTX 2.3 参考图插帧生视频客户端（keyframe_interpolation_two_stage）。
从 MetaXViMax_old/test/test.py 抽取，供 auto_storyboard 下游视频阶段调用。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import List

import aiohttp

from .config import (
    LTX_BASE_URL,
    LTX_DEFAULT_HEIGHT,
    LTX_DEFAULT_WIDTH,
    LTX_DIM_MULTIPLE,
    LTX_FRAME_RATE,
    LTX_NUM_INFERENCE_STEPS,
    is_valid_ltx_dimensions,
)

logger = logging.getLogger(__name__)


class VideoGeneratorLTX23API:
    def __init__(self, base_url: str | None = None):
        self.base_url = (base_url or LTX_BASE_URL).rstrip("/")

    def insert_frame_map(self, idxs: List[float], num_frames: int) -> List[int]:
        _idxs: List[int] = []
        for idx in idxs:
            if idx >= 0 and idx % 1 == 0:
                _idxs.append(int(idx))
            elif 0 < idx < 1:
                _idxs.append(int(round(num_frames * idx)))
            elif idx < 0 and idx % 1 == 0:
                _idxs.append(int(num_frames + idx))
            else:
                raise ValueError(f"Invalid image_idx: {idx}")
            assert _idxs[-1] >= 0
            if len(_idxs) > 1:
                assert _idxs[-1] > _idxs[-2]
            assert _idxs[-1] < num_frames
        return _idxs

    async def call_ltx23_gen_video_service(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        seed: int | None = None,
        height: int | None = None,
        width: int | None = None,
        num_frames: int = 121,
        frame_rate: float | int = LTX_FRAME_RATE,
        num_inference_steps: int = LTX_NUM_INFERENCE_STEPS,
        images: List[str] | None = None,
        image_idxs: List[float] | None = None,
        image_strengths: List[float] | None = None,
        pipeline_name: str = "keyframe_interpolation_two_stage",
        save_path: str,
    ) -> None:
        assert save_path is not None
        assert pipeline_name in (
            "ti2v_two_stage",
            "ti2vid_two_stages_hq",
            "keyframe_interpolation_two_stage",
            "a2vid_two_stage",
            "retake",
        )
        if pipeline_name != "retake":
            assert (num_frames - 1) % 8 == 0 and num_frames > 1

        resolved_height = LTX_DEFAULT_HEIGHT if height is None else height
        resolved_width = LTX_DEFAULT_WIDTH if width is None else width
        if not is_valid_ltx_dimensions(resolved_width, resolved_height):
            _, _, hint = resolve_ltx_resolution()
            raise ValueError(
                f"LTX 尺寸 {resolved_width}×{resolved_height} 非法："
                f"width/height 均须为 {LTX_DIM_MULTIPLE} 的倍数。"
                f"请使用 --ltx-resolution 预设或同时指定合法的 --ltx-width/--ltx-height。"
            )

        async with aiohttp.ClientSession() as session:
            data_form = aiohttp.FormData()
            data_form.add_field("prompt", prompt)
            data_form.add_field("height", str(resolved_height))
            data_form.add_field("width", str(resolved_width))
            data_form.add_field("num_frames", str(num_frames))
            data_form.add_field("frame_rate", str(frame_rate))
            data_form.add_field("num_inference_steps", str(num_inference_steps))
            data_form.add_field("pipeline_name", pipeline_name)
            if negative_prompt is not None:
                data_form.add_field("negative_prompt", negative_prompt)
            if seed is not None:
                data_form.add_field("seed", str(seed))

            images = images or []
            if images:
                if image_idxs is None:
                    raise ValueError("image_idxs required when images are provided")
                mapped_idxs = self.insert_frame_map(image_idxs, num_frames)
                image_strengths = image_strengths or [1.0] * len(images)
                if not (len(images) == len(mapped_idxs) == len(image_strengths)):
                    raise ValueError(
                        f"LTX 参考图参数长度不一致: images={len(images)}, "
                        f"image_idxs={len(image_idxs)} (mapped {len(mapped_idxs)}), "
                        f"image_strengths={len(image_strengths or [])}"
                    )

                for i, img_path in enumerate(images):
                    with open(img_path, "rb") as img_f:
                        data_form.add_field(
                            "images",
                            img_f.read(),
                            filename=os.path.basename(img_path),
                            content_type="image/png",
                        )
                    data_form.add_field("image_idxs", str(mapped_idxs[i]))
                    data_form.add_field("image_strengths", str(image_strengths[i]))
                    data_form.add_field("image_crfs", "0")

            async with session.post(f"{self.base_url}/submit", data=data_form) as r:
                r.raise_for_status()
                result = await r.json()

        if result.get("status") != "submitted":
            raise RuntimeError(f"LTX submit failed: {result}")

        job_id = result["task_id"]
        logger.info("LTX job submitted: %s", job_id)

        while True:
            await asyncio.sleep(20)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.base_url}/status/{job_id}") as r:
                    r.raise_for_status()
                    status = await r.json()
            if status["status"] == "done":
                break
            if status["status"] in ("running", "queued"):
                continue
            raise RuntimeError(f"LTX task failed: {status}")

        save_dir = os.path.dirname(save_path)
        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{self.base_url}/download/{job_id}") as r:
                        r.raise_for_status()
                        with open(save_path, "wb") as f:
                            async for chunk in r.content.iter_chunked(1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                break
            except Exception as e:
                last_error = e
                if attempt < 2:
                    await asyncio.sleep(2)
                else:
                    raise RuntimeError(f"LTX download failed: {last_error}") from last_error

        logger.info("LTX video saved: %s", save_path)

    async def keyframe_interpolation_to_video(
        self,
        *,
        prompt: str,
        negative_prompt: str | None = None,
        video_seconds: float = 5,
        frame_rate: float | int = LTX_FRAME_RATE,
        images: List[str] | None = None,
        image_idxs: List[float] | None = None,
        image_strengths: List[float] | None = None,
        save_path: str,
        height: int | None = None,
        width: int | None = None,
        num_inference_steps: int = LTX_NUM_INFERENCE_STEPS,
    ) -> None:
        num_frames = int(((video_seconds * frame_rate + 7) // 8) * 8 + 1)
        await self.call_ltx23_gen_video_service(
            prompt=prompt,
            negative_prompt=negative_prompt,
            height=height,
            width=width,
            num_frames=num_frames,
            frame_rate=frame_rate,
            num_inference_steps=num_inference_steps,
            images=images,
            image_idxs=image_idxs,
            image_strengths=image_strengths,
            pipeline_name="keyframe_interpolation_two_stage",
            save_path=save_path,
        )
