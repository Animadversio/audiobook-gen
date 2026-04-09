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

## Library

All books are stored in a local **library** — one folder per book, with a global index.

### Library root (in priority order)

```bash
# 1. CLI argument
python audiobook_gen.py parse book.epub --library ~/my-audiobooks

# 2. Environment variable
export AUDIOBOOK_LIBRARY=~/my-audiobooks

# 3. Default
~/.local/share/audiobook-gen/    # Linux / WSL
~/Library/audiobook-gen/         # macOS
```

### Library directory structure

```
~/audiobook-library/
  library.json                        ← global index of all books

  a3f8c12e/                           ← book_id (8-char SHA256)
    meta.json                         ← book metadata + progress summary
    segments.json                     ← full segment plan (source of truth)
    dashboard.html                    ← static status page snapshot
    audio/
      seg0001.mp3                     ← generated audio
      seg0001.json                    ← paired metadata with text hash
      seg0002.mp3
      seg0002.json
      ...
    source/
      original.epub                   ← archived copy of source file

  b7e2a91f/                           ← another book
    ...
```

`book_id` = 8-char SHA256 of (filename + file size) — stable, ASCII-safe, no Unicode issues.

### List your library

```bash
python audiobook_gen.py list
```

Output:
```
   book_id  Done  Total  Duration  Title
──────────────────────────────────────────────────────────────────────
  a3f8c12e    13     13    5h42m   我看见的世界：李飞飞自传
  b7e2a91f     3      8    1h12m   另一本书

Library root: /home/user/.local/share/audiobook-gen
```

## Quick Start

### 1. Parse — Preview segments

```bash
python audiobook_gen.py parse book.epub
```

Registers the book in your library, generates `segments.json`, and prints the chapter table.
If using NanoClaw, it sends the table to Discord and waits for your confirmation.

### 2. Confirm and optionally edit

The NanoClaw Discord skill handles this interactively. Or manually edit `segments.json`:
```json
{
  "confirmed": true,
  "voice_prompt": "平静温和的女声播音员",
  "segments": [
    {"seg_index": 15, "skip": true, ...}
  ]
}
```

### 3. Generate audio

```bash
# Use book_id prefix (first 4+ chars is enough)
python audiobook_gen.py generate a3f8 --steps 10
```

### 4. Upload to YouTube

```bash
python audiobook_gen.py upload a3f8 --playlist PLxxxxxxxxxxxx
```

### 5. View dashboard

```bash
python audiobook_gen.py dashboard a3f8 --port 8765
# Share publicly:
ssh -R 80:localhost:8765 nokey@localhost.run
```

### Full pipeline (no Discord, auto-confirm)

```bash
python audiobook_gen.py run book.epub \
  --voice "平静温和的女声播音员" \
  --playlist PLxxxxxxxxxxxx \
  --steps 10
```

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

## Troubleshooting

### ffmpeg not found (pydub export fails)

**Symptom:** Generation runs all chunks, then crashes with:
```
FileNotFoundError: [Errno 2] No such file or directory: 'ffmpeg'
```
The `seg{N}.mp3` file is 0 bytes. The `seg{N}._tmp.wav` (all chunks concatenated) still exists.

**Cause:** pydub calls `ffmpeg` by name but the conda env's bin directory isn't in PATH when running outside an activated conda environment.

**Fix:** The generator auto-detects and adds ffmpeg to PATH. Always run with the full Python path:
```bash
/path/to/miniforge3/envs/research/bin/python audiobook_gen.py generate ...
```

**Recovery (if _tmp.wav survived):** Manually convert without rerunning VoxCPM:
```bash
ffmpeg -y -i seg{N}._tmp.wav -codec:a libmp3lame -b:a 64k seg{N}.mp3
```
Then write `seg{N}.json` manually (copy from `segments.json`, add `generated_at` and `audio_duration_sec`). Re-running `generate` will skip the recovered segment via hash check.

---

### UnicodeEncodeError on emoji (❌ ✅)

**Symptom:** Crash after generation with:
```
UnicodeEncodeError: 'ascii' codec can't encode character '\u274c'
```

**Cause:** WSL/Linux terminal defaults to ASCII encoding; emoji in status messages fail on stdout/stderr.

**Fix:** Always run with `PYTHONIOENCODING=utf-8`:
```bash
PYTHONIOENCODING=utf-8 python audiobook_gen.py generate ...
```

---

### VoxCPM has no `.to()` method

**Symptom:** `AttributeError: 'VoxCPM' object has no attribute 'to'`

**Cause:** VoxCPM2 manages its own device placement internally — never call `.to(device)`.

**Fix:** Use `VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)` with no device call.

---

### torch inductor: Failed to find C compiler

**Symptom:** Every segment fails immediately with:
```
backend='inductor' raised:
RuntimeError: Failed to find C compiler. Please specify via CC environment variable.
```

**Cause:** VoxCPM2 internally calls `torch.compile(backend='inductor')` during warmup, which requires `gcc`. On WSL hosts without gcc, this crashes. `torch._dynamo.config.suppress_errors = True` is insufficient — the error occurs before dynamo's fallback intercepts it. The environment variable `TORCH_COMPILE_DISABLE` is not a real PyTorch variable (it's a no-op).

**Fix:** Set `TORCHDYNAMO_DISABLE=1` **before any torch import**. This is done at the top of `generator/voxcpm.py`:
```python
os.environ["TORCHDYNAMO_DISABLE"] = "1"  # must be module-level, before torch
```

**Why it must be module-level:** pydub (imported in generate_segment) pulls in torch indirectly. By the time `_load_model()` runs, torch may already be initialized with dynamo enabled.

---

### segments.json is empty after crash

**Symptom:** `JSONDecodeError: Expecting value: line 1 column 1` when reading segments.json.

**Cause:** `Path.write_text()` truncates the file before writing. A crash during the write leaves a 0-byte file.

**Fix:** `save_plan()` now uses an atomic write: write to `.json.tmp`, then `os.replace()`. On POSIX, rename is atomic — a crash mid-write leaves the old file intact.

**Recovery:** Re-run `python audiobook_gen.py parse <epub>` to rebuild from source, then manually restore `generated_at`/`youtube_video_id` fields from the per-segment `audio/seg*.json` files.

### Chinese text in voice_prompt crashes save_plan

**Symptom:** Generation completes a chapter successfully (MP3 written, `_tmp.wav` cleaned up), then crashes immediately after:

```
UnicodeEncodeError: 'ascii' codec can't encode characters in position 507-510
  File "audiobook_gen.py", in save_plan
    tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2))
```

**Cause:** `Path.write_text()` defaults to the system locale encoding, which is ASCII on WSL. When `voice_prompt` contains Chinese characters and `ensure_ascii=False` is used, the resulting string can't be encoded.

**Fix:** Always pass `encoding='utf-8'` to `write_text()`:

```python
tmp.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
```

**Why it was subtle:** Runs with `voice_prompt = None` (default) succeeded silently. The bug only surfaces when a Chinese voice prompt is set — every chapter completes its MP3 but the plan update crashes, so the chapter must be manually recovered from `audio/seg*.json`.

**Scope:** This affected every `write_text()` call in the codebase — `audiobook_gen.py` (3 sites), `generator/voxcpm.py`, `library.py`, `watchdog.py`, and `uploaders/youtube.py`. All have been patched with `encoding='utf-8'`.

> **Chinese support note:** audiobook-gen is designed for Chinese books and voice prompts. All JSON writes use `ensure_ascii=False` + `encoding='utf-8'`, and `PYTHONIOENCODING=utf-8` is required on WSL to prevent stdout crashes from CJK characters in status messages.

## Canonical run command

Always launch generation with the full env:

```bash
PYTHONIOENCODING=utf-8 \
  /path/to/miniforge3/envs/research/bin/python audiobook_gen.py generate BOOK_ID \
  --library /path/to/library \
  --steps 10
```

`PYTHONIOENCODING=utf-8` — prevents emoji crash on WSL stdout.
`TORCHDYNAMO_DISABLE=1` — set automatically inside generator/voxcpm.py; no need to pass externally.

## Credits

- TTS: [VoxCPM2](https://github.com/OpenBMB/VoxCPM) by OpenBMB
- Integration: [NanoClaw](https://github.com/anthropics/nanoclaw)
