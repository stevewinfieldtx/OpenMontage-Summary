"""Runware music generation tool — defaults to MiniMax Music 2.6.

MiniMax Music 2.6 (`minimax:music@2.6`) is the default workhorse for DRiX Montage
at $0.15 per generation. Caps around 3 minutes per generation, so 6-10 min videos
need 2-3 contiguous stems that the compose-director crossfades. Lacks structural
section-by-section control but produces solid full-song output from a good prompt.

ElevenLabs Music v1 (`elevenlabs:1@1`) is the upgrade path: $0.40/min, max 300s,
with a `compositionPlan` for section-by-section structural control over the
emotional arc. Switch to this model when MiniMax's structural ceiling becomes
the bottleneck.
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


class RunwareMusic(BaseTool):
    name = "runware_music"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "music_generation"
    provider = "runware"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.SEEDED
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "1. pip install runware\n"
        "2. Set RUNWARE_API_KEY in .env (get one at https://my.runware.ai/keys)"
    )
    agent_skills = []

    capabilities = ["generate_music", "text_to_music", "structured_composition"]
    supports = {
        "negative_prompt": True,
        "seed": True,
        "force_instrumental": True,
        "composition_plan": True,  # ElevenLabs Music v1 only
    }
    best_for = [
        "music with structural control over the emotional arc",
        "instrumental scoring for video content (sermon montages, documentaries)",
        "named-artist-style flavored generations via natural language prompting",
    ]
    not_good_for = [
        "loop-able stems for game audio (use a dedicated loop generator)",
        "voice cloning or singing TTS (different tool)",
    ]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Music description prompt (max 2000 chars). Include named-artist references for tonal flavor.",
            },
            "model": {
                "type": "string",
                "default": "minimax:music@2.6",
                "description": "Runware AIR model id. Default = MiniMax Music 2.6. Upgrade: elevenlabs:1@1.",
            },
            "duration_seconds": {
                "type": "integer",
                "default": 60,
                "minimum": 10,
                "maximum": 300,
            },
            "force_instrumental": {
                "type": "boolean",
                "default": True,
                "description": "DRiX Montage default: no lyrics — we use the preacher's words instead.",
            },
            "seed": {"type": "integer"},
            "output_dir": {"type": "string"},
            "output_path": {"type": "string"},
            "label": {"type": "string", "default": "music"},
            "output_format": {
                "type": "string",
                "enum": ["MP3", "WAV", "FLAC", "OGG"],
                "default": "WAV",
                "description": "WAV is best for downstream mixing.",
            },
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
    idempotency_key_fields = ["prompt", "model", "duration_seconds", "seed", "force_instrumental"]
    side_effects = ["writes audio file to disk", "calls Runware API (paid)"]
    user_visible_verification = ["Listen to generated track against the sermon's emotional arc"]

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
        model = inputs.get("model", "minimax:music@2.6")
        duration = int(inputs.get("duration_seconds", 60))
        if model.startswith("minimax"):
            return 0.15  # Fixed per generation
        if model.startswith("elevenlabs"):
            return round(0.40 * (duration / 60.0), 4)  # $0.40/min
        return 0.15  # default to MiniMax pricing

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        try:
            return asyncio.run(self._execute_async(inputs))
        except Exception as e:
            return ToolResult(success=False, error=f"runware_music failed: {type(e).__name__}: {e}")

    async def _execute_async(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_api_key()
        if not api_key:
            return ToolResult(success=False, error="No RUNWARE_API_KEY in env. " + self.install_instructions)

        try:
            from runware import Runware, IAudioInference
        except ImportError as e:
            return ToolResult(success=False, error=f"runware SDK not installed: {e}")

        import httpx

        start = time.time()

        model = inputs.get("model", "minimax:music@2.6")
        prompt = inputs["prompt"]
        duration = int(inputs.get("duration_seconds", 60))
        force_instrumental = bool(inputs.get("force_instrumental", True))
        output_format = inputs.get("output_format", "WAV")

        req_kwargs: dict[str, Any] = {
            "positivePrompt": prompt,
            "model": model,
            "duration": duration,
            "outputFormat": output_format,
            "includeCost": True,
        }
        if inputs.get("seed") is not None:
            req_kwargs["seed"] = int(inputs["seed"])

        # forceInstrumental lives in provider settings (varies per model).
        # ElevenLabs Music: providerSettings.elevenlabs.forceInstrumental
        # MiniMax Music: instrumental
        if force_instrumental:
            if model.startswith("elevenlabs"):
                req_kwargs["providerSettings"] = {"elevenlabs": {"forceInstrumental": True}}
            elif model.startswith("minimax"):
                req_kwargs["providerSettings"] = {"minimax": {"instrumental": True}}

        req = IAudioInference(**req_kwargs)

        rw = Runware(api_key=api_key)
        await rw.connect()

        # The SDK exposes audio inference; method name historically `audioInference`.
        # Defensive lookup in case of method-name variation across versions.
        method = getattr(rw, "audioInference", None) or getattr(rw, "musicInference", None)
        if method is None:
            return ToolResult(success=False, error="Runware SDK has no audioInference method available")

        results = await method(requestAudio=req) if "requestAudio" in method.__code__.co_varnames else await method(req)

        if not results:
            return ToolResult(success=False, error="Runware returned empty audio result set")

        # Normalize to a list
        if not isinstance(results, (list, tuple)):
            results = [results]

        saved_paths: list[str] = []
        total_cost = 0.0
        async with httpx.AsyncClient() as http:
            for i, audio in enumerate(results):
                url = (
                    getattr(audio, "audioURL", None)
                    or getattr(audio, "audio_url", None)
                    or getattr(audio, "url", None)
                )
                if not url:
                    continue
                cost = getattr(audio, "cost", 0.0) or 0.0
                total_cost += cost

                ext = output_format.lower()
                if inputs.get("output_path"):
                    out_path = Path(inputs["output_path"])
                elif inputs.get("output_dir"):
                    label = inputs.get("label", "music")
                    suffix = "" if len(results) == 1 else f"_v{i + 1}"
                    out_path = Path(inputs["output_dir"]) / f"{label}{suffix}.{ext}"
                else:
                    out_path = Path(f"runware_music_{int(time.time())}_{i}.{ext}")

                out_path.parent.mkdir(parents=True, exist_ok=True)
                r = await http.get(url, timeout=120.0)
                r.raise_for_status()
                out_path.write_bytes(r.content)
                saved_paths.append(str(out_path))

        return ToolResult(
            success=True,
            data={
                "provider": "runware",
                "model": model,
                "prompt": prompt,
                "duration_seconds": duration,
                "output_paths": saved_paths,
            },
            artifacts=saved_paths,
            cost_usd=round(total_cost or self.estimate_cost(inputs), 4),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )
