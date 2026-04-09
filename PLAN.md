# Audiobook Generator — Design Plan

## Overview

A Python tool to convert EPUB/Markdown books into audiobooks using VoxCPM2 TTS.
Integrates with NanoClaw/Discord as the natural-language control interface.

## Core Principles

1. **Confirm before generate** — show segment table to user, pause for approval
2. **Text-audio pairing** — every MP3 has a paired JSON with text, hash, metadata
3. **Idempotent** — skip already-generated segments (hash check)
4. **Traceable** — content hash embedded in YouTube description for auditing
5. **Safe** — no destructive operations without dry-run + confirmation

---

## Workflow

```
Input (EPUB/MD)
    ↓
[Phase 1] Parse → segment table → send to Discord → PAUSE
    ↓  (user confirms / edits via natural language)
[Phase 2] Voice selection → send prompt to Discord → PAUSE
    ↓  (user specifies voice description or sends reference audio)
[Phase 3] Generate audio → send progress updates to Discord
    ↓
[Phase 4] Upload to YouTube (dedup via hash) → send links to Discord
    ↓
[Phase 5] Dashboard (read-only HTML) auto-updates throughout
```

---

## Phase 1: Segmentation & Confirmation

### EPUB Parsing Fix
The key bug from the original script: `_split_000` files (Chinese headings, 7-11 chars)
were filtered by `len(text) < 80` before titles could be extracted.

**Fix**: Build heading map FIRST (scan all items regardless of length), then merge content.

```python
# Step 1: extract headings from ALL items (no length filter)
heading_map = {}  # split_id -> {"zh": "逐梦之旅", "en": "Something to Chase"}
for item in book.items:
    headings = extract_headings(item)
    heading_map[item.id] = headings

# Step 2: merge content items using heading map for titles
segments = merge_content(book.items, heading_map)
```

### Segment Table (sent to Discord)
```
《书名》分段预览 — 共13章

  #   标题                          字数    预估时长   状态
  01  如坐针毡的华盛顿之行           2847    ~14min    ✅
  02  逐梦之旅 / Something to Chase  6820    ~34min    ✅
  03  [无标题]                         43    ~13sec    ⚠️ 太短
  ...

回复 "确认" 继续，或说 "合并第3和第4章"、"跳过第15章" 等修改指令。
```

### Flags
- ⚠️ 太短: < 200 chars
- ⚠️ 太长: > 15000 chars (may overflow KV cache)
- Estimated duration: chars / 420 chars/min (Chinese), chars / 900 chars/min (English)

### Confirmed Segments File
`{outdir}/{book_id}_segments.json` — single source of truth for all subsequent phases.

---

## Phase 2: Voice Selection

After segmentation confirmed, ask:
```
音色设置（可选）：

  A) 整本书统一音色 — 请描述：如 "平静温和的女声播音员"
  B) 提供参考音频 — 发送 .wav/.mp3 文件到此对话
  C) 按章节分别设置
  D) 无条件生成（跳过）

留空或回复 D 跳过。
```

Voice config stored per-segment in `_segments.json`:
```json
{
  "voice_prompt": "平静温和的女声播音员",
  "voice_ref_audio": null
}
```

---

## Phase 3: Audio Generation

### Local File Naming (code-friendly)
```
{outdir}/
  {book_id}_seg0001.mp3       # audio
  {book_id}_seg0001.json      # paired metadata
  {book_id}_seg0002.mp3
  {book_id}_seg0002.json
  ...
  {book_id}_segments.json     # master segment plan
```

`book_id` = 8-char SHA256 of (epub_filename + file_size) — stable, ASCII-safe.

### Metadata JSON (per segment)
```json
{
  "book_id": "a3f8c12e",
  "seg_index": 3,
  "title": "逐梦之旅 / Something to Chase",
  "epub_split_ids": ["split_002_split_001"],
  "text": "...full source text...",
  "text_hash": "sha256:abc123def456...",
  "voice_prompt": "平静温和的女声播音员",
  "voice_ref_audio": null,
  "steps": 10,
  "generated_at": "2026-04-09T01:00:00Z",
  "audio_duration_sec": 2054,
  "youtube_video_id": null,
  "youtube_uploaded_at": null
}
```

### Idempotency
Before generating segment N:
1. Check if `{book_id}_segNNNN.mp3` exists
2. Check if `{book_id}_segNNNN.json` exists and `text_hash` matches
3. If both true → skip, log "already generated"

### Progress Updates (Discord)
```
✅ Ch02 逐梦之旅 完成 — 34分14秒，正在上传...
🔄 Ch03 鸿沟渐窄 生成中 (段落 3/8)...
⏳ Ch04-Ch13 等待中
```

### KV Cache / Segmentation
- Max chars per TTS call: 400 (Chinese), 600 (English)
- Split on sentence boundaries: `。！？…` and `.!?`
- Voice prompt prefix applied to EVERY segment call (for consistency)
- `generate_with_prompt_cache` for voice anchor across sub-segments

---

## Phase 4: Upload (YouTube)

### Deduplication
Before uploading segment N:
1. Query playlist for video with `text_hash` in description → skip if found
2. Query playlist for video with matching title → warn user if hash differs

### YouTube Title Format
```
《书名》Ch03 — 鸿沟渐窄 / A Narrowing Gulf
```

### YouTube Description Format
```
《我看见的世界》有声书 — VoxCPM2 TTS
段落：03 — 鸿沟渐窄 / A Narrowing Gulf
来源：split_003_split_001
摘要：大学开学第一天……（前80字）
文本哈希：sha256:abc123def456
声音：平静温和的女声播音员 | 步数：10
```

After upload, update `youtube_video_id` in segment JSON.

---

## Phase 5: Dashboard (Read-Only)

A static HTML file `{outdir}/{book_id}_dashboard.html` auto-generated and updated
during generation. Serve with `dashboard/server.py` for local viewing.

Shows:
- Book title, voice settings, overall progress
- Per-segment table: title, chars, status, duration, YouTube link
- Error details for failed segments
- Auto-refresh every 30 seconds

Can share via `localhost.run` for remote viewing.

---

## CLI Interface

```bash
# Full pipeline (parse → confirm via Discord → generate → upload)
python audiobook_gen.py run book.epub \
  --jid dc:CHANNEL_ID \
  --ipc /path/to/ipc \
  --outdir /tmp/audiobook \
  --youtube-playlist PLAYLIST_ID \
  --steps 10

# Individual phases
python audiobook_gen.py parse book.epub --outdir /tmp/audiobook
python audiobook_gen.py generate {book_id}_segments.json --steps 10
python audiobook_gen.py upload {book_id}_segments.json --playlist PLAYLIST_ID
python audiobook_gen.py dashboard {book_id}_segments.json --port 8765
```

---

## Safety Rules

- All file overwrites: check hash first, skip if unchanged
- No broad glob deletions: always specify exact segment indices
- Destructive ops: dry-run output before executing
- Re-runs: idempotent by design (hash check skips completed work)

---

## Repository Structure

```
audiobook-gen/
├── README.md
├── PLAN.md                    ← this file
├── audiobook_gen.py           ← main CLI entrypoint
├── parsers/
│   ├── __init__.py
│   ├── epub.py                ← EPUB parser (heading map first)
│   └── markdown.py            ← Markdown parser
├── generator/
│   ├── __init__.py
│   └── voxcpm.py              ← VoxCPM2 TTS generation
├── uploaders/
│   ├── __init__.py
│   └── youtube.py             ← YouTube upload + dedup
├── dashboard/
│   └── server.py              ← Read-only HTML dashboard server
└── examples/
    └── demo_segments.json     ← Example confirmed segments file
```

---

## Implementation Priority

| Priority | Feature |
|----------|---------|
| P0 | Segmentation confirmation (parse + Discord pause) |
| P0 | Metadata JSON + text_hash (foundation for everything) |
| P0 | Voice selection step |
| P1 | Consistent local naming (`book_id_seg0001`) |
| P1 | Upload deduplication (hash check) |
| P2 | Dashboard HTML |
| P2 | YouTube description with hash |
