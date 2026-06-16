"""
ImageGenerator: 封装 Qwen-Image 内部 API，支持批量生成候选图和自动重试。
复用 test_qwen_image.py 中的 ImageGeneratorQwenImageAPI 核心逻辑。
"""

import asyncio
import aiohttp
import os
import time
import logging
from typing import List, Optional

from .config import (
    QWEN_IMAGE_BASE_URL,
    DEFAULT_IMAGE_HEIGHT,
    DEFAULT_IMAGE_WIDTH,
    DEFAULT_NUM_INFERENCE_STEPS,
)

logger = logging.getLogger(__name__)


class ImageGenerator:
    def __init__(self, base_url: str = QWEN_IMAGE_BASE_URL):
        self.base_url = base_url

    async def generate(
        self,
        prompt: str,
        save_path: str,
        images: Optional[List[str]] = None,
        height: int = DEFAULT_IMAGE_HEIGHT,
        width: int = DEFAULT_IMAGE_WIDTH,
        num_inference_steps: int = DEFAULT_NUM_INFERENCE_STEPS,
        seed: Optional[int] = None,
        negative_prompt: Optional[str] = None,
        max_retries: int = 2,
    ) -> str:
        """生成单张图片，返回保存路径。失败时自动重试。"""
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)

        pipeline_name = "qwen_image" if (not images) else "qwen_image_edit"

        for attempt in range(max_retries + 1):
            try:
                job_id = await self._submit(
                    prompt, pipeline_name, images, height, width,
                    num_inference_steps, seed, negative_prompt,
                )
                await self._poll_until_done(job_id)
                await self._download(job_id, save_path)
                logger.info("Image saved: %s", save_path)
                return save_path
            except Exception as e:
                logger.warning(
                    "Generate attempt %d/%d failed: %s",
                    attempt + 1, max_retries + 1, e,
                )
                if attempt < max_retries:
                    await asyncio.sleep(5)
                else:
                    raise

    async def generate_candidates(
        self,
        prompt: str,
        save_dir: str,
        filename_prefix: str,
        num_candidates: int,
        images: Optional[List[str]] = None,
        height: int = DEFAULT_IMAGE_HEIGHT,
        width: int = DEFAULT_IMAGE_WIDTH,
        num_inference_steps: int = DEFAULT_NUM_INFERENCE_STEPS,
        negative_prompt: Optional[str] = None,
    ) -> List[str]:
        """并发生成多张候选图，返回所有保存路径列表。"""
        os.makedirs(save_dir, exist_ok=True)

        tasks = []
        paths = []
        for i in range(1, num_candidates + 1):
            path = os.path.join(save_dir, f"{filename_prefix}_candidate_{i}.png")
            paths.append(path)
            tasks.append(
                self.generate(
                    prompt=prompt,
                    save_path=path,
                    images=images,
                    height=height,
                    width=width,
                    num_inference_steps=num_inference_steps,
                    negative_prompt=negative_prompt,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        successful_paths = []
        for path, result in zip(paths, results):
            if isinstance(result, Exception):
                logger.error("Failed to generate %s: %s", path, result)
            else:
                successful_paths.append(path)

        if not successful_paths:
            raise RuntimeError(
                f"All {num_candidates} candidates failed for {filename_prefix}"
            )

        return successful_paths

    async def _submit(
        self,
        prompt: str,
        pipeline_name: str,
        images: Optional[List[str]],
        height: int,
        width: int,
        num_inference_steps: int,
        seed: Optional[int],
        negative_prompt: Optional[str],
    ) -> str:
        async with aiohttp.ClientSession() as session:
            data_form = aiohttp.FormData()
            data_form.add_field("prompt", prompt)
            data_form.add_field("height", str(height))
            data_form.add_field("width", str(width))
            data_form.add_field("num_inference_steps", str(num_inference_steps))
            data_form.add_field("pipeline_name", pipeline_name)

            if negative_prompt:
                data_form.add_field("negative_prompt", negative_prompt)
            if seed is not None:
                data_form.add_field("seed", str(seed))

            if images:
                for img_path in images:
                    ct = "image/png" if img_path.endswith(".png") else "image/jpeg"
                    data_form.add_field(
                        "images",
                        open(img_path, "rb"),
                        filename=os.path.basename(img_path),
                        content_type=ct,
                    )

            async with session.post(
                f"{self.base_url}/submit", data=data_form
            ) as r:
                r.raise_for_status()
                result = await r.json()

        if result.get("status") != "submitted":
            raise RuntimeError(f"Submit failed: {result}")

        job_id = result["task_id"]
        logger.info("Job submitted: %s", job_id)
        return job_id

    async def _poll_until_done(self, job_id: str, poll_interval: int = 20):
        while True:
            await asyncio.sleep(poll_interval)
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/status/{job_id}"
                ) as r:
                    r.raise_for_status()
                    s = await r.json()

            status = s.get("status")
            if status == "done":
                return
            elif status in ("running", "queued"):
                continue
            else:
                raise RuntimeError(f"Task failed: {s}")

    async def _download(self, job_id: str, save_path: str, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        f"{self.base_url}/download/{job_id}"
                    ) as r:
                        r.raise_for_status()
                        with open(save_path, "wb") as f:
                            async for chunk in r.content.iter_chunked(1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                return
            except Exception as e:
                logger.warning("Download attempt %d failed: %s", attempt + 1, e)
                await asyncio.sleep(2)

        raise RuntimeError(f"Download failed after {max_retries} attempts: {job_id}")
