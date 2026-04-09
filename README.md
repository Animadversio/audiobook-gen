# audiobook-gen

Convert EPUB/Markdown books into audiobooks using [VoxCPM2](https://github.com/OpenBMB/VoxCPM) TTS.

Integrates with [NanoClaw](https://github.com/anthropics/nanoclaw) for natural-language control via Discord.

## Features

- **EPUB + Markdown** input support
- **Bilingual title extraction** (Chinese h2 + English calibre9 paragraphs)
- **Segmentation confirmation** — shows chapter table to user before generating
- **Voice selection** — text description or reference audio (voice cloning)
- **Text-audio pairing** — every MP3 has a paired JSON with text hash for traceability
- **Idempotent generation** — skips already-generated segments (hash check)
- **YouTube upload** with deduplication (text hash in description)
- **Read-only dashboard** — auto-refreshing HTML progress page

## Requirements

```bash
pip install ebooklib pydub google-api-python-client google-auth-httplib2 google-auth-oauthlib
pip install voxcpm  # requires CUDA or Apple Silicon
```

ffmpeg must be installed for YouTube upload (MP3 → MP4 conversion).

## Quick Start

### 1. Parse — Preview segments

```bash
python audiobook_gen.py parse book.epub --outdir /tmp/audiobook
```

This generates `/tmp/audiobook/{book_id}_segments.json` and prints a chapter table.
If using NanoClaw, it sends the table to Discord and waits for your confirmation.

### 2. Confirm and optionally edit

The NanoClaw Discord skill handles this interactively. Or manually edit the JSON:
```json
// Set confirmed: true when ready
// Set skip: true for segments to exclude
// Set voice_prompt: "平静温和的女声播音员" globally or per-segment
```

### 3. Generate audio

```bash
python audiobook_gen.py generate /tmp/audiobook/{book_id}_segments.json --steps 10
```

### 4. Upload to YouTube

```bash
python audiobook_gen.py upload /tmp/audiobook/{book_id}_segments.json \
  --playlist PLxxxxxxxxxxxx
```

### 5. View dashboard

```bash
python audiobook_gen.py dashboard /tmp/audiobook/{book_id}_segments.json --port 8765
# Share publicly:
ssh -R 80:localhost:8765 nokey@localhost.run
```

### Full pipeline (no Discord, auto-confirm)

```bash
python audiobook_gen.py run book.epub \
  --outdir /tmp/audiobook \
  --voice "平静温和的女声播音员" \
  --playlist PLxxxxxxxxxxxx \
  --steps 10
```

## File Naming Convention

Local files use a code-friendly naming scheme:

```
{outdir}/
  {book_id}_segments.json       # master segment plan (single source of truth)
  {book_id}_seg0001.mp3         # audio for segment 1
  {book_id}_seg0001.json        # paired metadata for segment 1
  {book_id}_seg0002.mp3
  {book_id}_seg0002.json
  ...
  {book_id}_dashboard.html      # static dashboard snapshot
```

`book_id` = 8-char SHA256 of (filename + file size) — stable, ASCII-safe.

## Metadata JSON Format

Each `{book_id}_segNNNN.json` contains:

```json
{
  "book_id": "a3f8c12e",
  "seg_index": 3,
  "title": "逐梦之旅 / Something to Chase",
  "epub_split_ids": ["split_002_split_001"],
  "text": "...full source text...",
  "text_hash": "sha256:abc123...",
  "voice_prompt": "平静温和的女声播音员",
  "voice_ref_audio": null,
  "steps": 10,
  "generated_at": "2026-04-09T01:00:00Z",
  "audio_duration_sec": 2054,
  "youtube_video_id": "XpFdZRdRFd4",
  "youtube_uploaded_at": "2026-04-09T02:00:00Z"
}
```

The `text_hash` is embedded in the YouTube video description, enabling:
- Upload deduplication (skip if same text already uploaded)
- Content auditing (verify which text generated which audio)
- Future use: automatic subtitle generation, summary creation

## YouTube Description Format

```
《书名》有声书 — VoxCPM2 TTS
段落：03 — 逐梦之旅 / Something to Chase
来源：split_002_split_001
摘要：大学开学第一天……
文本哈希：sha256:abc123def456
声音：平静温和的女声播音员 | 步数：10
```

## NanoClaw Discord Skill

The `/audiobook` skill in NanoClaw handles the interactive workflow:
1. User sends EPUB/MD file → skill calls `parse`
2. Skill sends segment table to Discord
3. User confirms or gives edit instructions
4. Skill calls `generate` with progress updates
5. Skill calls `upload` and sends YouTube links

See `container/skills/audiobook/` in the NanoClaw repo for the skill implementation.

## Voice Options

**Text description:**
```
平静温和的女声播音员
温暖有力的男声主播
```

**Reference audio (voice cloning):**
Send a `.wav` or `.mp3` file (5-30 seconds of speech) to Discord.
The skill saves it and passes it as `--voice-ref` to the generator.

**Per-segment voice:**
Edit `_segments.json` to set `voice_prompt` per segment for mixed-voice books.

## Safety

- All destructive operations require explicit confirmation
- Generation is idempotent: re-running skips completed segments (hash check)
- Upload deduplication prevents double uploads
- No broad glob deletions

## Credits

- TTS: [VoxCPM2](https://github.com/OpenBMB/VoxCPM) by OpenBMB
- Integration: [NanoClaw](https://github.com/anthropics/nanoclaw)
