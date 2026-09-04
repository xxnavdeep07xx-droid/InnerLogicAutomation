#!/usr/bin/env python3
"""
step_thumbnail.py - creative AI-generated thumbnails, 100% FREE (v3).

Every thumbnail is painted by an AI image model - no video frames are
extracted. v3 swaps the Gemini image endpoint (paid-only: the key's free
tier allows ZERO image requests, proven by CI probes) for POLLINATIONS.AI,
a public Flux endpoint that needs NO API key, NO account, NO billing:

    https://image.pollinations.ai/prompt/<prompt>?width=..&height=..
    (model=flux, free anonymous tier, ~5-15 s per image)

Provider chain (THUMB_IMAGE_PROVIDER, default "auto"):
    auto          Pollinations flux; local brand artwork if it fails
    pollinations  Pollinations only (errors are not swallowed)
    gemini        the old paid Gemini path (if billing is ever enabled)
    local         always local brand artwork

Headline text on the free path: artwork is generated TEXT-FREE, then the
caption is typeset locally in the channel's Anton font (yellow #F5D90A or
white, black stroke + drop shadow, soft scrim, auto-fit <=3 lines, 84%
width cap) - deterministic spelling, guaranteed mobile-size legibility,
no vision verification loop needed. The gemini provider still paints and
verifies text itself as in v2.

Variants (all saved for review, one becomes the upload default):
    hook      bold typographic poster - huge yellow all-caps hook line on a
              dark symbolic scene matching beat[0]
    midpoint  dramatic scene from the strongest mid-video beat + short caption
    clean     text-free atmospheric artwork (pure mood, maximum intrigue)

Both resolutions are generated NATIVELY in their own aspect ratio - never a
stretch of one master image:
    YouTube   1280x720  (16:9)  -> thumbnails.set right after videos().insert
    Instagram 1080x1920 (9:16)  -> clip_upload(thumbnail=...) cover frame
(Pollinations generates at the exact requested size; a center-cover crop
fixes any drift - a crop, never a distortion - and the YouTube JPG is
squeezed under 2 MB.)

Quality gate: on the free path spelling is deterministic (local typeset).
On the gemini path every text-bearing image is read back by a cheap Gemini
vision call and regenerated once on mismatch (v2 behaviour).

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
    THUMB_IMAGE_PROVIDER    auto | pollinations | gemini | local (default auto)
    THUMB_IMAGE_MODEL       pollinations model (default flux, then turbo)
    GEMINI_IMAGE_MODEL      image model for the gemini provider
    GEMINI_THUMB_MODEL      caption-shorten / vision-verify model

COST: $0 - the default provider needs no key, no account and no quota.

This step is best-effort by design: any failure prints a WARNING and exits 0
(unless --strict) so a thumbnail problem can never block publishing.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote
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

# --- free image provider (v3): Pollinations.ai, no key, no billing --------
POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
POLLINATIONS_MODELS = ["flux", "turbo"]
PROVIDER = (os.getenv("THUMB_IMAGE_PROVIDER", "") or "auto").strip().lower()
if PROVIDER not in ("auto", "pollinations", "gemini", "local"):
    PROVIDER = "auto"
POLLINATIONS_MODEL_ENV = (os.getenv("THUMB_IMAGE_MODEL", "") or "").strip()
ACCENT_YELLOW = (245, 217, 10)          # #F5D90A - channel hook accent
ACCENT_WHITE = (255, 255, 255)

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
# Imagen uses the separate :predict endpoint with its own quota buckets -
# sometimes available when the generateContent image models are not.
_IMAGEN_FALLBACKS = [
    "imagen-4.0-fast-generate-001",
    "imagen-4.0-generate-001",
    "imagen-3.0-fast-generate-001",
    "imagen-3.0-generate-002",
]
_TEXT_FIRST = ((os.getenv("GEMINI_THUMB_MODEL", "") or "").strip()
               or (_resolve_models() or ["gemini-flash-latest"])[0])
VERIFY_MODELS = list(dict.fromkeys(
    [_TEXT_FIRST] + list(_resolve_models())))

_discovered_images: list[str] = []          # populated once per process


def _supports_generate_content(m) -> bool:
    """SDK 2.x: supported_actions is Optional[list[str]] (plain strings).

    Some key types return None/empty here - assume usable and let the call
    itself be the verdict."""
    actions = getattr(m, "supported_actions", None) or []
    if not actions:
        return True
    for a in actions:
        text = a if isinstance(a, str) else (getattr(a, "name", "") or "")
        if ":generateContent" in text or text == "generateContent":
            return True
    return False


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
            if "image" not in name or "embedding" in name or name.startswith("imagen"):
                continue
            if not _supports_generate_content(m):
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
                score -= 2                 # GA release beats same-name preview
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


def build_prompt(variant: str, caption: str, ctx: dict, wide: bool,
                 paint_text: bool = True) -> str:
    """Creative brief for one variant in one aspect ratio.

    paint_text=True  (gemini provider) - the model paints the headline.
    paint_text=False (free path) - strictly text-free artwork with clean
    negative space up top; the caption is typeset locally afterwards."""
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
    text_free_note = (
        "ABSOLUTELY NO TEXT of any kind - no words, letters, numbers, "
        "signs or typography anywhere in the image. Keep the upper-middle "
        "area of the composition darker and visually simple - a bold title "
        "will be added there later - with the main subject in the lower "
        "two-thirds of the frame. ")

    if variant == "hook":
        scene = (f'Concept to depict: {ctx["hook_concept"]}. '
                 if ctx["hook_concept"] else "")
        mood = _mood(ctx.get("hook_emotion", ""))
        if not paint_text:
            return (
                f"You are designing a scroll-stopping YouTube thumbnail "
                f"artwork. {title_ctx}{aspect}. {STYLE_BASE} {scene}"
                f"Mood: {mood}. {text_free_note}{bottom_note}")
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
        if not paint_text:
            return (
                f"You are designing a cinematic YouTube thumbnail artwork. "
                f"{title_ctx}{aspect}. {STYLE_BASE} {scene}Mood: {mood}. "
                f"{text_free_note}{bottom_note}")
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


def imagen_image(prompt: str, aspect: str, api_key: str) -> tuple[bytes, str, str]:
    """Imagen fallback via REST :predict (own quota bucket).

    Returns (bytes, mime, model_used). Raises on total failure."""
    import requests as _rq
    last = ""
    for model in _IMAGEN_FALLBACKS:
        for attempt in (1, 2):
            try:
                r = _rq.post(
                    f"https://generativelanguage.googleapis.com/v1beta/"
                    f"models/{model}:predict",
                    headers={"x-goog-api-key": api_key},
                    json={"instances": [{"prompt": prompt}],
                          "parameters": {"sampleCount": 1,
                                         "aspectRatio": aspect}},
                    timeout=180)
                if r.status_code == 200:
                    preds = r.json().get("predictions", [])
                    b64 = (preds[0].get("bytesBase64Encoded") if preds
                           else None)
                    if b64:
                        import base64
                        img = base64.b64decode(b64)
                        print(f"      image ok (imagen fallback: {model})")
                        return img, "image/png", model
                    last = RuntimeError(f"{model}: no predictions")
                else:
                    last = RuntimeError(f"{model}: HTTP {r.status_code} "
                                        f"{r.text[:120]}")
                    if r.status_code == 404:
                        break                  # model does not exist at all
                    time.sleep(2)
            except Exception as exc:
                last = exc
                time.sleep(2)
        # only keep looping while the error says quota/availability, not 404
        if "HTTP 404" in str(last):
            continue
        if last and "RESOURCE_EXHAUSTED" in str(last):
            continue
    raise RuntimeError(f"imagen fallback failed ({str(last)[:90]})")


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
    # last resort: Imagen :predict (separate quota bucket)
    return imagen_image(prompt, aspect, api_key)


def list_models(api_key: str) -> None:
    """CI probe: what can THIS key actually use right now?

    1. raw models.list count + image/text breakdown
    2. a REAL tiny image generation per candidate model (ground truth,
       because models.list is unreliable for some key types)
    """
    from google.genai import types
    client = _client(api_key)
    raw, images, texts = [], [], []
    try:
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            raw.append(name)
            if not _supports_generate_content(m):
                continue
            (images if "image" in name else texts).append(name)
    except Exception as exc:
        print(f"  models.list failed: {str(exc)[:200]}")
    print("=" * 60)
    print(f"  models.list returned {len(raw)} models; "
          f"image-capable: {len(images)}, text: {len(texts)}")
    for n in images[:10]:
        print(f"    [img ] {n}")
    for n in texts[:10]:
        print(f"    [text] {n}")

    print("  --- live generation test per candidate image model ---")
    candidates = list(dict.fromkeys(
        ([_IMAGE_ENV] if _IMAGE_ENV else []) + _IMAGE_FALLBACKS + images))
    for model in candidates:
        verdict = _quick_image_test(client, model)
        print(f"    {model:38s} -> {verdict}")

    print("  --- imagen :predict candidates (separate quota bucket) ---")
    imagen_names = [n for n in raw if n.startswith("imagen")]
    for model in list(dict.fromkeys(imagen_names + _IMAGEN_FALLBACKS)):
        verdict = _quick_imagen_test(api_key, model)
        print(f"    {model:38s} -> {verdict}")
    print("=" * 60)


def _quick_imagen_test(api_key: str, model: str) -> str:
    """Tiny :predict generation - Imagen models have their own quota."""
    import requests as _rq
    try:
        r = _rq.post(
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/{model}:predict",
            headers={"x-goog-api-key": api_key},
            json={"instances": [{"prompt": "A tiny plain blue circle on a "
                                           "plain white background."}],
                  "parameters": {"sampleCount": 1}},
            timeout=120)
        if r.status_code == 200:
            preds = r.json().get("predictions", [])
            if preds and preds[0].get("bytesBase64Encoded"):
                kb = len(preds[0]["bytesBase64Encoded"]) * 3 // 4 // 1024
                return f"WORKS ({kb} KB image)"
            return "no predictions"
        detail = str(r.text)
        limits = re.findall(r"limit: (\d+)", detail)
        extra = f" | quota_limit={limits[:2]}" if limits else ""
        return f"HTTP {r.status_code} ({detail[:60]}){extra}"
    except Exception as exc:
        return f"fails ({str(exc)[:80]})"


def _quick_image_test(client, model: str) -> str:
    """One tiny real generation - the only 100% reliable capability check."""
    from google.genai import types
    try:
        resp = client.models.generate_content(
            model=model,
            contents=["A tiny plain blue circle on a plain white background."],
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"]))
        got = _extract_image(resp)
        if got:
            return f"WORKS ({len(got[0]) // 1024} KB image)"
        return "no image part in response"
    except Exception as exc:
        detail = str(exc)
        # surface the quota verdict: metric name + limit value, e.g.
        # "...free_tier_requests, limit: 0, model: gemini-..."
        limits = re.findall(r"(\S*quota\S*|free_tier_\w+)[^;]*?limit: (\d+)",
                            detail) or \
            re.findall(r"limit: (\d+)", detail)
        retry = re.search(r"retry in ([\d.]+)\w*", detail)
        extra = f" | quota={limits[:3]}" if limits else ""
        if retry:
            extra += f" | retry_in={retry.group(1)}"
        return f"fails ({detail[:70]}){extra}"


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
# FREE image provider: Pollinations.ai (public Flux, no key, $0)
# ---------------------------------------------------------------------------

def seed_for(run_name: str, variant: str, tag: str) -> int:
    """Deterministic per (run, variant, aspect) - re-runs reproduce artwork.

    Masked to 31 bits: pollinations rejects seeds above the int32 max with
    an opaque HTTP 500 (cost a debugging session to find)."""
    digest = hashlib.sha256(f"{run_name}:{variant}:{tag}".encode()).hexdigest()
    return int(digest[:8], 16) & 0x7FFFFFFF


def pollinations_image(prompt: str, wide: bool,
                       seed: int) -> tuple[bytes, str, str]:
    """One image from the free Pollinations flux endpoint.

    Returns (bytes, mime, model_used). Raises on total failure.
    Anonymous tier: no key needed; occasional 429/500 under load ->
    seed-jittered retries inside a per-image time budget. The service
    honors the requested ASPECT but not exact pixels (flux returns
    1024x576 / 576x1024) - the postprocess cover-crop + LANCZOS upscale
    handles that, so any image with a >=500px short side is accepted."""
    import requests as _rq
    from PIL import Image as _PILImage
    w, h = YT_SIZE if wide else IG_SIZE
    models = [POLLINATIONS_MODEL_ENV] if POLLINATIONS_MODEL_ENV \
        else list(POLLINATIONS_MODELS)
    url_prompt = quote(prompt, safe="")
    last = ""
    for model in models:
        # per-model budget with a guaranteed first attempt - a slow/500ing
        # primary model must never eat the fallback model's shot entirely
        model_deadline = time.time() + 150
        attempt = 0
        while True:
            attempt += 1
            if attempt > 1 and time.time() > model_deadline:
                break
            try:
                url = (f"{POLLINATIONS_URL.format(prompt=url_prompt)}"
                       f"?width={w}&height={h}&model={model}"
                       f"&seed={seed + attempt * 9973}&nologo=true"
                       f"&private=true")
                r = _rq.get(url, timeout=90, headers={
                    "User-Agent": "InnerLogic-Pipeline/1.0"})
                ctype = r.headers.get("content-type", "")
                if (r.status_code == 200 and ctype.startswith("image")
                        and len(r.content) > 20_000):
                    _PILImage.open(io.BytesIO(r.content)).verify()  # not HTML
                    real = _PILImage.open(io.BytesIO(r.content))
                    if min(real.width, real.height) < 500:
                        last = f"{model}: too small " \
                               f"({real.width}x{real.height})"
                        continue
                    print(f"      image ok (pollinations:{model}, "
                          f"{real.width}x{real.height}, "
                          f"{len(r.content) // 1024} KB)")
                    return r.content, ctype.split(";")[0], \
                        f"pollinations:{model}"
                last = f"{model}: HTTP {r.status_code} {ctype[:30]} " \
                       f"{len(r.content)}B"
            except Exception as exc:
                last = f"{model}: {str(exc)[:80]}"
            if attempt >= 6:
                break
            time.sleep(min(8, 2 + 2 * attempt))  # backoff: queue / rate limit
        print(f"      pollinations model '{model}' exhausted")
    raise RuntimeError(f"pollinations failed ({last})")


def overlay_headline(path: Path, caption: str, wide: bool,
                     accent: tuple) -> None:
    """Typeset the caption onto the finished image (in place).

    Anton ALL CAPS, <=3 lines, <=84% width with auto-shrink, centered in the
    upper-middle safe area, soft dark scrim behind the block + black stroke
    and drop shadow. Deterministic spelling -> always legible at feed size."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    img = Image.open(path).convert("RGB")
    w, h = img.size
    font_path = ROOT / "fonts" / "Anton-Regular.ttf"
    if not font_path.is_file():
        print("      overlay: Anton font missing - text skipped")
        return
    max_w = int(w * 0.84)
    draw0 = ImageDraw.Draw(img)

    # 1) fit: shrink the font until the wrapped block fits the box
    size, lines, font = int(w * 0.105), [], None
    while size >= int(w * 0.045):
        font = ImageFont.truetype(str(font_path), size)
        lines, cur = [], ""
        for word in caption.split():
            trial = f"{cur} {word}".strip()
            bbox = draw0.textbbox((0, 0), trial, font=font)
            if bbox[2] - bbox[0] <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        too_wide = any(draw0.textbbox((0, 0), ln, font=font)[2] > max_w
                       for ln in lines)
        if len(lines) <= 3 and not too_wide:
            break
        size -= max(2, size // 14)
    if not (lines and font):
        return
    line_h = size + int(size * 0.18)
    block_h = line_h * len(lines)

    # 2) soft dark scrim behind the block (legibility on any artwork)
    cx, cy = w // 2, int(h * (0.36 if wide else 0.34))
    scrim = Image.new("L", (w, h), 0)
    ImageDraw.Draw(scrim).rounded_rectangle(
        [cx - max_w // 2 - size // 2, cy - block_h // 2 - size // 2,
         cx + max_w // 2 + size // 2, cy + block_h // 2 + size // 2],
        radius=size, fill=170)
    scrim = scrim.filter(ImageFilter.GaussianBlur(size * 0.6))
    dark = Image.new("RGB", (w, h), (2, 3, 8))
    img = Image.composite(dark, img, scrim.point(lambda v: v * 60 // 100))

    # 3) drop shadow pass + yellow/white fill with black stroke
    draw = ImageDraw.Draw(img)
    stroke = max(3, int(size * 0.075))
    y = cy - block_h // 2
    for ln in lines:
        bbox = draw.textbbox((0, 0), ln, font=font, stroke_width=stroke)
        lw = bbox[2] - bbox[0]
        x, ty = cx - lw // 2 - bbox[0], y - bbox[1]
        off = max(2, size // 22)
        draw.text((x + off, ty + off), ln, font=font, fill=(0, 0, 0),
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
        draw.text((x, ty), ln, font=font, fill=accent,
                  stroke_width=stroke, stroke_fill=(0, 0, 0))
        y += line_h
    if path.suffix.lower() == ".jpg":
        img.save(path, "JPEG", quality=92, optimize=True)
    else:
        img.save(path, "PNG")


GLOW_HUES = {                                  # emotion -> rim-glow tint
    "intense": (200, 30, 40), "curious": (110, 70, 200),
    "playful": (200, 80, 170), "serious": (60, 90, 200),
    "urgent": (220, 100, 30), "triumphant": (210, 160, 50),
    "calm": (60, 160, 170), "sad": (70, 90, 150),
}


def local_artwork(ctx: dict, wide: bool, seed: int) -> tuple[bytes, str, str]:
    """Offline safety net, rendered entirely locally (no network): dark
    navy/violet brand gradient + emotion-tinted rim glow + film grain +
    vignette. Text-free - the caption pass typesets the headline."""
    import random
    from PIL import Image, ImageDraw, ImageFilter
    rnd = random.Random(seed)
    w, h = YT_SIZE if wide else IG_SIZE
    top, bottom = (14, 17, 38), (3, 4, 10)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        f = y / max(1, h - 1)
        row = tuple(int(top[i] + (bottom[i] - top[i]) * f) for i in range(3))
        for x in range(w):
            px[x, y] = row
    hue = GLOW_HUES.get(str(ctx.get("hook_emotion", "")).lower(),
                        (110, 70, 200))
    glow = Image.new("RGB", (w, h), hue)
    mask = Image.new("L", (w, h), 0)
    gr = int(min(w, h) * (0.52 if wide else 0.42))
    gx = int(w * rnd.uniform(0.3, 0.7))
    gy = int(h * (0.62 if wide else 0.55))
    ImageDraw.Draw(mask).ellipse([gx - gr, gy - gr, gx + gr, gy + gr], fill=85)
    mask = mask.filter(ImageFilter.GaussianBlur(min(w, h) // 6))
    img = Image.composite(Image.blend(img, glow, 0.55), img, mask)
    vig = Image.new("L", (w, h), 0)
    ImageDraw.Draw(vig).ellipse([-w // 3, -h // 3, w + w // 3, h + h // 3],
                                fill=255)
    vig = vig.filter(ImageFilter.GaussianBlur(min(w, h) // 5))
    black = Image.new("RGB", (w, h), (0, 0, 0))
    img = Image.composite(img, black, vig.point(lambda v: 40 + v * 215 // 255))
    noise = Image.effect_noise((w, h), 22).convert("L")
    img = Image.blend(img, Image.merge("RGB", (noise, noise, noise)), 0.05)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), "image/png", "local-artwork (offline fallback)"


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

def build_variant(name: str, caption: str, ctx: dict, run_folder: Path,
                  api_key: str | None, fake: bool = False) -> dict | None:
    """One variant -> native 16:9 + native 9:16, saved.

    Free path (default): Pollinations paints TEXT-FREE artwork, the caption
    is typeset locally (deterministic spelling). Gemini path
    (THUMB_IMAGE_PROVIDER=gemini): the model paints the text itself and a
    vision read-back verifies it (v2 behaviour)."""
    provider = "local" if fake else PROVIDER
    info: dict = {"generator": ("local-artwork (debug)" if fake else
                                "gemini-image" if provider == "gemini" else
                                "pollinations-flux (free)"),
                  "frame_timestamp": None,          # no video frames anymore
                  "caption_used": caption or "",
                  "original_text": (ctx.get("hook_text") if name == "hook"
                                    else ctx.get("mid_text", "")) if name != "clean" else "",
                  "emotion": (ctx.get("hook_emotion") if name == "hook"
                              else ctx.get("mid_emotion", "")) if name != "clean" else "",
                  "beat_index": 0 if name == "hook" else ctx.get("mid_index"),
                  "files": {}, "models": {}}
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
        out = run_folder / "thumbnails" / f"{name}_{tag}.{ext}"

        if provider == "gemini" and not fake:
            # PAID path (v2 behaviour): the model paints the headline text.
            prompt = build_prompt(name, caption, ctx, wide, paint_text=True)
            img, mime, used = gemini_image(prompt, aspect, api_key)
            out = save_image(img, out, target)
            if caption:
                verdict = verify_text(img, mime, caption, api_key)
                info["text_verified"] = bool(verdict)
                if verdict is False:
                    print(f"      {name}/{tag}: text mismatch - "
                          "regenerating once")
                    img, mime, used = gemini_image(
                        prompt + "\nIMPORTANT: spell the headline EXACTLY as "
                        "given, letter by letter, no substitutions.",
                        aspect, api_key)
                    out = save_image(img, out, target)
                    verdict2 = verify_text(img, mime, caption, api_key)
                    info["text_verified"] = bool(verdict2) \
                        if verdict2 is not None else False
        else:
            # FREE path: text-free artwork, then deterministic typography.
            prompt = build_prompt(name, caption, ctx, wide, paint_text=False)
            if fake:
                img, mime, used = local_artwork(ctx, wide,
                                                seed_for(run_folder.name,
                                                         name, tag))
            else:
                try:
                    img, mime, used = pollinations_image(
                        prompt, wide, seed_for(run_folder.name, name, tag))
                except Exception as exc:
                    if provider == "pollinations":
                        raise
                    print(f"      pollinations unavailable "
                          f"({str(exc)[:70]})\n      -> local brand artwork "
                          "fallback")
                    img, mime, used = local_artwork(
                        ctx, wide, seed_for(run_folder.name, name, tag))
            out = save_image(img, out, target)
            if caption:
                overlay_headline(out, caption, wide,
                                 ACCENT_YELLOW if name == "hook"
                                 else ACCENT_WHITE)
                info["text_verified"] = True   # deterministic local typeset

        info["files"][tag] = str(out.relative_to(run_folder))
        info["models"][tag] = used
        kb = out.stat().st_size / 1024
        print(f"      {name}/{tag}: {out.name} ({kb:.0f} KB, {used})"
              f"{' [FAKE]' if fake else ''}")
        model_used = used
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
        description="Generate AI thumbnail variants (free Pollinations artwork "
                    " + local typography) for the newest (or given) run")
    parser.add_argument("--list-models", action="store_true",
                        help="probe: print which models this API key can use "
                             "(image + text) and exit")
    parser.add_argument("--fake", action="store_true",
                        help="debug: build local brand artwork instead of "
                             "calling the image provider (tests the full "
                             "save/overlay/manifest path offline)")
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
        if api_key:
            try:
                list_models(api_key)
            except Exception as exc:
                print(f"gemini model probe failed: {str(exc)[:300]}")
        else:
            print("GEMINI_API_KEY not set - skipping gemini probe")
        print("  --- free provider check (pollinations, no key needed) ---")
        try:
            img, _mime, used = pollinations_image(
                "A tiny plain blue circle on a plain white background.",
                True, seed_for("probe", "probe", "probe"))
            print(f"    pollinations:flux -> WORKS "
                  f"({len(img) // 1024} KB via {used})")
        except Exception as exc:
            print(f"    pollinations -> fails ({str(exc)[:90]})")
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
    print("  STEP 2.6 - THUMBNAILS (AI-generated, $0 free pipeline)")
    print(f"  run: {run_folder.name} | default variant: {variant_pref} "
          f"| build: {','.join(wanted)} | auto-attach: {auto_upload}")
    provider_note = {
        "auto": "pollinations flux (free) + local artwork fallback",
        "pollinations": "pollinations flux (free)",
        "gemini": "gemini-image (paid)",
        "local": "local artwork only",
    }[PROVIDER]
    print(f"  provider: {provider_note}")
    print("=" * 60)
    if not api_key and PROVIDER == "gemini":
        print("WARNING: GEMINI_API_KEY not set - gemini provider cannot run")
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
        "generator": ("gemini-image (no video frames)" if PROVIDER == "gemini"
                      else "pollinations-flux + local typography "
                           "(free, no video frames)"),
        "image_provider": "gemini" if PROVIDER == "gemini" else "pollinations",
        "cost": "$0",
        "image_model": variants[chosen].get("model", "") if chosen else "",
        "mode": ctx["mode"],
        "config": {"thumbnail_variant": variant_pref,
                   "auto_upload_thumbnail": auto_upload,
                   "variants_built": wanted,
                   "provider": PROVIDER,
                   "image_model": POLLINATIONS_MODEL_ENV or "flux"},
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
