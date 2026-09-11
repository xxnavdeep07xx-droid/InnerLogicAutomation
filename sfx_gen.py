#!/usr/bin/env python3
"""
sfx_gen.py - minimal, rights-cleared SFX layer for the v2 assembly.

Per the 2026-09 assembly spec + v2.1 user feedback ("that woosh sound in
every transition is irritating - add SFX wherever needed, whichever suits
that instant"):

    - WHOOSH: only on WHIP transitions (not every cut), one of THREE
      different air voices per cut, panned L->R / R->L following the whip
      direction, and quieter than before
    - BOOM: a sub-boom under punch and dip transitions (felt, not heard)
    - DING: a soft glass tick under emphasized words (fewer, quieter)

Everything is synthesized with numpy, so the pack is rights-cleared and
deterministic. Levels are deliberately LOW so the voiceover always dominates:
    whoosh peak ~ 0.075 (~ -22 dBFS)
    ding   peak ~ 0.12  (~ -18 dBFS)
    boom   peak ~ 0.20  (~ -14 dBFS)
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
WHOOSH_GAIN = 0.075                     # v2.1: quieter + only on whips
WHOOSH_DUR = 0.30
DING_GAIN = 0.12
DING_DUR = 0.45
BOOM_GAIN = 0.20                        # v3: sub-boom under punch/dip cuts
BOOM_DUR = 0.55
MAX_DINGS = 6
MIN_DING_GAP = 0.9
MIN_WHOOSH_GAP = 0.6                    # never two whooshes back to back
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


def make_whoosh(variant: int = 0) -> np.ndarray:
    """Three distinct soft air voices so consecutive whips never sound
    identical: 0 = round low breath, 1 = tighter + faster bloom,
    2 = airy high swell. Deterministic per variant."""
    n = int(WHOOSH_DUR * SAMPLE_RATE)
    cutoffs = (900.0, 550.0, 1700.0)
    powers = (1.6, 2.1, 1.25)
    seeds = (42, 137, 911)
    rng = np.random.default_rng(seeds[variant % 3])
    noise = rng.standard_normal(n)
    body = _lowpass(noise, cutoffs[variant % 3])
    body /= max(np.abs(body).max(), 1e-9)
    t = np.linspace(0, 1, n)
    bloom = np.sin(np.pi * t ** powers[variant % 3])
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


def make_boom() -> np.ndarray:
    """v3 sub-boom: 70 -> 38 Hz sine sweep, fast attack, long decay.
    Sits under the punch/dip transitions so a section change is FELT."""
    n = int(BOOM_DUR * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    freq = 70.0 * (38.0 / 70.0) ** (t / BOOM_DUR)
    phase = 2 * np.pi * np.cumsum(freq) / SAMPLE_RATE
    body = np.sin(phase)
    body *= _fade_envelope(n, 0.005, BOOM_DUR * 0.85)
    body *= np.exp(-t / 0.16)
    return body / max(np.abs(body).max(), 1e-9)


def _add(track: np.ndarray, sample: np.ndarray, start: float, gain: float,
         pan: np.ndarray | None = None) -> None:
    """Add a (mono) sample onto the STEREO track. `pan` optionally sweeps
    L->R (-1 -> +1) per sample for directional whooshes."""
    i0 = int(start * SAMPLE_RATE)
    if i0 < 0 or i0 >= len(track):
        return
    seg = sample[:len(track) - i0] * gain
    n = len(seg)
    if pan is None:
        track[i0:i0 + n, 0] += seg * 0.8
        track[i0:i0 + n, 1] += seg * 0.8
    else:
        p = pan[:n]
        track[i0:i0 + n, 0] += seg * 0.5 * (1.0 - p)
        track[i0:i0 + n, 1] += seg * 0.5 * (1.0 + p)


def build_layer(cuts: list[float], emphasis_times: list[float], duration: float,
                out_path: Path, level: float | None = None,
                boom_times: list[float] | None = None,
                events: list[dict] | None = None) -> Path | None:
    """Render the SFX track for one video -> _sfx_layer.wav (16-bit stereo).

    cuts            scene-cut timestamps (legacy: whoosh into EVERY cut)
    emphasis_times  start times of emphasized words (ding per word, capped)
    boom_times      v3: punch/dip transition starts (sub-boom under each)
    events          v2.1: [{t, kind, dir}] from motion_edit.sfx_events().
                    When given, whooshes fire ONLY on whips, each with a
                    different air voice, panned along the whip direction -
                    the fix for the irritating identical whoosh every cut.
    """
    level = LEVEL_DEFAULT if level is None else float(level)
    if level <= 0 or duration <= 1:
        return None
    whooshes = [make_whoosh(0), make_whoosh(1), make_whoosh(2)]
    ding, boom = make_ding(), make_boom()
    track = np.zeros((int(duration * SAMPLE_RATE) + SAMPLE_RATE // 2, 2))

    last_whoosh = -10.0
    if events is not None:
        whip_i = 0
        for ev in events:
            t = float(ev.get("t", 0.0))
            kind = ev.get("kind", "whip")
            if kind == "whip":
                if t - last_whoosh < MIN_WHOOSH_GAP:
                    continue
                start = max(0.0, t - WHOOSH_DUR + 0.02)
                direction = int(ev.get("dir", 1) or 1)
                n = len(whooshes[0])
                sweep = np.linspace(-direction, direction, n)
                _add(track, whooshes[whip_i % 3], start,
                     WHOOSH_GAIN * level, pan=sweep)
                whip_i += 1
                last_whoosh = t
            elif kind in ("punch", "dip"):
                _add(track, boom, t, BOOM_GAIN * level)
    else:
        # legacy v1/v2 behaviour: a whoosh into every cut (alternating voices)
        for k, cut in enumerate(cuts):
            start = float(cut) - WHOOSH_DUR + 0.02
            if start < 0.02:
                start = 0.0
            _add(track, whooshes[k % 3], start, WHOOSH_GAIN * level)
        if boom_times:
            for t in boom_times:
                _add(track, boom, float(t), BOOM_GAIN * level)

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
    frames = (np.clip(track, -1.0, 1.0) * 32767).astype(np.int16)
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
    events = [
        {"t": 3.4, "kind": "whip", "dir": 1},
        {"t": 9.1, "kind": "punch", "dir": 1},
        {"t": 15.7, "kind": "whip", "dir": -1},
        {"t": 18.2, "kind": "dip", "dir": -1},
    ]
    path = build_layer([3.4, 9.1, 15.7, 18.2], [0.6, 3.5, 10.0, 16.0, 17.2],
                       20.0, out_dir / "_sfx_layer.wav", events=events)
    if path:
        with wave.open(str(path), "rb") as wav:
            n = wav.getnframes()
            data = np.frombuffer(wav.readframes(n), dtype=np.int16).reshape(-1, 2)
        dbfs = 20 * math.log10(max(np.abs(data).max() / 32767, 1e-9))
        # pan check at the first whoosh peak (~t=3.25s): L should be louder
        # than R before the sweep crosses center (dir=+1 => sweeps L->R)
        i = int(3.25 * SAMPLE_RATE)
        seg = data[i:i + 2000].astype(float)
        l_rms = float(np.sqrt((seg[:, 0] ** 2).mean()))
        r_rms = float(np.sqrt((seg[:, 1] ** 2).mean()))
        print(f"self-test OK: {path.name} {n / SAMPLE_RATE:.1f}s "
              f"peak {dbfs:.1f} dBFS | whoosh pan L>R early: "
              f"{l_rms > r_rms} ({l_rms:.0f} vs {r_rms:.0f}) "
              f"(voiceover must stay dominant)")
    else:
        print("self-test FAILED: no layer written")
        sys.exit(1)
