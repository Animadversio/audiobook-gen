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


def render_html(plan: dict) -> str:
    segs = plan["segments"]
    book_title = plan.get("book_title", "未知")
    voice = plan.get("voice_prompt") or "unconditional"
    voice_ref = plan.get("voice_ref_audio") or "—"
    steps = plan.get("steps", "?")

    total = len([s for s in segs if not s.get("skip")])
    done = len([s for s in segs if s.get("youtube_video_id")])
    generated = len([s for s in segs if s.get("generated_at") and not s.get("youtube_video_id")])

    progress_pct = int(done / total * 100) if total else 0

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

        rows.append(f"""
        <tr style="background:{color}">
          <td>{idx:02d}</td>
          <td>{title} {flags}</td>
          <td>{chars:,}</td>
          <td>{dur_est}</td>
          <td>{dur_actual}</td>
          <td>{emoji} {status}</td>
          <td>{yt_link}</td>
          <td><small style="color:#666">{split_ids}</small></td>
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
    .progress-bar {{ background: #eee; border-radius: 8px; height: 20px; margin: 1em 0; }}
    .progress-fill {{ background: #28a745; height: 100%; border-radius: 8px; width: {progress_pct}%; transition: width 0.5s; }}
    .stats {{ display: flex; gap: 2em; margin: 1em 0; }}
    .stat {{ text-align: center; }}
    .stat-num {{ font-size: 2em; font-weight: bold; color: #28a745; }}
    .stat-label {{ color: #666; font-size: 0.9em; }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
    th {{ background: #343a40; color: white; padding: 8px 12px; text-align: left; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #dee2e6; }}
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

  <div class="progress-bar"><div class="progress-fill"></div></div>

  <div class="stats">
    <div class="stat"><div class="stat-num">{done}</div><div class="stat-label">已上传</div></div>
    <div class="stat"><div class="stat-num">{generated}</div><div class="stat-label">已生成待上传</div></div>
    <div class="stat"><div class="stat-num">{total - done - generated}</div><div class="stat-label">等待中</div></div>
    <div class="stat"><div class="stat-num">{total}</div><div class="stat-label">总段落</div></div>
    <div class="stat"><div class="stat-num">{progress_pct}%</div><div class="stat-label">上传进度</div></div>
  </div>

  <table>
    <thead>
      <tr>
        <th>#</th><th>标题</th><th>字数</th><th>预估时长</th>
        <th>实际时长</th><th>状态</th><th>YouTube</th><th>来源</th>
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
            html = render_html(plan).encode("utf-8")
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
