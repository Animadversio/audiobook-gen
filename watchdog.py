#!/usr/bin/env python3
"""
Watchdog for audiobook generation.

Monitors:
  - Generation PID (alive or dead)
  - Per-segment chunk progress (._progress.json files) — finer granularity than chapters
  - GPU / CUDA usage via nvidia-smi
  - Dashboard server PID

On detecting a stall (no new chunk progress for STALL_SECS) or dead process:
  - Sends an IPC alert to Discord so the agent can intervene
  - Does NOT auto-restart — failures are bugs that need human diagnosis

Usage:
  python watchdog.py \\
    --book-id 2c3a6054 \\
    --library /path/to/library \\
    --gen-pid 12345 \\
    --dash-pid 67890 \\
    --ipc /path/to/ipc \\
    --jid discord:channel_id \\
    [--stall-secs 300]
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


POLL_SECS = 30        # how often to check
DEFAULT_STALL_SECS = 300  # 5 min without chunk progress → stall


def ipc_send(ipc_dir: str | None, jid: str | None, text: str):
    if not ipc_dir or not jid:
        print(f"[WATCHDOG] {text}", flush=True)
        return
    msg = {"chatJid": jid, "text": text, "wakeAgent": True}
    ts = int(time.time() * 1000)
    path = Path(ipc_dir) / f"msg_{ts}.json"
    path.write_text(json.dumps(msg, ensure_ascii=False), encoding='utf-8')
    time.sleep(0.05)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def gpu_usage() -> str:
    """Return brief GPU status string, or 'n/a' if nvidia-smi not available."""
    # nvidia-smi may not be on PATH in WSL — try common locations
    nvidia_smi = (
        "nvidia-smi" if subprocess.run(["which", "nvidia-smi"], capture_output=True).returncode == 0
        else "/usr/lib/wsl/lib/nvidia-smi"
    )
    try:
        out = subprocess.check_output(
            [nvidia_smi, "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            timeout=5, text=True
        ).strip()
        util, mem_used, mem_total = [x.strip() for x in out.split(",")]
        return f"GPU {util}% | VRAM {mem_used}/{mem_total} MiB"
    except Exception:
        return "n/a"


def latest_chunk_progress(audio_dir: Path) -> tuple[str, int, int]:
    """
    Scan all ._progress.json files, return the most recently modified one.
    Returns (seg_id, chunk_done, chunk_total) or ("", 0, 0) if none.
    """
    best_mtime = 0.0
    best = ("", 0, 0)
    for f in audio_dir.glob("seg*._progress.json"):
        mtime = f.stat().st_mtime
        if mtime > best_mtime:
            try:
                data = json.loads(f.read_text())
                best = (f.stem.replace("._progress", ""), data["chunk_done"], data["chunk_total"])
                best_mtime = mtime
            except Exception:
                pass
    return best, best_mtime


def count_completed(segments_file: Path) -> int:
    try:
        plan = json.loads(segments_file.read_text())
        return sum(1 for s in plan["segments"] if s.get("generated_at") and not s.get("skip"))
    except Exception:
        return -1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-id", required=True)
    parser.add_argument("--library", required=True)
    parser.add_argument("--gen-pid", type=int, required=True)
    parser.add_argument("--dash-pid", type=int, default=None)
    parser.add_argument("--ipc", default=None)
    parser.add_argument("--jid", default=None)
    parser.add_argument("--stall-secs", type=int, default=DEFAULT_STALL_SECS)
    args = parser.parse_args()

    library = Path(args.library)
    book_dir = library / args.book_id
    audio_dir = book_dir / "audio"
    segments_file = book_dir / "segments.json"

    print(f"[WATCHDOG] Starting — gen PID={args.gen_pid}, poll={POLL_SECS}s, stall_threshold={args.stall_secs}s", flush=True)

    last_progress_time = time.time()
    last_progress_key = None   # (seg_id, chunk_done)
    alerted = False

    while True:
        time.sleep(POLL_SECS)

        gen_alive = pid_alive(args.gen_pid)
        dash_alive = args.dash_pid is None or pid_alive(args.dash_pid)

        (seg_id, chunk_done, chunk_total), prog_mtime = latest_chunk_progress(audio_dir)
        progress_key = (seg_id, chunk_done)

        if progress_key != last_progress_key and prog_mtime > 0:
            # New chunk completed — reset stall clock
            last_progress_key = progress_key
            last_progress_time = time.time()
            completed = count_completed(segments_file)
            gpu = gpu_usage()
            print(
                f"[WATCHDOG] chunk progress: {seg_id} {chunk_done}/{chunk_total} | "
                f"chapters done={completed} | {gpu}",
                flush=True,
            )
            alerted = False  # reset alert state on progress

        elapsed_since_progress = time.time() - last_progress_time

        if not gen_alive:
            if not alerted:
                alerted = True
                gpu = gpu_usage()
                completed = count_completed(segments_file)
                ipc_send(
                    args.ipc, args.jid,
                    f"⚠️ **[Watchdog]** 生成进程已退出 (PID {args.gen_pid})\n"
                    f"已完成章节: {completed} | {gpu}\n"
                    f"最后进度: {seg_id} chunk {chunk_done}/{chunk_total}\n"
                    f"请查看日志并重启生成。"
                )
            print(f"[WATCHDOG] Generator dead. Exiting watchdog.", flush=True)
            sys.exit(0)

        if elapsed_since_progress > args.stall_secs and not alerted:
            alerted = True
            gpu = gpu_usage()
            completed = count_completed(segments_file)
            ipc_send(
                args.ipc, args.jid,
                f"⚠️ **[Watchdog]** 生成疑似卡住！{elapsed_since_progress/60:.1f}分钟没有新chunk进度\n"
                f"最后进度: {seg_id} chunk {chunk_done}/{chunk_total}\n"
                f"已完成章节: {completed} | {gpu}\n"
                f"请检查生成日志。"
            )

        if not dash_alive and args.dash_pid:
            ipc_send(
                args.ipc, args.jid,
                f"⚠️ **[Watchdog]** Dashboard进程已退出 (PID {args.dash_pid})"
            )
            args.dash_pid = None  # only alert once


if __name__ == "__main__":
    main()
