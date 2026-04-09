#!/usr/bin/env python3
"""
Audiobook Generator — Main CLI

Usage:
    # Full pipeline (Discord confirmation at each pause point)
    python audiobook_gen.py run book.epub \
        --jid dc:CHANNEL_ID \
        --ipc /path/to/ipc \
        --outdir /tmp/audiobook \
        --playlist YOUTUBE_PLAYLIST_ID \
        --steps 10

    # Individual phases
    python audiobook_gen.py parse book.epub --outdir /tmp/audiobook
    python audiobook_gen.py generate /tmp/audiobook/abc123_segments.json --steps 10
    python audiobook_gen.py upload /tmp/audiobook/abc123_segments.json --playlist PL...
    python audiobook_gen.py dashboard /tmp/audiobook/abc123_segments.json --port 8765
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

# ──────────────────────────────────────────────
# IPC / Discord messaging
# ──────────────────────────────────────────────

def ipc_send(ipc_dir: str | None, jid: str | None, text: str):
    """Write an IPC message so NanoClaw delivers it to Discord."""
    if not ipc_dir or not jid:
        print(f"[MSG] {text}")
        return
    msg = {"chatJid": jid, "text": text}
    ts = int(time.time() * 1000)
    ipc_path = Path(ipc_dir) / f"msg_{ts}.json"
    ipc_path.write_text(json.dumps(msg, ensure_ascii=False))


# ──────────────────────────────────────────────
# Segment plan I/O
# ──────────────────────────────────────────────

def load_segments(segments_file: Path) -> dict:
    return json.loads(segments_file.read_text())


def save_segments(segments_file: Path, plan: dict):
    segments_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────

def estimate_duration(char_count: int, text: str) -> str:
    """Rough TTS duration estimate."""
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    ratio = cjk / max(len(text), 1)
    chars_per_min = 420 if ratio > 0.3 else 900
    mins = char_count / chars_per_min
    if mins < 1:
        return f"~{int(mins*60)}sec"
    return f"~{int(mins)}min"


def format_segment_table(plan: dict) -> str:
    """Format the segmentation plan as a Discord-friendly markdown table."""
    book_title = plan.get("book_title", "未知书名")
    segs = plan["segments"]
    lines = [
        f"**《{book_title}》分段预览 — 共{len(segs)}段**\n",
        "```",
        f"{'#':>4}  {'标题':<35}  {'字数':>6}  {'预估时长':>9}  状态",
        "─" * 75,
    ]
    for s in segs:
        idx = s["seg_index"]
        title = s["title"][:33]
        chars = s["char_count"]
        dur = estimate_duration(chars, s["text"])
        flags = s.get("flags", [])
        status = "✅" if not flags else ("⚠️ 太短" if "too_short" in flags else "⚠️ 太长")
        lines.append(f"{idx:>4}  {title:<35}  {chars:>6}  {dur:>9}  {status}")
    lines.append("```")
    lines.append(
        '\n回复 **"确认"** 继续，或说修改指令：\n'
        '• "合并第3和第4段"\n'
        '• "跳过第15段"\n'
        '• "第2段改名为逐梦之旅"'
    )
    return "\n".join(lines)


def format_voice_prompt(plan: dict) -> str:
    return (
        "**音色设置**（可选）：\n\n"
        "A) 整本书统一音色 — 请描述，如 `平静温和的女声播音员`\n"
        "B) 发送参考音频文件（.wav/.mp3）到此对话，用于声音克隆\n"
        "C) 按段落分别指定（回复后我会逐段询问）\n"
        "D) 无条件生成（跳过）\n\n"
        "请回复 A/B/C/D 或直接输入音色描述："
    )


# ──────────────────────────────────────────────
# Parse phase
# ──────────────────────────────────────────────

def cmd_parse(args):
    """Parse EPUB/MD → generate segments JSON → send table to Discord."""
    input_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    suffix = input_path.suffix.lower()
    if suffix == ".epub":
        from parsers.epub import extract_chapters_epub, compute_book_id
        chapters = extract_chapters_epub(input_path)
        book_id = compute_book_id(input_path)
    elif suffix in (".md", ".markdown"):
        from parsers.markdown import extract_chapters_markdown, compute_book_id
        chapters = extract_chapters_markdown(input_path, heading_level=getattr(args, "heading_level", 1))
        book_id = compute_book_id(input_path)
    else:
        print(f"Unsupported format: {suffix}", file=sys.stderr)
        sys.exit(1)

    book_title = input_path.stem

    # Build segment plan
    segments = []
    for i, ch in enumerate(chapters):
        text = ch["text"]
        text_hash = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
        segments.append({
            "seg_index": i + 1,
            "title": ch["title"],
            "text": text,
            "text_hash": text_hash,
            "char_count": ch["char_count"],
            "epub_split_ids": ch.get("epub_split_ids", []),
            "flags": ch.get("flags", []),
            "voice_prompt": None,
            "voice_ref_audio": None,
            "skip": False,
            "generated_at": None,
            "audio_duration_sec": None,
            "youtube_video_id": None,
            "youtube_uploaded_at": None,
        })

    plan = {
        "book_id": book_id,
        "book_title": book_title,
        "input_file": str(input_path),
        "outdir": str(outdir),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "voice_prompt": None,       # global voice (can be overridden per-segment)
        "voice_ref_audio": None,
        "steps": getattr(args, "steps", 10),
        "confirmed": False,
        "voice_confirmed": False,
        "segments": segments,
    }

    segments_file = outdir / f"{book_id}_segments.json"
    save_segments(segments_file, plan)
    print(f"Segments saved: {segments_file}")

    # Send table to Discord
    table = format_segment_table(plan)
    ipc_send(getattr(args, "ipc", None), getattr(args, "jid", None), table)

    print(f"\nParsed {len(segments)} segments. Waiting for user confirmation via Discord.")
    print(f"Segments file: {segments_file}")
    return str(segments_file)


# ──────────────────────────────────────────────
# Generate phase
# ──────────────────────────────────────────────

def cmd_generate(args):
    """Generate audio for all confirmed, non-skipped segments."""
    segments_file = Path(args.segments_file)
    plan = load_segments(segments_file)

    if not plan.get("confirmed"):
        print("ERROR: Segments not confirmed yet. Have user confirm via Discord first.", file=sys.stderr)
        sys.exit(1)

    outdir = Path(plan["outdir"])
    book_id = plan["book_id"]
    steps = getattr(args, "steps", None) or plan.get("steps", 10)
    jid = getattr(args, "jid", None)
    ipc = getattr(args, "ipc", None)

    from generator.voxcpm import AudiobookGenerator
    gen = AudiobookGenerator(steps=steps)

    segs_to_run = [s for s in plan["segments"] if not s.get("skip")]
    total = len(segs_to_run)

    for i, seg in enumerate(segs_to_run):
        idx = seg["seg_index"]
        mp3_path = outdir / f"{book_id}_seg{idx:04d}.mp3"
        json_path = outdir / f"{book_id}_seg{idx:04d}.json"

        # Idempotency check
        if mp3_path.exists() and json_path.exists():
            existing = json.loads(json_path.read_text())
            if existing.get("text_hash") == seg["text_hash"]:
                print(f"[{idx:03d}] Already generated (hash match), skipping.")
                continue

        voice_prompt = seg.get("voice_prompt") or plan.get("voice_prompt")
        voice_ref = seg.get("voice_ref_audio") or plan.get("voice_ref_audio")

        ipc_send(ipc, jid, f"🔄 [{i+1}/{total}] 生成 **{seg['title']}** ({seg['char_count']}字)...")

        try:
            duration_sec = gen.generate_segment(
                text=seg["text"],
                output_path=mp3_path,
                voice_prompt=voice_prompt,
                voice_ref_audio=voice_ref,
            )
        except Exception as e:
            ipc_send(ipc, jid, f"❌ 段落 {idx} 生成失败：{e}")
            continue

        # Update segment metadata
        seg["generated_at"] = datetime.now(timezone.utc).isoformat()
        seg["audio_duration_sec"] = duration_sec
        save_segments(segments_file, plan)

        # Write paired metadata JSON
        meta = {k: v for k, v in seg.items()}
        meta["steps"] = steps
        meta["mp3_path"] = str(mp3_path)
        json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))

        mins = int(duration_sec // 60)
        secs = int(duration_sec % 60)
        ipc_send(ipc, jid, f"✅ [{i+1}/{total}] **{seg['title']}** — {mins}分{secs}秒")


# ──────────────────────────────────────────────
# Upload phase
# ──────────────────────────────────────────────

def cmd_upload(args):
    """Upload generated MP3s to YouTube with deduplication."""
    segments_file = Path(args.segments_file)
    plan = load_segments(segments_file)
    outdir = Path(plan["outdir"])
    book_id = plan["book_id"]
    playlist_id = args.playlist
    jid = getattr(args, "jid", None)
    ipc = getattr(args, "ipc", None)

    from uploaders.youtube import YouTubeUploader
    uploader = YouTubeUploader(playlist_id=playlist_id)

    for seg in plan["segments"]:
        if seg.get("skip"):
            continue
        idx = seg["seg_index"]
        mp3_path = outdir / f"{book_id}_seg{idx:04d}.mp3"
        if not mp3_path.exists():
            print(f"[{idx:03d}] MP3 not found, skipping upload.")
            continue

        result = uploader.upload(
            mp3_path=mp3_path,
            title=f"《{plan['book_title']}》{seg['title']}",
            seg_meta=seg,
            book_title=plan["book_title"],
        )
        if result["skipped"]:
            print(f"[{idx:03d}] Already uploaded (hash match), skipping.")
            continue

        seg["youtube_video_id"] = result["video_id"]
        seg["youtube_uploaded_at"] = datetime.now(timezone.utc).isoformat()
        save_segments(segments_file, plan)
        ipc_send(ipc, jid, f"📤 上传完成：**{seg['title']}** → https://youtu.be/{result['video_id']}")


# ──────────────────────────────────────────────
# Dashboard phase
# ──────────────────────────────────────────────

def cmd_dashboard(args):
    """Start read-only dashboard HTTP server."""
    from dashboard.server import start_server
    start_server(
        segments_file=Path(args.segments_file),
        port=getattr(args, "port", 8765),
    )


# ──────────────────────────────────────────────
# Full pipeline (run)
# ──────────────────────────────────────────────

def cmd_run(args):
    """
    Full pipeline: parse → [Discord pause] → generate → upload.
    The 'confirmation' and 'voice selection' pauses are handled externally
    by the NanoClaw Discord skill — this script is called in two phases:
      Phase 1: parse (then skill waits for user confirmation)
      Phase 2: generate + upload (called after confirmation)
    """
    # For direct CLI use, run parse then generate (no Discord pause)
    segments_file = Path(cmd_parse(args))
    plan = load_segments(segments_file)

    # Auto-confirm if not using Discord
    if not args.jid:
        plan["confirmed"] = True
        plan["voice_confirmed"] = True
        if args.voice:
            plan["voice_prompt"] = args.voice
        save_segments(segments_file, plan)

    args.segments_file = str(segments_file)
    cmd_generate(args)

    if getattr(args, "playlist", None):
        cmd_upload(args)


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audiobook Generator using VoxCPM2")
    sub = parser.add_subparsers(dest="command", required=True)

    # Common args
    def add_common(p):
        p.add_argument("--jid", help="Discord channel JID for progress messages")
        p.add_argument("--ipc", help="NanoClaw IPC directory")
        p.add_argument("--outdir", default="/tmp/audiobook", help="Output directory")

    # parse
    p_parse = sub.add_parser("parse", help="Parse EPUB/MD and generate segment plan")
    p_parse.add_argument("input", help="Input EPUB or Markdown file")
    p_parse.add_argument("--heading-level", type=int, default=1, help="Markdown heading level to split on")
    add_common(p_parse)
    p_parse.set_defaults(func=cmd_parse)

    # generate
    p_gen = sub.add_parser("generate", help="Generate audio for confirmed segments")
    p_gen.add_argument("segments_file", help="Path to *_segments.json")
    p_gen.add_argument("--steps", type=int, default=10, help="VoxCPM2 diffusion steps")
    add_common(p_gen)
    p_gen.set_defaults(func=cmd_generate)

    # upload
    p_up = sub.add_parser("upload", help="Upload generated MP3s to YouTube")
    p_up.add_argument("segments_file", help="Path to *_segments.json")
    p_up.add_argument("--playlist", help="YouTube playlist ID")
    add_common(p_up)
    p_up.set_defaults(func=cmd_upload)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start read-only dashboard server")
    p_dash.add_argument("segments_file", help="Path to *_segments.json")
    p_dash.add_argument("--port", type=int, default=8765)
    p_dash.set_defaults(func=cmd_dashboard)

    # run (full pipeline)
    p_run = sub.add_parser("run", help="Full pipeline (parse + generate + upload)")
    p_run.add_argument("input", help="Input EPUB or Markdown file")
    p_run.add_argument("--steps", type=int, default=10)
    p_run.add_argument("--playlist", help="YouTube playlist ID")
    p_run.add_argument("--voice", help="Voice description (skip Discord voice selection)")
    p_run.add_argument("--voice-ref", dest="voice_ref_audio", help="Reference audio for voice cloning")
    add_common(p_run)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
