#!/usr/bin/env python3
"""
Audiobook Generator — Main CLI

Usage:
    # Full pipeline (Discord confirmation at each pause point)
    python audiobook_gen.py run book.epub \
        --jid dc:CHANNEL_ID \
        --ipc /path/to/ipc \
        --library ~/audiobook-library \
        --playlist YOUTUBE_PLAYLIST_ID \
        --steps 10

    # Individual phases
    python audiobook_gen.py parse book.epub
    python audiobook_gen.py generate a3f8c12e --steps 10
    python audiobook_gen.py upload a3f8c12e --playlist PL...
    python audiobook_gen.py dashboard a3f8c12e --port 8765
    python audiobook_gen.py list

Library root priority:
    --library arg  >  AUDIOBOOK_LIBRARY env var  >  ~/.local/share/audiobook-gen
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from library import Library, resolve_library_root


# ──────────────────────────────────────────────
# IPC / Discord messaging
# ──────────────────────────────────────────────

def ipc_send(ipc_dir: str | None, jid: str | None, text: str):
    """Write an IPC message so NanoClaw delivers it to Discord."""
    if not ipc_dir or not jid:
        print(f"[MSG] {text}")
        return
    msg = {"type": "message", "chatJid": jid, "text": text}
    ts = int(time.time() * 1000)
    ipc_path = Path(ipc_dir) / f"msg_{ts}.json"
    ipc_path.write_text(json.dumps(msg, ensure_ascii=False), encoding='utf-8')
    time.sleep(0.05)  # avoid filename collision on fast machines


# ──────────────────────────────────────────────
# Segment plan I/O
# ──────────────────────────────────────────────

def load_plan(paths) -> dict:
    return json.loads(paths.segments_file.read_text())


def save_plan(paths, plan: dict):
    # Atomic write: write to temp file then rename to avoid corruption on crash.
    # Preserve youtube_video_id / youtube_uploaded_at written by the upload watcher
    # (the generator doesn't own those fields but must not clobber them).
    if paths.segments_file.exists():
        try:
            existing = json.loads(paths.segments_file.read_text(encoding='utf-8'))
            yt_by_idx = {
                s["seg_index"]: {
                    "youtube_video_id": s.get("youtube_video_id"),
                    "youtube_uploaded_at": s.get("youtube_uploaded_at"),
                }
                for s in existing.get("segments", [])
                if s.get("youtube_video_id")
            }
            for seg in plan.get("segments", []):
                idx = seg.get("seg_index")
                if idx in yt_by_idx and not seg.get("youtube_video_id"):
                    seg.update(yt_by_idx[idx])
            if not plan.get("youtube_playlist_id") and existing.get("youtube_playlist_id"):
                plan["youtube_playlist_id"] = existing["youtube_playlist_id"]
        except Exception:
            pass
    tmp = paths.segments_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(paths.segments_file)


# ──────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────

def estimate_duration(char_count: int, text: str) -> str:
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    ratio = cjk / max(len(text), 1)
    cpm = 420 if ratio > 0.3 else 900
    mins = char_count / cpm
    if mins < 1:
        return f"~{int(mins*60)}sec"
    return f"~{int(mins)}min"


def format_segment_table(plan: dict) -> str:
    book_title = plan.get("book_title", "未知书名")
    book_id = plan.get("book_id", "?")
    segs = plan["segments"]
    lines = [
        f"**《{book_title}》分段预览** (book_id: `{book_id}`) — 共{len(segs)}段\n",
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
        '\n回复 **"确认"** 继续，或给出修改指令：\n'
        '• "合并第3和第4段"\n'
        '• "跳过第15段"\n'
        '• "第2段改名为逐梦之旅"'
    )
    return "\n".join(lines)


def format_voice_prompt_msg() -> str:
    return (
        "**音色设置**（可选）：\n\n"
        "A) 整本书统一音色 — 请描述，如 `平静温和的女声播音员`\n"
        "B) 发送参考音频文件（.wav/.mp3）到此对话，用于声音克隆\n"
        "C) 无条件生成（跳过）\n\n"
        "请回复 A/B/C 或直接输入音色描述："
    )


# ──────────────────────────────────────────────
# Resolve book_id arg (prefix or full)
# ──────────────────────────────────────────────

def resolve_book(lib: Library, book_id_arg: str):
    """Find BookPaths from a book_id prefix or title substring."""
    match = lib.find_book(book_id_arg)
    if not match:
        print(f"ERROR: No book found matching '{book_id_arg}'", file=sys.stderr)
        lib.print_library()
        sys.exit(1)
    return lib.book_paths(match["book_id"]), match


# ──────────────────────────────────────────────
# Parse phase
# ──────────────────────────────────────────────

def cmd_parse(args):
    """Parse EPUB/MD → register in library → send segment table to Discord."""
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    lib = Library(resolve_library_root(getattr(args, "library", None)))

    suffix = input_path.suffix.lower()
    if suffix == ".epub":
        from parsers.epub import extract_chapters_epub, compute_book_id, get_epub_title
        chapters = extract_chapters_epub(input_path)
        book_id = compute_book_id(input_path)
        book_title = get_epub_title(input_path)
    elif suffix in (".md", ".markdown"):
        from parsers.markdown import extract_chapters_markdown, compute_book_id
        chapters = extract_chapters_markdown(
            input_path, heading_level=getattr(args, "heading_level", 1)
        )
        book_id = compute_book_id(input_path)
    else:
        print(f"ERROR: Unsupported format: {suffix}", file=sys.stderr)
        sys.exit(1)

    # book_title set above from epub/markdown metadata; fallback for markdown:
    if suffix in (".md", ".markdown"):
        book_title = input_path.stem

    # Register in library (creates folder structure)
    paths = lib.register_book(
        book_id=book_id,
        title=book_title,
        input_file=str(input_path),
        youtube_playlist=getattr(args, "playlist", None),
    )

    # Copy source file to library (optional archival)
    lib.copy_source(book_id, input_path)

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
        "library_root": str(lib.root),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "voice_prompt": None,
        "voice_ref_audio": None,
        "steps": getattr(args, "steps", 10),
        "confirmed": False,
        "voice_confirmed": False,
        "segments": segments,
    }

    save_plan(paths, plan)
    lib.update_book_progress(book_id, plan)

    print(f"Book registered: {book_id}")
    print(f"  Library: {lib.root / book_id}/")
    print(f"  Segments: {paths.segments_file}")
    print(f"  Parsed {len(segments)} segments")

    # Send table to Discord
    table = format_segment_table(plan)
    ipc_send(getattr(args, "ipc", None), getattr(args, "jid", None), table)

    return book_id


# ──────────────────────────────────────────────
# Generate phase
# ──────────────────────────────────────────────

def cmd_generate(args):
    """Generate audio for all confirmed, non-skipped segments."""
    lib = Library(resolve_library_root(getattr(args, "library", None)))
    paths, _ = resolve_book(lib, args.book_id)
    plan = load_plan(paths)

    if not plan.get("confirmed"):
        print("ERROR: Segments not confirmed. Confirm via Discord or set confirmed=true in segments.json.",
              file=sys.stderr)
        sys.exit(1)

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
        mp3_path = paths.seg_mp3(idx)
        json_path = paths.seg_json(idx)

        # Idempotency: skip if already generated with same text
        if mp3_path.exists() and json_path.exists():
            try:
                raw = json_path.read_text(encoding='utf-8')
                existing = json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, OSError):
                existing = {}
            if existing.get("text_hash") == seg["text_hash"]:
                print(f"[{idx:03d}] Already generated (hash match), skipping.")
                continue
            if not existing and mp3_path.exists():
                # Empty/corrupt JSON but MP3 exists — recover JSON from MP3 metadata
                import subprocess as _sp
                r = _sp.run([
                    str(next(Path(p) for p in [
                        "/home/binxu/miniforge3/envs/research/bin/ffprobe", "/usr/bin/ffprobe"
                    ] if Path(p).exists())),
                    "-v","quiet","-show_entries","format=duration",
                    "-of","default=noprint_wrappers=1:nokey=1", str(mp3_path)
                ], capture_output=True, text=True)
                if r.returncode == 0:
                    recovered_duration = float(r.stdout.strip())
                    from datetime import datetime as _dt, timezone as _tz
                    meta = {k: v for k, v in seg.items()}
                    meta["steps"] = steps
                    meta["mp3_path"] = str(mp3_path)
                    meta["generated_at"] = _dt.fromtimestamp(
                        mp3_path.stat().st_mtime, tz=_tz.utc).isoformat()
                    meta["audio_duration_sec"] = recovered_duration
                    json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
                    seg["generated_at"] = meta["generated_at"]
                    seg["audio_duration_sec"] = recovered_duration
                    save_plan(paths, plan)
                    print(f"[{idx:03d}] Recovered JSON from MP3 ({recovered_duration:.0f}s), skipping regeneration.")
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
        save_plan(paths, plan)
        lib.update_book_progress(book_id, plan)

        # Write paired metadata JSON
        meta = {k: v for k, v in seg.items()}
        meta["steps"] = steps
        meta["mp3_path"] = str(mp3_path)
        json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')

        m, s = divmod(int(duration_sec), 60)
        ipc_send(ipc, jid, f"✅ [{i+1}/{total}] **{seg['title']}** — {m}分{s:02d}秒")


# ──────────────────────────────────────────────
# Upload phase
# ──────────────────────────────────────────────

def cmd_upload(args):
    """Upload generated MP3s to YouTube with deduplication."""
    lib = Library(resolve_library_root(getattr(args, "library", None)))
    paths, book_meta = resolve_book(lib, args.book_id)
    plan = load_plan(paths)

    playlist_id = getattr(args, "playlist", None) or book_meta.get("youtube_playlist")
    if not playlist_id:
        print("ERROR: No playlist ID. Use --playlist or set it during parse.", file=sys.stderr)
        sys.exit(1)

    jid = getattr(args, "jid", None)
    ipc = getattr(args, "ipc", None)

    from uploaders.youtube import YouTubeUploader
    uploader = YouTubeUploader(playlist_id=playlist_id)

    for seg in plan["segments"]:
        if seg.get("skip"):
            continue
        idx = seg["seg_index"]
        mp3_path = paths.seg_mp3(idx)
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
        save_plan(paths, plan)
        lib.update_book_progress(plan["book_id"], plan)
        ipc_send(ipc, jid, f"📤 **{seg['title']}** → https://youtu.be/{result['video_id']}")

    # Update playlist in library index
    if playlist_id:
        lib.register_book(plan["book_id"], plan["book_title"], plan["input_file"], playlist_id)


# ──────────────────────────────────────────────
# Dashboard
# ──────────────────────────────────────────────

def cmd_dashboard(args):
    """Start read-only dashboard server. Without book_id shows library index."""
    lib = Library(resolve_library_root(getattr(args, "library", None)))
    book_id = getattr(args, "book_id", None) or None
    if book_id:
        # Validate book exists
        resolve_book(lib, book_id)
    from dashboard.server import start_server
    start_server(
        library_root=lib.root,
        port=getattr(args, "port", 8765),
        single_book_id=book_id,
    )


# ──────────────────────────────────────────────
# List library
# ──────────────────────────────────────────────

def cmd_list(args):
    """List all books in the library."""
    lib = Library(resolve_library_root(getattr(args, "library", None)))
    lib.print_library()


# ──────────────────────────────────────────────
# Full pipeline
# ──────────────────────────────────────────────

def cmd_run(args):
    """Full pipeline: parse → generate → upload (auto-confirm when no Discord)."""
    book_id = cmd_parse(args)
    lib = Library(resolve_library_root(getattr(args, "library", None)))
    paths = lib.book_paths(book_id)
    plan = load_plan(paths)

    # Auto-confirm if not using Discord
    if not getattr(args, "jid", None):
        plan["confirmed"] = True
        plan["voice_confirmed"] = True
        if getattr(args, "voice", None):
            plan["voice_prompt"] = args.voice
        if getattr(args, "voice_ref_audio", None):
            plan["voice_ref_audio"] = args.voice_ref_audio
        save_plan(paths, plan)

    args.book_id = book_id
    cmd_generate(args)
    if getattr(args, "playlist", None):
        cmd_upload(args)


# ──────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Audiobook Generator using VoxCPM2")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, book_id=False):
        p.add_argument("--library", help="Library root directory (or set AUDIOBOOK_LIBRARY env var)")
        p.add_argument("--jid", help="Discord channel JID for progress messages")
        p.add_argument("--ipc", help="NanoClaw IPC directory")
        if book_id:
            p.add_argument("book_id", help="book_id prefix or title substring")

    # parse
    p_parse = sub.add_parser("parse", help="Parse EPUB/MD and register in library")
    p_parse.add_argument("input", help="Input EPUB or Markdown file")
    p_parse.add_argument("--heading-level", type=int, default=1)
    p_parse.add_argument("--playlist", help="YouTube playlist ID to associate")
    p_parse.add_argument("--steps", type=int, default=10)
    add_common(p_parse)
    p_parse.set_defaults(func=cmd_parse)

    # generate
    p_gen = sub.add_parser("generate", help="Generate audio for confirmed segments")
    p_gen.add_argument("--steps", type=int, default=10)
    add_common(p_gen, book_id=True)
    p_gen.set_defaults(func=cmd_generate)

    # upload
    p_up = sub.add_parser("upload", help="Upload generated MP3s to YouTube")
    p_up.add_argument("--playlist", help="YouTube playlist ID")
    add_common(p_up, book_id=True)
    p_up.set_defaults(func=cmd_upload)

    # dashboard
    p_dash = sub.add_parser("dashboard", help="Start read-only dashboard server (all books or one)")
    p_dash.add_argument("book_id", nargs="?", default=None, help="book_id prefix (optional; omit to show library index)")
    p_dash.add_argument("--port", type=int, default=8765)
    add_common(p_dash, book_id=False)
    p_dash.set_defaults(func=cmd_dashboard)

    # list
    p_list = sub.add_parser("list", help="List all books in the library")
    add_common(p_list)
    p_list.set_defaults(func=cmd_list)

    # run
    p_run = sub.add_parser("run", help="Full pipeline (parse + generate + upload)")
    p_run.add_argument("input", help="Input EPUB or Markdown file")
    p_run.add_argument("--steps", type=int, default=10)
    p_run.add_argument("--playlist", help="YouTube playlist ID")
    p_run.add_argument("--voice", help="Voice description")
    p_run.add_argument("--voice-ref", dest="voice_ref_audio", help="Reference audio for voice cloning")
    add_common(p_run)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
