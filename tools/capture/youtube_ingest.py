"""YouTube sermon ingest tool — downloads audio, transcribes with Whisper, caches.

Pipeline contract:
  Input:  YouTube URL
  Output: dict with paths to cached audio + transcript + metadata, plus the
          source_media and transcript artifact data.

Cache layout (default `<repo_root>/.cache/youtube/<video_id>/`):
  audio.<ext>                 raw audio download from yt-dlp
  metadata.json               video metadata (title, channel, duration, etc.)
  transcript.whisper.json     faster-whisper output with word-level timestamps
  transcript.corrected.json   placeholder for future scripture/proper-noun pass

CLI usage:
  python -m tools.capture.youtube_ingest <youtube_url> [--cache-root <dir>] [--model <name>]
  python -m tools.capture.youtube_ingest <youtube_url> --metadata-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import imageio_ffmpeg
import yt_dlp
from faster_whisper import WhisperModel


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_ROOT = REPO_ROOT / ".cache" / "youtube"
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_COMPUTE_TYPE = "int8"
DEFAULT_DEVICE = "cpu"

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()


def _ydl_opts(out_dir: Path, audio_only: bool = True) -> dict:
    opts = {
        "ffmpeg_location": FFMPEG_PATH,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "outtmpl": str(out_dir / "audio.%(ext)s"),
    }
    if audio_only:
        opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "postprocessors": [],  # no conversion; keep original codec
        })
    return opts


def fetch_metadata(url: str) -> dict:
    """Fetch video metadata without downloading. Fast (~1-5 sec)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return _pack_metadata(info)


def _pack_metadata(info: dict) -> dict:
    return {
        "video_id": info.get("id"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "duration_seconds": info.get("duration"),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "description": info.get("description"),
        "webpage_url": info.get("webpage_url"),
        "thumbnail_url": info.get("thumbnail"),
    }


def download_audio(url: str, out_dir: Path) -> tuple[Path, dict]:
    """Download audio + return (audio_path, metadata)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with yt_dlp.YoutubeDL(_ydl_opts(out_dir)) as ydl:
        info = ydl.extract_info(url, download=True)
    # Find the downloaded audio file (extension varies by source format)
    audio_files = list(out_dir.glob("audio.*"))
    if not audio_files:
        raise RuntimeError(f"yt-dlp completed but no audio file found in {out_dir}")
    return audio_files[0], _pack_metadata(info)


def transcribe(
    audio_path: Path,
    model_name: str = DEFAULT_MODEL,
    device: str = DEFAULT_DEVICE,
    compute_type: str = DEFAULT_COMPUTE_TYPE,
) -> dict:
    """Run faster-whisper transcription with word-level timestamps + VAD filter."""
    print(f"  loading model: {model_name} (device={device}, compute={compute_type})")
    t0 = time.time()
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    t_load = time.time() - t0

    print(f"  transcribing audio: {audio_path.name}")
    t0 = time.time()
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )

    out_segments = []
    full_text_parts = []
    for seg in segments:
        seg_dict = {
            "id": seg.id,
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": [
                {"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3), "probability": round(w.probability, 4)}
                for w in (seg.words or [])
            ],
        }
        out_segments.append(seg_dict)
        full_text_parts.append(seg.text.strip())
        # Print progress every ~30s of audio processed
        if seg.id % 20 == 0:
            print(f"    ...processed through {seg.end:.0f}s of audio")

    t_transcribe = time.time() - t0
    print(f"  transcription complete: {len(out_segments)} segments in {t_transcribe:.1f}s")

    return {
        "model": model_name,
        "language": info.language,
        "language_probability": round(info.language_probability, 4),
        "duration_seconds": round(info.duration, 3),
        "load_time_seconds": round(t_load, 2),
        "transcribe_time_seconds": round(t_transcribe, 2),
        "text": " ".join(full_text_parts),
        "segments": out_segments,
    }


def ingest_youtube(
    url: str,
    cache_root: Path = DEFAULT_CACHE_ROOT,
    model_name: str = DEFAULT_MODEL,
    force: bool = False,
) -> dict:
    """Idempotent end-to-end ingest. Returns a dict with paths + artifact data."""
    cache_root.mkdir(parents=True, exist_ok=True)

    # Step 1: get metadata so we know the video_id
    print(f"[ingest] fetching metadata: {url}")
    metadata = fetch_metadata(url)
    video_id = metadata["video_id"]
    if not video_id:
        raise RuntimeError(f"could not resolve video_id from URL: {url}")

    video_dir = cache_root / video_id
    video_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = video_dir / "metadata.json"
    transcript_path = video_dir / "transcript.whisper.json"
    corrected_path = video_dir / "transcript.corrected.json"

    # Persist metadata
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))
    print(f"[ingest] video_id={video_id} title={metadata['title']!r}")
    print(f"[ingest] duration={metadata['duration_seconds']}s channel={metadata['channel']!r}")

    # Step 2: audio download (skip if cached)
    audio_files = list(video_dir.glob("audio.*"))
    if audio_files and not force:
        audio_path = audio_files[0]
        print(f"[ingest] audio cache hit: {audio_path.name}")
    else:
        print(f"[ingest] downloading audio...")
        audio_path, _ = download_audio(url, video_dir)
        print(f"[ingest] audio saved: {audio_path.name} ({audio_path.stat().st_size / 1e6:.1f} MB)")

    # Step 3: transcribe (skip if cached)
    if transcript_path.exists() and not force:
        print(f"[ingest] transcript cache hit: {transcript_path.name}")
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
    else:
        print(f"[ingest] transcribing...")
        transcript = transcribe(audio_path, model_name=model_name)
        transcript_path.write_text(json.dumps(transcript, indent=2, ensure_ascii=False))
        print(f"[ingest] transcript saved: {transcript_path.name}")

    # Step 4: correction pass — placeholder for now (just copy)
    if not corrected_path.exists() or force:
        corrected = dict(transcript)
        corrected["correction_status"] = "pending_v2_lexicon_pass"
        corrected_path.write_text(json.dumps(corrected, indent=2, ensure_ascii=False))

    return {
        "video_id": video_id,
        "video_dir": str(video_dir),
        "audio_path": str(audio_path),
        "metadata_path": str(metadata_path),
        "transcript_path": str(transcript_path),
        "corrected_transcript_path": str(corrected_path),
        "metadata": metadata,
        "transcript_summary": {
            "language": transcript.get("language"),
            "duration_seconds": transcript.get("duration_seconds"),
            "segment_count": len(transcript.get("segments", [])),
            "char_count": len(transcript.get("text", "")),
        },
    }


def _print_summary(result: dict) -> None:
    md = result["metadata"]
    ts = result["transcript_summary"]
    print("\n=== Ingest summary ===")
    print(f"  video_id:   {result['video_id']}")
    print(f"  title:      {md['title']}")
    print(f"  channel:    {md['channel']}")
    print(f"  duration:   {md['duration_seconds']}s")
    print(f"  language:   {ts['language']}")
    print(f"  segments:   {ts['segment_count']}")
    print(f"  characters: {ts['char_count']}")
    print(f"  cache dir:  {result['video_dir']}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest a YouTube sermon: download audio, transcribe, cache.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT, help="cache root directory")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="faster-whisper model name")
    parser.add_argument("--metadata-only", action="store_true", help="fetch metadata only; skip audio + transcription")
    parser.add_argument("--force", action="store_true", help="re-download and re-transcribe even if cached")
    args = parser.parse_args(argv)

    if args.metadata_only:
        md = fetch_metadata(args.url)
        print(json.dumps(md, indent=2, ensure_ascii=False))
        return 0

    result = ingest_youtube(args.url, cache_root=args.cache_root, model_name=args.model, force=args.force)
    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
