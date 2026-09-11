#!/usr/bin/env python3
"""
art_library.py - the channel's OWN illustration pack (v3 "motion edit" layer B).

Why
---
Stock footage alone is why the videos read as AI slop: nothing on screen is
owned by the channel. This module is a persistent, style-LOCKED library of
dark-surreal editorial illustrations (generated free via Pollinations Flux -
the same $0 endpoint step_thumbnail.py already uses) that pop onto the
screen as framed cards on top of the background video, layered with the
karaoke captions. Multiple cards can be visible at once (main + secondary +
micro) - the compositor in motion_edit.py decides placement per scene.

Hybrid strategy (user decision, 2026-09)
----------------------------------------
    - the LIBRARY is persistent: manifest + generated PNGs live in the repo
      (art_library/cache/<id>.png) and are reused across videos
    - the background VIDEO stays fresh Pexels per run (curated_library.py)
    - every render commits the reuse log + any newly generated PNGs back,
      so day 1 pays the generation cost and later days are free

Style lock
----------
Every prompt ends with the same style suffix, so all cards share one look:
deep violet/indigo palette, warm amber rim light, grainy film texture.
That consistency - not any single image - is the art direction.

Reuse rules (mirrors curated_library.py)
----------------------------------------
    - one entry never appears twice inside one video
    - entries used in >= 3 of the last 5 videos are deprioritized
    - the log (art_library/reuse_log.json) keeps the last 30 videos

Everything is best-effort: a failed generation simply means that scene gets
no card (background + captions still carry it). A render can never die here.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

ART_DIR = Path(__file__).resolve().parent / "art_library"
MANIFEST_FILE = ART_DIR / "library.json"
CACHE_DIR = ART_DIR / "cache"
REUSE_FILE = ART_DIR / "reuse_log.json"

# --- style lock ----------------------------------------------------------
STYLE_SUFFIX = (", dark surreal editorial illustration, deep violet and "
                "indigo palette, warm amber rim light highlights, grainy "
                "film texture, dramatic chiaroscuro lighting, ONE isolated "
                "subject centered on a plain dark empty background, minimal "
                "composition, no environment, no people, no person, no human "
                "figure, no silhouette of a person, no face, no text, no "
                "words, no watermark")

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_MODELS = ["flux", "turbo"]
POLLINATIONS_TIMEOUT = 75          # seconds per request
MAX_ATTEMPTS = 4
GLOBAL_BUDGET = float(             # total seconds this step may spend online
    os.getenv("ART_POLLINATIONS_BUDGET", "300"))
MIN_SIDE = 500                     # reject tiny/broken responses

# Card geometry (fractions of frame - resolved to px by motion_edit.py)
CARD_W = {"lg": 0.40, "md": 0.30, "sm": 0.22}
SOURCE_W, SOURCE_H = 832, 1216     # portrait ~0.685 ratio for Pollinations

RECENT_WINDOW, RECENT_MAX = 5, 3
HISTORY_KEEP = 30
EMOTION_BONUS = 0.75               # same weight as curated_library


# ---------------------------------------------------------------------------
# Manifest: 36 hand-written concepts covering the channel's themes
# ---------------------------------------------------------------------------

def _e(eid, prompt, tags, emotions):
    return {"id": eid, "prompt": prompt, "tags": tags, "emotions": emotions}


# v2.1: entries that depict human FIGURES/FACES are banned. The user's #1
# complaint was "a girl body shown between transitions" - these render as
# people silhouettes even as illustrations. This blocklist is checked at
# SELECTION time too, so a stale library.json can never pick them again.
NO_FIGURE_ENTRIES = {
    "mirror_split", "puppet_hands", "mask_reveal", "crowd_blur",
    "smoke_figure", "split_face", "shadow_follow", "weight_shrug",
}

ENTRIES = [
    _e("chess_king", "a lone chess king piece standing on the board edge surrounded by fallen pieces",
       ["chess", "strategy", "power", "game", "move"], ["serious", "calm"]),
    _e("storm_head", "a dark storm cloud swirling inside a transparent glass head sculpture",
       ["mind", "thought", "storm", "overthink", "anxiety"], ["intense", "serious"]),
    _e("hourglass_ember", "an hourglass where the falling sand turns into glowing embers",
       ["time", "patience", "burn", "wait", "running"], ["calm", "urgent"]),
    _e("maze_brain", "a labyrinth carved into the shape of a human brain seen from above",
       ["mind", "maze", "puzzle", "thought", "logic"], ["curious", "serious"]),
    _e("eye_keyhole", "a giant eye looking through a keyhole of a locked door",
       ["watch", "observe", "secret", "attention", "notice"], ["curious"]),
    _e("broken_chain", "a heavy iron chain snapped open with warm light bursting from the gap",
       ["chain", "free", "break", "habit", "freedom"], ["triumphant", "intense"]),
    _e("rose_thorns", "a single wilting rose wrapped in dark barbed wire",
       ["rose", "love", "pain", "toxic", "relationship"], ["serious", "intense"]),
    _e("letter_burn", "an old handwritten letter burning slowly from one corner with glowing embers",
       ["letter", "memory", "secret", "burn", "truth"], ["curious", "serious"]),
    _e("crown_stone", "a heavy iron crown resting on a cracked stone pedestal in mist",
       ["power", "crown", "ego", "burden", "king"], ["serious", "calm"]),
    _e("door_ajar", "an old heavy door slightly open with blinding warm light pouring through the gap",
       ["door", "opportunity", "secret", "light", "open"], ["curious", "triumphant"]),
    _e("balloon_night", "one dark balloon escaping upward into a starry night sky, its string snapped",
       ["freedom", "escape", "release", "let go", "rise"], ["triumphant", "calm"]),
    _e("ice_key", "a golden key frozen inside a clear block of ice",
       ["locked", "frozen", "secret", "access", "barrier"], ["curious", "serious"]),
    _e("thread_snap", "a golden thread stretched to its breaking point between two iron hooks",
       ["tension", "break", "limit", "pressure", "snap"], ["intense", "urgent"]),
    _e("twin_doors", "two doors side by side, one bright one dark, a hand hesitating between them",
       ["choice", "decision", "path", "hesitate", "pick"], ["curious", "urgent"]),
    _e("ladder_fog", "a wooden ladder climbing into thick fog with no top visible",
       ["climb", "goal", "unknown", "ambition", "fog"], ["curious", "calm"]),
    _e("scale_tipping", "old brass scales tipping heavily to one side under a single feather",
       ["balance", "justice", "favor", "weigh", "give"], ["serious"]),
    _e("hand_release", "an open hand releasing a small bird into the sky",
       ["let go", "release", "freedom", "give", "trust"], ["triumphant", "calm"]),
    _e("iceberg_tip", "an iceberg at night with a vast glowing mass hidden below the waterline",
       ["hidden", "depth", "iceberg", "more", "surface"], ["curious", "serious"]),
    _e("wolf_fold", "a wolf wearing a sheep's wool coat standing inside a flock",
       ["wolf", "disguise", "fake", "trust", "danger"], ["intense", "curious"]),
    _e("rope_fray", "a rope bridge mid-span with strands fraying apart over a dark canyon",
       ["trust", "fragile", "break", "risk", "bridge"], ["intense", "urgent"]),
    _e("compass_spin", "a brass compass with its needle spinning wildly on an old map",
       ["lost", "direction", "confuse", "purpose", "compass"], ["curious"]),
    _e("clocks_wall", "a wall of melting clocks all showing different times",
       ["time", "clock", "wait", "inconsistent", "drift"], ["curious", "calm"]),
    _e("throne_dust", "an empty throne in an abandoned hall covered in dust with one beam of light",
       ["power", "empty", "ego", "throne", "fall"], ["serious", "calm"]),
    _e("glass_crack", "a drinking glass mid-shatter frozen in time with amber light inside",
       ["break", "fragile", "moment", "shatter", "pressure"], ["intense", "urgent"]),
    _e("book_ember", "an open book with pages glowing and curling into embers at the edges",
       ["knowledge", "burn", "learn", "story", "read"], ["curious", "calm"]),
    _e("key_lock", "an old key turning in a lock with light spilling through the opening door",
       ["unlock", "solution", "secret", "key", "open"], ["curious", "triumphant"]),
    _e("card_tower", "a tall tower of playing cards wavering with one card sliding out",
       ["fragile", "collapse", "risk", "unstable", "fall"], ["intense", "playful"]),
    _e("footsteps_fade", "a trail of footprints in snow slowly covered by fresh snowfall",
       ["past", "erase", "forget", "trace", "memory"], ["calm", "serious"]),
    _e("lantern_dark", "a hand holding a small lantern cutting through a vast dark forest",
       ["guide", "hope", "dark", "light", "alone"], ["calm", "triumphant"]),
    _e("whisper_hands", "two open hands cupped together with sound ripples drawn around them",
       ["listen", "whisper", "influence", "talk", "words"], ["curious", "playful"]),
    _e("mirror_hands", "two hands reaching toward each other through a mirror surface",
       ["connect", "reach", "help", "ask", "mirror"], ["curious", "triumphant"]),
    _e("candle_gust", "a single candle flame bending hard in the wind but not going out",
       ["persist", "resist", "pressure", "endure", "flame"], ["intense", "triumphant"]),
    _e("needle_thread", "a needle pulling a golden thread stitching two torn pieces together",
       ["fix", "repair", "connect", "mend", "favor"], ["curious", "calm"]),
    _e("empty_chair", "an empty chair at a dinner table set for two",
       ["absence", "lonely", "wait", "missing", "attention"], ["serious", "calm"]),
    _e("moth_flame", "a moth circling a bright flame dangerously close",
       ["attract", "desire", "pull", "danger", "draw"], ["curious", "urgent"]),
]


def load_entries() -> list[dict]:
    """Manifest entries; falls back to the built-in ENTRIES if the JSON is
    missing/corrupt (the JSON exists so the pack can be curated later
    without touching code)."""
    try:
        data = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        entries = [e for e in data.get("entries", []) if e.get("id") and e.get("prompt")]
        if entries:
            return entries
    except Exception:
        pass
    return ENTRIES


# ---------------------------------------------------------------------------
# Reuse tracking (same contract as curated_library.py)
# ---------------------------------------------------------------------------

def _load_reuse() -> dict:
    try:
        return json.loads(REUSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"videos": {}, "totals": {}}


def _recent_use_count(reuse: dict, entry_id: str) -> int:
    """How many of the last 5 videos used this entry."""
    vids = reuse.get("videos", {})
    recent = sorted(vids.items())[-RECENT_WINDOW:]
    return sum(1 for _, used in recent if entry_id in (used or []))


def commit_reuse(run_key: str, used_ids: list[str]) -> None:
    """Record which art entries this video used (CI commits the file back)."""
    try:
        reuse = _load_reuse()
        reuse.setdefault("videos", {})[run_key] = sorted(set(used_ids))
        vids = reuse["videos"]
        if len(vids) > HISTORY_KEEP:
            for k in sorted(vids)[:-HISTORY_KEEP]:
                vids.pop(k)
        ART_DIR.mkdir(parents=True, exist_ok=True)
        REUSE_FILE.write_text(json.dumps(reuse, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"      art reuse log: not saved ({str(exc)[:60]})")


# ---------------------------------------------------------------------------
# Selection: tag-overlap match per beat (reuses curated_library's scorer)
# ---------------------------------------------------------------------------

def pick_cards_for_beats(beats: list[dict], run_key: str) -> dict[int, dict]:
    """Best art entry per beat -> {beat_index: entry}.

    Same rules as the footage library: tag overlap against the beat's
    visual_concept, emotion affinity bonus, no repeat within the video,
    recent-use cooldown across videos."""
    import curated_library as clib

    entries = load_entries()
    if not entries or not beats:
        return {}
    # v2.1: human-figure entries are banned from selection entirely
    entries = [e for e in entries if e["id"] not in NO_FIGURE_ENTRIES]
    reuse = _load_reuse()

    scored: list[tuple[float, str, dict, int]] = []   # (score, tiebreak, entry, beat_idx)
    chosen: dict[int, dict] = {}
    used_ids: set[str] = set()

    # score every (beat, entry) pair
    pairs: list[tuple[int, dict, float]] = []
    for beat in beats:
        idx = beat.get("index")
        if idx is None:
            continue
        concept = beat.get("visual_concept") or ""
        emotion = (beat.get("emotion") or "").lower()
        for entry in entries:
            if entry["id"] in used_ids:
                continue
            score, _hits = clib.score_entry(entry, concept, emotion)
            if score < 1.0:
                continue
            score -= 0.6 * _recent_use_count(reuse, entry["id"])
            pairs.append((idx, entry, score))

    # best (highest score) first, keeping spacing between beats
    pairs.sort(key=lambda p: (-p[2], p[1]["id"]))
    for idx, entry, score in pairs:
        if idx in chosen or entry["id"] in used_ids:
            continue
        chosen[idx] = entry
        used_ids.add(entry["id"])

    # pass 2: emotion-affinity fallback - abstract concepts often share no
    # literal tag with the pack, but the BEAT's emotion always matches the
    # art mood (intense -> puppet strings, calm -> lantern, curious -> keys)
    for beat in beats:
        idx = beat.get("index")
        if idx is None or idx in chosen:
            continue
        emotion = (beat.get("emotion") or "").lower()
        candidates = []
        for entry in entries:
            if entry["id"] in used_ids:
                continue
            if emotion and emotion in [e.lower() for e in entry.get("emotions", [])]:
                penalty = 0.6 * _recent_use_count(reuse, entry["id"])
                candidates.append((penalty, entry["id"], entry))
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1]))
            chosen[idx] = candidates[0][2]
            used_ids.add(candidates[0][1])

    return chosen


# ---------------------------------------------------------------------------
# Pollinations client (free, no key) - same acceptance gate as step_thumbnail
# ---------------------------------------------------------------------------

def _accept(content: bytes, content_type: str) -> tuple[bool, str]:
    if not content:
        return False, "empty body"
    if len(content) <= 20 * 1024:
        return False, f"too small ({len(content)}B)"
    if "image" not in (content_type or "").lower():
        return False, f"content-type {content_type[:40]!r}"
    from io import BytesIO
    from PIL import Image
    try:
        probe = Image.open(BytesIO(content))
        probe.verify()
        img = Image.open(BytesIO(content))
        if min(img.size) < MIN_SIDE:
            return False, f"resolution too low {img.size}"
    except Exception as exc:
        return False, f"corrupt image ({str(exc)[:50]})"
    return True, ""


def _fetch_png(prompt: str, seed: int, deadline: float) -> bytes | None:
    """Download one illustration; every model gets at least one attempt."""
    import requests as rq
    from urllib.parse import quote

    for model in POLLINATIONS_MODELS:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if time.time() > deadline:
                return None
            params = {"width": SOURCE_W, "height": SOURCE_H, "model": model,
                      "seed": (seed + attempt * 17) % 9_999_991,
                      "nologo": "true", "private": "true"}
            url = POLLINATIONS_URL.format(prompt=quote(prompt, safe=""))
            try:
                resp = rq.get(url, params=params, timeout=POLLINATIONS_TIMEOUT)
            except Exception as exc:
                print(f"      art {model}: request error ({str(exc)[:50]})")
                time.sleep(min(6, 1.5 * attempt))
                continue
            ok, why = _accept(resp.content, resp.headers.get("content-type", ""))
            if ok and resp.status_code == 200:
                print(f"      art: '{prompt[:38]}...' via {model} "
                      f"({len(resp.content) // 1024}KB)")
                return resp.content
            print(f"      art {model}: try {attempt} rejected (HTTP "
                  f"{resp.status_code}, {why})")
            time.sleep(min(5, 1.5 * attempt))
    return None


def ensure_png(entry: dict, deadline: float) -> Path | None:
    """The generated PNG for a library entry, from cache or freshly made.
    Tainted generations shift the seed, so a retry draws a NEW image."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / f"{entry['id']}.png"
    if cached.is_file() and cached.stat().st_size > 20 * 1024:
        return cached
    import hashlib
    seed = int(hashlib.sha256(entry["id"].encode()).hexdigest()[:8], 16)
    seed = (seed + 1009 * _bad_count(entry["id"])) % 9_999_991
    png = _fetch_png(entry["prompt"] + STYLE_SUFFIX, seed, deadline)
    if png is None:
        return None
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(png)).convert("RGB")
        img.save(cached, "PNG", optimize=True)
        return cached
    except Exception as exc:
        print(f"      art: cache save failed ({str(exc)[:50]})")
        return None


# ---------------------------------------------------------------------------
# Card prep: rounded corners + thin rim + baked drop shadow (RGBA)
# ---------------------------------------------------------------------------

def prepare_card(src: Path, width_px: int, out_path: Path,
                 corner_ratio: float = 0.055) -> Path | None:
    """Turn a library PNG into the finished RGBA card:
    rounded corners, subtle amber rim, soft drop shadow baked below.
    Returns out_path (the compositor just pastes this image)."""
    try:
        from PIL import Image, ImageDraw, ImageFilter

        src_img = Image.open(src).convert("RGB")
        # crop to the card aspect (portrait 0.78 - slightly taller than wide)
        target_ratio = 0.78
        w, h = src_img.size
        if w / h > target_ratio:                    # too wide -> crop sides
            nw = int(h * target_ratio)
            x1 = (w - nw) // 2
            src_img = src_img.crop((x1, 0, x1 + nw, h))
        else:                                       # too tall -> crop bottom
            nh = int(w / target_ratio)
            src_img = src_img.crop((0, 0, w, nh))

        card_w = width_px
        card_h = int(card_w / target_ratio)
        card = src_img.resize((card_w, card_h), Image.LANCZOS)

        radius = max(8, int(card_w * corner_ratio))
        # rounded-corner alpha mask
        mask = Image.new("L", (card_w, card_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, card_w - 1, card_h - 1), radius=radius, fill=255)
        card.putalpha(mask)

        # thin amber rim (drawn inside the mask so corners stay clean)
        rim = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        ImageDraw.Draw(rim).rounded_rectangle(
            (1, 1, card_w - 2, card_h - 2), radius=radius,
            outline=(245, 217, 10, 90), width=2)
        card = Image.alpha_composite(card, rim)

        # baked drop shadow: card alpha -> blurred black halo below-right
        pad = max(14, card_w // 12)
        canvas = Image.new("RGBA", (card_w + 2 * pad, card_h + 2 * pad + pad // 2),
                           (0, 0, 0, 0))
        shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        sh_layer = Image.new("L", (card_w, card_h), 0)
        ImageDraw.Draw(sh_layer).rounded_rectangle(
            (0, 0, card_w - 1, card_h - 1), radius=radius, fill=150)
        shadow.paste(Image.new("RGBA", (card_w, card_h), (0, 0, 0, 255)),
                     (pad + pad // 3, pad + pad // 2), sh_layer)
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=pad // 2))
        canvas = Image.alpha_composite(canvas, shadow)
        canvas.paste(card, (pad, pad), card)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path
    except Exception as exc:
        print(f"      card prep failed ({str(exc)[:60]})")
        return None


# ---------------------------------------------------------------------------
# AI cutout: rembg (U2Net salient-object segmentation, pre-trained - no
# training required) turns each illustration into a TRANSPARENT PNG so the
# compositor can float the actual subject (a hand, a mask, a chess piece)
# over the video - no background box. u2netp is the 4.7 MB variant chosen
# so CI stays fast; the model file is cached by the workflow.
# ---------------------------------------------------------------------------

CUTOUT_MODEL = (os.getenv("ART_CUTOUT_MODEL", "u2netp") or "u2netp").strip()
CUTOUT_MIN_COVERAGE = 0.03          # alpha must cover >= 3% (a real subject)
CUTOUT_MAX_COVERAGE = 0.80          # ... but < 80% (not the whole frame)
HUMAN_MAX_COVERAGE = float(         # generated art containing a person at
    os.getenv("ART_HUMAN_MAX", "0.10"))   # >= this coverage is rejected
_SESSION = None
_HUMAN_SESSION = None


def _rembg_session():
    """Lazy rembg session (model load is ~1-2 s; reused for all entries)."""
    global _SESSION
    if _SESSION is None:
        from rembg import new_session
        _SESSION = new_session(CUTOUT_MODEL)
    return _SESSION


def _env_flag(name: str, default: str = "on") -> bool:
    """Local env-flag helper (step2's env_flag, without the heavy import)."""
    v = (os.getenv(name, "") or "").strip().lower()
    if not v:
        v = default
    return v not in ("off", "0", "false", "none")


def _human_session():
    """DEPRECATED stub kept for API compat - the u2net_human_seg model
    flagged hourglasses and chains as 'human' while missing small dark
    figures. Person detection now uses YOLOv8n (see _person_detections)."""
    return None


# --- person detection (SSD-MobileNet v1, ONNX model zoo) --------------------
# u2net_human_seg misfired on human-SHAPED objects (an hourglass is literally
# human-silhouette shaped), so the gate runs a real COCO person detector
# instead: SSD-MobileNet v1 from the onnx/models GitHub release (~27 MB,
# cached, github.com is always reachable from CI). Person class only,
# confidence-gated. A hit means the generated art contains a person -
# regenerate with a new seed.
DETECTOR_URL = ("https://github.com/onnx/models/raw/main/validated/vision/"
                "object_detection_segmentation/ssd-mobilenetv1/model/"
                "ssd_mobilenet_v1_10.onnx")
DETECTOR_DIR = Path(os.getenv("ART_DETECT_DIR",
                              str(Path.home() / ".cache" / "artdetect")))
PERSON_CONF = float(os.getenv("ART_PERSON_CONF", "0.55"))
# 0.55 calibrated visually: real-person art scores 0.60-0.99 (girl in
# mirror 0.77, victorian woman 0.90, smoke figure 0.99), while hands
# (0.45) and iceberg silhouettes (0.43) stay below the gate.
PERSON_MIN_AREA = float(os.getenv("ART_PERSON_MIN_AREA", "0.03"))  # of frame


def _detector_path() -> Path:
    DETECTOR_DIR.mkdir(parents=True, exist_ok=True)
    return DETECTOR_DIR / "ssd_mobilenet_v1_10.onnx"


def _load_detector():
    import onnxruntime as ort
    p = _detector_path()
    if not p.is_file() or p.stat().st_size < 1_000_000:
        import requests as rq
        tmp = p.with_suffix(".part")
        with rq.get(DETECTOR_URL, stream=True, timeout=180) as r:
            r.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
        tmp.replace(p)
    return ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])


def _person_detections(img, conf_thres: float | None = None):
    """[[x0,y0,x1,y1,conf], ...] for COCO class 1 (person) above threshold.
    Coordinates are fractions of the original image size. Empty on any
    failure (detector unavailable must never block a render)."""
    try:
        import numpy as _np
        conf_thres = PERSON_CONF if conf_thres is None else conf_thres
        sess = _load_detector()
        W = H = 300
        im = img.convert("RGB").resize((W, H))
        # ssd_mobilenet_v1_10 takes NHWC uint8 0..255
        blob = _np.asarray(im, dtype=_np.uint8)[None]
        outs = sess.run(["detection_boxes:0", "detection_classes:0",
                         "detection_scores:0"], {"image_tensor:0": blob})
        boxes, classes, scores = outs[0][0], outs[1][0], outs[2][0]
        hits = []
        for i in range(len(classes)):
            if int(classes[i]) != 1 or float(scores[i]) < conf_thres:
                continue
            y0, x0, y1, x1 = boxes[i]           # SSD boxes are yxyn
            frac = (x1 - x0) * (y1 - y0)
            if frac >= PERSON_MIN_AREA:
                hits.append([float(x0), float(y0), float(x1), float(y1),
                             float(scores[i])])
        return hits
    except Exception as exc:
        print(f"      person detector unavailable ({str(exc)[:60]}) - "
              "gate passes")
        return []


def _bad_path(entry_id: str) -> Path:
    return CACHE_DIR / f"{entry_id}.bad"


def _bad_count(entry_id: str) -> int:
    try:
        return int(_bad_path(entry_id).read_text().strip() or 0)
    except Exception:
        return 0


def _bump_bad(entry_id: str) -> int:
    n = _bad_count(entry_id) + 1
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _bad_path(entry_id).write_text(str(n), encoding="utf-8")
    except Exception:
        pass
    return n


def ensure_cutout(entry: dict, deadline: float) -> tuple:
    """Transparent-background PNG for a library entry (cached per entry).

    Returns (path_or_None, tainted). `tainted=True` means the generated
    illustration contained a human figure - the base was deleted for a
    re-seeded regeneration, and the caller must NOT use the framed-card
    fallback either (the person is in the full image too)."""
    cut_path = CACHE_DIR / f"{entry['id']}_cut.png"
    if cut_path.is_file() and cut_path.stat().st_size > 10 * 1024:
        return cut_path, False
    src = ensure_png(entry, deadline)
    if src is None:
        return None, False
    try:
        import time as _t
        t0 = _t.time()
        from PIL import Image
        img = Image.open(src).convert("RGB")

        # person detector first: a human in the generated art is exactly
        # the "girl body" the user complained about - never ship it
        hits = _person_detections(img)
        if hits:
            best = max(h[4] for h in hits)
            n = _bump_bad(entry["id"])
            src.unlink(missing_ok=True)     # taint recorded, retry reseeds
            print(f"      art gate: '{entry['id']}' REJECTED - person "
                  f"detected (conf {best:.2f}, {len(hits)} hit(s)); "
                  f"regenerating with a new seed (attempt {n})")
            return None, True

        from rembg import remove
        cut = remove(img, session=_rembg_session())
        alpha = cut.getchannel("A")
        hist = alpha.histogram()
        solid = sum(hist[200:])                      # clearly-opaque pixels
        cov = solid / float(alpha.size[0] * alpha.size[1])
        if cov < CUTOUT_MIN_COVERAGE or cov > CUTOUT_MAX_COVERAGE:
            print(f"      art cutout: '{entry['id']}' rejected "
                  f"(alpha coverage {cov:.0%})")
            return None, False
        cut.save(cut_path, "PNG", optimize=True)
        print(f"      art cutout: '{entry['id']}' -> transparent PNG "
              f"({cov:.0%} subject, {(_t.time() - t0):.1f}s, {CUTOUT_MODEL})")
        return cut_path, False
    except Exception as exc:
        print(f"      art cutout: '{entry['id']}' failed ({str(exc)[:70]})")
        return None, False


# ---------------------------------------------------------------------------
# Cutout prep: trim to the subject, amber edge glow + baked drop shadow,
# optional tilt. NO background box, NO rounded frame - the silhouette IS
# the shape, so every asset is a different size and shape by nature.
# ---------------------------------------------------------------------------

def prepare_cutout(src: Path, width_px: int, out_path: Path,
                   tilt_deg: float = 0.0) -> Path | None:
    """Turn a transparent cutout into a finished RGBA overlay:
    trimmed silhouette + amber rim glow + soft drop shadow (+ tilt).
    Returns out_path; the compositor just pastes this image."""
    try:
        from PIL import Image, ImageFilter

        img = Image.open(src).convert("RGBA")
        alpha = img.getchannel("A")
        bbox = alpha.point(lambda a: 255 if a >= 8 else 0).getbbox()
        if not bbox:
            return None
        img = img.crop(bbox)

        # scale the subject to the requested width. v2.1: ADAPTIVE lift -
        # noir art is dark-on-dark, so measure the subject's mean luminance
        # and lift it to a readable level over a dark video (per-asset:
        # near-black art gets a big lift, bright art almost none).
        w0, h0 = img.size
        if w0 < 8 or h0 < 8:
            return None
        scale = width_px / float(w0)
        img = img.resize((width_px, max(8, int(h0 * scale))), Image.LANCZOS)
        try:
            import numpy as _np
            a_arr = _np.asarray(img.getchannel("A"), dtype=_np.float32)
            mask = a_arr >= 8
            rgb_arr = _np.asarray(img.convert("RGB"), dtype=_np.float32)
            if mask.any():
                lum = (0.299 * rgb_arr[..., 0] + 0.587 * rgb_arr[..., 1]
                       + 0.114 * rgb_arr[..., 2])[mask]
                mean_lum = float(lum.mean())
                lift = min(2.2, max(1.0, 98.0 / max(mean_lum, 24.0)))
                rgb_arr = _np.clip(rgb_arr * lift, 0, 255)
                print(f"      cutout prep: luminance {mean_lum:.0f} -> "
                      f"x{lift:.2f} lift")
            rgba = Image.fromarray(
                _np.dstack([rgb_arr.astype("uint8"),
                            _np.asarray(img.getchannel("A"), dtype="uint8")]),
                "RGBA")
            img = rgba
        except Exception:
            pass

        pad = max(16, width_px // 7)               # room for glow/shadow/tilt
        canvas_w = img.size[0] + 2 * pad
        canvas_h = img.size[1] + 2 * pad + pad // 2

        a = img.getchannel("A")
        glow = a.filter(ImageFilter.MaxFilter(9))            # dilate
        glow = glow.filter(ImageFilter.GaussianBlur(5))
        glow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        amber = Image.new("RGBA", img.size, (245, 180, 60, 150))
        glow_layer.paste(amber, (pad, pad), glow)

        shadow = a.filter(ImageFilter.GaussianBlur(max(4, width_px // 22)))
        shadow_layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        black = Image.new("RGBA", img.size, (0, 0, 0, 120))
        shadow_layer.paste(black, (pad + pad // 3, pad + pad // 2), shadow)

        canvas = Image.alpha_composite(shadow_layer, glow_layer)
        canvas.paste(img, (pad, pad), img)

        if abs(tilt_deg) >= 0.5:
            canvas = canvas.rotate(tilt_deg, resample=Image.BICUBIC,
                                   expand=True)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        return out_path
    except Exception as exc:
        print(f"      cutout prep failed ({str(exc)[:60]})")
        return None


def _tilt_for(entry_id: str, variant: str) -> float:
    """Deterministic per-(entry, variant) tilt so cards stop looking uniform."""
    import hashlib
    seed = int(hashlib.sha1(f"{entry_id}:{variant}".encode()).hexdigest()[:6], 16)
    return float(seed % 13) - 6.0                   # -6..+6 degrees


def ensure_cards_for_beats(beats: list[dict], run_folder: Path) -> dict[int, Path]:
    """Public API for motion_edit.py:
    beat index -> prepared RGBA card PNG (or absent if nothing matched /
    generation failed). Respects the global generation budget."""
    chosen = pick_cards_for_beats(beats, run_folder.name)
    if not chosen:
        print("      art cards: no matching entries for this script")
        return {}
    deadline = time.time() + GLOBAL_BUDGET
    cards: dict[int, Path] = {}
    used_ids: list[str] = []
    n_cut, n_framed = 0, 0
    prep_dir = run_folder / "cards"
    for idx in sorted(chosen):
        entry = chosen[idx]
        out = prep_dir / f"{entry['id']}_lg.png"
        ready = None
        cut, tainted = ensure_cutout(entry, deadline)   # transparent subject
        if tainted:
            continue          # person detected in the art - no card at all
        if cut is not None:
            ready = prepare_cutout(cut, int(1080 * CARD_W["lg"]), out,
                                   tilt_deg=_tilt_for(entry["id"], "lg"))
            if ready:
                n_cut += 1
        if ready is None and _env_flag("ART_FRAMED_FALLBACK", "off"):
            # v2.1.1: framed fallback is OFF by default - the user wants the
            # bare PNG subject, and an unvetted full-scene card can smuggle a
            # person back in. Opt in only if rembg is unavailable.
            src = ensure_png(entry, deadline)
            if src is not None:
                ready = prepare_card(src, int(1080 * CARD_W["lg"]), out)
                if ready:
                    n_framed += 1
        if ready:
            cards[idx] = ready
            used_ids.append(entry["id"])
        if time.time() > deadline:
            print("      art: generation budget spent - continuing with "
                  f"{len(cards)} card(s)")
            break
    commit_reuse(run_key := run_folder.name, used_ids)
    print(f"      art cards: {len(cards)}/{len(chosen)} ready "
          f"({n_cut} transparent cutouts, {n_framed} framed fallback) - "
          f"{', '.join(e['id'] for e in chosen.values() if e['id'] in used_ids) or 'none'}")
    return cards
