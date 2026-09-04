#!/usr/bin/env python3
"""
step2_render_video.py - Step 2: render the final vertical short.

What it does
------------
1. Loads a run folder produced by step 1 (voiceover.mp3 + word_timings.json).
2. Builds the 9:16 vertical background (1080x1920):
     - DYNAMIC (preferred): if step 1 stored "search_queries" and a
       PEXELS_API_KEY is available (env or .env), one vertical Pexels clip
       per query is downloaded, and the clips are hard-cut at the
       concept-transition timestamps - each cut snaps to a natural pause in
       the narration (computed from the word-level timings), so the scene
       changes exactly when the spoken idea changes.
     - STATIC fallback: --background / background.mp4 / assets/background.mp4,
       center-cropped to 9:16, looped/trimmed to the voiceover length.
   Either way the background is pre-rendered once to an intermediate file
   so the main render only decodes frames (much faster).
3. Adds a dark ambient music bed (music_gen.py; Gemini-generated audio is
   used automatically if such a model ever becomes available), looped with
   afx.AudioLoop to the exact voiceover duration and mixed at 10% volume
   under the voiceover so it never overpowers the narration.
4. Builds dynamic karaoke-style subtitles from the word-level timing JSON:
       - groups of 2-4 words on screen at a time (fast-paced)
       - bundled Anton typeface, ALL CAPS, big, white with a thick black stroke
       - the spoken word pops: accent color, ~10% larger + a thicker
         accent-colored stroke (glow), baseline-aligned so nothing shifts
       - long groups auto-shrink to stay inside the safe area
   Each word group is flattened into a single composite layer, which keeps
   the main render fast. This logic is unchanged - it renders on top of
   whatever background (dynamic or static) was built above.
5. Applies subtle punch-in zooms to the background segments (scale variance
   per scene + a tighter push on the reframe turn) so hard cuts feel
   intentional instead of random.
6. Exports the final video as final_short.mp4 (libx264 + aac, yuv420p).

Outputs (inside the run folder)
    final_short.mp4        the finished 1080x1920 vertical short
    backgrounds/           downloaded Pexels clips (dynamic mode)
    _music_bed.wav         generated ambient loop (reused on re-renders)
    _background_*.mp4      pre-rendered 9:16 background cache

Usage
-----
    python step2_render_video.py                          # latest step-1 run
    python step2_render_video.py --run-id run_20260831_...  # specific run
    python step2_render_video.py --background mybg.mp4    # force static bg
    python step2_render_video.py --highlight none         # plain white captions
    python step2_render_video.py --mixed-case            # normal casing (ALL CAPS default)
    python step2_render_video.py --limit 10               # quick 10s preview
    python step2_render_video.py --music none             # no music bed
    python step2_render_video.py --no-punchins            # flat background zoom

Background video (static fallback)
----------------------------------
    Drop any video file (mp4/mov/mkv, any resolution, landscape or portrait)
    named background.mp4 next to this script - or pass --background <path>.
    Landscape sources are center-cropped to 9:16; short clips are looped.

Font
----
    Auto-detected per OS (Impact / Arial Bold / DejaVu Sans Bold).
    Override with --font /path/to/font.ttf or CAPTION_FONT in .env.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path

# Load .env (PEXELS_API_KEY / MUSIC_LEVEL / BG_MUSIC / GEMINI_MUSIC_MODEL)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except Exception:
    pass

try:
    from moviepy import (AudioFileClip, CompositeAudioClip, CompositeVideoClip,
                         ImageClip, TextClip, VideoFileClip, afx,
                         concatenate_videoclips, vfx)
except ImportError:
    sys.exit("Missing dependency 'moviepy' (>= 2.0). Run:  pip install -r requirements.txt")

from PIL import ImageFont

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TARGET_W, TARGET_H = 1080, 1920          # 9:16 vertical output

# Horizontal safe area (Issue 2): Instagram/TikTok overlay their own UI on
# vertical video - username/follow chip at the top, like/comment/share rail
# hugging the right edge, caption text + music line at the bottom. Captions
# stay inside 8% from the left and 18% from the right so the button rail can
# never cover a word.
CAPTION_LEFT_MARGIN_PCT = 0.08
CAPTION_RIGHT_MARGIN_PCT = 0.18
CAPTION_LEFT_MARGIN = int(TARGET_W * CAPTION_LEFT_MARGIN_PCT)          # 86 px

# Hard cap on caption text width (v3): no caption line may ever exceed ~84%
# of the frame width, whatever the margins say - this is what keeps text out
# of the right-side icon rail on every platform (like/comment/share buttons).
# The effective wrap width is min(margin width, this cap).
CAPTION_MAX_WIDTH_PCT = 0.84

# Vertical caption anchor + bottom clearance (Issue 2, v3 - platform aware):
# each platform's bottom chrome covers a different share of the frame, so a
# single fixed anchor is wrong. YouTube Shorts' bottom UI (channel name +
# title that can wrap to 2 lines + the Public/views line) eats ~28% of frame
# height; Instagram/TikTok need less. caption_vertical_position_pct places
# the caption LINE (anchor), bottom_clearance_pct keeps everything below
# (1 - clearance) * frame_height free of captions AND title cards.
# Both are overridable per video (explicit --caption-y-pct / env wins over
# the preset); the same preset drives the hook title card.
PLATFORM_PRESETS = {
    #            caption anchor   bottom UI clearance
    "youtube":   {"caption_y": 0.52, "bottom_clearance": 0.28},
    "instagram": {"caption_y": 0.62, "bottom_clearance": 0.20},
    "tiktok":    {"caption_y": 0.60, "bottom_clearance": 0.22},
    "generic":   {"caption_y": 0.58, "bottom_clearance": 0.24},   # one export for all
}
DEFAULT_PLATFORM = "generic"

# Font sizes as a percentage of FRAME WIDTH (v3) instead of fixed pixels, so
# text scales correctly at any export resolution. At 1080p wide:
#   caption 0.065 -> 70 px | title card 0.075 -> 81 px
# Targets: a 4-5 word caption line spans ~70-80% of frame width (not
# edge-to-edge) and a full hook sentence fits in <= 3 title-card lines
# without touching the left/right safe margins.
DEFAULT_CAPTION_FONT_SIZE_PCT = 0.065
DEFAULT_TITLE_CARD_FONT_SIZE_PCT = 0.075

# Hook title card (Issue 1, Option A): beat[0] opens with a bold static text
# card alone, then hands off to the word-by-word captions.
TITLE_CARD_SECONDS = 1.4                # fixed window, spec 1.2-1.5 s
HOOK_BOOST_FACTOR = 1.15                # beat[0] caption size in word_by_word
HOOK_SCALE_IN_SECS = 0.15               # 110% -> 100% scale-in on beat[0]
TEXT_COLOR = "white"
STROKE_COLOR = "black"
DEFAULT_HIGHLIGHT = "#F5D90A"            # karaoke accent; "none" disables
DEFAULT_EMPHASIS_COLOR = "#FF3D2E"       # metadata-driven emphasis words
DEFAULT_MUSIC_LEVEL = 0.10               # music bed volume under the voice
PEXELS_QUERIES_MAX = 4
PUNCHIN_LADDER = (1.0, 1.05, 1.03, 1.07, 1.04, 1.08)   # per-scene zoom variance
PUNCHIN_TURN_ZOOM = 1.11                 # extra push on the reframe segment
MAX_BG_SEGMENTS = 9                      # one scene per beat, capped

POP_FACTOR = 1.10                        # active-word size pop
GLOW_FACTOR = 1.9                        # active-word stroke multiplier (glow)
EMPHASIS_POP = 1.22                      # emphasis-word size pop (bigger)
EMPHASIS_GLOW = 2.3                      # emphasis-word stroke multiplier

# v2 zoom pulse: a short subtle scale 'punch' at every beat cut
PULSE_AMP = 0.05                         # +5% scale right after the cut
PULSE_TAU = 0.30                         # exponential decay constant (s)
DUCK_FLOOR = 0.45                        # music keeps 45% volume under speech

# Bundled typeface (SIL OFL 1.1 - free for commercial use, see fonts/OFL.txt).
# Anton is the classic Shorts caption face: tall, heavy, condensed. Shipping
# it in the repo makes captions identical on every machine and in CI.
_SHIPPED_FONT = Path(__file__).resolve().parent / "fonts" / "Anton-Regular.ttf"
FONT_CANDIDATES = [
    str(_SHIPPED_FONT),                  # bundled Anton - always preferred
    # Windows
    "C:/Windows/Fonts/impact.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "C:/Windows/Fonts/arial.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Impact.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resolve_font(explicit: str | None) -> str:
    """Find a usable bold TrueType font (explicit arg > env > per-OS probe)."""
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path)
        sys.exit(f"ERROR: font file not found: {explicit}")
    env = os.getenv("CAPTION_FONT", "").strip()
    if env and Path(env).expanduser().is_file():
        return str(Path(env).expanduser())
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    try:  # last resort on Linux/macOS with fontconfig
        out = subprocess.run(["fc-list", ":style=Bold", "file"],
                             capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            path = line.split(":")[0].strip()
            if path.endswith((".ttf", ".otf")):
                return path
    except Exception:
        pass
    sys.exit("ERROR: no bold .ttf font found. Install one or pass --font <path>")


def find_run_folder(output_dir: str, run_id: str | None) -> Path:
    base = Path(output_dir)
    if run_id:
        folder = base / run_id
        if not folder.is_dir():
            sys.exit(f"ERROR: run folder not found: {folder}")
    else:
        if not base.is_dir():
            sys.exit(f"ERROR: no '{base}' directory yet - run step 1 first")
        candidates = [
            d for d in base.iterdir()
            if d.is_dir()
            and (d / "voiceover.mp3").is_file()
            and (d / "word_timings.json").is_file()
        ]
        if not candidates:
            sys.exit("ERROR: no run folder containing voiceover.mp3 + word_timings.json "
                     "- run step 1 first")
        folder = max(candidates, key=lambda d: d.stat().st_mtime)
    for required in ("voiceover.mp3", "word_timings.json"):
        if not (folder / required).is_file():
            sys.exit(f"ERROR: {required} missing in {folder} - re-run step 1")
    return folder


# ---------------------------------------------------------------------------
# Subtitle grouping: 2-4 words per group, split on pauses and sentence ends
# ---------------------------------------------------------------------------

def group_words(words: list[dict], max_words: int, pause_threshold: float) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            ends_sentence = current[-1]["word"].rstrip().endswith((".", "!", "?"))
            # v2: a beat boundary always forces a new caption group, so
            # captions follow the script's sentence/beat rhythm (1-6 words)
            new_beat = (word.get("beat") is not None
                        and current[-1].get("beat") is not None
                        and word["beat"] != current[-1]["beat"])
            if (len(current) >= max_words or gap > pause_threshold
                    or ends_sentence or new_beat):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)

    # Rebalance so almost no group is a single lonely word.
    merged: list[list[dict]] = []
    for group in groups:
        if merged and len(group) == 1:
            if len(merged[-1]) < max_words:
                merged[-1].append(group[0])
                continue
            moved = merged[-1].pop()          # 4 -> 3, and the singleton becomes 2
            group.insert(0, moved)
        merged.append(group)
    return merged


def display_windows(groups: list[list[dict]], audio_duration: float) -> list[tuple[float, float]]:
    """Each group stays visible until the next one appears (short hold at the end)."""
    windows = []
    for i, group in enumerate(groups):
        start = group[0]["start"]
        last_end = group[-1]["end"]
        if i + 1 < len(groups):
            end = min(groups[i + 1][0]["start"], last_end + 1.5)
        else:
            end = min(audio_duration, last_end + 0.6)
        windows.append((start, max(end, last_end)))
    return windows


# ---------------------------------------------------------------------------
# Background: center-crop to 9:16, loop/trim to audio, pre-render once
# ---------------------------------------------------------------------------

def crop_to_vertical(clip: VideoFileClip) -> VideoFileClip:
    width, height = clip.size
    target_ratio = TARGET_W / TARGET_H
    if width / height > target_ratio:          # too wide -> crop the sides
        new_w, new_h = int(round(height * target_ratio)), height
    else:                                      # too tall -> crop top/bottom
        new_w, new_h = width, int(round(width / target_ratio))
    x1 = (width - new_w) // 2
    y1 = (height - new_h) // 2
    return clip.cropped(x1=x1, y1=y1, x2=x1 + new_w, y2=y1 + new_h).resized((TARGET_W, TARGET_H))


def prepare_background(background_path: Path, run_folder: Path,
                       duration: float, fps: int) -> VideoFileClip:
    """Crop to 9:16 + loop/trim to the audio length, then write one
    intermediate file so the main render only decodes frames."""
    cache = run_folder / "_background_9x16.mp4"
    if cache.is_file():
        try:
            cached = VideoFileClip(str(cache))
            if cached.duration and cached.duration >= duration - 0.2:
                cached = cached.subclipped(0, min(duration, cached.duration))
                print("      reusing cached 9:16 background: _background_9x16.mp4")
                return cached
            cached.close()
        except Exception:
            pass  # corrupted cache -> rebuild

    source = VideoFileClip(str(background_path))
    source = crop_to_vertical(source)
    if source.duration is None or source.duration < duration:
        source = source.with_effects([vfx.Loop(duration=duration)])
    elif source.duration > duration + 0.05:
        source = source.subclipped(0, duration)
    print("      pre-rendering 9:16 background (one-time) ...")
    source.write_videofile(str(cache), codec="libx264", preset="faster",
                           fps=fps, audio=False, logger=None)
    source.close()
    background = VideoFileClip(str(cache))
    return background.subclipped(0, min(duration, background.duration))


def prepare_background_only(background_path: Path, run_folder: Path,
                            duration: float, fps: int) -> None:
    """Pre-render the 9:16 background cache, then exit (no main render)."""
    prepare_background(background_path, run_folder, duration, fps)
    print("      background cache ready.")


# ---------------------------------------------------------------------------
# Dynamic backgrounds: concept-transition hard cuts between Pexels clips
# ---------------------------------------------------------------------------

def compute_cut_points(words: list[dict], n_segments: int,
                       total: float) -> list[float]:
    """Timestamps where the visual should hard-cut to the next scene.

    The script's concept transitions are approximated by splitting the
    timeline into n_segments even parts, then snapping each target time to
    the nearest word boundary - preferring real pauses in the speech, so a
    cut never lands mid-word and usually coincides with a natural beat in
    the narration."""
    if n_segments <= 1 or total <= 0:
        return []
    cuts: list[float] = []
    for i in range(1, n_segments):
        target = total * i / n_segments
        best_mid, best_score = None, -1e9
        for a, b in zip(words, words[1:]):
            mid = (a["end"] + b["start"]) / 2
            if mid <= 0.5 or mid >= total - 0.25:
                continue
            if abs(mid - target) > 5.0:
                continue
            gap = max(0.0, b["start"] - a["end"])
            score = min(gap, 1.0) - abs(mid - target) * 0.02
            if score > best_score:
                best_score, best_mid = score, mid
        cut = best_mid if best_mid is not None else target
        if not cuts or cut - cuts[-1] >= 1.5:      # keep cuts apart
            cuts.append(round(min(max(cut, 0.5), total - 0.25), 3))
    return cuts


def _load_segment(path: Path, seg_duration: float, seg_index: int,
                  zoom: float = 1.0, pulse_amp: float = 0.0) -> tuple:
    """One 9:16-patched piece of a Pexels clip (looped if too short).
    `zoom` > 1 applies a centered punch-in crop before the final resize.
    `pulse_amp` > 0 adds a short zoom pulse that decays after the cut
    (0.05 = +5% scale easing back over ~0.9 s) - the beat 'punch'.
    Returns (segment_clip, parent_clip_to_keep_open)."""
    src = VideoFileClip(str(path))
    src = crop_to_vertical(src)
    if zoom > 1.01:
        w, h = src.size
        nw, nh = int(w / zoom), int(h / zoom)
        x1 = (w - nw) // 2
        y1 = (h - nh) // 2
        src = src.cropped(x1=x1, y1=y1, x2=x1 + nw, y2=y1 + nh) \
                 .resized((TARGET_W, TARGET_H))
    if pulse_amp > 0:
        import numpy as np
        from PIL import Image as PILImage

        def pulse_frame(get_frame, t, amp=pulse_amp,
                        w=TARGET_W, h=TARGET_H):
            frame = get_frame(t)
            zz = 1.0 + amp * math.exp(-t / PULSE_TAU)
            nw, nh = int(w / zz), int(h / zz)
            px1, py1 = (w - nw) // 2, (h - nh) // 2
            crop = frame[py1:py1 + nh, px1:px1 + nw]
            return np.asarray(
                PILImage.fromarray(crop).resize((w, h), PILImage.BILINEAR))

        src = src.transform(pulse_frame)
    src_dur = src.duration or seg_duration
    if src_dur > seg_duration + 0.05:
        max_offset = src_dur - seg_duration
        offset = (seg_index * 2.7) % max_offset    # vary start across segments
        return src.subclipped(offset, offset + seg_duration), src
    if src_dur < seg_duration:
        return src.with_effects([vfx.Loop(duration=seg_duration)]), src
    return src.subclipped(0, seg_duration), src


def build_dynamic_background(clip_paths: list[Path], cuts: list[float],
                             duration: float, fps: int,
                             run_folder: Path,
                             zooms: list[float] | None = None,
                             pulse_amp: float = 0.0) -> VideoFileClip:
    """Concatenate the Pexels clips with hard cuts at `cuts` (method="chain"
    = exact frame cut, no transition), pre-rendered to a cache file so the
    main render only decodes frames. `zooms[i]` is the punch-in factor for
    segment i (1.0 = untouched); `pulse_amp` adds the beat-synced zoom pulse."""
    bounds = [0.0] + list(cuts) + [float(duration)]
    parents: list = []
    segments = []
    for i in range(len(bounds) - 1):
        seg_dur = max(0.2, bounds[i + 1] - bounds[i])
        path = clip_paths[i % len(clip_paths)]
        zoom = float(zooms[i]) if zooms and i < len(zooms) else 1.0
        seg, parent = _load_segment(path, seg_dur, i, zoom, pulse_amp)
        parents.append(parent)
        segments.append(seg)
    combined = concatenate_videoclips(segments, method="chain")
    combined = combined.with_duration(duration)

    tag = ("|".join(p.name for p in clip_paths) + "|"
           + ",".join(f"{c:.2f}" for c in cuts) + f"|{duration:.2f}"
           + "|" + ",".join(f"{z:.2f}" for z in (zooms or []))
           + f"|{pulse_amp:.3f}")
    cache = run_folder / \
        f"_background_dyn_{hashlib.sha1(tag.encode()).hexdigest()[:8]}.mp4"
    if not (cache.is_file() and cache.stat().st_size > 50_000):
        print("      pre-rendering dynamic 9:16 background (one-time) ...")
        combined.write_videofile(str(cache), codec="libx264", preset="faster",
                                 fps=fps, audio=False, logger=None)
    combined.close()
    for parent in parents:
        try:
            parent.close()
        except Exception:
            pass
    background = VideoFileClip(str(cache))
    return background.subclipped(0, min(duration, background.duration))


def resolve_music_bed(mode: str, run_folder: Path, duration: float,
                      mood: str = "serious") -> Path | None:
    """--music auto (default) -> music_gen bed; 'none' -> off; else a path."""
    if mode.strip().lower() == "none":
        return None
    candidate = Path(mode)
    if mode.strip().lower() != "auto" and candidate.is_file():
        return candidate
    try:
        import music_gen
    except Exception as exc:
        print(f"      music: module unavailable ({exc}) - continuing without")
        return None
    try:
        return music_gen.ensure_music_bed(run_folder, duration, mood=mood)
    except Exception as exc:
        print(f"      music: generation failed ({str(exc)[:80]}) - continuing without")
        return None


def punchins_enabled(args) -> bool:
    """--no-punchins or PUNCHINS=0/off disables background zoom variance."""
    if getattr(args, "no_punchins", False):
        return False
    return (os.getenv("PUNCHINS", "") or "").strip().lower() not in (
        "0", "off", "none", "false")


# strategist beat structure, as fractions of the word list:
# end of hook / tension / examples / reframe / escalation (payoff starts here)
BEAT_END_FRACTIONS = (0.08, 0.22, 0.45, 0.65, 0.82)


def estimate_beat_boundaries(words: list[dict]) -> tuple | None:
    """Map the strategist 6-beat structure onto the word timeline.

    Returns (hook_end, tension_end, examples_end, reframe_end,
    escalation_end) in seconds - the payoff starts at escalation_end.
    Returns None for very short word lists (previews)."""
    n = len(words)
    if n < 16:
        return None
    out = []
    for frac in BEAT_END_FRACTIONS:
        idx = min(n - 1, max(0, int(round(n * frac)) - 1))
        out.append(float(words[idx].get("end", 0.0)))
    # guarantee monotonic, sane boundaries
    for i in range(1, len(out)):
        out[i] = max(out[i], out[i - 1] + 0.5)
    return tuple(out)


def punchin_zooms(cuts: list[float], words: list[dict], duration: float,
                  n_segments: int) -> list[float]:
    """Zoom factor per background segment: a small ladder so consecutive
    scenes never sit at the same scale, plus a tighter push on the segment
    containing the reframe turn (the strategist's 'major shift' beat)."""
    zooms = [PUNCHIN_LADDER[i % len(PUNCHIN_LADDER)] for i in range(n_segments)]
    try:
        beats = estimate_beat_boundaries(words)
        if beats:
            turn_t = beats[2]          # examples_end -> the reframe begins
            bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [duration]
            for i in range(len(bounds) - 1):
                if bounds[i] <= turn_t < bounds[i + 1]:
                    zooms[i] = max(zooms[i], PUNCHIN_TURN_ZOOM)
                    break
    except Exception:
        pass
    return [round(z, 3) for z in zooms]


def resolve_background(timings: dict, args, run_folder: Path,
                       duration: float, fps: int) -> tuple:
    """Return (background_clip, kind, cuts).

    Dynamic Pexels splicing wins unless the user forced --background; the
    static background video is the guaranteed fallback. `cuts` are the
    scene-change timestamps (drives the punch-in zooms)."""
    words = timings.get("words", [])
    queries = timings.get("search_queries") or []
    want_punchins = punchins_enabled(args)

    if not args.background and queries:
        clips: list[Path] = []
        try:
            import pexels_bg
            clips = pexels_bg.fetch_backgrounds(
                queries[:PEXELS_QUERIES_MAX], run_folder / "backgrounds")
        except Exception as exc:
            print(f"      pexels: fetch failed ({str(exc)[:80]}) - static fallback")
        if clips:
            cuts = compute_cut_points(words, len(clips), duration)
            print(f"      dynamic background: {len(clips)} scene(s), "
                  f"hard cuts at {[f'{c:.1f}s' for c in cuts]}")
            zooms = (punchin_zooms(cuts, words, duration, len(clips))
                     if want_punchins else None)
            if zooms:
                print(f"      punch-ins: on (zooms {zooms})")
            return (build_dynamic_background(clips, cuts, duration, fps,
                                             run_folder, zooms),
                    "dynamic", cuts)
        print("      falling back to the static background")

    background_path = Path(args.background) if args.background \
        else Path(__file__).resolve().parent / "background.mp4"
    if not background_path.is_file():
        assets_default = (Path(__file__).resolve().parent
                          / "assets" / "background.mp4")
        if assets_default.is_file():
            background_path = assets_default
    if not background_path.is_file():
        sys.exit(f"ERROR: background video not found at {background_path}\n"
                 "  Drop any video file as background.mp4 next to this script,\n"
                 "  pass --background /path/to/video.mp4, or set PEXELS_API_KEY\n"
                 "  to enable dynamic Pexels backgrounds")
    if want_punchins and duration >= 8:
        # Segment the static loop too, so it also gets cuts + zoom variance.
        try:
            n_seg = len(queries) if queries else 4
            cuts = compute_cut_points(words, n_seg, duration)
            if len(cuts) >= 1:
                zooms = punchin_zooms(cuts, words, duration, len(cuts) + 1)
                print(f"      static punch-ins: on (cuts "
                      f"{[f'{c:.1f}s' for c in cuts]}, zooms {zooms})")
                return (build_dynamic_background(
                            [background_path] * (len(cuts) + 1), cuts,
                            duration, fps, run_folder, zooms),
                        "static", cuts)
        except Exception as exc:
            print(f"      static punch-ins skipped ({str(exc)[:60]})")
    return prepare_background(background_path, run_folder, duration, fps), \
        "static", []


# ---------------------------------------------------------------------------
# v2: beat-driven assembly (curated library, concept queries, ducking, mood)
# ---------------------------------------------------------------------------

def env_flag(name: str, default: str = "on") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in \
        ("0", "off", "none", "false")


def load_beats(timings: dict) -> list[dict]:
    """Beats list from word_timings.json (v2 runs embed it; [] otherwise)."""
    beats = timings.get("beats") or []
    return [b for b in beats if isinstance(b, dict) and b.get("text")]


def beat_cut_points(beats: list[dict], duration: float) -> list[float]:
    """Scene cuts exactly when each new beat starts speaking.

    Merges beats closer than 1.2 s and caps the segment count, so a
    10-beat script becomes at most MAX_BG_SEGMENTS scenes."""
    starts = sorted({round(float(b.get("start", 0.0)), 3) for b in beats})
    cuts = [t for t in starts if 0.6 <= t <= duration - 0.25]
    merged: list[float] = []
    for t in cuts:
        if not merged or t - merged[-1] >= 1.2:
            merged.append(t)
    while len(merged) > MAX_BG_SEGMENTS - 1:
        gaps = [(merged[i + 1] - merged[i], i) for i in range(len(merged) - 1)]
        merged.pop(min(gaps)[1] + 1)          # drop the cut with the smallest gap
    return merged


def dominant_mood(beats: list[dict]) -> str:
    """Emotion with the most spoken time across the script."""
    if not beats:
        return "serious"
    totals: dict[str, float] = {}
    for beat in beats:
        try:
            dur = max(0.0, float(beat.get("end", 0)) - float(beat.get("start", 0)))
        except (TypeError, ValueError):
            dur = 0.0
        emotion = (beat.get("emotion") or "serious").lower()
        totals[emotion] = totals.get(emotion, 0.0) + dur
    return max(totals, key=lambda e: totals[e]) if totals else "serious"


def resolve_background_v2(timings: dict, args, run_folder: Path,
                          duration: float, fps: int,
                          beats: list[dict]) -> tuple:
    """v2 background: curated library matches first, then concept-refined
    live Pexels search, then Gemini's classic queries, then the static
    background - all cut exactly at the beat starts with a zoom pulse."""
    cuts = beat_cut_points(beats, duration)
    print(f"      beat cuts: {[f'{c:.1f}s' for c in cuts]} "
          f"({len(cuts) + 1} scenes, one per beat)")
    want_punchins = punchins_enabled(args)
    pulse_amp = PULSE_AMP if (want_punchins and env_flag("BEAT_PULSE")) else 0.0

    # per-beat clip assignment -------------------------------------------------
    beat_clip: dict[int, Path] = {}
    refined_queries: dict[int, str] = {}
    concepts = {b.get("index", i): (b.get("visual_concept") or "")
                for i, b in enumerate(beats)}
    concept_list = [b for b in beats if b.get("visual_concept")]

    if env_flag("CURATED_LIB"):
        try:
            import curated_library as clib
            api_key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
            clip_map, refined = clib.ensure_clips_for_beats(
                beats, run_folder.name, api_key=api_key)
            beat_clip.update(clip_map)
            refined_queries.update(refined)
        except Exception as exc:
            print(f"      curated library unavailable ({str(exc)[:80]})")

    # live-Pexels fallback for beats without a library clip
    missing = [i for i in concepts if i not in beat_clip]
    if missing:
        queries: dict[int, str] = {}
        for i in missing:
            if refined_queries.get(i):
                queries[i] = refined_queries[i]
            elif concepts.get(i):
                queries[i] = " ".join(
                    w for w in concepts[i].lower().replace(",", " ").split()
                    if w not in ("the", "a", "an", "of", "and", "with",
                                 "in", "on", "one", "side"))[:32]
        try:
            import pexels_bg
            used_ids: set[int] = set()
            out_dir = run_folder / "backgrounds"
            out_dir.mkdir(parents=True, exist_ok=True)
            for i, q in queries.items():
                if not q:
                    continue
                got = pexels_bg.search_and_download(
                    q, out_dir / f"concept_{i:02d}.mp4", used_ids)
                if got:
                    beat_clip[i] = got
                if len(beat_clip) >= MAX_BG_SEGMENTS + 2:
                    break
        except Exception as exc:
            print(f"      concept search failed ({str(exc)[:80]})")

    # final per-segment clip list (cuts[i] starts segment i+1) -----------------
    ordered_beats = sorted(beats, key=lambda b: float(b.get("start", 0.0)))
    clip_paths: list[Path] = []
    prev = None
    for cut in [0.0] + cuts:
        beat = None
        for b in ordered_beats:
            if float(b.get("start", 0.0)) <= cut + 0.05:
                beat = b
        idx = beat.get("index") if beat else None
        path = beat_clip.get(idx) if idx is not None else None
        if path is None:
            path = prev or next(iter(beat_clip.values()), None)
        if path is not None:
            clip_paths.append(path)
            prev = path
    if clip_paths and len(clip_paths) < len(cuts) + 1:
        clip_paths += [clip_paths[-1]] * (len(cuts) + 1 - len(clip_paths))

    if clip_paths:
        zooms = (punchin_zooms(cuts, timings.get("words", []), duration,
                               len(clip_paths)) if want_punchins else None)
        if pulse_amp:
            print(f"      beat pulse: on (+{int(PULSE_AMP * 100)}% zoom easing "
                  "back after each cut)")
        return (build_dynamic_background(clip_paths, cuts, duration, fps,
                                         run_folder, zooms, pulse_amp),
                "dynamic", cuts)

    # static fallback, still beat-cut + pulsed ----------------------------------
    print("      falling back to the static background (beat-cut)")
    background_path = Path(args.background) if args.background \
        else Path(__file__).resolve().parent / "background.mp4"
    if not background_path.is_file():
        assets_default = (Path(__file__).resolve().parent
                          / "assets" / "background.mp4")
        if assets_default.is_file():
            background_path = assets_default
    if not background_path.is_file():
        sys.exit(f"ERROR: background video not found at {background_path}")
    if duration >= 8:
        n_seg = min(len(cuts) + 1, MAX_BG_SEGMENTS)
        cuts_static = cuts if cuts else \
            compute_cut_points(timings.get("words", []), n_seg, duration)
        zooms = punchin_zooms(cuts_static, timings.get("words", []),
                              duration, len(cuts_static) + 1) \
            if want_punchins else None
        return (build_dynamic_background(
                    [background_path] * (len(cuts_static) + 1), cuts_static,
                    duration, fps, run_folder, zooms, pulse_amp),
                "static", cuts_static)
    return prepare_background(background_path, run_folder, duration, fps), \
        "static", []


def duck_music_bed(music_path: Path, voiceover_path: Path, duration: float,
                   level: float, out_path: Path) -> Path | None:
    """Sidechain-style ducking: music dips while the voice speaks.

    Envelope of the voiceover (50 ms windows, smoothed) drives the music
    gain between `level` (silence) and `level * DUCK_FLOOR` (full speech)."""
    try:
        import numpy as np
        from tts_engine import SAMPLE_RATE as SR, _decode_to_pcm
        voice = _decode_to_pcm(Path(voiceover_path))
        music = _decode_to_pcm(Path(music_path))
        hop = int(0.05 * SR)
        n_frames = int(math.ceil(len(music) / hop))
        target = n_frames * hop
        voice = voice[:target]                    # preview mode: voice may exceed music
        voice_padded = np.concatenate([voice, np.zeros(target - len(voice))])
        rms = np.sqrt(np.maximum(
            voice_padded[:n_frames * hop].reshape(n_frames, hop) ** 2, 1e-12).mean(axis=1))
        hi = np.percentile(rms, 95)
        activity = np.clip(rms / max(hi, 1e-9), 0.0, 1.0)
        # smooth (simple 3-frame box = 150 ms)
        kernel = np.ones(3) / 3.0
        activity = np.convolve(activity, kernel, mode="same")
        gains = level * (DUCK_FLOOR + (1.0 - DUCK_FLOOR) * (1.0 - activity))
        gain_samples = np.repeat(gains, hop)[:len(music)]
        ducked = music * gain_samples
        # write as 16-bit stereo wav (music_gen beds are stereo; make it match)
        import wave
        peak = float(np.abs(ducked).max()) or 1.0
        if peak > 0.95:
            ducked *= 0.95 / peak
        frames = (ducked * 32767).astype(np.int16)
        with wave.open(str(out_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SR)
            wav.writeframes(frames.tobytes())
        return out_path
    except Exception as exc:
        print(f"      ducking skipped ({str(exc)[:80]})")
        return None


def wrap_by_width(text: str, font: str, size: int, stroke: int,
                  max_width: int) -> list[str]:
    """Word-based wrap by measured width - keeps EVERY word (moviepy's own
    caption wrap can split mid-word) and never truncates the text."""
    try:
        from PIL import ImageFont
        probe = ImageFont.truetype(font, size)
    except Exception:
        probe = None

    def width_of(line: str) -> int:
        if probe is not None:
            return int(probe.getlength(line)) + 2 * stroke
        return int(0.52 * size * len(line)) + 2 * stroke

    lines: list[str] = []
    current = ""
    for word in text.upper().split():
        candidate = f"{current} {word}".strip()
        if not current or width_of(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def target_platform(args) -> str:
    """Platform this export targets - picks the safe-zone preset.

    Resolution order: --target-platform > TARGET_PLATFORM env >
    DEFAULT_PLATFORM ("generic" = one export for all platforms)."""
    for value in (getattr(args, "target_platform", None),
                  os.getenv("TARGET_PLATFORM", "")):
        if value is None or str(value).strip() == "":
            continue
        platform = str(value).strip().lower()
        if platform in PLATFORM_PRESETS:
            return platform
        print(f"      WARNING: unknown target_platform '{platform}' - "
              f"using '{DEFAULT_PLATFORM}' (known: {', '.join(PLATFORM_PRESETS)})")
        break
    return DEFAULT_PLATFORM


def platform_preset(args) -> dict:
    """Safe-zone preset (caption anchor + bottom clearance) for the target."""
    return PLATFORM_PRESETS[target_platform(args)]


def caption_y_pct(args) -> float:
    """Vertical caption anchor as a fraction of frame height.

    Resolution order: --caption-y-pct > --subtitle-y (legacy alias) >
    CAPTION_Y_PCT env (all EXPLICIT overrides) >
    platform preset caption_vertical_position_pct (youtube 0.52 /
    instagram 0.62 / tiktok 0.60 / generic 0.58)."""
    for value in (getattr(args, "caption_y_pct", None),
                  getattr(args, "subtitle_y", None),
                  os.getenv("CAPTION_Y_PCT", "")):
        if value is None or str(value).strip() == "":
            continue
        try:
            return min(max(float(value), 0.0), 1.0)
        except (TypeError, ValueError):
            continue
    return platform_preset(args)["caption_y"]


def bottom_clearance_pct(args) -> float:
    """Fraction of frame height kept clear at the bottom (no captions, no
    title card) - sized to the target platform's bottom UI chrome.

    Resolution order: --bottom-clearance-pct > BOTTOM_CLEARANCE_PCT env >
    platform preset (youtube 0.28 / instagram 0.20 / tiktok 0.22 /
    generic 0.24)."""
    for value in (getattr(args, "bottom_clearance_pct", None),
                  os.getenv("BOTTOM_CLEARANCE_PCT", "")):
        if value is None or str(value).strip() == "":
            continue
        try:
            return min(max(float(value), 0.0), 0.9)
        except (TypeError, ValueError):
            continue
    return platform_preset(args)["bottom_clearance"]


def caption_max_width_pct(args) -> float:
    """Hard cap on caption width as a fraction of frame width (default 0.84).

    Resolution order: --caption-max-width-pct > CAPTION_MAX_WIDTH_PCT env >
    CAPTION_MAX_WIDTH_PCT constant. Whatever the margins allow, rendered
    text never gets wider than this - it is what keeps lines out of the
    right-side icon rail on every platform."""
    for value in (getattr(args, "caption_max_width_pct", None),
                  os.getenv("CAPTION_MAX_WIDTH_PCT", "")):
        if value is None or str(value).strip() == "":
            continue
        try:
            return min(max(float(value), 0.3), 1.0)
        except (TypeError, ValueError):
            continue
    return CAPTION_MAX_WIDTH_PCT


def caption_max_width(args) -> int:
    """Effective text wrap width in px: the tighter of the horizontal
    safe margins (8% left / 18% right -> 74% of width) and the ~84%
    frame-width hard cap."""
    margin_width = TARGET_W - CAPTION_LEFT_MARGIN \
        - int(TARGET_W * CAPTION_RIGHT_MARGIN_PCT)                 # 800 px
    cap_width = int(TARGET_W * caption_max_width_pct(args))        # 907 px
    return min(margin_width, cap_width)


def caption_font_size(args) -> int:
    """Word-by-word caption size in px, as a percentage of frame width.

    Resolution order: --font-size (explicit px override) >
    --caption-font-size-pct > CAPTION_FONT_SIZE_PCT env >
    DEFAULT_CAPTION_FONT_SIZE_PCT (0.065 -> 70 px at 1080 wide)."""
    if getattr(args, "font_size", None):
        return int(args.font_size)
    for value in (getattr(args, "caption_font_size_pct", None),
                  os.getenv("CAPTION_FONT_SIZE_PCT", "")):
        if value is None or str(value).strip() == "":
            continue
        try:
            return int(round(TARGET_W * min(max(float(value), 0.02), 0.2)))
        except (TypeError, ValueError):
            continue
    return int(round(TARGET_W * DEFAULT_CAPTION_FONT_SIZE_PCT))


def title_card_font_size(args) -> int:
    """Hook title-card (and legacy overlay) size in px, as a percentage of
    frame width.

    Resolution order: --title-card-font-size-pct >
    TITLE_CARD_FONT_SIZE_PCT env > DEFAULT_TITLE_CARD_FONT_SIZE_PCT
    (0.075 -> 81 px at 1080 wide, down from the old fixed 92 px)."""
    for value in (getattr(args, "title_card_font_size_pct", None),
                  os.getenv("TITLE_CARD_FONT_SIZE_PCT", "")):
        if value is None or str(value).strip() == "":
            continue
        try:
            return int(round(TARGET_W * min(max(float(value), 0.02), 0.2)))
        except (TypeError, ValueError):
            continue
    return int(round(TARGET_W * DEFAULT_TITLE_CARD_FONT_SIZE_PCT))


def clamp_to_safe_band(y_top: int, box_h: int, args) -> int:
    """Clamp a text block's top-y so the block never crosses the platform's
    bottom-clearance line (and never rides up into the top username bar).

    Applied to word-by-word caption groups AND the hook title card, so both
    respect the same bottom_clearance_pct for the target platform."""
    floor_y = TARGET_H * (1.0 - bottom_clearance_pct(args))   # bottom UI line
    if y_top + box_h > floor_y:                               # would cross it
        y_top = int(floor_y - box_h)
    top_ui = int(TARGET_H * 0.12)                             # top bar zone
    return max(top_ui if y_top < top_ui else y_top, 0)


def build_hook_overlay(hook_text: str, hook_end: float, font: str, args) -> object:
    """Bold hook text pinned over the opening clip for the first ~2 s.

    Rendered independently of the caption animation so the very first frame
    already carries the hook (mobile-first: big, top-third, high contrast).
    Used only by --hook-style both (overlay AND captions at once).

    v3: size comes from title_card_font_size_pct (0.075 of frame width) and
    the wrap width from the shared caption_max_width cap."""
    if not hook_text or hook_end <= 0.2:
        return None
    size = title_card_font_size(args)
    stroke = max(5, size // 12)
    max_w = caption_max_width(args)
    text = "\n".join(wrap_by_width(hook_text, font, size, stroke, max_w))

    color = os.getenv("HOOK_COLOR", DEFAULT_HIGHLIGHT)
    clip = TextClip(font=font, text=text, font_size=size, color=color,
                    stroke_color=STROKE_COLOR, stroke_width=stroke,
                    size=(max_w, None), method="caption",
                    text_align="center",
                    margin=(int(size * 0.5), int(size * 0.5)))
    dur = min(max(0.8, hook_end), 2.6)
    return (clip.with_start(0.0).with_duration(dur)
                .with_position(("center", int(TARGET_H * 0.10)))
                .with_effects([vfx.CrossFadeOut(0.3)]))


def build_title_card(hook_text: str, dur: float, font: str, args) -> object:
    """Large static hook text shown ALONE (no captions underneath) for the
    first ~1.4 s of the video (Issue 1, Option A).

    Styled like the hook overlay (bold caps, yellow, thick stroke) but placed
    inside the platform safe zone: horizontally within the 8%/18% margins,
    vertically centered on the caption anchor - so when it fades out and the
    word-by-word captions take over, the text simply swaps in one place.

    v3: same platform preset as the captions - its bottom edge is clamped to
    (1 - bottom_clearance_pct) of frame height, and its size comes from
    title_card_font_size_pct (0.075 of frame width) so the full hook
    sentence fits in <= 3 lines without touching the side margins."""
    if not hook_text or dur <= 0.2:
        return None
    size = title_card_font_size(args)
    stroke = max(5, size // 12)
    max_w = caption_max_width(args)
    lines = wrap_by_width(hook_text, font, size, stroke, max_w)
    text = "\n".join(lines)

    color = os.getenv("HOOK_COLOR", DEFAULT_HIGHLIGHT)
    clip = TextClip(font=font, text=text, font_size=size, color=color,
                    stroke_color=STROKE_COLOR, stroke_width=stroke,
                    size=(max_w, None), method="caption",
                    text_align="center",
                    margin=(int(size * 0.5), int(size * 0.5)))
    w, h = clip.size
    x = int(CAPTION_LEFT_MARGIN + (max_w - w) / 2)
    y = int(TARGET_H * caption_y_pct(args) - h / 2)
    y = clamp_to_safe_band(y, h, args)   # platform bottom-clearance aware
    return (clip.with_start(0.0).with_duration(dur)
                .with_position((x, y))
                .with_effects([vfx.CrossFadeOut(0.25)]))


def build_safe_zone_guides(duration: float, anchor_y: float, args) -> list:
    """Debug overlay marking where the TARGET PLATFORM draws its own UI.

    Red rectangles = platform UI zones a caption must never enter (top
    username bar, right button rail, bottom caption/description area sized
    by the platform's bottom_clearance_pct). Green rectangle = the safe
    caption band this renderer targets (anchored on caption_y_pct, clamped
    above the bottom-clearance line). Enable with --safe-zone-guides (or
    RENDER_SAFE_ZONE_GUIDES=on) for a test render, and keep it off for
    final output."""
    import numpy as np
    w_frame, h_frame = TARGET_W, TARGET_H
    floor_y = (1.0 - bottom_clearance_pct(args)) * h_frame

    def rect(x0: float, y0: float, x1: float, y1: float,
             rgb: tuple, opacity: float) -> ImageClip:
        w_px, h_px = max(1, int(x1 - x0)), max(1, int(y1 - y0))
        frame = np.empty((h_px, w_px, 3), dtype=np.uint8)
        frame[:] = rgb
        return (ImageClip(frame).with_start(0.0).with_duration(duration)
                    .with_position((int(x0), int(y0))).with_opacity(opacity))

    green_top = max(0.0, anchor_y - 0.055)
    green_bottom = min(floor_y / h_frame, anchor_y + 0.055)
    return [
        # red: top UI bar (username / follow chip)
        rect(0, 0, w_frame, 0.14 * h_frame, (255, 46, 46), 0.30),
        # red: right button rail (like / comment / share), down to the
        # bottom UI zone for this platform
        rect(0.84 * w_frame, 0.24 * h_frame, w_frame, floor_y,
             (255, 46, 46), 0.30),
        # red: bottom UI (title, channel, caption text, music line) - its
        # height comes from the platform preset (youtube 28% of frame)
        rect(0, floor_y, w_frame, h_frame, (255, 46, 46), 0.30),
        # green: caption safe band (8% left / 18% right margins)
        rect(CAPTION_LEFT_MARGIN, green_top * h_frame,
             w_frame - int(w_frame * CAPTION_RIGHT_MARGIN_PCT),
             green_bottom * h_frame, (60, 255, 90), 0.28),
    ]


# ---------------------------------------------------------------------------
# Karaoke captions: one flattened composite layer per word group
# ---------------------------------------------------------------------------

def build_group_layer(group: list[dict], group_start: float, group_end: float,
                      font: str, args, hook_boost: bool = False) -> CompositeVideoClip:
    """Render one word group as a self-contained composite clip.

    Inside the nested clip the timeline is relative to group_start, so the
    accent word appears exactly when it is spoken.

    Enhanced caption design (2026-09):
      - ALL CAPS by default (--mixed-case restores normal casing)
      - the spoken word POPS: accent color, ~10% larger, wrapped in a
        thicker accent-colored stroke (glow), baseline-aligned and
        center-anchored so only the emphasis moves, never the layout
      - every word reserves a horizontal SLOT as wide as its popped state,
        so the pop can never overlap a neighbour; the box is padded
        upward so the taller pop never clips at the composite bounds

    NOTE on the vertical margin: moviepy's TextClip sizes the image to the
    tight ink bounding box but draws the text at a fixed baseline offset
    (ascent + margin + stroke). For words with no capitals/ascenders that
    offset lands below the image and descenders (p, y, g) get clipped. A
    fixed vertical margin both prevents that AND makes the baseline offset
    identical for every word, so all words share one clean baseline.

    hook_boost (Issue 1, Option B): beat[0] groups under --hook-style
    word_by_word render ~15% larger and scale in from 110% to 100% over the
    first 150 ms, so the hook still lands heavier than every other caption
    without a separate overlay layer.

    v3 sizing/placement: font size comes from caption_font_size_pct (0.065
    of frame width), the wrap/shrink width from caption_max_width (margins
    vs the 84% frame-width cap), and the block is clamped so its bottom
    edge never crosses the platform's bottom_clearance_pct line."""
    font_size = caption_font_size(args)
    if hook_boost:
        font_size = int(font_size * HOOK_BOOST_FACTOR)
    stroke = max(5, font_size // 11)
    highlight = None if args.highlight.lower() == "none" else args.highlight
    pop_size = int(font_size * POP_FACTOR) if highlight else font_size
    glow = max(8, int(stroke * GLOW_FACTOR)) if highlight else stroke
    emphasis_color = getattr(args, "emphasis_color", DEFAULT_EMPHASIS_COLOR)
    max_w = caption_max_width(args)

    def render(text: str, size: int, color: str, width: int) -> TextClip:
        margin_v = int(size * 0.5) + 2 * width
        return TextClip(font=font, text=text, font_size=size,
                        color=color, stroke_color=STROKE_COLOR, stroke_width=width,
                        margin=(0, margin_v))

    def baseline_offset(size: int, width: int) -> int:
        ascent, _ = ImageFont.truetype(font, size).getmetrics()
        return int(size * 0.5) + 2 * width + ascent + width

    def word_text(word: dict) -> str:
        text = word["word"]
        return text.upper() if not args.mixed_case else text

    # Emphasis words (from the script's emphasis_words metadata) pop harder:
    # distinct color, bigger pop, thicker glow. Everything else keeps the
    # classic yellow karaoke accent.
    def accent_spec(word: dict, base_size: int, base_stroke: int) -> tuple | None:
        if not highlight:
            return None
        if word.get("emphasis"):
            return (int(base_size * EMPHASIS_POP), emphasis_color,
                    max(9, int(base_stroke * EMPHASIS_GLOW)))
        return (int(base_size * POP_FACTOR), highlight,
                max(8, int(base_stroke * GLOW_FACTOR)))

    def render_word_set(base_size: int, base_stroke: int) -> tuple:
        bases = [render(word_text(w), base_size, TEXT_COLOR, base_stroke)
                 for w in group]
        specs = [accent_spec(w, base_size, base_stroke) for w in group]
        accents = [render(word_text(w), spec[0], spec[1], spec[2])
                   if spec else None for w, spec in zip(group, specs)]
        return bases, accents, specs

    # Pass 1: measure base words + their per-word popped accents.
    word_clips, accent_clips, accent_specs = render_word_set(font_size, stroke)
    gap = max(4, int(font_size * 0.10))

    # Slot layout: each word's slot is as wide as its widest state (popped
    # accent included), so a pop expands inside its own slot - never onto
    # a neighbour word.
    def slot_widths() -> list[int]:
        if not any(accent_clips):
            return [c.size[0] for c in word_clips]
        return [max(b.size[0], a.size[0] if a else 0)
                for b, a in zip(word_clips, accent_clips)]

    total = sum(slot_widths()) + gap * (len(word_clips) - 1)

    # Pass 2: shrink to fit the horizontal safe area if needed.
    if total > max_w:
        font_size = max(40, int(font_size * max_w / total))
        stroke = max(4, font_size // 11)
        for clip in word_clips:
            clip.close()
        for clip in accent_clips:
            if clip:
                clip.close()
        word_clips, accent_clips, accent_specs = render_word_set(font_size, stroke)
        gap = max(4, int(font_size * 0.10))
        total = sum(slot_widths()) + gap * (len(word_clips) - 1)

    # Vertical layout: base words sit at y=pad; a popped accent reaches
    # above them, so pad the box upward and keep every baseline where it was.
    base_y = baseline_offset(font_size, stroke)     # base baseline from box top
    if any(accent_clips):
        acc_ys = [base_y - baseline_offset(spec[0], spec[2])
                  for spec in accent_specs]
        pad = max(0, -min(acc_ys))
        base_height = max(c.size[1] for c in word_clips)
        acc_height = max(c.size[1] for c in accent_clips if c)
        box_height = max(base_height + pad,
                         acc_height + max(acc_ys) + pad)
    else:
        pad = 0
        base_height = max(c.size[1] for c in word_clips)
        box_height = base_height
    box_w = total
    group_duration = max(0.1, group_end - group_start)

    layers = []
    x = 0
    for i, clip in enumerate(word_clips):
        slot_w = slot_widths()[i]
        # base word (white), centered in its slot, visible the whole window
        layers.append(clip.with_start(0.0)
                           .with_duration(group_duration)
                           .with_position((x + (slot_w - clip.size[0]) // 2, pad)))
        # karaoke accent on top of the base word, only while it is spoken:
        # accent color + size pop + thicker accent stroke (glow),
        # baseline-aligned and centered in the same slot as its base word.
        # Emphasis words (metadata) get their own color + stronger pop.
        if accent_clips and accent_clips[i]:
            accent = accent_clips[i]
            accent = accent.with_start(max(0.0, group[i]["start"] - group_start))
            accent = accent.with_duration(max(0.08, group[i]["end"] - group[i]["start"]))
            accent = accent.with_position((x + (slot_w - accent.size[0]) // 2,
                                           acc_ys[i] + pad))
            layers.append(accent)
        x += slot_w + gap

    composite = CompositeVideoClip(layers, size=(box_w, box_height))
    # place so the visual ink block sits centered on the subtitle line
    y = int(TARGET_H * caption_y_pct(args) + 0.24 * font_size - base_y - pad)
    # clamp: the block's bottom edge must stay above the platform's
    # bottom-clearance line (youtube 28% / instagram 20% / tiktok 22% /
    # generic 24% of frame height) - same rule as the title card
    y = clamp_to_safe_band(y, box_height, args)
    # absolute placement on the final frame, centered inside the effective
    # safe band (8% left margin .. 18% right margin, capped at 84% width)
    x = int(CAPTION_LEFT_MARGIN + (max_w - box_w) / 2)
    if hook_boost:
        # scale-in 110% -> 100% over ~150 ms, kept centered on the same spot
        box_cx, box_cy = x + box_w / 2, y + box_height / 2

        def boost_pos(t: float, _cx=box_cx, _cy=box_cy,
                      _w=box_w, _h=box_height) -> tuple:
            factor = 1.10 - 0.10 * min(t / HOOK_SCALE_IN_SECS, 1.0)
            return (_cx - _w * factor / 2, _cy - _h * factor / 2)

        composite = composite.with_effects([
            vfx.Resize(lambda t: 1.10 - 0.10 * min(t / HOOK_SCALE_IN_SECS, 1.0))])
        return composite.with_start(group_start) \
                        .with_duration(group_duration) \
                        .with_position(boost_pos)
    return composite.with_start(group_start) \
                    .with_duration(group_duration) \
                    .with_position((x, y))


def build_caption_layers(words: list[dict], audio_duration: float, font: str,
                         args, skip_before: float = 0.0,
                         boost_until: float = 0.0) -> list:
    """All caption group layers, with the hook handoff rules applied.

    skip_before (title_card style): no word-by-word captions may appear
    before this timestamp - groups fully inside the window are dropped and
    a group straddling the boundary is pushed to start exactly at it.
    boost_until (word_by_word style): groups starting before this timestamp
    belong to beat[0] and get the bigger scale-in hook treatment."""
    groups = group_words(words, args.max_words, args.pause)
    windows = display_windows(groups, audio_duration)
    layers = []
    for group, (group_start, group_end) in zip(groups, windows):
        if args.limit and group_start >= audio_duration:
            continue
        if skip_before > 0 and group_end <= skip_before + 0.01:
            continue                        # fully under the title card
        boost = boost_until > 0 and group_start < boost_until
        layer = build_group_layer(group, group_start, group_end, font, args,
                                  hook_boost=boost)
        if skip_before > 0 and group_start < skip_before:
            # Straddler: begin at the handoff, but keep the ORIGINAL display
            # end (== the next group's start). Shifting start while keeping
            # the duration would make it overlap the next caption group.
            layer = (layer.with_start(skip_before)
                         .with_duration(max(0.1, group_end - skip_before)))
        layers.append(layer)
    return layers


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 2: render the final 1080x1920 short with karaoke captions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python step2_render_video.py\n"
            "  python step2_render_video.py --run-id run_20260831_120000\n"
            "  python step2_render_video.py --background my_bg.mp4 --mixed-case\n"
            "  python step2_render_video.py --limit 10   # fast 10s preview\n"
        ),
    )
    parser.add_argument("--run-id", help="run folder under --output-dir (default: newest)")
    parser.add_argument("--output-dir", default="output", help="base output directory")
    parser.add_argument("--background", help="background video path (default: background.mp4 next to this script)")
    parser.add_argument("--font", help="path to a bold .ttf font (default: auto-detect)")
    parser.add_argument("--font-size", type=int, default=None,
                        help="explicit caption font size in px (overrides the "
                             "percent-based default; optional)")
    parser.add_argument("--caption-font-size-pct", type=float, default=None,
                        help="caption font size as a fraction of frame width "
                             "(default 0.065 = 70 px at 1080; "
                             "CAPTION_FONT_SIZE_PCT env)")
    parser.add_argument("--title-card-font-size-pct", type=float, default=None,
                        help="hook title-card font size as a fraction of frame "
                             "width (default 0.075 = 81 px at 1080; "
                             "TITLE_CARD_FONT_SIZE_PCT env)")
    parser.add_argument("--max-words", type=int, default=4,
                        help="max words on screen at once (default: 4)")
    parser.add_argument("--pause", type=float, default=0.45,
                        help="speech gap (s) that forces a caption split (default: 0.45)")
    parser.add_argument("--subtitle-y", type=float, default=None,
                        help="legacy alias for --caption-y-pct (0.5 = old dead-center)")
    parser.add_argument("--caption-y-pct", type=float, default=None,
                        help="EXPLICIT vertical caption anchor override, 0=top "
                             "1=bottom (beats the platform preset; default: use "
                             "the target_platform preset)")
    parser.add_argument("--target-platform", choices=tuple(PLATFORM_PRESETS),
                        default=None,
                        help="which platform's bottom UI this export must clear: "
                             "youtube (anchor 0.52, clearance 0.28) / instagram "
                             "(0.62, 0.20) / tiktok (0.60, 0.22) / generic (0.58, "
                             "0.24, default = one export for all). "
                             "TARGET_PLATFORM env")
    parser.add_argument("--bottom-clearance-pct", type=float, default=None,
                        help="EXPLICIT fraction of frame height kept clear at the "
                             "bottom (beats the platform preset; "
                             "BOTTOM_CLEARANCE_PCT env)")
    parser.add_argument("--caption-max-width-pct", type=float, default=None,
                        help="hard cap on caption width as a fraction of frame "
                             "width (default 0.84 - keeps text out of the right "
                             "icon rail; CAPTION_MAX_WIDTH_PCT env)")
    parser.add_argument("--hook-style", choices=("title_card", "word_by_word", "both"),
                        default=None,
                        help="beat[0] hook presentation (v2): title_card = bold "
                             "static text alone ~1.4s, then captions (default); "
                             "word_by_word = no overlay, beat[0] captions pop "
                             "bigger with a scale-in; both = legacy overlay + "
                             "captions together. Default: HOOK_STYLE env, else "
                             "title_card")
    parser.add_argument("--safe-zone-guides", action="store_true",
                        help="draw translucent platform-UI safe-zone rectangles "
                             "(debug renders; RENDER_SAFE_ZONE_GUIDES env)")
    parser.add_argument("--highlight", default=DEFAULT_HIGHLIGHT,
                        help=f"karaoke accent color, or 'none' (default: {DEFAULT_HIGHLIGHT})")
    parser.add_argument("--emphasis-color", default=DEFAULT_EMPHASIS_COLOR,
                        help=f"color for metadata-driven emphasis words "
                             f"(default: {DEFAULT_EMPHASIS_COLOR})")
    parser.add_argument("--mode", choices=("auto", "beats", "classic"), default="auto",
                        help="assembly style: 'beats' uses the v2 beat metadata "
                             "(cut-per-beat, pulse, mood music, ducking, SFX, hook "
                             "overlay); 'classic' = legacy render; auto = beats when "
                             "the run has beats, else classic")
    parser.add_argument("--sfx", choices=("auto", "off"), default="auto",
                        help="whoosh-on-cut + ding-on-emphasis layer (v2). "
                             "Default auto = on, quietly mixed under the voice")
    parser.add_argument("--sfx-level", type=float, default=None,
                        help="master gain for the SFX layer (default 1.0, or SFX_LEVEL env)")
    parser.add_argument("--mixed-case", action="store_true",
                        help="keep normal casing (captions are ALL CAPS by default)")
    parser.add_argument("--uppercase", action="store_true",
                        help="(kept for backward compatibility; ALL CAPS is now the default)")
    parser.add_argument("--fps", type=int, default=30, help="output frame rate (default: 30)")
    parser.add_argument("--preset", default="medium",
                        help="x264 speed/quality preset (default: medium)")
    parser.add_argument("--threads", type=int, default=os.cpu_count() or 4,
                        help="encoder threads (default: all cores)")
    parser.add_argument("--limit", type=float, default=0,
                        help="render only the first N seconds (preview mode)")
    parser.add_argument("--music", default="auto",
                        help="'auto' = generate/loop the ambient bed (default), "
                             "'none' = off, or a path to an audio file")
    parser.add_argument("--music-level", type=float, default=None,
                        help=f"music volume 0-1 (default {DEFAULT_MUSIC_LEVEL}, "
                             "or MUSIC_LEVEL env)")
    parser.add_argument("--no-punchins", action="store_true",
                        help="disable background punch-in zooms")
    parser.add_argument("--prep-only", action="store_true",
                        help="only pre-render the 9:16 background cache and exit")
    parser.add_argument("--out", default="final_short.mp4",
                        help="output file name inside the run folder (default: final_short.mp4)")
    args = parser.parse_args()
    music_level = args.music_level if args.music_level is not None \
        else float(os.getenv("MUSIC_LEVEL", str(DEFAULT_MUSIC_LEVEL)) or DEFAULT_MUSIC_LEVEL)
    punch_on = punchins_enabled(args)

    print("=" * 60)
    print("  FACELESS CHANNEL PIPELINE - STEP 2")
    print("  dynamic scenes + music bed + karaoke captions -> final_short.mp4")
    print("=" * 60)

    run_folder = find_run_folder(args.output_dir, args.run_id)
    print(f"[1/5] Run folder: {run_folder}")
    platform = target_platform(args)
    print(f"      target platform: {platform} "
          f"(anchor {caption_y_pct(args):.2f}, bottom clearance "
          f"{bottom_clearance_pct(args):.2f}, max width "
          f"{caption_max_width_pct(args):.2f})")

    timings = json.loads((run_folder / "word_timings.json").read_text(encoding="utf-8"))
    words = timings.get("words", [])
    if not words:
        sys.exit("ERROR: word_timings.json contains no words - re-run step 1")
    beats = load_beats(timings)
    mode = args.mode if args.mode != "auto" else ("beats" if beats else "classic")
    if mode == "beats" and not beats:
        sys.exit("ERROR: --mode beats but this run has no beats metadata - "
                 "re-run step 1 (beats mode) or use --mode classic")
    meta = timings.get("meta", {})
    print(f"      mode: {mode} | tts backend: {meta.get('tts_backend', 'edge')} "
          f"| {len(beats)} beats" if mode == "beats" else
          f"      mode: classic")
    font = resolve_font(args.font)
    print(f"      caption font: {font}")

    # Audio + background --------------------------------------------------------
    print("[2/5] Loading audio + building background ...")
    audio = AudioFileClip(str(run_folder / "voiceover.mp3"))
    duration = audio.duration
    if args.limit:
        duration = min(duration, args.limit)
        print(f"      PREVIEW MODE: rendering only the first {duration:.1f}s")
    if args.prep_only:
        if mode == "beats":
            background, _kind, _cuts = resolve_background_v2(
                timings, args, run_folder, duration, args.fps, beats)
        else:
            background, _kind, _cuts = resolve_background(timings, args, run_folder,
                                                          duration, args.fps)
        background.close()
        audio.close()
        print("      background cache ready.")
        return 0
    if mode == "beats":
        background, bg_kind, scene_cuts = resolve_background_v2(
            timings, args, run_folder, duration, args.fps, beats)
    else:
        background, bg_kind, scene_cuts = resolve_background(timings, args, run_folder,
                                                             duration, args.fps)
    print(f"      voiceover: {duration:.1f}s | background: {bg_kind}")

    # Music bed + sound design ---------------------------------------------------
    mood = dominant_mood(beats) if mode == "beats" else "serious"
    print(f"[3/5] Audio: music bed ({music_level * 100:.0f}%)" +
          (f" | mood: {mood}" if mode == "beats" else "") + " ...")
    mixed_audio = audio
    music_path = resolve_music_bed(args.music, run_folder, duration, mood=mood)
    ducked = False
    if music_path and mode == "beats" and env_flag("MUSIC_DUCKING"):
        ducked_path = duck_music_bed(music_path, run_folder / "voiceover.mp3",
                                     duration, music_level,
                                     run_folder / "_music_bed_ducked.wav")
        if ducked_path:
            music_path = ducked_path
            ducked = True
            print(f"      ducking: on (music dips to {int(DUCK_FLOOR * 100)}% "
                  "under speech)")
    audio_layers = [audio]
    if music_path:
        music = AudioFileClip(str(music_path))
        music = music.with_effects([afx.AudioLoop(duration=duration)])
        music = music.subclipped(0, duration)
        music = music.with_effects([afx.MultiplyVolume(
            1.0 if ducked else music_level)])
        audio_layers.append(music)
        print(f"      bed: {Path(music_path).name} looped to {duration:.1f}s "
              f"@ {music_level * 100:.0f}% volume" +
              (" (ducked)" if ducked else ""))
    else:
        print("      running without a music bed")

    # SFX layer: whoosh into every cut + ding under emphasized words ------------
    sfx_on = False
    if mode == "beats":
        try:
            import sfx_gen
            sfx_on = sfx_gen.sfx_enabled(args)
            if sfx_on:
                sfx_level = args.sfx_level if args.sfx_level is not None \
                    else float(os.getenv("SFX_LEVEL", "1.0") or 1.0)
                emphasis_times = [w["start"] for w in words if w.get("emphasis")]
                sfx_path = sfx_gen.build_layer(scene_cuts, emphasis_times, duration,
                                               run_folder / "_sfx_layer.wav",
                                               level=sfx_level)
                if sfx_path:
                    sfx_clip = AudioFileClip(str(sfx_path)).subclipped(0, duration)
                    audio_layers.append(sfx_clip)
                    print(f"      sfx: on ({len(scene_cuts)} cut whooshes, "
                          f"{len(emphasis_times)} emphasis dings @ {sfx_level:.1f}x)")
                else:
                    sfx_on = False
        except Exception as exc:
            print(f"      sfx: skipped ({str(exc)[:80]})")
            sfx_on = False
    mixed_audio = CompositeAudioClip(audio_layers).with_duration(duration)

    # Captions + hook title card -------------------------------------------------
    hook_style = (args.hook_style or os.getenv("HOOK_STYLE", "") or "title_card").strip().lower()
    if hook_style not in ("title_card", "word_by_word", "both"):
        hook_style = "title_card"
    guides_on = args.safe_zone_guides or env_flag("RENDER_SAFE_ZONE_GUIDES", "off")
    print(f"[4/5] Building karaoke captions ({len(words)} words, "
          f"max {args.max_words}/group, highlight: {args.highlight}) ...")
    overlay = None
    skip_before = 0.0
    boost_until = 0.0
    hook_note = "none"
    if mode == "beats":
        beat0_end = float(beats[0].get("end", 0.0) or 0.0)
        if hook_style == "title_card" and env_flag("HOOK_OVERLAY"):
            # Option A: title card alone for a fixed 1.2-1.5 s window; if the
            # hook beat is shorter than the window, the card covers the whole
            # beat and that beat gets no word-by-word captions at all.
            tc_dur = TITLE_CARD_SECONDS if beat0_end > TITLE_CARD_SECONDS else beat0_end
            overlay = build_title_card(beats[0]["text"], tc_dur, font, args)
            if overlay:
                skip_before = tc_dur
                hook_note = f"title_card ({tc_dur:.1f}s)"
                print(f"      hook title card: '{beats[0]['text'][:40]}...' "
                      f"alone for {tc_dur:.1f}s -> captions resume after")
        elif hook_style == "word_by_word":
            # Option B: no overlay; beat[0] captions are bigger + scale in.
            boost_until = beat0_end
            hook_note = "word_by_word (hook boosted)"
            print(f"      hook style: word_by_word - beat[0] captions +15% "
                  f"with scale-in, no overlay")
        elif hook_style == "both" and env_flag("HOOK_OVERLAY"):
            hook_end = float(beats[0].get("end", 2.2) or 2.2)
            overlay = build_hook_overlay(beats[0]["text"], hook_end, font, args)
            hook_note = "both (legacy)"
            if overlay:
                print(f"      hook overlay: '{beats[0]['text'][:40]}...' "
                      f"pinned for the first {min(hook_end, 2.6):.1f}s "
                      f"(legacy: over the captions)")
    caption_layers = build_caption_layers(words, duration, font, args,
                                          skip_before=skip_before,
                                          boost_until=boost_until)
    print(f"      {len(caption_layers)} caption group layers ready "
          f"| anchor-y: {caption_y_pct(args):.2f} "
          f"({platform}) | caption font: {caption_font_size(args)}px "
          f"({caption_font_size(args) / TARGET_W:.3f} of width) "
          f"| hook: {hook_note}")

    # Composite + export --------------------------------------------------------
    print("[5/5] Rendering final_short.mp4 (libx264 + aac) ...")
    layers = [background] + caption_layers + ([overlay] if overlay else [])
    if guides_on:
        layers += build_safe_zone_guides(duration, caption_y_pct(args), args)
        print("      safe-zone guides: ON (debug render - do not publish)")
    final = CompositeVideoClip(layers, size=(TARGET_W, TARGET_H))
    final = final.with_duration(duration).with_audio(mixed_audio)
    out_path = run_folder / args.out
    final.write_videofile(
        str(out_path),
        codec="libx264",
        audio_codec="aac",
        audio_bitrate="192k",
        fps=args.fps,
        preset=args.preset,
        threads=args.threads,
        ffmpeg_params=["-pix_fmt", "yuv420p"],   # player/social-platform safe
    )

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print()
    print("=" * 60)
    print(f"  done: {out_path}")
    print(f"  {TARGET_W}x{TARGET_H} @ {args.fps} fps | {duration:.1f}s | "
          f"{size_mb:.1f} MB | mode: {mode} | bg: {bg_kind} "
          f"| music: {'on' if music_path else 'off'}"
          f"{' (ducked)' if ducked else ''} "
          f"| sfx: {'on' if sfx_on else 'off'} "
          f"| punch-ins: {'on' if punch_on else 'off'} "
          f"| hook: {hook_style if mode == 'beats' else 'n/a (classic)'} "
          f"| platform: {platform} "
          f"| caption-y: {caption_y_pct(args):.2f} "
          f"| clearance: {bottom_clearance_pct(args):.2f}")
    print("=" * 60)

    final.close()
    background.close()
    audio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
