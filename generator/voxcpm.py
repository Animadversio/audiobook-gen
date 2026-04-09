"""
VoxCPM2 TTS generator for audiobook production.

Features:
- Text chunking (respects sentence boundaries, max 400 chars Chinese / 600 English)
- Voice prompt prefix on every chunk (for consistent voice across segments)
- Reference audio support for voice cloning (generate_with_prompt_cache)
- Outputs MP3 via pydub; returns duration in seconds
- KV cache anchor per chapter for voice consistency
"""

import re
import os
import tempfile
from pathlib import Path

_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

# Sentence boundary patterns
_SENT_BOUNDARY_ZH = re.compile(r'(?<=[。！？…])\s*')
_SENT_BOUNDARY_EN = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')


def _has_cjk(text: str) -> bool:
    return bool(_CJK.search(text))


def _split_sentences(text: str, max_chars: int = 400) -> list[str]:
    """
    Split text into TTS-safe chunks at sentence boundaries.
    Chinese: split on 。！？… | English: split on .!? followed by capital.
    """
    # Unified sentence splitter
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
    return chunks


class AudiobookGenerator:
    """
    Wraps VoxCPM2 model for audiobook generation.
    Loads model once, reuses across segments.
    """

    def __init__(self, steps: int = 10, device: str = "cuda"):
        self.steps = steps
        self.device = device
        self._model = None
        self._tokenizer = None

    def _load_model(self):
        if self._model is not None:
            return

        import torch
        # Required workaround: suppress torch.compile errors (no gcc on host)
        torch._dynamo.config.suppress_errors = True
        os.environ.setdefault("TORCH_COMPILE_DISABLE", "1")

        from voxcpm import VoxCPM
        print("Loading VoxCPM2 model...")
        self._model = VoxCPM.from_pretrained("openbmb/VoxCPM2")
        self._model = self._model.to(self.device)
        self._model.eval()
        print("VoxCPM2 model loaded.")

    def _chunks_to_wav(
        self,
        chunks: list[str],
        voice_prompt: str | None,
        voice_ref_audio: str | None,
    ) -> list:
        """Generate WAV audio arrays for a list of text chunks."""
        import torch
        self._load_model()

        wav_arrays = []

        if voice_ref_audio and Path(voice_ref_audio).exists():
            # Voice cloning via KV cache anchor
            import torchaudio
            ref_wav, ref_sr = torchaudio.load(voice_ref_audio)
            if ref_sr != 24000:
                ref_wav = torchaudio.functional.resample(ref_wav, ref_sr, 24000)
            prompt_cache = self._model.build_prompt_cache(ref_wav)

            for chunk in chunks:
                text = f"({voice_prompt}){chunk}" if voice_prompt else chunk
                with torch.no_grad():
                    wav = self._model.generate_with_prompt_cache(
                        text, prompt_cache, num_steps=self.steps
                    )
                wav_arrays.append(wav.cpu().numpy())
        else:
            for chunk in chunks:
                text = f"({voice_prompt}){chunk}" if voice_prompt else chunk
                with torch.no_grad():
                    wav = self._model.generate(text, num_steps=self.steps)
                wav_arrays.append(wav.cpu().numpy())

        return wav_arrays

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
        Splits into TTS-safe chunks, generates each, concatenates to MP3.
        Returns audio duration in seconds.
        """
        import numpy as np
        try:
            from pydub import AudioSegment
        except ImportError:
            raise RuntimeError("pydub required: pip install pydub")

        chunks = _split_sentences(text, max_chars=max_chars_per_chunk)
        if not chunks:
            raise ValueError("No text chunks to generate")

        wav_arrays = self._chunks_to_wav(chunks, voice_prompt, voice_ref_audio)

        # Concatenate all chunks
        combined = np.concatenate(wav_arrays, axis=-1)

        # Convert to pydub AudioSegment (VoxCPM2 outputs 24kHz mono)
        pcm = (combined * 32767).astype(np.int16).tobytes()
        audio = AudioSegment(pcm, frame_rate=24000, sample_width=2, channels=1)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio.export(str(output_path), format="mp3", bitrate="64k")

        return len(audio) / 1000.0  # duration in seconds
