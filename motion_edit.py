#!/usr/bin/env python3
"""
motion_edit.py - the v3 "motion edit" engine (STYLE=v2).

What changed vs the v1/v2 renderer (STYLE=v1)
---------------------------------------------
v1 assembly is one flat layer: a stock clip with a fixed zoom ladder +
karaoke text. Professional shorts are LAYERED - the frame is a stack of
independent assets, each with its own motion. This module builds that stack:

  Layer stack (bottom -> top)
    A. background video per scene with true Ken Burns (one continuous
       directional move per shot: zoom + pan drifting across the whole
       scene, alternating in/out between scenes)
    B. 1-3 art cards ON SCREEN AT ONCE (the user's core requirement):
       style-locked PNG illustrations from art_library.py that pop in with
       an under-damped spring, drift with slow parallax, and exit with the
       scene - layered OVER the background, UNDER the captions
    C. transitions instead of hard cuts: whip-pan between ideas,
       zoom-punch on the reframe turn, dip-to-black at emotional section
       changes ("editor's mix" - varied like a human editor)
    D. captions: same karaoke engine, upgraded with a spring pop on the
       accent word + a decaying jitter on emphasis words
    E. finishing pass: brand-noir grade (violet shadows, amber highlights),
       vignette + film grain via one fast ffmpeg filter pass

  Pacing: hybrid ramp - fast hook (cuts every ~1.3-2.5 s), relaxed body
  (2.6-4.3 s holds), calm outro. Cut times always snap to beat starts.

Everything reuses step2_render_video.py helpers (safe zones, caption
engine, clip sourcing) so STYLE=v1 stays available as the instant fallback
flag for the A/B rollout.

Performance budget (GitHub Actions, 2-core): per-frame work is one crop+
resize on a 1080x1920 patch (~5 ms) + small card ops; transitions composite
only ~6 frames per cut. Full render stays well under the ~35 min ceiling.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from step2_render_video import (  # reuse the proven v1 machinery
    TARGET_W, TARGET_H,
    crop_to_vertical,
    env_flag,
    bottom_clearance_pct,
    caption_y_pct,
)

# --- tuning -----------------------------------------------------------------
HOOK_SCENE = (1.3, 2.5)          # min/max scene length in the hook region
BODY_SCENE = (2.6, 4.3)
OUTRO_SCENE = (3.0, 5.2)
MAX_SCENES = 11                  # hard cap (merging smallest gaps)
KB_ZOOM_IN = (1.06, 1.16)        # Ken Burns range per scene (alternating)
KB_ZOOM_OUT = (1.16, 1.06)
KB_TURN_ZOOM = (1.06, 1.22)      # the reframe push
KB_PAN = 0.025                   # pan drift as a fraction of width/height

TRANS_WHIP_DUR = 0.20            # seconds (6 frames @30fps)
TRANS_PUNCH_DUR = 0.166
TRANS_DIP_DUR = 0.24
WHIP_TRAVEL = 1.00               # whip slide width (1.0 = panels tile exactly,
                                 # no black slit mid-transition)
WHIP_MAX_BLUR = 42               # px of horizontal motion blur at the mid
PUNCH_KICK = 0.08                # +8% scale kick on the punch transition

CARD_W = {"lg": 0.40, "md": 0.30, "sm": 0.22}
CARD_W_VAR = {"lg": 0.12, "md": 0.15, "sm": 0.15}   # ± width variance per slot
CARD_STAGGER = (0.12, 0.95, 1.70)   # entrance delays: main / secondary / micro
CARD_DRIFT = {"lg": 9.0, "md": 6.5, "sm": 4.5}   # px of parallax drift
CARD_BOB = {"lg": 8.0, "md": 6.0, "sm": 4.0}     # px of continuous float
CARD_BREATH = 0.015                 # ±1.5% slow scale breathing
CARD_BREATH_HZ = 0.33
CARD_BOB_HZ = 0.45
CARD_SLIDE = 70.0                   # px secondary cards slide in from their side
CARD_FADE_IN, CARD_FADE_OUT = 0.12, 0.18

EMOTION_GROUPS = {               # for dip-to-black section changes
    "intense": "high", "urgent": "high", "playful": "high",
    "curious": "mid", "triumphant": "mid",
    "serious": "low", "calm": "low",
}


# ---------------------------------------------------------------------------
# Easing (spring math ported from the motion-ai skill's css-spring guide)
# ---------------------------------------------------------------------------

def spring_scale(t: float, drop: float = 0.30, decay: float = 0.055,
                 omega: float = 26.0) -> float:
    """Under-damped spring 0.70 -> overshoot ~1.06 -> settle at 1.0.

    s(t) = 1 - drop * e^(-t/decay) * cos(omega * t)
    Reaches 1.0 in ~0.25 s - snappy like the Motion spring curve, not linear.
    """
    if t <= 0:
        return 1.0 - drop
    return 1.0 - drop * math.exp(-t / decay) * math.cos(omega * t)


def jitter_offset(t: float, amp: float = 3.0, tau: float = 0.09,
                  omega: float = 40.0) -> float:
    """Decaying shake for emphasis words: a few px that die out fast."""
    if t <= 0:
        return 0.0
    return amp * math.exp(-t / tau) * math.sin(omega * t)


def _smooth(p: float) -> float:
    """smoothstep - continuous velocity at both ends (Ken Burns ease)."""
    p = min(max(p, 0.0), 1.0)
    return p * p * (3.0 - 2.0 * p)


# ---------------------------------------------------------------------------
# Scene planner: hybrid ramp pacing on beat boundaries
# ---------------------------------------------------------------------------

@dataclass
class Scene:
    start: float
    end: float
    role: str = "body"              # hook | body | turn | outro
    transition: str = "whip"        # how we arrive into this scene
    whip_dir: int = 1               # +1 exits left / -1 exits right
    beat_indices: list = field(default_factory=list)
    clip: Path | None = None

    @property
    def duration(self) -> float:
        return max(0.2, self.end - self.start)


def _region_bounds(duration: float, words: list[dict]) -> tuple[float, float]:
    """(hook_end, outro_start) timestamps for the hybrid ramp."""
    hook_end = min(0.20 * duration, 5.5)
    outro_start = duration - max(3.0, 0.12 * duration)
    try:
        from step2_render_video import estimate_beat_boundaries
        beats = estimate_beat_boundaries(words)
        if beats:
            hook_end = max(hook_end, min(beats[0], 6.0))
    except Exception:
        pass
    return hook_end, max(outro_start, hook_end + 2.0)


def plan_scenes(beats: list[dict], words: list[dict],
                duration: float) -> list[Scene]:
    """Group beats into scenes with hybrid-ramp lengths.

    Cuts only happen at beat starts (never mid-beat), scenes respect the
    region's min/max hold, and the total scene count is capped."""
    ordered = sorted(beats, key=lambda b: float(b.get("start", 0.0)))
    if not ordered:
        return []
    # preview renders: beats that start after the cut can never be spoken
    ordered = [b for b in ordered if float(b.get("start", 0.0)) < duration - 0.25]
    if not ordered:
        return []
    for i, b in enumerate(ordered):          # normalize: every beat has an index
        if b.get("index") is None:
            b["index"] = i
    hook_end, outro_start = _region_bounds(duration, words)
    # the reframe turn (strategist beat 3 -> 4 boundary) gets the punch
    turn_t = None
    try:
        from step2_render_video import estimate_beat_boundaries
        bounds = estimate_beat_boundaries(words)
        if bounds:
            turn_t = bounds[2]
    except Exception:
        pass

    def region_for(start: float) -> tuple[float, float]:
        if start < hook_end:
            return HOOK_SCENE
        if start >= outro_start:
            return OUTRO_SCENE
        return BODY_SCENE

    # walk beats, closing scenes when the region's hold allows
    scenes: list[Scene] = []
    cur_start = 0.0
    cur_beats = [ordered[0]["index"]]
    for prev, nxt in zip(ordered, ordered[1:]):
        lo, hi = region_for(cur_start)
        elapsed = float(prev.get("end", prev.get("start", 0.0))) - cur_start
        # would ADDING this beat push the scene past the region max?
        nxt_elapsed = float(nxt.get("end", nxt.get("start", 0.0))) - cur_start
        if elapsed >= lo or nxt_elapsed > hi:      # close the scene here
            scenes.append(Scene(cur_start, float(prev.get("end", 0.0)),
                                beat_indices=list(cur_beats)))
            cur_start = float(nxt.get("start", 0.0))
            cur_beats = [nxt["index"]]
        else:
            cur_beats.append(nxt["index"])
    scenes.append(Scene(cur_start, float(duration), beat_indices=list(cur_beats)))

    # cap scene count: merge the smallest gaps first
    while len(scenes) > MAX_SCENES:
        gaps = [(scenes[i + 1].start - scenes[i].start, i)
                for i in range(len(scenes) - 1)]
        _, i = min(gaps)
        scenes[i].end = scenes[i + 1].end
        scenes[i].beat_indices += scenes[i + 1].beat_indices
        scenes.pop(i + 1)

    # scenes must be CONTIGUOUS: beat-end based closes can leave small pause
    # gaps, and the background walker snaps any in-gap time to the LAST scene
    # (visible as an outro flash). Extend every scene to the next start.
    for prev_sc, nxt_sc in zip(scenes, scenes[1:]):
        prev_sc.end = max(prev_sc.end, nxt_sc.start)

    # roles, transitions, whip direction
    turn_used = False
    prev_group = None
    for i, sc in enumerate(scenes):
        if i == 0:
            sc.transition = "none"
            sc.role = "hook"
            continue
        if sc.start >= outro_start and i == len(scenes) - 1:
            sc.role = "outro"
        elif turn_t is not None and not turn_used and sc.start <= turn_t < sc.end:
            sc.role = "turn"
            sc.transition = "punch"
            turn_used = True
        else:
            sc.role = "body"
        # dip-to-black when the emotional section changes (max every 3rd)
        group = EMOTION_GROUPS.get(_scene_emotion(sc, beats), "low")
        if sc.transition != "punch" and i % 3 == 0 and prev_group is not None \
                and group != prev_group:
            sc.transition = "dip"
        else:
            sc.transition = sc.transition if sc.transition == "punch" else "whip"
        prev_group = group
        sc.whip_dir = 1 if i % 2 == 0 else -1
    return scenes


def _scene_emotion(scene: Scene, beats: list[dict]) -> str:
    """Emotion of the scene's first beat that carries one."""
    by_index = {b.get("index"): b for b in beats if b.get("index") is not None}
    for idx in scene.beat_indices:
        b = by_index.get(idx)
        if b and b.get("emotion"):
            return b["emotion"].lower()
    return "serious"


def boom_times(scenes: list[Scene]) -> list[float]:
    """Cut timestamps that get a sub-boom under the transition."""
    return [sc.start for sc in scenes
            if sc.transition in ("punch", "dip") and sc.start > 0.3]


def sfx_events(scenes: list[Scene]) -> list[dict]:
    """Per-transition SFX events for sfx_gen.build_layer (v2.1).

    The user complaint: the SAME whoosh on EVERY cut was irritating. Now
    whips get the whoosh (varied voices, panned along the whip direction),
    punches and dips get the sub-boom instead - each cut gets the sound
    that suits it."""
    events: list[dict] = []
    for sc in scenes:
        if sc.transition == "none" or sc.start <= 0.0:
            continue
        events.append({"t": sc.start, "kind": sc.transition,
                       "dir": int(sc.whip_dir or 1)})
    return events


# ---------------------------------------------------------------------------
# Background: Ken Burns scenes + editor's-mix transitions, pre-rendered once
# ---------------------------------------------------------------------------

def assign_clips_to_scenes(timings: dict, args, run_folder: Path,
                           scenes: list[Scene], beats: list[dict]) -> None:
    """Per-beat background clips (curated library -> concept Pexels),
    inherited by each scene from its first beat. Same sourcing chain as
    v1's resolve_background_v2 - just re-plumbed onto the scene plan."""
    beat_clip: dict[int, Path] = {}
    if env_flag("CURATED_LIB"):
        try:
            import curated_library as clib
            api_key = (os.getenv("GEMINI_API_KEY", "") or "").strip()
            clip_map, refined = clib.ensure_clips_for_beats(
                beats, run_folder.name, api_key=api_key)
            beat_clip.update(clip_map)
        except Exception as exc:
            print(f"      curated library unavailable ({str(exc)[:80]})")

    missing = [b for b in beats
               if b.get("index") is not None and b["index"] not in beat_clip]
    if missing:
        try:
            import pexels_bg
            used_ids: set[int] = set()
            out_dir = run_folder / "backgrounds"
            out_dir.mkdir(parents=True, exist_ok=True)
            for b in missing[:12]:
                concept = (b.get("visual_concept") or "").lower()
                q = " ".join(w for w in re.sub(r"[^a-z0-9 ]", " ", concept).split()
                             if w not in ("the", "a", "an", "of", "and", "with",
                                          "in", "on", "one", "side"))[:32]
                if not q:
                    continue
                got = pexels_bg.search_and_download(
                    q, out_dir / f"concept_{b['index']:02d}.mp4", used_ids)
                if got:
                    beat_clip[b["index"]] = got
        except Exception as exc:
            print(f"      concept search failed ({str(exc)[:80]})")

    prev = next(iter(beat_clip.values()), None)
    for sc in scenes:
        clip = None
        for idx in sc.beat_indices:
            if idx in beat_clip:
                clip = beat_clip[idx]
                break
        sc.clip = clip or prev
        if sc.clip:
            prev = sc.clip

    if not prev:
        # no Pexels clips at all -> the static background still gets the
        # full motion treatment (Ken Burns + transitions are source-agnostic)
        for candidate in (getattr(args, "background", None),
                          "background.mp4", "assets/background.mp4"):
            if candidate and Path(candidate).is_file():
                prev = Path(candidate)
                break
        if prev:
            print(f"      no dynamic clips - using static bg {prev} "
                  "(motion treatment still applies)")
        for sc in scenes:
            sc.clip = prev


def _kb_params(scene: Scene, scene_index: int, tag: str) -> dict:
    """Deterministic Ken Burns move for one scene (seeded by run+index)."""
    if scene.role == "turn":
        z0, z1 = KB_TURN_ZOOM
    elif scene_index % 2 == 0:
        z0, z1 = KB_ZOOM_IN
    else:
        z0, z1 = KB_ZOOM_OUT
    seed = int(hashlib.sha1(f"{tag}:{scene_index}".encode()).hexdigest()[:6], 16)
    rng = np.random.default_rng(seed)
    dx = float(rng.uniform(-KB_PAN, KB_PAN))
    dy = float(rng.uniform(-KB_PAN * 0.6, KB_PAN * 0.6))
    if scene.role == "turn":
        dx, dy = 0.0, dy * 0.4          # the turn pushes straight in
    return {"z0": z0, "z1": z1, "dx": dx, "dy": dy,
            "reverse": bool(rng.integers(0, 2))}


def _load_kb_segment(path: Path, seg_dur: float, scene_index: int,
                     kb: dict) -> tuple:
    """One scene's background with its Ken Burns move baked in.

    The patch is center-cropped to 9:16 at 1080x1920 (same as v1), then
    every frame samples a slightly smaller window (zoom) drifting toward
    (dx, dy) (pan) with a smoothstep ease - one continuous directional
    move per shot, exactly like a real editor's push/pull."""
    src = VideoFileClip(str(path))
    patch = crop_to_vertical(src)
    src_dur = patch.duration or seg_dur
    if src_dur > seg_dur + 0.05:
        offset = (scene_index * 2.7) % (src_dur - seg_dur)
        patch = patch.subclipped(offset, offset + seg_dur)
    elif src_dur < seg_dur:
        patch = patch.with_effects([vfx.Loop(duration=seg_dur)])
    else:
        patch = patch.subclipped(0, seg_dur)

    z0, z1 = kb["z0"], kb["z1"]
    dx, dy = kb["dx"], kb["dy"]
    if kb["reverse"]:                    # some shots pull out instead
        z0, z1 = z1, z0
        dx, dy = -dx, -dy

    def kb_frame(get_frame, t, dur=seg_dur, w=TARGET_W, h=TARGET_H):
        frame = get_frame(t)
        e = _smooth(t / max(dur, 0.2))
        z = z0 + (z1 - z0) * e
        cw, ch = int(w / z), int(h / z)
        cx = w / 2 + dx * e * w
        cy = h / 2 + dy * e * h
        cx = min(max(cx, cw / 2), w - cw / 2)
        cy = min(max(cy, ch / 2), h - ch / 2)
        x1 = int(cx - cw / 2)
        y1 = int(cy - ch / 2)
        crop = frame[y1:y1 + ch, x1:x1 + cw]
        return np.asarray(
            PILImage.fromarray(crop).resize((w, h), PILImage.BILINEAR))

    return patch.transform(kb_frame), src


def _shift_blur(frame: np.ndarray, offset_px: float, blur_px: float,
                direction: int) -> np.ndarray:
    """Slide a frame sideways with horizontal motion blur (box blur via
    cumulative sum - fast, no scipy)."""
    h, w = frame.shape[:2]
    shift = int(round(offset_px))
    out = np.zeros_like(frame)
    if shift >= w or shift <= -w:
        return out
    if shift >= 0:
        out[:, shift:] = frame[:, :w - shift]
    else:
        out[:, :shift] = frame[:, -shift:]
    k = int(max(1, blur_px))
    if k > 1:
        pad = np.pad(out.astype(np.float32),
                     ((0, 0), (k, k), (0, 0)), mode="edge")
        csum = np.cumsum(pad, axis=1, dtype=np.float32)
        blurred = (csum[:, 2 * k:] - csum[:, :-2 * k]) / (2 * k)
        out = blurred.astype(frame.dtype)
    return out


def _scale_center(frame: np.ndarray, scale: float) -> np.ndarray:
    """Center crop by 1/scale then resize back (zoom punch)."""
    h, w = frame.shape[:2]
    cw, ch = int(w / scale), int(h / scale)
    x1, y1 = (w - cw) // 2, (h - ch) // 2
    return np.asarray(PILImage.fromarray(frame[y1:y1 + ch, x1:x1 + cw])
                      .resize((w, h), PILImage.BILINEAR))


def build_background_v3(timings: dict, args, run_folder: Path,
                        duration: float, fps: int,
                        beats: list[dict]) -> tuple:
    """The v3 background track: Ken Burns scenes + editor's-mix transitions.

    Returns (background_clip, cut_times, scenes). Pre-renders to a cache
    file (same one-time strategy as v1) so the main composite only decodes."""
    scenes = plan_scenes(beats, timings.get("words", []), duration)
    if not scenes:
        raise RuntimeError("scene planner produced no scenes")
    cuts = [sc.start for sc in scenes if sc.transition != "none"]
    print(f"      motion plan: {len(scenes)} scenes | "
          f"{[f'{sc.role}:{sc.duration:.1f}s' for sc in scenes]}")
    print(f"      transitions: "
          f"{[f'{sc.start:.1f}s:{sc.transition}' for sc in scenes if sc.transition != 'none']}")

    assign_clips_to_scenes(timings, args, run_folder, scenes, beats)
    have_clips = sum(1 for sc in scenes if sc.clip)
    if not have_clips:
        raise RuntimeError("no background clips available for the scene plan")
    if have_clips < len(scenes):
        print(f"      scenes without a matched clip reuse earlier ones "
              f"({have_clips}/{len(scenes)} matched)")

    tag = f"{run_folder.name}|{duration:.2f}|{len(scenes)}"
    seg_paths: list[Path] = []
    for i, sc in enumerate(scenes):
        seg_path = run_folder / f"_kb3_seg_{i:02d}.mp4"
        if not (seg_path.is_file() and seg_path.stat().st_size > 30_000):
            kb = _kb_params(sc, i, tag)
            seg, parent = _load_kb_segment(sc.clip, sc.duration, i, kb)
            tmp = seg_path.with_suffix(".tmp.mp4")
            seg.write_videofile(str(tmp), codec="libx264",
                                preset="veryfast", fps=fps, audio=False,
                                logger=None)
            seg.close()
            parent.close()
            tmp.replace(seg_path)                   # atomic: no truncated caches
        seg_paths.append(seg_path)

    cache = run_folder / f"_background_v3_{hashlib.sha1(tag.encode()).hexdigest()[:8]}.mp4"
    if not (cache.is_file() and cache.stat().st_size > 50_000):
        _write_transition_track(seg_paths, scenes, cache, duration, fps)
    background = VideoFileClip(str(cache))
    return (background.subclipped(0, min(duration, background.duration)),
            "dynamic-v3", cuts, scenes)


def _write_transition_track(seg_paths: list[Path], scenes: list[Scene],
                            cache: Path, duration: float, fps: int) -> None:
    """Encode the final background: straight decode between cuts, composite
    frames only inside transition windows (whip / punch / dip)."""
    readers = [VideoFileClip(str(p)) for p in seg_paths]
    starts = [sc.start for sc in scenes]
    ends = [sc.end for sc in scenes]

    def scene_frame(i: int, t_global: float) -> np.ndarray:
        local = min(max(t_global - starts[i], 0.0),
                    readers[i].duration - 0.001)
        return readers[i].get_frame(local)

    def make_frame(t: float) -> np.ndarray:
        i = len(scenes) - 1
        for k in range(len(scenes)):
            if starts[k] <= t < ends[k] or k == len(scenes) - 1:
                i = k
                break
        sc = scenes[i]
        if sc.transition == "none" or i == 0:
            return scene_frame(i, t)

        dur = {"whip": TRANS_WHIP_DUR, "punch": TRANS_PUNCH_DUR,
               "dip": TRANS_DIP_DUR}[sc.transition]
        t0 = sc.start - dur / 2
        if t < t0 or t > sc.start + dur / 2:
            return scene_frame(i, t)
        p = min(max((t - t0) / dur, 0.0), 1.0)

        if sc.transition == "whip":
            out_f = scene_frame(i - 1, t)
            in_f = scene_frame(i, t)
            travel = TARGET_W * WHIP_TRAVEL * sc.whip_dir
            off = travel * p
            blur = WHIP_MAX_BLUR * (1.0 - abs(2 * p - 1)) ** 1.5
            a = _shift_blur(out_f, -off * sc.whip_dir, blur, sc.whip_dir)
            b = _shift_blur(in_f, (TARGET_W * WHIP_TRAVEL - off) * sc.whip_dir,
                            blur, sc.whip_dir)
            canvas = b.astype(np.float32)
            mask = a.astype(np.float32)
            alpha = (np.abs(mask).sum(axis=2, keepdims=True) > 0).astype(np.float32)
            return (mask * alpha + canvas * (1 - alpha)).astype(np.uint8)

        if sc.transition == "punch":
            if t < sc.start:
                q = (t - t0) / (dur / 2)
                return _scale_center(scene_frame(i - 1, t), 1.0 + PUNCH_KICK * q * q)
            q = (t - sc.start) / (dur / 2)
            return _scale_center(scene_frame(i, t),
                                 1.0 + PUNCH_KICK * (1.0 - q) ** 1.6)

        # dip to black
        if p < 0.5:
            f = scene_frame(i - 1, t).astype(np.float32) * (1.0 - 2 * p)
        else:
            f = scene_frame(i, t).astype(np.float32) * (2 * p - 1)
        return f.astype(np.uint8)

    track = VideoClip(frame_function=make_frame, duration=duration)
    print("      pre-rendering v3 background (Ken Burns + transitions) ...")
    tmp = cache.with_suffix(".tmp.mp4")
    try:
        track.write_videofile(str(tmp), codec="libx264", preset="medium",
                              fps=fps, audio=False, logger=None)
        tmp.replace(cache)                          # atomic: no truncated caches
    finally:
        track.close()
        for r in readers:
            r.close()
        if tmp.exists() and not cache.is_file():
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Layer B: art cards - up to 3 on screen at once, spring pop + parallax drift
# ---------------------------------------------------------------------------

def _card_variants(beat_cards: dict[int, Path], run_folder: Path) -> dict:
    """md/sm variants come from the SAME asset as the lg card so every size
    of one entry shares its silhouette. Transparent cutouts are re-prepared
    per size (each with its own tilt); framed cards fall back to the old
    rounded-card prep."""
    import art_library as art
    variants: dict[int, dict[str, Path]] = {}
    prep_dir = run_folder / "cards"
    for idx, lg_path in beat_cards.items():
        entry_id = lg_path.stem.replace("_lg", "")
        cutout = art.CACHE_DIR / f"{entry_id}_cut.png"
        original = art.CACHE_DIR / f"{entry_id}.png"
        sizes = {"lg": lg_path}
        for size in ("md", "sm"):
            out = prep_dir / f"{entry_id}_{size}.png"
            if out.is_file() and out.stat().st_size > 5_000:
                sizes[size] = out
            elif cutout.is_file():
                ready = art.prepare_cutout(
                    cutout, int(TARGET_W * CARD_W[size]), out,
                    tilt_deg=art._tilt_for(entry_id, size))
                if ready:
                    sizes[size] = ready
            elif original.is_file():
                ready = art.prepare_card(original,
                                         int(TARGET_W * CARD_W[size]), out)
                if ready:
                    sizes[size] = ready
        variants[idx] = sizes
    return variants


def _slot_width(size_key: str, beat_idx: int, slot_i: int) -> int:
    """Seeded per-slot width so cards stop being all the same size."""
    seed = int(hashlib.sha1(
        f"{beat_idx}:{size_key}:{slot_i}".encode()).hexdigest()[:6], 16)
    var = CARD_W_VAR[size_key]
    span = 1.0 + var * ((seed % 200) / 100.0 - 1.0)      # 1-var .. 1+var
    return int(TARGET_W * CARD_W[size_key] * span)


def _placement(size_key: str, side: str, zone: str, args,
               target_w: int, aspect: float) -> tuple[int, int, int, int]:
    """Safe-zone placement resolver for one card.

    Returns (x, y, w, h). Zones clear the platform UI (top bar, right icon
    rail, bottom clearance) AND the caption band around caption_y_pct.
    `aspect` is the ACTUAL asset aspect (cutout silhouettes vary wildly)."""
    caption_anchor = TARGET_H * caption_y_pct(args)
    floor = TARGET_H * (1.0 - bottom_clearance_pct(args))
    band_half = int(TARGET_H * 0.095)                 # ~2 caption lines
    top_y0, top_y1 = int(TARGET_H * 0.145), caption_anchor - band_half
    bot_y0, bot_y1 = caption_anchor + band_half, int(floor)
    y0, y1 = (top_y0, top_y1) if zone == "top" else (bot_y0, bot_y1)

    w = max(40, int(target_w))
    h = int(w / max(0.2, aspect))
    if h > y1 - y0:                                   # shrink to the zone
        h = y1 - y0 - 8
        w = int(h * max(0.2, aspect))
    if w <= 40 or h <= 40:
        return (0, 0, 0, 0)                           # zone collapsed - skip

    if side == "right":
        x = int(TARGET_W * 0.80) - w                  # clear the icon rail
    elif side == "left":
        x = int(TARGET_W * 0.075)
    else:                                             # center
        x = (TARGET_W - w) // 2
    y = y0 + max(0, ((y1 - y0) - h) // 2)
    return (x, y, w, h)


def build_card_layers(scenes: list[Scene], beat_cards: dict[int, Path],
                      run_folder: Path, args) -> list:
    """The multi-asset layer: for every scene, up to three art cards
    (main lg + secondary md + micro sm) on screen together - each with its
    own size, silhouette, tilt and motion: spring pop-in, secondary cards
    SLIDE in from their side, then everything keeps floating (bob + slow
    scale breathing + parallax drift) until the scene releases them."""
    if not beat_cards:
        return []
    variants = _card_variants(beat_cards, run_folder)
    layers: list = []
    n_cards = 0
    for i, sc in enumerate(scenes):
        if sc.transition == "none" or sc.role == "hook":
            slots = []                                 # hook scene: no cards
        else:
            slots = []
            main_idx = next((b for b in sc.beat_indices if b in variants), None)
            if main_idx is not None:
                side = "right" if i % 2 == 0 else "left"
                # main card ALWAYS takes the tall top zone (the bottom band
                # between captions and platform UI is only ~160 px - it can
                # only host a micro accent, never a main/secondary card)
                slots.append(("lg", main_idx, side, "top", CARD_STAGGER[0], "pop"))
                other = [b for b in sc.beat_indices
                         if b in variants and b != main_idx]
                if other and sc.duration >= 2.8:
                    # secondary on the OPPOSITE side, same tall top zone
                    sec_side = "left" if side == "right" else "right"
                    slots.append(("md", other[0], sec_side, "top",
                                  CARD_STAGGER[1], "slide"))
                elif sc.duration >= 3.4 and i % 3 == 0:
                    # micro accent: same art, small, bottom-corner
                    mic_side = "left" if side == "right" else "right"
                    slots.append(("sm", main_idx, mic_side, "bottom",
                                  CARD_STAGGER[2], "rise"))

        for j, (size_key, beat_idx, side, zone, delay, entrance) in \
                enumerate(slots):
            path = variants[beat_idx].get(size_key)
            if path is None:
                continue
            img_w, img_h = PILImage.open(str(path)).size
            aspect = img_w / max(1, img_h)
            target_w = _slot_width(size_key, beat_idx, j)
            x, y, w, h = _placement(size_key, side, zone, args,
                                    target_w, aspect)
            if w <= 0:
                continue
            card = ImageClip(str(path))
            # scale the prepared asset (incl. glow/shadow padding) to slot width
            native_w = card.size[0]
            fit = (w + int(w * 0.28)) / native_w       # padding factor
            card = card.resized(fit)
            start = sc.start + delay
            dur = max(0.6, sc.end - start - 0.05)
            if dur <= 0.3:
                continue

            base_x, base_y = x - int(w * 0.14), y - int(h * 0.14)
            cx = base_x + card.size[0] / 2
            cy = base_y + card.size[1] / 2
            amp = CARD_DRIFT[size_key]
            bob = CARD_BOB[size_key]
            phase = i * 1.7 + j * 2.3
            slide_dir = (1 if side == "left" else -1) if entrance == "slide" \
                else 0
            rise_px = 46 if entrance == "rise" else 0

            def pos(t, _cx=cx, _cy=cy, _w=card.size[0], _h=card.size[1],
                    _amp=amp, _bob=bob, _ph=phase, _dir=slide_dir,
                    _rise=rise_px):
                s = spring_scale(t)
                # entrance offsets: slide from the side / rise from below
                slide = (1.0 - min(1.0, t / 0.45)) * _dir * CARD_SLIDE \
                    if _dir else 0.0
                rise = (1.0 - _smooth(min(1.0, t / 0.5))) * _rise if _rise else 0.0
                return (
                    _cx - _w * s / 2 + slide
                    + _amp * math.sin(0.9 * t + _ph),
                    _cy - _h * s / 2 + rise
                    + _bob * math.sin(2 * math.pi * CARD_BOB_HZ * t + _ph)
                    + _amp * 0.6 * math.sin(0.7 * t + _ph + 1.1),
                )

            def breathe(t, _ph=phase):
                return spring_scale(t) * (
                    1.0 + CARD_BREATH
                    * math.sin(2 * math.pi * CARD_BREATH_HZ * t + _ph))

            card = (card.with_start(start)
                        .with_duration(dur)
                        .with_effects([vfx.Resize(breathe),
                                       vfx.CrossFadeIn(CARD_FADE_IN),
                                       vfx.CrossFadeOut(CARD_FADE_OUT)])
                        .with_position(pos))
            layers.append(card)
            n_cards += 1
    if n_cards:
        print(f"      art layers: {n_cards} card(s) across "
              f"{len(scenes)} scenes (multi-asset compositing ON)")
    return layers


# ---------------------------------------------------------------------------
# Layer E: brand-noir finishing pass (fast ffmpeg filter chain)
# ---------------------------------------------------------------------------

GRADE_VF = ("eq=contrast=1.05:saturation=1.06:gamma=0.985,"
            "colorbalance=rs=0.05:bs=0.12:rm=0.03:bm=-0.03:"
            "rh=0.05:gh=0.015:bh=-0.08,"
            "vignette=angle=PI/4.2,"
            "noise=alls=4:allf=t+u")


def apply_grade(video_path: Path) -> Path:
    """Violet shadows + amber highlights + vignette + film grain.
    Replaces the file in place; on any failure the ungraded file stays."""
    tmp = video_path.with_name(video_path.stem + "_graded.mp4")
    cmd = ["ffmpeg", "-y", "-i", str(video_path),
           "-vf", GRADE_VF,
           "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-c:a", "copy", "-pix_fmt", "yuv420p", str(tmp)]
    try:
        print("      grade: brand noir (violet shadows / amber highs / "
              "vignette / grain)")
        subprocess.run(cmd, check=True, capture_output=True)
        tmp.replace(video_path)
    except Exception as exc:
        print(f"      grade skipped ({str(exc)[:80]})")
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return video_path


# ---------------------------------------------------------------------------
# Style switch
# ---------------------------------------------------------------------------

def style_enabled(args) -> bool:
    """--style v2 / STYLE env turns the motion edit on (v1 = fallback)."""
    style = getattr(args, "style", None) or os.getenv("STYLE", "") or "v1"
    return str(style).strip().lower() in ("v2", "2", "v3", "motion", "on")


# late imports (moviepy objects, PIL) - keeps module import cheap for step2
from moviepy import VideoFileClip, VideoClip, ImageClip, vfx  # noqa: E402
from PIL import Image as PILImage                              # noqa: E402
