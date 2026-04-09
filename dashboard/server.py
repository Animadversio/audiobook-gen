"""
Read-only audiobook dashboard server.

Serves a self-refreshing HTML page showing:
- Book title, voice settings, overall progress
- Per-segment table: title, chars, status, duration, YouTube link
- Error details for failed segments
- Auto-refresh every 30 seconds

Usage:
    python -m dashboard.server /tmp/audiobook/abc123_segments.json --port 8765

Then share via: ssh -R 80:localhost:8765 nokey@localhost.run
"""

import json
import http.server
import threading
import time
from pathlib import Path
from datetime import datetime


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "—"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _estimate_duration(chars: int, text: str) -> str:
    cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    ratio = cjk / max(len(text), 1)
    cpm = 420 if ratio > 0.3 else 900
    mins = chars / cpm
    if mins < 1:
        return f"~{int(mins*60)}s"
    return f"~{int(mins)}m"


STATUS_EMOJI = {
    "done": "✅",
    "uploading": "📤",
    "generating": "🔄",
    "pending": "⏳",
    "skipped": "🚫",
    "error": "❌",
}

STATUS_COLOR = {
    "done": "#d4edda",
    "uploading": "#cce5ff",
    "generating": "#fff3cd",
    "pending": "#f8f9fa",
    "skipped": "#e2e3e5",
    "error": "#f8d7da",
}


def _seg_status(seg: dict) -> str:
    if seg.get("skip"):
        return "skipped"
    if seg.get("error"):
        return "error"
    if seg.get("youtube_video_id"):
        return "done"
    if seg.get("generated_at"):
        return "uploading"
    return "pending"


def _compute_eta(segs: list) -> str:
    """
    Estimate remaining generation time based on completed segments.
    Uses: avg real_seconds_per_char from segments with generated_at timestamps.
    """
    # Collect (chars, real_elapsed) for completed segments
    timed = []
    prev_time = None
    for seg in segs:
        if seg.get("skip") or not seg.get("generated_at"):
            continue
        try:
            ts = datetime.fromisoformat(seg["generated_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        chars = seg.get("char_count", 0)
        if prev_time is not None:
            elapsed = (ts - prev_time).total_seconds()
            if elapsed > 0 and chars > 0:
                timed.append((chars, elapsed))
        prev_time = ts

    if not timed:
        return "—"

    total_chars = sum(c for c, _ in timed)
    total_secs = sum(s for _, s in timed)
    secs_per_char = total_secs / total_chars if total_chars else 0

    remaining_chars = sum(
        s.get("char_count", 0)
        for s in segs
        if not s.get("skip") and not s.get("generated_at")
    )

    if remaining_chars == 0:
        return "完成"

    eta_secs = remaining_chars * secs_per_char
    h, r = divmod(int(eta_secs), 3600)
    m = r // 60
    if h:
        return f"~{h}h{m:02d}m"
    return f"~{m}m"


def _read_chunk_progress(seg_mp3_path: Path) -> tuple[int, int] | None:
    """Read per-chunk progress from ._progress.json if it exists."""
    prog_file = seg_mp3_path.with_suffix("._progress.json")
    try:
        data = json.loads(prog_file.read_text())
        return data["chunk_done"], data["chunk_total"]
    except Exception:
        return None


def render_html(plan: dict, audio_dir: Path | None = None) -> str:
    segs = plan["segments"]
    book_title = plan.get("book_title", "未知")
    voice = plan.get("voice_prompt") or "unconditional"
    voice_ref = plan.get("voice_ref_audio") or "—"
    steps = plan.get("steps", "?")

    active_segs = [s for s in segs if not s.get("skip")]
    total = len(active_segs)
    done = len([s for s in segs if s.get("youtube_video_id")])
    generated = len([s for s in segs if s.get("generated_at")])

    upload_pct = int(done / total * 100) if total else 0
    gen_pct = int(generated / total * 100) if total else 0
    # upload as % of the generated portion (for inner dark bar)
    upload_inner_pct = int(done / generated * 100) if generated else 0
    eta = _compute_eta(segs)

    rows = []
    for seg in segs:
        idx = seg["seg_index"]
        title = seg["title"]
        chars = seg["char_count"]
        status = _seg_status(seg)
        emoji = STATUS_EMOJI[status]
        color = STATUS_COLOR[status]
        dur_actual = _fmt_duration(seg.get("audio_duration_sec"))
        dur_est = _estimate_duration(chars, seg.get("text", ""))
        flags = " ".join(f'<span class="badge">⚠️ {f}</span>' for f in seg.get("flags", []))
        yt_id = seg.get("youtube_video_id")
        yt_link = (f'<a href="https://youtu.be/{yt_id}" target="_blank">▶ 播放</a>'
                   if yt_id else "—")
        split_ids = ", ".join(seg.get("epub_split_ids", [])[:2])

        # Per-segment mini progress bar with chunk counts
        if seg.get("skip"):
            seg_bar = '<span style="color:#999">—</span>'
        elif seg.get("generated_at"):
            seg_bar = '<span style="color:#28a745;font-weight:bold">✓ 完成</span>'
        else:
            # Try to read live chunk progress
            chunk_prog = None
            if audio_dir:
                mp3 = audio_dir / f"seg{idx:04d}.mp3"
                chunk_prog = _read_chunk_progress(mp3)
            if chunk_prog:
                cdone, ctotal = chunk_prog
                pct = int(cdone / ctotal * 100) if ctotal else 0
                seg_bar = (
                    f'<div style="display:flex;align-items:center;gap:6px;min-width:130px">'
                    f'<div style="flex:1;background:#ddd;border-radius:4px;height:8px">'
                    f'<div style="width:{pct}%;background:#007bff;height:100%;border-radius:4px"></div>'
                    f'</div>'
                    f'<span style="font-size:0.8em;color:#555;white-space:nowrap">{cdone}/{ctotal}</span>'
                    f'</div>'
                )
            else:
                seg_bar = '<span style="color:#aaa;font-size:0.85em">等待中</span>'

        # ETA for this segment from generated_at
        gen_at = seg.get("generated_at")
        if gen_at:
            try:
                ts = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
                seg_eta = ts.strftime("%H:%M")
            except Exception:
                seg_eta = "—"
        else:
            seg_eta = "—"

        rows.append(f"""
        <tr style="background:{color}">
          <td>{idx:02d}</td>
          <td>{title} {flags}</td>
          <td>{chars:,}</td>
          <td>{seg_bar}</td>
          <td>{dur_est}</td>
          <td>{dur_actual}</td>
          <td>{seg_eta}</td>
          <td>{emoji} {status}</td>
          <td>{yt_link}</td>
        </tr>""")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="30">
  <title>《{book_title}》有声书进度</title>
  <style>
    body {{ font-family: -apple-system, sans-serif; max-width: 1200px; margin: 2em auto; padding: 0 1em; }}
    h1 {{ color: #333; }}
    .meta {{ color: #666; margin-bottom: 1.5em; }}
    .progress-wrap {{ margin: 0.5em 0 1.5em; }}
    .progress-label {{ font-size: 0.85em; color: #555; margin-bottom: 3px; }}
    .progress-bar {{ background: #eee; border-radius: 8px; height: 16px; }}
    .progress-fill-green {{ background: #28a745; height: 100%; border-radius: 8px; transition: width 0.5s; }}
    .stats {{ display: flex; gap: 2em; margin: 1em 0 1.5em; flex-wrap: wrap; }}
    .stat {{ text-align: center; }}
    .stat-num {{ font-size: 2em; font-weight: bold; color: #28a745; }}
    .stat-label {{ color: #666; font-size: 0.9em; }}
    .eta-box {{ display:inline-block; background:#fff3cd; border:1px solid #ffc107; border-radius:6px; padding:4px 12px; font-weight:bold; color:#856404; margin-left:1em; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
    th {{ background: #343a40; color: white; padding: 8px 12px; text-align: left; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #dee2e6; vertical-align:middle; }}
    .badge {{ background: #ffc107; color: #000; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; }}
    a {{ color: #007bff; text-decoration: none; }}
    .refresh {{ color: #999; font-size: 0.8em; text-align: right; margin-top: 1em; }}
  </style>
</head>
<body>
  <h1>📖 《{book_title}》有声书</h1>
  <div class="meta">
    声音：{voice} &nbsp;|&nbsp; 参考音频：{voice_ref} &nbsp;|&nbsp; 步数：{steps}
  </div>

  <div class="progress-wrap">
    <div class="progress-label" style="display:flex;justify-content:space-between;align-items:center">
      <span>进度：{generated}/{total} 段已生成 ({gen_pct}%) &nbsp;·&nbsp; {done} 已上传</span>
      <span class="eta-box">⏱ ETA {eta}</span>
    </div>
    <div class="progress-bar" style="position:relative;overflow:hidden">
      <div style="width:{gen_pct}%;background:#28a745;height:100%;border-radius:8px;position:relative">
        <div style="width:{upload_inner_pct}%;background:#155724;height:100%;border-radius:8px 0 0 8px;position:absolute;top:0;left:0"></div>
      </div>
    </div>
  </div>

  <div class="stats">
    <div class="stat"><div class="stat-num">{generated}</div><div class="stat-label">已生成</div></div>
    <div class="stat"><div class="stat-num">{done}</div><div class="stat-label">已上传</div></div>
    <div class="stat"><div class="stat-num">{total - generated}</div><div class="stat-label">等待中</div></div>
    <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">总段落</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>标题</th><th>字数</th><th>进度</th><th>预估时长</th>
        <th>实际时长</th><th>完成时间</th><th>状态</th><th>YouTube</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>

  <div class="refresh">最后更新：{now} &nbsp;·&nbsp; 每30秒自动刷新</div>
</body>
</html>"""


def generate_static_html(segments_file: Path) -> Path:
    """Generate a static HTML file alongside the segments JSON."""
    plan = json.loads(segments_file.read_text())
    html = render_html(plan)
    out = segments_file.with_suffix("_dashboard.html")
    out.write_text(html, encoding="utf-8")
    return out


def start_server(segments_file: Path, port: int = 8765):
    """Start a live-updating HTTP server for the dashboard."""
    segments_file = Path(segments_file)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            plan = json.loads(segments_file.read_text())
            html = render_html(plan, audio_dir=segments_file.parent / "audio").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, fmt, *args):
            pass  # suppress access logs

    server = http.server.HTTPServer(("", port), Handler)
    print(f"Dashboard: http://localhost:{port}")
    print("Share publicly: ssh -R 80:localhost:{port} nokey@localhost.run")
    server.serve_forever()
