#!/usr/bin/env python3
"""
step_thumbnail.py - fully Gemini-GENERATED thumbnails (v2, creative mode).

Every thumbnail is painted BY Gemini (gemini-2.5-flash-image, "nano banana"):
no video frames are extracted and no text is drawn locally. Gemini creates
the artwork AND renders the headline text itself, guided entirely by the
beats JSON it already produced in step 1 (hook text, emotion, emphasis,
visual concepts, title) - no extra script-generation call.

Variants (all saved for review, one becomes the upload default):
    hook      bold typographic poster - huge yellow all-caps hook line on a
              dark symbolic scene matching beat[0]
    midpoint  dramatic scene from the strongest mid-video beat + short caption
    clean     text-free atmospheric artwork (pure mood, maximum intrigue)

Both resolutions are generated NATIVELY in their own aspect ratio - never a
stretch of one master image:
    YouTube   1280x720  (16:9)  -> thumbnails.set right after videos().insert
    Instagram 1080x1920 (9:16)  -> clip_upload(thumbnail=...) cover frame
(The raw Gemini output is center-cover cropped to the exact pixel size - a
crop, never a distortion - and the YouTube JPG is squeezed under 2 MB.)

Quality gate: every text-bearing image is read back by a cheap Gemini vision
call ("transcribe the text"); if the headline is not spelled exactly, the
image is regenerated once (max 1 retry per variant/aspect).

Outputs (review folder: output/<run_id>/thumbnails/)
    <variant>_youtube.jpg / <variant>_instagram.png   (+ same for all variants)
    preview_mobile_selected.png / preview_mobile_youtube.png (tiny legibility
    proof of the auto-selected variant)
    thumbnail_manifest.json - everything step3_upload.py needs

Config (env or CLI, all optional)
    THUMBNAIL_VARIANT       hook | midpoint | clean      (default hook)
    AUTO_UPLOAD_THUMBNAIL   true | false                 (default true; false
                            = generate + save for review, skip auto-attach)
    THUMB_VARIANTS          comma list to build (default "hook,midpoint,clean")
    GEMINI_IMAGE_MODEL      image model (default gemini-2.5-flash-image)
    GEMINI_THUMB_MODEL      caption-shorten / vision-verify model
                            (default gemini-2.5-flash-lite)

This step is best-effort by design: any failure prints a WARNING and exits 0
(unless --strict) so a thumbnail problem can never block publishing.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:    # local .env (CI injects real env vars itself)
    from step1_generate import load_environment
except Exception:                                        # pragma: no cover
    def load_environment(_):
        return None

try:    # proven text-model chain for shortening + verification
    from step1_generate import resolve_models as _resolve_models
except Exception:                                        # pragma: no cover
    def _resolve_models():
        return ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.5-flash-lite"]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

YT_SIZE = (1280, 720)          # YouTube thumbnail spec (16:9)
IG_SIZE = (1080, 1920)         # Instagram Reels cover spec (9:16)

DEFAULT_VARIANT = "hook"       # hook | midpoint | clean
VARIANTS_ALL = ("hook", "midpoint", "clean")
TRUNCATE_WORDS = 6             # caption longer than this -> shorten it

# Static fallbacks are only used when live model discovery fails (discovery
# needs a geo-supported IP - CI has one, this matters because image-model
# names rotate fast and retired names answer 404 / limit:0).
_IMAGE_FALLBACKS = [
    "gemini-3.6-flash-image",
    "gemini-3.1-flash-image-preview",
    "gemini-3-pro-image-preview",
    "gemini-2.5-flash-image",
]
_IMAGE_ENV = (os.getenv("GEMINI_IMAGE_MODEL", "") or "").strip()
_TEXT_FIRST = ((os.getenv("GEMINI_THUMB_MODEL", "") or "").strip()
               or (_resolve_models() or ["gemini-flash-latest"])[0])
VERIFY_MODELS = list(dict.fromkeys(
    [_TEXT_FIRST] + list(_resolve_models())))

_discovered_images: list[str] = []          # populated once per process


def discover_image_models(api_key: str) -> list[str]:
    """Live-discover generateContent-capable image models for THIS key.

    Models.list works only from a geo-supported IP (CI yes, some sandboxes
    no) - on failure we simply keep the static fallback chain.
    """
    global _discovered_images
    if _discovered_images:
        return _discovered_images
    try:
        client = _client(api_key)
        found: list[str] = []
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            if "image" not in name or "embedding" in name:
                continue
            actions = [getattr(a, "name", "") for a in
                       (getattr(m, "supported_actions", None) or [])]
            if not any(":generateContent" in a for a in actions):
                continue
            # flash tier first (free-tier friendly + fast), then the rest
            score = 0
            if "flash-image" in name:
                score += 100
            elif "flash" in name:
                score += 80
            elif "pro" in name:
                score += 60
            if "preview" in name:
                score += 1
            found.append((-score, name))
        found.sort()
        _discovered_images = [name for _, name in found]
        if _discovered_images:
            print(f"      discovered image models: "
                  f"{', '.join(_discovered_images[:4])}")
    except Exception as exc:
        print(f"      model discovery unavailable ({str(exc)[:60]}) - "
              "using fallback chain")
    return _discovered_images

STOPWORD_TAIL = {"a", "an", "and", "are", "but", "for", "in", "is", "of",
                 "on", "or", "that", "the", "this", "to", "with", "you",
                 "your", "it", "its", "was", "were", "so", "as", "at", "by"}

# channel look - matches the v2 caption / title-card aesthetic
STYLE_BASE = (
    "Moody cinematic digital artwork for a faceless dark-psychology and "
    "philosophy YouTube channel. Deep blacks, navy and violet palette, "
    "dramatic rim lighting, high contrast, subtle film grain, symbolic "
    "surreal imagery with one clear focal subject. Photorealistic detail, "
    "premium poster composition. No watermark, no logo, no signature."
)
EMOTION_MOODS = {
    "intense":    "tense confrontational energy, harsh shadows",
    "curious":    "intriguing mystery, drifting fog, a single spotlight",
    "playful":    "unexpected ironic twist, mischievous juxtaposition",
    "serious":    "grave stillness, cold blue light",
    "urgent":     "kinetic urgency, motion blur, pulsing light",
    "triumphant": "quiet victory, low golden light breaking through darkness",
    "calm":       "serene stillness, soft diffused light",
    "sad":        "melancholic emptiness, muted desaturated tones",
}
MOOD_DEFAULT = "contemplative tension, chiaroscuro lighting"


def env_flag(name: str, default: str = "on") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in \
        ("0", "off", "none", "false")


# ---------------------------------------------------------------------------
# Caption shortening (kept from v1 - one cheap text call when needed)
# ---------------------------------------------------------------------------

def _local_shorten(text: str, emphasis: list[str] | None = None) -> str:
    """No-Gemini fallback: emphasis phrase if it is punchy, else head words."""
    words = re.sub(r"[^A-Za-z0-9' ]+", " ", text).split()
    if not words:
        return text.upper()[:40]
    if emphasis:
        phrase = re.sub(r"[^A-Za-z0-9' ]+", " ", " ".join(emphasis)).split()
        if 3 <= len(phrase) <= TRUNCATE_WORDS:
            return " ".join(phrase).upper()
    keep = words[:TRUNCATE_WORDS]
    while len(keep) > 3 and keep[-1].lower() in STOPWORD_TAIL:
        keep.pop()
    return " ".join(keep).upper()


def gemini_shorten(texts: dict[str, str], api_key: str | None) -> dict[str, str]:
    """One quick call: '3-6 word punchy thumbnail caption, all caps, no period'.

    texts = {"hook": ..., "mid": ...} (mid optional). Returns the same keys
    with shortened strings; raises on total failure so callers can fall back.
    """
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    from google import genai
    from google.genai import types

    parts = "\n".join(f"{k.upper()}_TEXT: {v}" for k, v in texts.items())
    prompt = (
        "Shorten each line below into a punchy thumbnail caption.\n"
        "RULES: 3 to 6 words, ALL CAPS, no period, keep the strongest "
        "tension/curiosity words, do not invent new claims.\n\n"
        f"{parts}\n\n"
        'Return STRICT JSON only: {"hook": "...", "mid": "..."} '
        "(include only the keys given)."
    )
    client = genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=45000))
    last_exc: Exception | None = None
    for model in VERIFY_MODELS:
        for attempt in (1, 2):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.4, max_output_tokens=120,
                        response_mime_type="application/json"))
                raw = re.sub(r"^```(?:json)?|```$", "",
                             (resp.text or "").strip(), flags=re.MULTILINE)
                data = json.loads(raw)
                out = {}
                for key, original in texts.items():
                    value = str(data.get(key, "")).strip().strip('"').upper()
                    value = re.sub(r"[.\u2026]+$", "", value).strip()
                    n_words = len(value.split())
                    if not (3 <= n_words <= 8):
                        raise ValueError(f"{key}: bad shorten ({value[:40]!r})")
                    out[key] = value
                print(f"      gemini shorten ok ({model})")
                return out
            except Exception as exc:
                last_exc = exc
                if attempt == 1:
                    time.sleep(1.5)
        print(f"      gemini shorten: {model} failed ({str(last_exc)[:70]})")
    raise RuntimeError(f"gemini shorten failed ({str(last_exc)[:80]})")


def resolve_thumbnail_texts(hook_text: str, mid_text: str,
                            emphasis: list[str], api_key: str | None) -> dict:
    """Shorten only what exceeds the word budget; one Gemini call for all."""
    need = {"hook": len(hook_text.split()) > TRUNCATE_WORDS}
    mid_short = ""
    if mid_text:
        need["mid"] = len(mid_text.split()) > TRUNCATE_WORDS
    result = {"hook": hook_text.upper(), "mid": mid_text.upper(),
              "hook_source": "full", "mid_source": "full"}
    if not any(need.values()):
        return result
    try:
        ask = {k: (hook_text if k == "hook" else mid_text)
               for k, wanted in need.items() if wanted}
        got = gemini_shorten(ask, api_key)
    except Exception as exc:
        print(f"      shorten: Gemini unavailable -> local heuristic "
              f"({str(exc)[:70]})")
        if need.get("hook"):
            result["hook"] = _local_shorten(hook_text, emphasis)
            result["hook_source"] = "local_shorten"
        if need.get("mid"):
            result["mid"] = _local_shorten(mid_text)
            result["mid_source"] = "local_shorten"
        return result
    if need.get("hook"):
        result["hook"] = got["hook"]
        result["hook_source"] = "gemini"
    if need.get("mid"):
        result["mid"] = got["mid"]
        result["mid_source"] = "gemini"
    return result


# ---------------------------------------------------------------------------
# Context from the beats JSON (step 1 output - no extra generation call)
# ---------------------------------------------------------------------------

def strongest_mid_beat(beats: list[dict]) -> dict | None:
    """A strong mid-video emotional beat: most emphasis words, longer wins."""
    mids = [b for b in beats[1:-1] if isinstance(b, dict) and b.get("text")]
    if not mids:
        mids = beats[1:] or beats
    if not mids:
        return None
    return max(mids, key=lambda b: (len(b.get("emphasis_words") or []),
                                    len(str(b.get("text", "")))))


def load_context(run_folder: Path) -> dict:
    """Everything the prompts need, from word_timings.json (beats or classic)."""
    timings_file = run_folder / "word_timings.json"
    if not timings_file.is_file():
        raise RuntimeError("word_timings.json missing (old run?)")
    timings = json.loads(timings_file.read_text(encoding="utf-8"))
    beats = [b for b in (timings.get("beats") or [])
             if isinstance(b, dict) and b.get("text")]
    script = str(timings.get("script", "") or "")
    title = str(timings.get("title", "") or timings.get("topic", "") or "")
    ctx = {"title": title.replace("_", " ").replace("-", " ").strip(),
           "topic": str(timings.get("topic", "") or "").replace("_", " ").strip(),
           "mode": "beats" if beats else "classic",
           "beats": beats, "script": script}

    if beats:
        ctx["hook_text"] = str(beats[0]["text"]).strip()
        ctx["hook_emotion"] = str(beats[0].get("emotion", "") or "").lower()
        ctx["hook_concept"] = str(beats[0].get("visual_concept", "") or "")
        mid = strongest_mid_beat(beats)
        ctx["mid_text"] = str(mid["text"]).strip() if mid else ""
        ctx["mid_emotion"] = str(mid.get("emotion", "") or "").lower() if mid else ""
        ctx["mid_concept"] = str(mid.get("visual_concept", "") or "") if mid else ""
        ctx["mid_index"] = beats.index(mid) + 1 if mid in beats else None
        ctx["emphasis"] = list(beats[0].get("emphasis_words") or [])
    else:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", script)
                     if s.strip()]
        ctx["hook_text"] = sentences[0] if sentences else script[:80]
        ctx["hook_emotion"] = ""
        ctx["hook_concept"] = ""
        ctx["mid_text"] = sentences[len(sentences) // 2] \
            if len(sentences) > 2 else ""
        ctx["mid_emotion"] = ""
        ctx["mid_concept"] = ""
        ctx["mid_index"] = None
        ctx["emphasis"] = []
    return ctx


# ---------------------------------------------------------------------------
# Prompt builders - the creative core
# ---------------------------------------------------------------------------

def _mood(emotion: str) -> str:
    return EMOTION_MOODS.get((emotion or "").strip().lower(), MOOD_DEFAULT)


def _text_directive(caption: str, color: str, outline: str) -> str:
    return (
        f'THE ONLY TEXT in the entire image is this exact headline: "{caption}"\n'
        f"Render it character-for-character, ALL CAPITALS, in an ultra-bold "
        f"condensed sans-serif display font, bright {color} fill with a thick "
        f"{outline} outline and a slight drop shadow. The headline block spans "
        "about 60-70% of the frame width and stays perfectly legible even when "
        "the image is shrunk to a tiny phone-screen size. No other words, "
        "letters, numbers, captions or typography anywhere in the image."
    )


def build_prompt(variant: str, caption: str, ctx: dict, wide: bool) -> str:
    """Creative brief for one variant in one aspect ratio."""
    aspect = ("horizontal 16:9 widescreen composition"
              if wide else "vertical 9:16 portrait composition")
    bottom_note = ("Keep the bottom quarter of the frame visually simple and "
                   "dark - platform UI (duration, progress bar, title) is "
                   "drawn over it on covers."
                   if not wide else
                   "Keep the very bottom edge simple - a progress bar is "
                   "drawn over it in some YouTube views.")
    title_ctx = (f'The video is about "{ctx["title"]}". '
                 if ctx["title"] else "")

    if variant == "hook":
        scene = (f'Concept to depict: {ctx["hook_concept"]}. '
                 if ctx["hook_concept"] else "")
        mood = _mood(ctx.get("hook_emotion", ""))
        return (
            f"You are designing a scroll-stopping YouTube thumbnail. "
            f"{title_ctx}{aspect}. {STYLE_BASE} {scene}Mood: {mood}. "
            f"{_text_directive(caption, 'yellow (#F5D90A)', 'black')} "
            f"Position the headline in the upper-middle area of the frame. "
            f"{bottom_note}"
        )
    if variant == "midpoint":
        scene = (f'Concept to depict: {ctx["mid_concept"]}. '
                 if ctx["mid_concept"] else "")
        mood = _mood(ctx.get("mid_emotion", ""))
        return (
            f"You are designing a cinematic YouTube thumbnail. "
            f"{title_ctx}{aspect}. {STYLE_BASE} {scene}Mood: {mood}. "
            f"{_text_directive(caption, 'white (#FFFFFF)', 'black')} "
            f"Position the headline in the upper-middle area of the frame. "
            f"{bottom_note}"
        )
    # clean - no text at all
    hook_concept = ctx.get("hook_concept", "") or ""
    concept_note = f" - {hook_concept}" if hook_concept else ""
    return (
        f"You are designing a wordless cinematic YouTube thumbnail that "
        f"intrigues in one glance. {title_ctx}{aspect}. {STYLE_BASE} "
        f"Depict the single most emotionally striking image suggested by the "
        f"video theme{concept_note}. "
        f"Mood: {_mood(ctx.get('hook_emotion', ''))}. "
        "ABSOLUTELY NO TEXT of any kind - no words, letters, numbers, signs "
        "or typography anywhere. One strong focal subject, generous negative "
        f"space, instantly readable silhouette. {bottom_note}"
    )


# ---------------------------------------------------------------------------
# Gemini image generation + verification
# ---------------------------------------------------------------------------

def _client(api_key: str):
    from google import genai
    from google.genai import types
    return genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=180000))


def _extract_image(resp) -> tuple[bytes, str] | None:
    for cand in (getattr(resp, "candidates", None) or []):
        for part in (getattr(cand.content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                return bytes(inline.data), (inline.mime_type or "image/png")
    return None


def gemini_image(prompt: str, aspect: str, api_key: str) -> tuple[bytes, str, str]:
    """Generate one image; returns (bytes, mime, model_used). Raises on failure.

    Tries the model chain with native aspect-ratio image_config first; if the
    API rejects image_config, retries without it (the postprocess cover-crop
    then fixes the aspect)."""
    client = _client(api_key)
    chain = list(dict.fromkeys(
        ([_IMAGE_ENV] if _IMAGE_ENV else [])
        + discover_image_models(api_key) + _IMAGE_FALLBACKS))
    last_exc: Exception | None = None
    for model in chain:
        skip_cfg = False
        for attempt in (1, 2):
            cfg_options = (False,) if skip_cfg else (True, False)
            for use_cfg in cfg_options:
                try:
                    kwargs: dict = {"response_modalities": ["TEXT", "IMAGE"]}
                    if use_cfg:
                        from google.genai import types
                        kwargs["image_config"] = types.ImageConfig(
                            aspect_ratio=aspect)
                    resp = client.models.generate_content(
                        model=model, contents=[prompt],
                        config=types.GenerateContentConfig(
                            temperature=0.85, **kwargs))
                    got = _extract_image(resp)
                    if got:
                        print(f"      image ok ({model}, aspect={aspect}"
                              f"{' native' if use_cfg else ' post-crop'})")
                        return got[0], got[1], model
                    last_exc = RuntimeError("response contained no image")
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if use_cfg and ("image_config" in msg or "aspect" in msg
                                    or "unknown name" in msg
                                    or "invalid json payload" in msg):
                        skip_cfg = True   # model/API lacks image_config
                    time.sleep(2 if attempt == 1 else 5)
        print(f"      image gen: {model} failed ({str(last_exc)[:70]})")
    raise RuntimeError(f"gemini image failed ({str(last_exc)[:90]})")


def list_models(api_key: str) -> None:
    """CI probe: print which image + text models THIS key can actually use."""
    client = _client(api_key)
    images, texts = [], []
    for m in client.models.list():
        name = (getattr(m, "name", "") or "").replace("models/", "")
        actions = [getattr(a, "name", "") for a in
                   (getattr(m, "supported_actions", None) or [])]
        if not any(":generateContent" in a for a in actions):
            continue
        (images if "image" in name else texts).append(name)
    print("=" * 60)
    print(f"  IMAGE models ({len(images)}):")
    for n in images:
        print(f"    {n}")
    print(f"  TEXT models ({len(texts)}) [first 15]:")
    for n in texts[:15]:
        print(f"    {n}")
    print("=" * 60)


def verify_text(image_bytes: bytes, mime: str, expected: str,
                api_key: str) -> bool | None:
    """Gemini vision read-back: True/False; None = check unavailable."""
    if not (expected or "").strip():
        return None
    try:
        from google.genai import types
        client = _client(api_key)
        ask = ("Transcribe EXACTLY all text visible in this image. "
               "Reply with the transcribed characters only, nothing else.")
        last_exc: Exception | None = None
        for model in VERIFY_MODELS:
            for attempt in (1, 2):
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=[types.Part.from_bytes(data=image_bytes,
                                                        mime_type=mime), ask])
                    got = re.sub(r"[^A-Z0-9]+", "",
                                 (resp.text or "").upper())
                    want = re.sub(r"[^A-Z0-9]+", "", expected.upper())
                    return bool(want) and want in got
                except Exception as exc:
                    last_exc = exc
                    if attempt == 1:
                        time.sleep(1.5)
        print(f"      verify: all models failed ({str(last_exc)[:60]})")
        return None
    except Exception as exc:
        print(f"      verify: unavailable ({str(exc)[:60]})")
        return None


# ---------------------------------------------------------------------------
# Postprocess: exact pixel sizes, crop-never-stretch, API size limits
# ---------------------------------------------------------------------------

def save_image(img_bytes: bytes, out_path: Path, target: tuple[int, int]) -> Path:
    """Center-cover crop to the exact target size (crop, never stretch)."""
    from PIL import Image
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    tw, th = target
    scale = max(tw / img.width, th / img.height)
    new_w, new_h = round(img.width * scale), round(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    x0 = (new_w - tw) // 2
    y0 = (new_h - th) // 2
    img = img.crop((x0, y0, x0 + tw, y0 + th))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() == ".jpg":
        quality = 92
        while quality >= 70:
            img.save(out_path, "JPEG", quality=quality, optimize=True)
            if out_path.stat().st_size <= 2 * 1024 * 1024:  # API limit 2 MB
                break
            quality -= 6
    else:
        img.save(out_path, "PNG")
    return out_path


# ---------------------------------------------------------------------------
# Variant building
# ---------------------------------------------------------------------------

def _fake_image(caption: str, wide: bool) -> tuple[bytes, str]:
    """Debug-only local placeholder (no API): dark gradient + caption text.

    Lets the whole save / preview / manifest path be exercised on machines
    where the Gemini API is geo-blocked. Never used unless --fake is passed.
    """
    from PIL import Image, ImageDraw, ImageFont
    w, h = YT_SIZE if wide else IG_SIZE
    top, bottom = (16, 20, 46), (4, 5, 12)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        f = y / max(1, h - 1)
        px_row = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3))
        for x in range(w):
            px[x, y] = px_row
    draw = ImageDraw.Draw(img)
    # symbolic "focal subject": a dim violet orb with rim light
    orb_r = int(min(w, h) * 0.16)
    cx, cy = int(w * 0.5), int(h * (0.68 if wide else 0.62))
    draw.ellipse([cx - orb_r, cy - orb_r, cx + orb_r, cy + orb_r],
                 fill=(52, 34, 84), outline=(120, 90, 200), width=max(2, w // 200))
    if caption:
        font_path = ROOT / "fonts" / "Anton-Regular.ttf"
        font = ImageFont.truetype(str(font_path), int(w * 0.075)) \
            if font_path.is_file() else ImageFont.load_default()
        stroke = max(3, int(w * 0.008))
        bbox = draw.textbbox((0, 0), caption, font=font, stroke_width=stroke)
        tw_, th_ = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = (w - tw_) // 2
        ty = int(h * 0.12)
        draw.text((tx, ty), caption, font=font, fill=(245, 217, 10),
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
    import io as _io
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), "image/png"


def build_variant(name: str, caption: str, ctx: dict, run_folder: Path,
                  api_key: str | None, fake: bool = False) -> dict | None:
    """One variant -> native 16:9 + native 9:16, text-verified, saved."""
    info: dict = {"generator": "fake-local (debug)" if fake else "gemini-image",
                  "frame_timestamp": None,          # no video frames anymore
                  "caption_used": caption or "",
                  "original_text": (ctx.get("hook_text") if name == "hook"
                                    else ctx.get("mid_text", "")) if name != "clean" else "",
                  "emotion": (ctx.get("hook_emotion") if name == "hook"
                              else ctx.get("mid_emotion", "")) if name != "clean" else "",
                  "beat_index": 0 if name == "hook" else ctx.get("mid_index"),
                  "files": {}}
    if name == "clean":
        info.update({"original_text": "", "emotion": ctx.get("hook_emotion", ""),
                     "beat_index": None, "caption_used": ""})
    elif name == "hook":
        info["caption_source"] = None          # filled by caller
    model_used = None
    for wide in (True, False):
        tag = "youtube" if wide else "instagram"
        aspect = "16:9" if wide else "9:16"
        target = YT_SIZE if wide else IG_SIZE
        ext = "jpg" if wide else "png"
        prompt = build_prompt(name, caption, ctx, wide)
        if fake:
            img, mime = _fake_image(caption, wide)
            used = "fake-local (debug)"
        else:
            img, mime, used = gemini_image(prompt, aspect, api_key)
        model_used = used
        out = save_image(img, run_folder / "thumbnails" / f"{name}_{tag}.{ext}",
                         target)
        info["files"][tag] = str(out.relative_to(run_folder))
        kb = out.stat().st_size / 1024
        print(f"      {name}/{tag}: {out.name} ({kb:.0f} KB)"
              f"{' [FAKE]' if fake else ''}")

        if caption and not fake:
            verdict = verify_text(img, mime, caption, api_key)
            info["text_verified"] = bool(verdict)
            if verdict is False:
                print(f"      {name}/{tag}: text mismatch - regenerating once")
                img, mime, used = gemini_image(
                    prompt + "\nIMPORTANT: spell the headline EXACTLY as "
                    "given, letter by letter, no substitutions.",
                    aspect, api_key)
                out = save_image(img, out, target)
                verdict2 = verify_text(img, mime, caption, api_key)
                info["text_verified"] = bool(verdict2) if verdict2 is not None \
                    else False
    info["model"] = model_used or ""
    info["prompt_style"] = name
    return info


def find_run(output_dir: str, run_id: str | None) -> Path:
    base = Path(output_dir)
    if run_id:
        folder = base / run_id
        if not folder.is_dir():
            sys.exit(f"ERROR: run folder not found: {folder}")
        return folder
    candidates = [d for d in base.iterdir()
                  if d.is_dir() and (d / "final_short.mp4").is_file()]
    if not candidates:
        sys.exit("ERROR: no run folder with final_short.mp4 (render first)")
    return max(candidates, key=lambda d: d.stat().st_mtime)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate fully-Gemini thumbnail variants for the newest "
                    "(or given) run")
    parser.add_argument("--list-models", action="store_true",
                        help="probe: print which models this API key can use "
                             "(image + text) and exit")
    parser.add_argument("--fake", action="store_true",
                        help="debug: build local placeholder images instead of "
                             "calling Gemini (tests the save/manifest path)")
    parser.add_argument("--run-id", help="run folder under --output-dir")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--video", help="explicit final_short.mp4 (run = parent)")
    parser.add_argument("--variant", choices=VARIANTS_ALL, default=None,
                        help="which variant becomes the upload default "
                             "(default: THUMBNAIL_VARIANT env, else hook)")
    parser.add_argument("--variants", default=None,
                        help="comma list of variants to build "
                             "(default: THUMB_VARIANTS env, else all three)")
    parser.add_argument("--auto-upload", choices=("true", "false"), default=None,
                        help="false = review only, upload step will not attach "
                             "(default: AUTO_UPLOAD_THUMBNAIL env, else true)")
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on failure (CI default is soft)")
    args = parser.parse_args()

    load_environment(None)
    api_key = (os.getenv("GEMINI_API_KEY", "") or "").strip() or None

    # --list-models must work WITHOUT a run folder -> handle before find_run
    if args.list_models:
        if not api_key:
            print("ERROR: GEMINI_API_KEY not set - cannot probe models")
            return 1
        try:
            list_models(api_key)
        except Exception as exc:
            print(f"model probe failed: {str(exc)[:300]}")
            return 1
        return 0

    run_folder = Path(args.video).parent if args.video \
        else find_run(args.output_dir, args.run_id)
    variant_pref = (args.variant or os.getenv("THUMBNAIL_VARIANT", "")
                    or DEFAULT_VARIANT).strip().lower()
    if variant_pref not in VARIANTS_ALL:
        variant_pref = DEFAULT_VARIANT
    wanted = [v.strip().lower() for v in
              (args.variants or os.getenv("THUMB_VARIANTS", "")
               or ",".join(VARIANTS_ALL)).split(",") if v.strip() in VARIANTS_ALL]
    if variant_pref not in wanted:
        wanted.insert(0, variant_pref)
    auto_upload = (args.auto_upload or
                   os.getenv("AUTO_UPLOAD_THUMBNAIL", "") or "true").lower() \
        not in ("0", "false", "off", "no")

    print("=" * 60)
    print("  STEP 2.6 - THUMBNAILS (fully Gemini-generated)")
    print(f"  run: {run_folder.name} | default variant: {variant_pref} "
          f"| build: {','.join(wanted)} | auto-attach: {auto_upload}")
    print("=" * 60)
    if not api_key:
        print("WARNING: GEMINI_API_KEY not set - cannot generate thumbnails")
        return 1 if args.strict else 0

    t0 = time.time()
    try:
        ctx = load_context(run_folder)
    except Exception as exc:
        print(f"WARNING: thumbnail context missing - pipeline continues "
              f"without thumbnails ({str(exc)[:120]})")
        return 1 if args.strict else 0

    print(f"      context: mode={ctx['mode']} title={ctx['title']!r} "
          f"hook={ctx['hook_text'][:48]!r}")

    # captions: shorten what exceeds the word budget (one cheap call)
    texts = resolve_thumbnail_texts(ctx["hook_text"], ctx.get("mid_text", ""),
                                    ctx["emphasis"], api_key)

    variants: dict[str, dict] = {}
    for name in wanted:
        caption = "" if name == "clean" else \
            (texts["hook"] if name == "hook" else texts["mid"])
        if name != "clean" and not caption:
            print(f"      variant '{name}': no caption text - skipped")
            continue
        print(f"   building variant '{name}'"
              f"{' (no text)' if name == 'clean' else f': {caption}'}")
        try:
            info = build_variant(name, caption, ctx, run_folder, api_key,
                                 fake=args.fake)
            if name == "hook":
                info["caption_source"] = texts["hook_source"]
            elif name == "midpoint":
                info["caption_source"] = texts["mid_source"]
            variants[name] = info
        except Exception as exc:
            print(f"      variant '{name}' failed ({str(exc)[:90]})")

    if not variants:
        print("WARNING: no thumbnail variant could be generated - pipeline "
              "continues without thumbnails")
        return 1 if args.strict else 0

    # selection: preferred variant first, then hook > midpoint > clean
    order = [variant_pref] + [v for v in VARIANTS_ALL if v != variant_pref]
    chosen = next((v for v in order if variants.get(v)), None)
    selected = None
    if chosen:
        files = variants[chosen]["files"]
        selected = {
            "variant": chosen,
            "youtube": files.get("youtube"),
            "instagram": files.get("instagram"),
            "attach": auto_upload,
        }
        # tiny mobile-feed legibility proof (what it looks like scrolled past)
        try:
            from PIL import Image
            if files.get("instagram"):
                img = Image.open(run_folder / files["instagram"])
                img.resize((270, 480), Image.LANCZOS).save(
                    run_folder / "thumbnails" / "preview_mobile_selected.png")
            if files.get("youtube"):
                img_yt = Image.open(run_folder / files["youtube"])
                img_yt.resize((320, 180), Image.LANCZOS).save(
                    run_folder / "thumbnails" / "preview_mobile_youtube.png")
        except Exception:
            pass

    manifest = {
        "run_id": run_folder.name,
        "video": "final_short.mp4",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "gemini-image (no video frames)",
        "image_model": variants[chosen].get("model", "") if chosen else "",
        "mode": ctx["mode"],
        "config": {"thumbnail_variant": variant_pref,
                   "auto_upload_thumbnail": auto_upload,
                   "variants_built": wanted,
                   "image_model": _IMAGE_ENV or _IMAGE_FALLBACKS[0]},
        "variants": variants,
        "selected": selected,
    }
    manifest_path = run_folder / "thumbnails" / "thumbnail_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    print()
    print(f"  thumbnails ready in {time.time() - t0:.1f}s -> {manifest_path.parent}")
    for name, info in variants.items():
        mark = " <-- default for upload" if chosen and name == chosen else ""
        verified = info.get("text_verified")
        if not info.get("caption_used"):
            v_note = "no text"
        elif verified is True:
            v_note = "text OK"
        elif verified is False:
            v_note = "TEXT UNVERIFIED"
        else:
            v_note = "unverified"
        print(f"    {name:9s} {v_note:<16s} "
              f"caption=\"{info.get('caption_used', '')[:34]}\"{mark}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
