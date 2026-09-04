#!/usr/bin/env python3
"""
tts_engine.py - swappable voiceover backends with emotion-aware prosody.

Public API
----------
    generate_voiceover(script_beats, backend="edge", out_dir=Path,
                       voice=None, rate=None, pitch=None) -> dict

    script_beats : list of beat dicts
        [{"text": ..., "emotion": intense|serious|curious|playful|urgent|
          calm|triumphant, "emphasis_words": [...], "pause_after_ms": 0-800}]
        (a plain string is also accepted and treated as one neutral beat)
    returns : {"audio_path": Path, "words": [ {word,start,end,beat} ],
               "beats": [ {..., "start", "end"} ], "backend": str}

Every beat is synthesized with its own prosody profile (faster/higher for
excitement, slower/lower for serious statements), pauses are inserted as
real silence, and word timings are offset onto the final mixed timeline so
downstream captions/cuts stay frame-accurate.

Backends
--------
    edge        free Microsoft neural voices. Per-beat rate/pitch deltas,
                exact WordBoundary timestamps. Needs no API key.
    elevenlabs  needs ELEVENLABS_API_KEY (optional ELEVENLABS_VOICE_ID,
                ELEVENLABS_MODEL_ID). Break tags for pauses, emotion-tuned
                voice_settings. Word timings are estimated.
    playht      needs PLAYHT_API_KEY + PLAYHT_USER_ID (optional
                PLAYHT_VOICE_URI). Emotion/speed params. Estimated words.

A failed premium backend prints a warning and falls back to edge
(TTS_FALLBACK_EDGE=0 turns the fallback off), so a render never dies on TTS.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 44100
TICKS_PER_SECOND = 10_000_000
BEAT_GAP_MS = 120                    # natural micro-pause between beats
MAX_PAUSE_MS = 800

EMOTIONS = ("intense", "serious", "curious", "playful", "urgent",
            "calm", "triumphant")

# Per-emotion delivery. rate/pitch deltas are ADDED to the user's base
# EDGE_TTS_RATE / EDGE_TTS_PITCH (e.g. base "+10%" + intense "+12%" -> "+22%").
EMOTION_PROSODY = {
    "intense":    {"rate": "+12%", "pitch": "+12Hz"},
    "serious":    {"rate": "-5%",  "pitch": "-8Hz"},
    "curious":    {"rate": "+3%",  "pitch": "+14Hz"},
    "playful":    {"rate": "+8%",  "pitch": "+22Hz"},
    "urgent":     {"rate": "+16%", "pitch": "+6Hz"},
    "calm":       {"rate": "-10%", "pitch": "-4Hz"},
    "triumphant": {"rate": "+5%",  "pitch": "+16Hz"},
}

# ElevenLabs voice_settings per emotion (0..1 scale).
ELEVEN_SETTINGS = {
    "intense":    {"stability": 0.30, "similarity_boost": 0.80, "style": 0.55},
    "serious":    {"stability": 0.60, "similarity_boost": 0.75, "style": 0.20},
    "curious":    {"stability": 0.45, "similarity_boost": 0.75, "style": 0.35},
    "playful":    {"stability": 0.35, "similarity_boost": 0.75, "style": 0.60},
    "urgent":     {"stability": 0.25, "similarity_boost": 0.80, "style": 0.65},
    "calm":       {"stability": 0.75, "similarity_boost": 0.70, "style": 0.10},
    "triumphant": {"stability": 0.40, "similarity_boost": 0.80, "style": 0.50},
}

# PlayHT speech v2 "emotion" hints where supported (best effort, omitted on 400).
PLAYHT_EMOTION = {
    "intense": "female_angry", "serious": "female_serious",
    "curious": "female_curious", "playful": "female_cheerful",
    "urgent": "female_angry", "calm": "female_whisper",
    "triumphant": "female_cheerful",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ffmpeg(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True, timeout=300)


def _pct(base: str, delta: str) -> str:
    """Add two edge-tts rate strings like '+10%' and '-5%'."""
    def val(s: str) -> int:
        m = re.search(r"([+-]?\d+)", str(s or "0"))
        return int(m.group(1)) if m else 0
    total = max(-60, min(90, val(base) + val(delta)))
    return f"{total:+d}%"


def _hz(base: str, delta: str) -> str:
    def val(s: str) -> int:
        m = re.search(r"([+-]?\d+)", str(s or "0"))
        return int(m.group(1)) if m else 0
    total = max(-50, min(50, val(base) + val(delta)))
    return f"{total:+d}Hz"


def normalize_beats(script_beats) -> list[dict]:
    """Accept a plain string or a list of beat dicts; return clean beats."""
    if isinstance(script_beats, str):
        script_beats = [{"text": script_beats}]
    out: list[dict] = []
    for i, raw in enumerate(script_beats):
        if isinstance(raw, str):
            raw = {"text": raw}
        text = re.sub(r"\s+", " ", str(raw.get("text", ""))).strip()
        if not text:
            continue
        emotion = str(raw.get("emotion", "serious")).strip().lower()
        if emotion not in EMOTIONS:
            emotion = "serious"
        emphasis = [str(w).strip() for w in (raw.get("emphasis_words") or [])
                    if str(w).strip()][:3]
        try:
            pause = int(raw.get("pause_after_ms") or 0)
        except (TypeError, ValueError):
            pause = 0
        pause = max(0, min(MAX_PAUSE_MS, pause))
        out.append({
            "index": i,
            "text": text,
            "emotion": emotion,
            "emphasis_words": emphasis,
            "visual_concept": str(raw.get("visual_concept", "")).strip()[:80],
            "pause_after_ms": pause,
        })
    return out


def _decode_to_pcm(path: Path) -> np.ndarray:
    """Any audio file -> float32 mono PCM at SAMPLE_RATE via ffmpeg."""
    tmp = path.with_suffix(".decoded.wav")
    proc = _ffmpeg(["-i", str(path), "-ar", str(SAMPLE_RATE), "-ac", "1",
                    "-sample_fmt", "s16", str(tmp)])
    if proc.returncode != 0 or not tmp.is_file():
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.strip()[:120]}")
    with wave.open(str(tmp), "rb") as wav:
        pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
    tmp.unlink(missing_ok=True)
    return pcm.astype(np.float32) / 32768.0


def _write_wav_pcm(pcm: np.ndarray, path: Path) -> None:
    """Write float PCM in [-1, 1] as 16-bit mono WAV, peak-limited to 0.94."""
    peak = float(np.abs(pcm).max()) if pcm.size else 0.0
    if peak > 0.94:
        pcm = pcm * (0.94 / peak)
    frames = (np.clip(pcm, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames.tobytes())


def _wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    proc = _ffmpeg(["-i", str(wav_path), "-codec:a", "libmp3lame",
                    "-b:a", "192k", str(mp3_path)])
    if proc.returncode != 0 or not mp3_path.is_file():
        raise RuntimeError(f"ffmpeg mp3 encode failed: {proc.stderr.strip()[:120]}")


def _assemble(segments: list[tuple[np.ndarray, int]], total_beats: int,
              out_dir: Path) -> tuple[Path, list[float]]:
    """Concatenate beat PCMs with silence gaps -> (voiceover.mp3, boundary
    list where each entry is the time in SECONDS at which that beat's audio
    starts on the final timeline)."""
    gap = np.zeros(int(SAMPLE_RATE * BEAT_GAP_MS / 1000), dtype=np.float32)
    parts: list[np.ndarray] = []
    boundaries: list[float] = []
    pos = 0
    for i, (pcm, pause_ms) in enumerate(segments):
        boundaries.append(pos / SAMPLE_RATE)
        parts.append(pcm)
        pos += len(pcm)
        tail = gap.copy()
        if pause_ms > 0:
            tail = np.concatenate([tail, np.zeros(
                int(SAMPLE_RATE * pause_ms / 1000), dtype=np.float32)])
        if i < len(segments) - 1:            # no trailing gap after last beat
            parts.append(tail)
            pos += len(tail)
    mixed = np.concatenate(parts) if parts else np.zeros(1, dtype=np.float32)
    wav_path = out_dir / "_voiceover_assembled.wav"
    _write_wav_pcm(mixed, wav_path)
    mp3_path = out_dir / "voiceover.mp3"
    _wav_to_mp3(wav_path, mp3_path)
    wav_path.unlink(missing_ok=True)
    return mp3_path, boundaries


def _estimate_words(beats: list[dict], boundaries: list[float],
                    lengths: list[float]) -> list[dict]:
    """Duration-proportional word timings for backends without boundaries.

    `lengths[i]` is each beat's audio length in seconds; words inside a beat
    are allocated time proportional to their character count."""
    words: list[dict] = []
    for beat, start_s, dur_s in zip(beats, boundaries, lengths):
        tokens = beat["text"].split()
        if not tokens:
            continue
        weights = np.array([max(len(re.sub(r"[^A-Za-z0-9']", "", w)), 1)
                            for w in tokens], dtype=float)
        cuts = np.concatenate([[0.0], np.cumsum(weights / weights.sum())]) * dur_s
        for j, token in enumerate(tokens):
            words.append({
                "word": token,
                "start": round(start_s + float(cuts[j]), 3),
                "end": round(start_s + float(cuts[j + 1]), 3),
                "beat": beat["index"],
                "estimated": True,
            })
    return words


# ---------------------------------------------------------------------------
# Backend: edge-tts (free, exact word boundaries)
# ---------------------------------------------------------------------------

async def _edge_synthesize_beat(text: str, voice: str, rate: str, pitch: str,
                                out_path: Path) -> list[dict]:
    """One beat -> mp3 + raw WordBoundary chunks (100-ns ticks)."""
    import edge_tts
    kwargs: dict = {"rate": rate}
    if pitch:
        kwargs["pitch"] = pitch
    if "boundary" in inspect.signature(edge_tts.Communicate.__init__).parameters:
        kwargs["boundary"] = "WordBoundary"
    communicate = edge_tts.Communicate(text, voice, **kwargs)
    raw: list[dict] = []
    with out_path.open("wb") as fh:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                fh.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                raw.append(chunk)
    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RuntimeError(f"edge-tts produced no audio for: {text[:40]}")
    return raw


async def _voiceover_edge(beats: list[dict], out_dir: Path, voice: str,
                          base_rate: str, base_pitch: str) -> tuple:
    beat_files: list[Path] = []
    word_chunks: list[list[dict]] = []
    for beat in beats:
        prosody = EMOTION_PROSODY.get(beat["emotion"], EMOTION_PROSODY["serious"])
        f = out_dir / f"_beat_{beat['index']:02d}.mp3"
        raw = await _edge_synthesize_beat(
            beat["text"], voice, _pct(base_rate, prosody["rate"]),
            _hz(base_pitch, prosody["pitch"]), f)
        beat_files.append(f)
        word_chunks.append(raw)

    segments, lengths = [], []
    for beat, f in zip(beats, beat_files):
        pcm = _decode_to_pcm(f)
        segments.append((pcm, beat["pause_after_ms"]))
        lengths.append(len(pcm) / SAMPLE_RATE)
    audio_path, boundaries = _assemble(segments, len(beats), out_dir)
    for f in beat_files:
        f.unlink(missing_ok=True)

    # exact word timings: beat offset + tick offsets, punctuation restored
    words: list[dict] = []
    for beat, chunks, start_s in zip(beats, word_chunks, boundaries):
        script_words = beat["text"].split()
        cursor = 0
        for chunk in chunks:
            token = str(chunk.get("text", "")).strip()
            if not token:
                continue
            off = int(chunk.get("offset", 0)) / TICKS_PER_SECOND
            dur = int(chunk.get("duration", 0)) / TICKS_PER_SECOND
            # best-effort punctuation restore from the beat text
            norm = re.sub(r"[^a-z0-9']", "", token.lower())
            for j in range(cursor, len(script_words)):
                if re.sub(r"[^a-z0-9']", "", script_words[j].lower()) == norm:
                    token = script_words[j]
                    cursor = j + 1
                    break
            words.append({"word": token, "start": round(start_s + off, 3),
                          "end": round(start_s + off + dur, 3),
                          "beat": beat["index"]})
    return audio_path, words, lengths


def _run_async(coro):
    """asyncio.run, safe to call from inside a running loop (step1's
    run_pipeline is async) - delegates to a fresh loop in a worker thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _run_edge(beats, out_dir, voice, base_rate, base_pitch):
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return _run_async(_voiceover_edge(beats, out_dir, voice, base_rate, base_pitch))


# ---------------------------------------------------------------------------
# Backend: ElevenLabs
# ---------------------------------------------------------------------------

def _voiceover_elevenlabs(beats, out_dir, voice_id: str, model_id: str):
    import requests
    api_key = (os.getenv("ELEVENLABS_API_KEY", "") or "").strip()
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    session = requests.Session()
    session.headers.update({"xi-api-key": api_key, "Accept": "audio/mpeg"})
    segments, lengths = [], []
    for beat in beats:
        settings = ELEVEN_SETTINGS.get(beat["emotion"], ELEVEN_SETTINGS["serious"])
        text = beat["text"]
        if beat["pause_after_ms"] > 0:
            text += f' <break time="{beat["pause_after_ms"] / 1000:.1f}s" />'
        r = session.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            json={"text": text, "model_id": model_id,
                  "voice_settings": {**settings, "use_speaker_boost": True}},
            timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"elevenlabs HTTP {r.status_code}: {r.text[:120]}")
        f = out_dir / f"_el_{beat['index']:02d}.mp3"
        f.write_bytes(r.content)
        pcm = _decode_to_pcm(f)
        f.unlink(missing_ok=True)
        segments.append((pcm, 0))            # breaks already inside the audio
        lengths.append(len(pcm) / SAMPLE_RATE)
    audio_path, boundaries = _assemble(segments, len(beats), out_dir)
    words = _estimate_words(beats, boundaries, lengths)
    return audio_path, words, lengths


# ---------------------------------------------------------------------------
# Backend: PlayHT
# ---------------------------------------------------------------------------

def _voiceover_playht(beats, out_dir, voice_uri: str):
    import requests
    api_key = (os.getenv("PLAYHT_API_KEY", "") or "").strip()
    user_id = (os.getenv("PLAYHT_USER_ID", "") or "").strip()
    if not api_key or not user_id:
        raise RuntimeError("PLAYHT_API_KEY / PLAYHT_USER_ID not set")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {api_key}",
                            "X-User-Id": user_id, "Accept": "audio/mpeg",
                            "Content-Type": "application/json"})
    segments, lengths = [], []
    for beat in beats:
        speed = {"urgent": 1.15, "intense": 1.1, "calm": 0.9,
                 "serious": 0.95}.get(beat["emotion"], 1.0)
        body = {"text": beat["text"], "voice": voice_uri,
                "output_format": "mp3", "speed": speed}
        r = session.post("https://api.play.ht/api/v2/tts", json=body, timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"playht HTTP {r.status_code}: {r.text[:120]}")
        f = out_dir / f"_ph_{beat['index']:02d}.mp3"
        f.write_bytes(r.content)
        pcm = _decode_to_pcm(f)
        f.unlink(missing_ok=True)
        segments.append((pcm, beat["pause_after_ms"]))
        lengths.append(len(pcm) / SAMPLE_RATE)
    audio_path, boundaries = _assemble(segments, len(beats), out_dir)
    words = _estimate_words(beats, boundaries, lengths)
    return audio_path, words, lengths


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_voiceover(script_beats, backend: str = "edge",
                       out_dir: Path | None = None, voice: str | None = None,
                       rate: str | None = None, pitch: str | None = None,
                       base_dir: Path | None = None) -> dict:
    """Synthesize `script_beats` with the chosen backend.

    Returns {"audio_path", "words", "beats", "backend"}. See module docstring
    for the beat dict shape. Premium backends fall back to edge on failure
    unless TTS_FALLBACK_EDGE=0."""
    beats = normalize_beats(script_beats)
    if not beats:
        raise RuntimeError("no usable beats to synthesize")
    out_dir = Path(out_dir) if out_dir else (Path(base_dir) if base_dir else Path.cwd())
    out_dir.mkdir(parents=True, exist_ok=True)
    backend = (backend or "edge").strip().lower()
    voice = (voice or os.getenv("EDGE_TTS_VOICE", "")).strip() or \
        "en-US-AndrewMultilingualNeural"
    base_rate = (rate or os.getenv("EDGE_TTS_RATE", "+0%")).strip() or "+0%"
    base_pitch = (pitch or os.getenv("EDGE_TTS_PITCH", "")).strip() or ""

    try:
        if backend == "edge":
            audio_path, words, lengths = _run_edge(
                beats, out_dir, voice, base_rate, base_pitch)
        elif backend == "elevenlabs":
            voice_id = (os.getenv("ELEVENLABS_VOICE_ID", "")).strip() or voice
            model_id = (os.getenv("ELEVENLABS_MODEL_ID", "")).strip() or \
                "eleven_multilingual_v2"
            audio_path, words, lengths = _voiceover_elevenlabs(
                beats, out_dir, voice_id, model_id)
            words = words if words else []
        elif backend == "playht":
            voice_uri = (os.getenv("PLAYHT_VOICE_URI", "")).strip() or voice
            audio_path, words, lengths = _voiceover_playht(
                beats, out_dir, voice_uri)
        else:
            raise RuntimeError(f"unknown TTS backend '{backend}' "
                               "(edge | elevenlabs | playht)")
    except Exception as exc:
        if os.getenv("TTS_FALLBACK_EDGE", "1").strip().lower() in ("0", "off", "false"):
            raise
        print(f"      tts: {backend} backend failed ({str(exc)[:100]}) "
              "- falling back to edge-tts")
        backend = "edge"
        audio_path, words, lengths = _run_edge(
            beats, out_dir, voice, base_rate, base_pitch)

    # beat start/end on the final timeline
    starts: list[float | None] = []
    for w in words:
        b = w.get("beat", 0)
        if len(starts) <= b:
            starts.extend([None] * (b + 1 - len(starts)))
        if starts[b] is None:
            starts[b] = w["start"]
    for beat in beats:
        i = beat["index"]
        beat["start"] = round(float(starts[i]) if i < len(starts)
                              and starts[i] is not None else 0.0, 3)
        nxt = starts[i + 1] if i + 1 < len(starts) and starts[i + 1] is not None \
            else None
        beat["end"] = round(min(nxt, beat["start"] + lengths[i])
                            if nxt is not None else beat["start"] + lengths[i], 3)
    return {"audio_path": audio_path, "words": words, "beats": beats,
            "backend": backend}


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    fixture = [
        {"text": "Doing favors makes people like you less, not more.",
         "emotion": "intense", "emphasis_words": ["like you less"],
         "pause_after_ms": 400},
        {"text": "It's called the Ben Franklin effect.",
         "emotion": "curious", "emphasis_words": ["Ben Franklin effect"],
         "pause_after_ms": 0},
        {"text": "Stop helping them. Ask them to help you.",
         "emotion": "triumphant", "emphasis_words": ["Ask them to help you"],
         "pause_after_ms": 0},
    ]
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/_tts_selftest")
    result = generate_voiceover(fixture, backend=os.getenv("TTS_BACKEND", "edge"),
                                out_dir=dest)
    dur = result["words"][-1]["end"] if result["words"] else 0
    print(f"self-test OK: {result['backend']} backend, "
          f"{len(result['words'])} words, ~{dur:.1f}s -> {result['audio_path']}")
    for b in result["beats"]:
        print(f"  beat {b['index']} [{b['emotion']}] {b['start']:.2f}-{b['end']:.2f}s "
              f"pause={b['pause_after_ms']}ms: {b['text'][:50]}")
