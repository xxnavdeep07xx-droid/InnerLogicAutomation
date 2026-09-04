#!/usr/bin/env python3
"""
sfx_gen.py - minimal, rights-cleared SFX layer for the v2 assembly.

Per the 2026-09 assembly spec:
    - a soft whoosh leading into every scene cut (ends exactly at the cut)
    - a distinct "ding" under emphasized words (from emphasis_words metadata)

Everything is synthesized with numpy, so the pack is rights-cleared and
deterministic. Levels are deliberately LOW so the voiceover always dominates:
    whoosh peak ~ 0.10  (~ -20 dBFS)
    ding   peak ~ 0.16  (~ -16 dBFS)
Disable the whole layer with SFX=off / --sfx off; scale with SFX_LEVEL.

Standalone self-test:  python sfx_gen.py [out_dir]
"""

from __future__ import annotations

import math
import os
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
WHOOSH_GAIN = 0.10
WHOOSH_DUR = 0.30
DING_GAIN = 0.16
DING_DUR = 0.45
MAX_DINGS = 8
MIN_DING_GAP = 0.8
LEVEL_DEFAULT = 1.0


def _fade_envelope(n: int, attack: float, release: float) -> np.ndarray:
    env = np.ones(n)
    na, nr = int(attack * SAMPLE_RATE), int(release * SAMPLE_RATE)
    na, nr = max(1, min(na, n)), max(1, min(nr, n))
    env[:na] = np.linspace(0, 1, na)
    env[-nr:] *= np.linspace(1, 0, nr)
    return env


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    spec /= (1.0 + (freqs / max(cutoff, 1.0)) ** 2)
    return np.fft.irfft(spec, n=len(x))


def make_whoosh() -> np.ndarray:
    """Soft air movement: low-passed noise, slow bloom, quick tail."""
    n = int(WHOOSH_DUR * SAMPLE_RATE)
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(n)
    body = _lowpass(noise, 900.0)
    body /= max(np.abs(body).max(), 1e-9)
    # bloom: rises to 60% then falls (ends with the cut)
    t = np.linspace(0, 1, n)
    bloom = np.sin(np.pi * t ** 1.6)
    return body * bloom


def make_ding() -> np.ndarray:
    """Small glass ding: two harmonics, fast attack, exponential decay."""
    n = int(DING_DUR * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    tone = (np.sin(2 * np.pi * 1320.0 * t)
            + 0.45 * np.sin(2 * np.pi * 1980.0 * t)
            + 0.18 * np.sin(2 * np.pi * 2640.0 * t))
    decay = np.exp(-t / 0.11)
    attack = _fade_envelope(n, 0.004, DING_DUR * 0.9)
    tone *= decay * attack
    return tone / max(np.abs(tone).max(), 1e-9)


def _add(track: np.ndarray, sample: np.ndarray, start: float, gain: float) -> None:
    i0 = int(start * SAMPLE_RATE)
    if i0 < 0 or i0 >= len(track):
        return
    seg = sample[:len(track) - i0] * gain
    track[i0:i0 + len(seg)] += seg


def build_layer(cuts: list[float], emphasis_times: list[float], duration: float,
                out_path: Path, level: float | None = None) -> Path | None:
    """Render the SFX track for one video -> _sfx_layer.wav (16-bit stereo).

    cuts            scene-cut timestamps (whoosh ends exactly at each cut)
    emphasis_times  start times of emphasized words (ding per word, capped)
    """
    level = LEVEL_DEFAULT if level is None else float(level)
    if level <= 0 or duration <= 1:
        return None
    whoosh, ding = make_whoosh(), make_ding()
    track = np.zeros(int(duration * SAMPLE_RATE) + SAMPLE_RATE // 2)

    for cut in cuts:
        start = float(cut) - WHOOSH_DUR + 0.02
        if start < 0.02:
            start = 0.0
        _add(track, whoosh, start, WHOOSH_GAIN * level)

    last = -10.0
    used = 0
    for t in sorted(emphasis_times):
        if used >= MAX_DINGS or t - last < MIN_DING_GAP:
            continue
        _add(track, ding, float(t), DING_GAIN * level)
        last, used = t, used + 1

    peak = float(np.abs(track).max())
    if peak < 1e-6:
        return None
    if peak > 0.9:
        track *= 0.9 / peak
    st = np.stack([track, track], axis=1)
    frames = (np.clip(st, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(out_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames.tobytes())
    return out_path


def sfx_enabled(args=None) -> bool:
    """--sfx off / SFX=off disables; 'auto'/unset = enabled."""
    if args is not None and getattr(args, "sfx", "auto") in ("off", "none"):
        return False
    return (os.getenv("SFX", "") or "").strip().lower() not in \
        ("0", "off", "none", "false")


if __name__ == "__main__":
    import sys
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/_sfx_selftest")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = build_layer([3.4, 9.1, 15.7], [0.6, 3.5, 10.0, 16.0, 17.2],
                       20.0, out_dir / "_sfx_layer.wav")
    if path:
        with wave.open(str(path), "rb") as wav:
            n = wav.getnframes()
            data = np.frombuffer(wav.readframes(n), dtype=np.int16).reshape(-1, 2)
        dbfs = 20 * math.log10(max(np.abs(data).max() / 32767, 1e-9))
        print(f"self-test OK: {path.name} {n / SAMPLE_RATE:.1f}s "
              f"peak {dbfs:.1f} dBFS (voiceover must stay dominant)")
    else:
        print("self-test FAILED: no layer written")
        sys.exit(1)
