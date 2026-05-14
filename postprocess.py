#!/usr/bin/env python3
"""
Post-process audiobook MP3s: trim trailing silence before upload.

Usage:
    python postprocess.py <mp3_path> [--out <output_path>] [--threshold-db -40] [--min-duration 0.5]

Returns trimmed file at --out (default: <name>_trimmed.mp3).
Does NOT modify the original.
"""

import argparse
import re
import shutil
import subprocess
from pathlib import Path

# Resolve ffmpeg - not on system PATH in conda envs on WSL
_FFMPEG = shutil.which("ffmpeg") or next(
    (p for p in [
        "/home/binxu/miniforge3/envs/research/bin/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ] if Path(p).exists()), "ffmpeg"
)


def get_duration(path: Path) -> float:
    """Return audio duration in seconds."""
    r = subprocess.run([_FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
    if not m:
        raise ValueError(f"Could not determine duration of {path}")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def trim_trailing_silence(
    input_path: Path,
    output_path: Path,
    threshold_db: float = -40.0,
    min_silence_duration: float = 0.5,
) -> tuple[float, float]:
    """
    Trim trailing silence from an MP3 file.

    Strategy: reverse → remove leading silence → reverse back.
    This is the standard ffmpeg approach for trailing silence removal.

    Returns (dur_before, dur_after) in seconds.
    """
    dur_before = get_duration(input_path)

    # silenceremove filter applied after reversal removes what was trailing silence
    silence_filter = (
        f"areverse,"
        f"silenceremove=start_periods=1"
        f":start_duration={min_silence_duration}"
        f":start_threshold={threshold_db}dB,"
        f"areverse"
    )

    result = subprocess.run(
        [
            _FFMPEG, "-y", "-i", str(input_path),
            "-af", silence_filter,
            "-codec:a", "libmp3lame", "-b:a", "64k",
            str(output_path),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")

    dur_after = get_duration(output_path)
    return dur_before, dur_after


def main():
    parser = argparse.ArgumentParser(description="Trim trailing silence from audiobook MP3s")
    parser.add_argument("input", type=Path, help="Input MP3 file")
    parser.add_argument("--out", type=Path, default=None, help="Output path (default: <name>_trimmed.mp3)")
    parser.add_argument("--threshold-db", type=float, default=-40.0, help="Silence threshold in dB (default: -40)")
    parser.add_argument("--min-duration", type=float, default=0.5, help="Min silence duration to trim (default: 0.5s)")
    parser.add_argument("--dry-run", action="store_true", help="Detect only, don't write output")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"ERROR: {args.input} not found")
        return 1

    if args.out is None:
        args.out = args.input.with_stem(args.input.stem + "_trimmed")

    if args.dry_run:
        dur = get_duration(args.input)
        print(f"{args.input.name}: {dur:.2f}s (dry-run, no output written)")
        return 0

    print(f"Processing: {args.input.name}")
    before, after = trim_trailing_silence(
        args.input, args.out, args.threshold_db, args.min_duration
    )
    trimmed = before - after
    print(f"  Before: {before:.2f}s → After: {after:.2f}s (removed {trimmed:.2f}s)")
    print(f"  Saved to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
