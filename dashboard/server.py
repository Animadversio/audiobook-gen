"""
Read-only audiobook dashboard server.

Routes:
  /                    → Library index: all books, progress summary
  /book/<book_id>      → Per-book detail: segment table, ETA, YouTube links

Serves self-refreshing HTML pages (dark theme, GitHub Dark palette).

Usage:
    python -m dashboard.server --library /path/to/library --port 8765
    python -m dashboard.server --library /path/to/library --book-id 2c3a6054  # single book

Then expose via: cloudflared tunnel --url localhost:8765
"""

import json
import http.server
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

from library import Library, BookPaths

DARK_CSS = """
    body { font-family: -apple-system, sans-serif; max-width: 1200px; margin: 2em auto; padding: 0 1em; background: #0d1117; color: #e6edf3; }
    h1, h2 { color: #e6edf3; }
    a { color: #58a6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .meta { color: #8b949e; margin-bottom: 1.5em; }
    .progress-wrap { margin: 0.5em 0 1.5em; }
    .progress-label { font-size: 0.85em; color: #8b949e; margin-bottom: 3px; }
    .progress-bar { background: #21262d; border-radius: 8px; height: 16px; }
    .progress-fill-green { background: #238636; height: 100%; border-radius: 8px; transition: width 0.5s; }
    .stats { display: flex; gap: 2em; margin: 1em 0 1.5em; flex-wrap: wrap; }
    .stat { text-align: center; }
    .stat-num { font-size: 2em; font-weight: bold; color: #3fb950; }
    .stat-label { color: #8b949e; font-size: 0.9em; }
    .eta-box { display:inline-block; background:#272115; border:1px solid #9e6a03; border-radius:6px; padding:4px 12px; font-weight:bold; color:#d29922; margin-left:1em; }
    table { border-collapse: collapse; width: 100%; margin-top: 1em; }
    th { background: #161b22; color: #8b949e; padding: 8px 12px; text-align: left; border-bottom: 1px solid #30363d; }
    td { padding: 8px 12px; border-bottom: 1px solid #21262d; vertical-align:middle; }
    .badge { background: #9e6a03; color: #e6edf3; padding: 2px 6px; border-radius: 4px; font-size: 0.8em; }
    .refresh { color: #484f58; font-size: 0.8em; text-align: right; margin-top: 1em; }
    .back { display:inline-block; margin-bottom:1em; color:#8b949e; font-size:0.9em; }
    .book-card { background:#161b22; border:1px solid #30363d; border-radius:8px; padding:1em 1.5em; margin-bottom:1em; }
    .book-card h2 { margin:0 0 0.3em; font-size:1.2em; }
    .book-card .subtitle { color:#8b949e; font-size:0.85em; margin-bottom:0.8em; }
    .pill { display:inline-block; background:#21262d; border-radius:12px; padding:2px 10px; font-size:0.8em; color:#8b949e; margin-right:6px; }
    .pill-green { background:#0d1f17; color:#3fb950; }
    .pill-yellow { background:#272115; color:#d29922; }
"""


def _fmt_duration(secs: float | None) -> str:
    if not secs:
        return "—"
    m, s = divmod(int(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    return f"{m}m{s:02d}s"


def _fmt_hours(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m = r // 60
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


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
    "done": "#0d1f17",
    "uploading": "#0d1b2e",
    "generating": "#1f1b0d",
    "pending": "#0d1117",
    "skipped": "#161b22",
    "error": "#2a0f0f",
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
    prog_file = seg_mp3_path.with_suffix("._progress.json")
    try:
        data = json.loads(prog_file.read_text())
        return data["chunk_done"], data["chunk_total"]
    except Exception:
        return None


def render_index_html(library: Library) -> str:
    """Render the library index page listing all books."""
    books = library.list_books()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cards = []
    for b in books:
        book_id = b["book_id"]
        title = b.get("title", book_id)
        total = b.get("total_segments", 0)
        generated = b.get("generated_segments", 0)
        done = b.get("done_segments", 0)
        dur = _fmt_hours(b.get("total_audio_sec", 0))
        gen_pct = int(generated / total * 100) if total else 0
        upload_inner_pct = int(done / generated * 100) if generated else 0
        playlist = b.get("youtube_playlist", "")
        yt_link = (f'<a href="https://www.youtube.com/playlist?list={playlist}" target="_blank">▶ 播放列表</a>'
                   if playlist else "—")

        status_pill = ""
        if generated == total and total > 0:
            status_pill = '<span class="pill pill-green">✅ 全部完成</span>'
        elif generated > 0:
            status_pill = f'<span class="pill pill-yellow">🔄 生成中 {generated}/{total}</span>'
        else:
            status_pill = f'<span class="pill">⏳ 待开始</span>'

        cards.append(f"""
  <div class="book-card">
    <h2><a href="/book/{book_id}">《{title}》</a></h2>
    <div class="subtitle">ID: <code>{book_id}</code> &nbsp;·&nbsp; {yt_link}</div>
    <div style="margin-bottom:0.6em">
      {status_pill}
      <span class="pill">🎵 {dur}</span>
      <span class="pill">上传 {done}/{total}</span>
    </div>
    <div style="background:#21262d;border-radius:6px;height:10px;overflow:hidden">
      <div style="width:{gen_pct}%;background:#238636;height:100%;border-radius:6px;position:relative">
        <div style="width:{upload_inner_pct}%;background:#155724;height:100%;border-radius:6px 0 0 6px;position:absolute;top:0;left:0"></div>
      </div>
    </div>
  </div>""")

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>有声书书库</title>
  <style>{DARK_CSS}</style>
</head>
<body>
  <h1>📚 有声书书库</h1>
  <div class="meta">{len(books)} 本书 &nbsp;·&nbsp; 每60秒自动刷新</div>
  {"".join(cards) if cards else "<p style='color:#8b949e'>书库为空，使用 audiobook_gen.py parse 添加书籍。</p>"}
  <div class="refresh">最后更新：{now}</div>
</body>
</html>"""


def render_book_html(plan: dict, audio_dir: Path | None = None) -> str:
    """Render the per-book detail page."""
    segs = plan["segments"]
    book_id = plan.get("book_id", "")
    book_title = plan.get("book_title", "未知")
    voice = plan.get("voice_prompt") or "unconditional"
    voice_ref = plan.get("voice_ref_audio") or "—"
    steps = plan.get("steps", "?")

    active_segs = [s for s in segs if not s.get("skip")]
    total = len(active_segs)
    done = len([s for s in segs if s.get("youtube_video_id")])
    generated = len([s for s in segs if s.get("generated_at")])

    gen_pct = int(generated / total * 100) if total else 0
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

        if seg.get("skip"):
            seg_bar = '<span style="color:#484f58">—</span>'
        elif seg.get("generated_at"):
            seg_bar = '<span style="color:#3fb950;font-weight:bold">✓ 完成</span>'
        else:
            chunk_prog = None
            if audio_dir:
                mp3 = audio_dir / f"seg{idx:04d}.mp3"
                chunk_prog = _read_chunk_progress(mp3)
            if chunk_prog:
                cdone, ctotal = chunk_prog
                pct = int(cdone / ctotal * 100) if ctotal else 0
                seg_bar = (
                    f'<div style="display:flex;align-items:center;gap:6px;min-width:130px">'
                    f'<div style="flex:1;background:#21262d;border-radius:4px;height:8px">'
                    f'<div style="width:{pct}%;background:#58a6ff;height:100%;border-radius:4px"></div>'
                    f'</div>'
                    f'<span style="font-size:0.8em;color:#8b949e;white-space:nowrap">{cdone}/{ctotal}</span>'
                    f'</div>'
                )
            else:
                seg_bar = '<span style="color:#484f58;font-size:0.85em">等待中</span>'

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
  <style>{DARK_CSS}</style>
</head>
<body>
  <a class="back" href="/">← 书库</a>
  <h1>📖 《{book_title}》有声书</h1>
  <div class="meta">
    ID: <code>{book_id}</code> &nbsp;|&nbsp;
    声音：{voice[:40] + "…" if len(voice) > 40 else voice} &nbsp;|&nbsp; 步数：{steps}
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


def start_server(library_root: Path, port: int = 8765, single_book_id: str | None = None):
    """Start a live-updating HTTP server.

    Routes:
      /                  → library index (or redirect to single book if single_book_id set)
      /book/<book_id>    → per-book detail page
    """
    library_root = Path(library_root)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"

            if path == "/":
                if single_book_id:
                    self._redirect(f"/book/{single_book_id}")
                    return
                lib = Library(library_root)
                html = render_index_html(lib).encode("utf-8")

            elif path.startswith("/book/"):
                book_id = path[len("/book/"):]
                seg_file = library_root / book_id / "segments.json"
                if not seg_file.exists():
                    self._404(f"Book not found: {book_id}")
                    return
                plan = json.loads(seg_file.read_text(encoding="utf-8"))
                audio_dir = seg_file.parent / "audio"
                html = render_book_html(plan, audio_dir=audio_dir).encode("utf-8")

            else:
                self._404(f"Unknown path: {path}")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _redirect(self, location: str):
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def _404(self, msg: str):
            body = f"<h1>404</h1><p>{msg}</p>".encode()
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass  # suppress access logs

    server = http.server.HTTPServer(("", port), Handler)
    print(f"Dashboard: http://localhost:{port}")
    if single_book_id:
        print(f"  → Redirects to /book/{single_book_id}")
    print("Share publicly: cloudflared tunnel --url localhost:{port}")
    server.serve_forever()


def generate_static_html(segments_file: Path) -> Path:
    """Generate a static HTML file alongside the segments JSON."""
    plan = json.loads(segments_file.read_text(encoding="utf-8"))
    html = render_book_html(plan)
    out = segments_file.with_suffix("_dashboard.html")
    out.write_text(html, encoding="utf-8")
    return out
