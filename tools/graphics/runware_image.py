"""Runware image generation tool.

Wraps the Runware SDK as a pipeline-callable tool. Default model is FLUX.1 Dev
(`runware:101@1`) at ~$0.0045/image; any Runware-supported image model AIR id
can be passed in.

The tool is prompt-agnostic — it does NOT auto-apply the DRiX Montage style
scaffold. Callers (e.g., the DRiX-Montage asset-director skill) are responsible
for constructing the full prompt including the canonical Kodak Portra 400 anchor.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class RunwareImage(BaseTool):
    name = "runware_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
    provider = "runware"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.API

    dependencies = []  # `runware` package; checked dynamically
    install_instructions = (
        "1. pip install runware\n"
        "2. Set RUNWARE_API_KEY in .env (get one at https://my.runware.ai/keys)"
    )
    agent_skills = []  # Runware MCP server is optional dev surface; no skill required

    capabilities = ["generate_image", "text_to_image"]
    supports = {
        "negative_prompt": False,  # FLUX expects anti-cues in the positive prompt
        "seed": True,
        "custom_size": True,
        "batch_variants": True,
    }
    best_for = [
        "ultra-cheap image generation (~$0.0045/image on FLUX.1 Dev)",
        "high-volume pipelines where image cost matters",
        "DRiX Montage 'cinematic nostalgia documentary' aesthetic via FLUX.1 Dev",
        "multi-variant generation for hand-anatomy retries",
    ]
    not_good_for = [
        "text rendering inside images (FLUX weak point — use a typography overlay instead)",
        "offline generation",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string", "description": "Full positive prompt (caller constructs with any style scaffold)"},
            "model": {
                "type": "string",
                "default": "runware:101@1",
                "description": "Runware AIR model id. Default = FLUX.1 Dev.",
            },
            "width": {"type": "integer", "default": 1024},
            "height": {"type": "integer", "default": 1024},
            "num_variants": {"type": "integer", "default": 1, "minimum": 1, "maximum": 8},
            "seed": {"type": "integer"},
            "output_dir": {"type": "string", "description": "Directory to save images; auto-named with label + variant suffix"},
            "output_path": {"type": "string", "description": "Single explicit output path (overrides output_dir)"},
            "label": {"type": "string", "default": "image", "description": "Base filename when output_dir is used"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=2.0,
        retryable_errors=["rate_limit", "timeout", "connection_error"],
    )
    idempotency_key_fields = ["prompt", "model", "width", "height", "seed"]
    side_effects = ["writes PNG file(s) to disk", "calls Runware API (paid)"]
    user_visible_verification = ["Inspect each generated image against the aesthetic spec"]

    def _get_api_key(self) -> str | None:
        return os.environ.get("RUNWARE_API_KEY")

    def get_status(self) -> ToolStatus:
        if not self._get_api_key():
            return ToolStatus.UNAVAILABLE
        try:
            import runware  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE
        return ToolStatus.AVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # FLUX.1 Dev on Runware is ~$0.0045/image at 1024x1024.
        # Other models vary; this is a reasonable default for the workhorse path.
        n = inputs.get("num_variants", 1)
        model = inputs.get("model", "runware:101@1")
        per_image = 0.0045 if "101@1" in model else 0.01
        return round(per_image * n, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            return asyncio.run(self._execute_async(inputs))
        except Exception as e:
            return ToolResult(success=False, error=f"runware_image failed: {type(e).__name__}: {e}")

    async def _execute_async(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(success=False, error="No RUNWARE_API_KEY in env. " + self.install_instructions)

        try:
            from runware import Runware, IImageInference
        except ImportError as e:
            return ToolResult(success=False, error=f"runware SDK not installed: {e}")

        import httpx

        start = time.time()

        model = inputs.get("model", "runware:101@1")
        prompt = inputs["prompt"]
        width = int(inputs.get("width", 1024))
        height = int(inputs.get("height", 1024))
        num_variants = max(1, min(int(inputs.get("num_variants", 1)), 8))

        # Build the inference request. seed is optional.
        req_kwargs: dict[str, Any] = {
            "positivePrompt": prompt,
            "model": model,
            "numberResults": num_variants,
            "width": width,
            "height": height,
            "outputFormat": "PNG",
            "includeCost": True,
        }
        if inputs.get("seed") is not None:
            req_kwargs["seed"] = int(inputs["seed"])

        req = IImageInference(**req_kwargs)

        rw = Runware(api_key=api_key)
        await rw.connect()
        results = await rw.imageInference(requestImage=req)

        if not results:
            return ToolResult(success=False, error="Runware returned empty result set")

        saved_paths: list[str] = []
        total_cost = 0.0
        seeds: list[int | None] = []

        async with httpx.AsyncClient() as http:
            for i, img in enumerate(results):
                url = getattr(img, "imageURL", None) or getattr(img, "image_url", None)
                if not url:
                    continue
                cost = getattr(img, "cost", 0.0) or 0.0
                total_cost += cost
                seeds.append(getattr(img, "seed", None))

                # Resolve output path
                if inputs.get("output_path"):
                    base = Path(inputs["output_path"])
                    out_path = base if num_variants == 1 else base.with_stem(f"{base.stem}_v{i + 1}")
                elif inputs.get("output_dir"):
                    out_dir = Path(inputs["output_dir"])
                    label = inputs.get("label", "image")
                    suffix = "" if num_variants == 1 else f"_v{i + 1}"
                    out_path = out_dir / f"{label}{suffix}.png"
                else:
                    task_id = getattr(img, "taskUUID", None) or str(i)
                    out_path = Path(f"runware_image_{task_id}.png")

                out_path.parent.mkdir(parents=True, exist_ok=True)
                r = await http.get(url, timeout=30.0)
                r.raise_for_status()
                out_path.write_bytes(r.content)
                saved_paths.append(str(out_path))

        return ToolResult(
            success=True,
            data={
                "provider": "runware",
                "model": model,
                "prompt": prompt,
                "num_variants": len(saved_paths),
                "output_paths": saved_paths,
                "seeds": seeds,
            },
            artifacts=saved_paths,
            cost_usd=round(total_cost, 4),
            duration_seconds=round(time.time() - start, 2),
            seed=seeds[0] if seeds else None,
            model=model,
        )
