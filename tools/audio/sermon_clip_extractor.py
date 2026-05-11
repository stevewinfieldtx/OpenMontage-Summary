"""Sermon audio clip extractor — cuts clean segments from cached sermon audio.

Inputs:
  source_audio: path to a sermon audio file (e.g., .cache/youtube/{id}/audio.m4a)
  clips:        list of {label, start_seconds, end_seconds, padding_seconds?} dicts
  output_dir:   where the clipped WAV files land

For DRiX Montage, this is how we get the preacher's own voice into the final
piece — 30-90 seconds total across 3-4 strategic moments (open hook, mid-piece
anchor, CTA close). The script-director identifies the clip boundaries from the
transcript word-timestamps; this tool cuts them cleanly.

Uses ffmpeg via imageio_ffmpeg's bundled binary — no system ffmpeg install
required. Default output is mono 44.1kHz WAV for clean downstream mixing.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import imageio_ffmpeg

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


FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


class SermonClipExtractor(BaseTool):
    name = "sermon_clip_extractor"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "audio_extraction"
    provider = "ffmpeg"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["imageio-ffmpeg"]
    install_instructions = "pip install imageio-ffmpeg (ffmpeg binary bundled)"
    agent_skills = []

    capabilities = ["extract_audio_segment", "clip_audio"]
    supports = {"batch": True, "padding": True, "format_conversion": True}
    best_for = [
        "extracting preacher voice clips from a long sermon recording",
        "producing clean WAV stems for mixing in compose stage",
    ]
    not_good_for = [
        "voice separation from background music (use a stem-splitter)",
        "noise reduction (use audio_enhance)",
    ]

    input_schema = {
        "type": "object",
        "required": ["source_audio", "clips"],
        "properties": {
            "source_audio": {"type": "string", "description": "Path to source audio file"},
            "clips": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["label", "start_seconds", "end_seconds"],
                    "properties": {
                        "label": {"type": "string"},
                        "start_seconds": {"type": "number", "minimum": 0},
                        "end_seconds": {"type": "number", "minimum": 0},
                        "padding_seconds": {
                            "type": "number",
                            "default": 0.2,
                            "description": "Extra audio bracketing each cut for natural breath beats.",
                        },
                    },
                },
            },
            "output_dir": {"type": "string"},
            "output_format": {
                "type": "string",
                "enum": ["wav", "mp3", "flac"],
                "default": "wav",
            },
            "sample_rate": {"type": "integer", "default": 44100},
            "channels": {"type": "integer", "default": 1, "minimum": 1, "maximum": 2},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=50, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=0)
    idempotency_key_fields = ["source_audio", "clips", "output_format", "sample_rate", "channels"]
    side_effects = ["writes audio files to output_dir"]
    user_visible_verification = ["Listen to each clip: should be clean and naturally bounded"]

    def get_status(self) -> ToolStatus:
        if FFMPEG and Path(FFMPEG).exists():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0  # Local ffmpeg — free.

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        source_audio = Path(inputs["source_audio"])
        if not source_audio.exists():
            return ToolResult(success=False, error=f"source_audio not found: {source_audio}")

        clips = inputs["clips"]
        if not clips:
            return ToolResult(success=False, error="no clips provided")

        output_dir = Path(inputs.get("output_dir") or source_audio.parent / "preacher_clips")
        output_dir.mkdir(parents=True, exist_ok=True)
        out_format = inputs.get("output_format", "wav")
        sample_rate = int(inputs.get("sample_rate", 44100))
        channels = int(inputs.get("channels", 1))

        saved: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for clip in clips:
            label = clip["label"]
            start_s = max(0.0, float(clip["start_seconds"]) - float(clip.get("padding_seconds", 0.2)))
            end_s = float(clip["end_seconds"]) + float(clip.get("padding_seconds", 0.2))
            duration = max(0.1, end_s - start_s)

            out_path = output_dir / f"{label}.{out_format}"
            cmd = [
                FFMPEG,
                "-y",
                "-ss", f"{start_s:.3f}",
                "-t", f"{duration:.3f}",
                "-i", str(source_audio),
                "-vn",
                "-ar", str(sample_rate),
                "-ac", str(channels),
                str(out_path),
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if proc.returncode != 0:
                    failures.append({"label": label, "error": proc.stderr.strip()[-500:]})
                    continue
                saved.append({
                    "label": label,
                    "path": str(out_path),
                    "start_seconds": round(start_s, 3),
                    "end_seconds": round(end_s, 3),
                    "duration_seconds": round(duration, 3),
                })
            except subprocess.TimeoutExpired:
                failures.append({"label": label, "error": "ffmpeg timeout"})
            except Exception as e:
                failures.append({"label": label, "error": f"{type(e).__name__}: {e}"})

        if not saved:
            return ToolResult(
                success=False,
                error=f"all clip extractions failed: {failures}",
            )

        total_duration = sum(c["duration_seconds"] for c in saved)
        return ToolResult(
            success=success_flag(saved, failures),
            data={
                "source_audio": str(source_audio),
                "clip_count": len(saved),
                "clips": saved,
                "failures": failures,
                "total_clip_duration_seconds": round(total_duration, 3),
            },
            artifacts=[c["path"] for c in saved],
            cost_usd=0.0,
            duration_seconds=round(time.time() - start, 2),
        )


def success_flag(saved: list, failures: list) -> bool:
    """Partial success policy: if any clips landed AND no fatal failures, succeed."""
    return bool(saved) and not failures
