"""
VoxCPM2 TTS generator for audiobook production.

Features:
- Text chunking (respects sentence boundaries, max 400 chars)
- First chunk generates with voice prompt → saved as anchor WAV
- Subsequent chunks use prompt_wav_path voice cloning for consistency
- Outputs MP3 via pydub; returns duration in seconds
"""

import os
import re
import tempfile
from pathlib import Path

# Disable torch dynamo/inductor compilation globally — no C compiler on host (WSL).
# Must be set before any torch import. suppress_errors alone is insufficient because
# VoxCPM's internal torch.compile(backend='inductor') raises before dynamo can intercept.
os.environ["TORCHDYNAMO_DISABLE"] = "1"

import numpy as np

_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')


def _split_sentences(text: str, max_chars: int = 400) -> list[str]:
    """Split text into TTS-safe chunks at sentence boundaries."""
    parts = re.split(r'(?<=[。！？…\.!?])\s*', text)
    chunks = []
    current = ""
    for part in parts:
        if not part.strip():
            continue
        if len(current) + len(part) > max_chars and current:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


class AudiobookGenerator:
    """
    Wraps VoxCPM2 model for audiobook generation.
    Loads model once, reuses across segments.
    """

    def __init__(self, steps: int = 10):
        self.steps = steps
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        from voxcpm import VoxCPM
        print("Loading VoxCPM2 model...")
        # load_denoiser=False is faster and sufficient for audiobook quality
        self._model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
        print("VoxCPM2 model ready.")

    def generate_segment(
        self,
        text: str,
        output_path: Path,
        voice_prompt: str | None = None,
        voice_ref_audio: str | None = None,
        max_chars_per_chunk: int = 400,
    ) -> float:
        """
        Generate audio for a full chapter segment.
        - Chunk 1: generated with voice_prompt prefix → saved as anchor
        - Chunks 2+: voice-cloned from anchor via prompt_wav_path

        Returns audio duration in seconds.
        """
        import soundfile as sf
        import shutil
        from pydub import AudioSegment
        # Ensure ffmpeg is found — pydub caches converter at import, so set it explicitly
        if not shutil.which("ffmpeg"):
            for candidate in [
                "/home/binxu/miniforge3/envs/research/bin/ffmpeg",
                "/usr/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
            ]:
                if Path(candidate).exists():
                    AudioSegment.converter = candidate
                    AudioSegment.ffmpeg = candidate
                    AudioSegment.ffprobe = candidate.replace("ffmpeg", "ffprobe")
                    break

        self._load_model()
        model = self._model
        sample_rate = model.tts_model.sample_rate

        chunks = _split_sentences(text, max_chars=max_chars_per_chunk)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Progress file: output_path.with_suffix("._progress.json")
        progress_file = output_path.with_suffix("._progress.json")
        total_chunks = len(chunks)

        anchor_wav_path = None
        anchor_text = None
        parts = []

        # If caller provides a reference audio, use it as the voice anchor
        if voice_ref_audio and Path(voice_ref_audio).exists():
            anchor_wav_path = voice_ref_audio
            anchor_text = None  # no paired text

        import json as _json
        for j, chunk in enumerate(chunks):
            print(f"  chunk {j+1}/{total_chunks} ({len(chunk)} chars)...", flush=True)

            if anchor_wav_path is None:
                # First chunk: generate with voice prompt, save as anchor
                prompt_text = f"({voice_prompt}){chunk}" if voice_prompt else chunk
                wav = model.generate(
                    text=prompt_text,
                    cfg_value=2.0,
                    inference_timesteps=self.steps,
                )
                # Save anchor alongside output
                anchor_wav_path = str(output_path.with_suffix("._anchor.wav"))
                sf.write(anchor_wav_path, wav, sample_rate)
                anchor_text = chunk
            else:
                # Subsequent chunks: clone voice from anchor
                kwargs = dict(
                    text=chunk,
                    prompt_wav_path=anchor_wav_path,
                    cfg_value=2.0,
                    inference_timesteps=self.steps,
                )
                if anchor_text:
                    kwargs["prompt_text"] = anchor_text
                wav = model.generate(**kwargs)

            parts.append(wav)
            # Write progress after each chunk completes
            progress_file.write_text(_json.dumps({
                "chunk_done": j + 1,
                "chunk_total": total_chunks,
            }), encoding='utf-8')

        # Concatenate all chunks
        combined = np.concatenate(parts, axis=-1)

        # Save as WAV first, then convert to MP3
        tmp_wav = str(output_path.with_suffix("._tmp.wav"))
        sf.write(tmp_wav, combined, sample_rate)
        audio = AudioSegment.from_wav(tmp_wav)
        audio.export(str(output_path), format="mp3", bitrate="64k")
        Path(tmp_wav).unlink(missing_ok=True)

        return len(audio) / 1000.0  # duration in seconds
