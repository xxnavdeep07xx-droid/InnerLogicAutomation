#!/usr/bin/env python3
"""
step1_generate.py - Step 1 of the faceless channel automation pipeline.

What it does
------------
1. Generates a ~40-second voiceover script (~130 words) with the Google
   Gemini API for the philosophy / dark-psychology niche:

       viral hook  ->  one concept explained simply  ->  clear conclusion

   Gemini returns a JSON object with TWO fields:
       "script"         - the spoken voiceover text (pure text, no markdown)
       "search_queries" - 3-4 short (1-2 word) visual search terms mapping
                          to different parts of the script (e.g. "glass",
                          "dark forest", "crowd"). Step 2 uses them to pull
                          matching Pexels b-roll and hard-cut between scenes
                          exactly when the narration moves on.

2. Converts the script into speech with edge-tts using a deep, authoritative
   male voice (default: en-US-ChristopherNeural)  ->  voiceover.mp3

3. Captures word-level timing metadata (the WordBoundary events edge-tts
   emits while streaming the audio)  ->  word_timings.json

   Every word gets exact "start" / "end" times in seconds. Step 2 of the
   pipeline will read this file to build perfectly synced subtitles.

Outputs (one folder per run, e.g. output/run_20260831_120000/)
    script.txt          the plain-text voiceover script
    voiceover.mp3       the synthesized voiceover audio
    word_timings.json   word-by-word timing metadata (see below)

JSON shape
    {
      "meta":           { "voice": ..., "rate": ..., "word_count": ... },
      "script":         "full script text",
      "search_queries": ["glass", "dark forest", "crowd", "mirror"],
      "words":          [ {"index": 0, "word": "Every", "start": 0.06, "end": 0.21}, ... ]
    }

Usage
-----
    python step1_generate.py                       # full pipeline (needs GEMINI_API_KEY)
    python step1_generate.py --topic stoicism      # force a specific topic
    python step1_generate.py --list-topics         # show built-in topic ideas
    python step1_generate.py --script-file my.txt  # skip Gemini, TTS stage only
    python step1_generate.py --run-id my-video     # custom output folder name

Secrets & configuration
-----------------------
    API keys are loaded from a .env file (never hardcoded, never printed):

        GEMINI_API_KEY=...              # required for script generation
        GEMINI_MODEL=<name>             # optional: force one specific model
        GEMINI_MODELS=m1,m2,m3          # optional: custom fallback chain (in order)
        EDGE_TTS_VOICE=en-US-ChristopherNeural   # optional
        EDGE_TTS_RATE=+0%               # optional, e.g. "+10%" for faster reads

    See .env.example. Python 3.9+ required.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import random
import re
import sys
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing dependency 'python-dotenv'. Run:  pip install -r requirements.txt")

# ---------------------------------------------------------------------------
# Configuration defaults (all overridable via .env)
# ---------------------------------------------------------------------------

DEFAULT_VOICE = "en-US-AndrewMultilingualNeural"  # most natural narrator voice
DEFAULT_RATE = "+10%"                       # "+10%" = 10% faster, "-5%" = slower
DEFAULT_PITCH = "-3Hz"                      # slight lowering -> deeper, less 'AI'
DEFAULT_OUTPUT_DIR = "output"

# Model fallback chain (tried in order, first one that responds wins).
# - gemini-3.1-pro-preview: best quality, but NOT on the free tier (limit 0);
#   it works immediately if billing is enabled on your AI Studio project.
# - flash variants: free-tier friendly in most supported regions.
# - legacy 2.5 names: kept for accounts that still have access to them.
DEFAULT_MODEL_CHAIN = [
    "gemini-3.1-pro-preview",
    "gemini-flash-latest",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
]

WORDS_MIN, WORDS_MAX = 120, 140             # ~40 s of spoken audio
MAX_API_ATTEMPTS = 3
TICKS_PER_SECOND = 10_000_000               # edge-tts offsets are 100-ns ticks

TOPICS = [
    "the spotlight effect: why nobody is watching you as closely as you think",
    "Stoic negative visualization: how preparing for loss makes you unshakeable",
    "the shadow self: the part of your personality you refuse to see (Jung)",
    "Machiavellianism: why strategic silence beats argument every time",
    "loss aversion: why losing feels twice as painful as winning feels good",
    "the Ben Franklin effect: how asking for a favor makes someone like you more",
    "reactance: why people want exactly what they are told they cannot have",
    "social proof: how the herd quietly decides what you call beautiful or true",
    "the paradox of choice: why more options make you miserable",
    "amor fati: the Stoic practice of loving whatever happens",
    "gaslighting: how someone slowly makes you doubt your own memory",
    "the Zeigarnik effect: why unfinished business keeps haunting your mind",
]

SYSTEM_PROMPT = (
    "You are an elite short-form retention specialist and scriptwriter for a "
    "faceless YouTube Shorts and Instagram Reels channel about philosophy and "
    "dark psychology. Your scripts open with a scroll-stopping hook inside the "
    "first two seconds, move beat by beat with escalating tension, and end on "
    "the strongest line so the video loops or lands a quote. You write for the "
    "ear, not the page. You always follow the user's formatting rules exactly, "
    "and when asked for JSON you reply with a bare, valid JSON object - never "
    "markdown fences."
)


def build_user_prompt(topic: str) -> str:
    return (
        "Write the content for a 40-second vertical video. Reply with ONE bare "
        "JSON object (no markdown, no code fences) with exactly these keys:\n"
        "\n"
        "{\n"
        '  "script": "<the voiceover text>",\n'
        '  "search_queries": ["<term1>", "<term2>", "<term3>", "<term4>"]\n'
        "}\n"
        "\n"
        f"TOPIC: {topic}\n"
        "\n"
        'RULES FOR "script" - follow this beat structure:\n'
        "1. LENGTH: 120-140 words of spoken text.\n"
        "2. BEATS, in this order:\n"
        "   - HOOK (first sentence, max 8 words): trigger curiosity, tension, "
        "contradiction, or an uncomfortable truth - ideally a command or a "
        "claim that feels wrong but is true. Never greet the viewer, never "
        "say \"in this video\", never open with \"Have you ever\" or \"Most "
        "people\".\n"
        "   - TENSION (~20 words): why the hook hurts. Name the exact "
        "mechanism working against the viewer.\n"
        "   - SCENARIOS (~30 words): two or three concrete, recognizable "
        "moments from real life - specific people, objects, or phone calls, "
        "not abstract nouns. Address the viewer as \"you\".\n"
        "   - REFRAME (~25 words): reveal the named concept that flips the "
        "fear into an advantage. If the topic has a real name (a Latin term, "
        "a bias, a principle), use it.\n"
        "   - ESCALATION (~20 words): what changes when the viewer actually "
        "applies it - one level deeper.\n"
        "   - PAYOFF (final ~20 words): the strongest line of the script. It "
        "must echo the opening hook so the video loops, or land as a quotable "
        "one-liner. No \"thanks for watching\", no generic call to action.\n"
        "3. TONE: deep, calm, authoritative, slightly dark.\n"
        "4. LANGUAGE: write for the EAR. Short sentences. Everyday words. Use "
        "contractions (you're, it's, that's). Ban formal constructions like "
        "\"It is about\" or \"that is exactly how\" - say \"it's about\" and "
        "\"that's how\". Pure spoken text: no markdown, no headings, no "
        "emojis, no stage directions, no title, no commentary.\n"
        "\n"
        'RULES FOR "search_queries":\n'
        "1. Exactly 4 short search terms (1-2 words each) describing VISUAL "
        "scenes that match the script's flow, in story order - one per beat "
        "movement.\n"
        "2. Each term must be concrete stock-footage material - things a "
        "camera can film: \"glass shattering\", \"dark forest\", \"crowd "
        "walking\", \"mirror reflection\", \"storm clouds\".\n"
        "3. Query 1 must visually match the hook; the last query must match "
        "the closing line. Build a visual progression (for example: "
        "destruction, then isolation, then discipline, then rebirth). "
        "Abstract words like \"psychology\" or \"memory\" are useless here - "
        "pick filmable imagery.\n"
    )


@dataclass
class Config:
    models: list[str]
    voice: str
    rate: str
    pitch: str = ""
    api_key: str | None = None


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def clean_script_text(raw: str) -> str:
    """Defensively turn whatever the model returned into pure spoken text."""
    text = (raw or "").strip()
    # Drop a leading label ("Hook:", "Title:", "Script:") if the model adds one.
    text = re.sub(r"^\s*(?:hook|title|script|voiceover)\s*:\s*", "", text, flags=re.IGNORECASE)
    # Strip a pair of wrapping quotes.
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'"):
        text = text[1:-1].strip()
    # Remove markdown artifacts: headings, emphasis, blockquotes, inline code.
    text = re.sub(r"[#*_`>]+", "", text)
    # Remove bullet dashes at line starts.
    text = re.sub(r"(?m)^\s*[-\u2013\u2014]\s+", "", text)
    # Collapse all whitespace into single spaces -> one flowing block of speech.
    text = re.sub(r"\s+", " ", text).strip()
    return text


def count_words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))


def clean_search_queries(raw) -> list[str]:
    """Normalize Gemini's search_queries into 2-4 clean, short visual terms."""
    if not isinstance(raw, list):
        return []
    queries: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        q = re.sub(r"\s+", " ", item.strip().strip("\"'").lower())
        if not q or len(q) > 24 or len(q.split()) > 2:
            continue                      # too long / not 1-2 words -> drop
        if q not in queries:
            queries.append(q)
    return queries[:4]


def parse_script_payload(raw: str) -> tuple[str, list[str]]:
    """Parse Gemini's reply into (script_text, search_queries).

    Expects a JSON object {"script": ..., "search_queries": [...]}. Falls
    back to treating the whole reply as plain script text (queries = []) so
    a chatty model can never break the pipeline."""
    text = (raw or "").strip()
    # Tolerate markdown code fences even though we ask for bare JSON.
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", text).strip()
    data = None
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if isinstance(data, dict) and isinstance(data.get("script"), str) \
            and data["script"].strip():
        script = clean_script_text(data["script"])
        queries = clean_search_queries(data.get("search_queries"))
        if script:
            return script, queries
    # Fallback: not usable JSON - treat the whole reply as the script.
    return clean_script_text(text), []


# ---------------------------------------------------------------------------
# V2 "beats" mode: structured script with emotion / emphasis / visual metadata
# ---------------------------------------------------------------------------

EMOTIONS = ("intense", "serious", "curious", "playful", "urgent",
            "calm", "triumphant")

BANNED_OPENERS = ("have you ever", "ever wondered", "let's", "lets ", "lets,",
                  "today we", "in this video", "most people", "we all ",
                  "imagine ", "what if i told you", "here's the thing",
                  "here is the thing", "so what does", "in conclusion",
                  "as you can see", "let me ask", "welcome back")
HEDGE_PATTERNS = ("might", "could be", "perhaps", "maybe", "probably",
                  "some studies", "some say", "it is said", "it's said")
PROVOCATIVE_MARKERS = ("never", "stop", "no one", "everyone", "nobody",
                       "always", "why ", "truth", "lie", "lying", "destroy",
                       "kill", "lose", "dead", "die", "secret", "control",
                       "respect", "weak", "power", "money", "everyone you")
IMPERATIVE_VERBS = ("stop", "kill", "delete", "lose", "try", "ask", "give",
                    "take", "do", "be", "trust", "doubt", "question",
                    "forget", "remember", "become", "start", "quit", "throw",
                    "burn", "cut", "want", "need", "listen", "look", "watch",
                    "think", "change", "break", "build", "act", "say")
HOOK_MIN_WORDS, HOOK_MAX_WORDS = 3, 18      # hard gate bounds
HOOK_IDEAL_MIN, HOOK_IDEAL_MAX = 8, 12      # spoken < 3 s at 2.5 w/s

# The full writing spec (user-facing rules), sent as the user prompt in v2.
PROMPT_V2_TEMPLATE = """You are a viral short-form video scriptwriter for a Philosophy & Psychology content account (Instagram Reels / YouTube Shorts). Your scripts are read aloud by a text-to-speech voice over stock footage, with animated word-by-word captions. You write ONLY for the 15-45 second vertical video format.

## YOUR OUTPUT
Return ONLY valid JSON, no markdown fences, no preamble, matching this schema:

{
  "title": "short internal title for the video",
  "hook_strength_notes": "1 sentence explaining why the opening line will stop someone scrolling",
  "beats": [
    {
      "text": "the exact sentence to be spoken",
      "emotion": "one of: intense | serious | curious | playful | urgent | calm | triumphant",
      "emphasis_words": ["word_or_phrase_1", "word_or_phrase_2"],
      "visual_concept": "2-4 word abstract description of what should be shown on screen for THIS beat (not literal objects mentioned in the sentence - the underlying feeling or idea)",
      "pause_after_ms": 0
    }
  ],
  "cta": "the final line of the video - a closing punch or call to action"
}

## RULES FOR THE SCRIPT ITSELF

1. HOOK (beats[0]):
   - Must be a provocative claim, an uncomfortable truth, or a direct question aimed at the viewer.
   - No throat-clearing. Never start with "Have you ever wondered," "Let's talk about," "Today we're discussing," or any setup sentence.
   - Should create a knowledge gap or emotional reaction in under 3 seconds of spoken audio (roughly 8-12 words).
   - Bad: "Doing favors for people is something we all do, but it can backfire."
   - Good: "The more favors you do for someone, the less they respect you."

2. SENTENCE LENGTH: Every beat's "text" must be a single short sentence, max ~12 words. Break longer ideas into multiple beats. Short sentences hit harder as captions and give the TTS natural places to pause.

3. NO FILLER: Cut any sentence whose only job is transition or summary ("So what does this mean?", "In conclusion,", "As you can see,"). Every beat must add new information or escalate the emotional stakes.

4. STRUCTURE (aim for 6-10 beats total):
   - Beat 1: Hook (see above)
   - Beats 2-3: The counterintuitive mechanism or psychological concept, explained simply
   - Beats 4-6: A concrete example, study reference, or vivid mental image that makes it feel real
   - Beats 7-8: The twist or the "why this matters to you" reframe
   - Final beat / cta: A punchy closing line or direct instruction to the viewer. Never end on a passive summary. End on something quotable.

5. EMPHASIS_WORDS: For each beat, pick 1-3 words or short phrases that should be visually/vocally punched (color pop in captions, pitch/volume lift in TTS). Choose the words that carry the emotional or informational weight of the sentence, not filler words.

6. EMOTION TAGS: Assign the emotion that should drive both the TTS delivery (pitch/rate) and the background music/SFX choice for that beat. Vary emotion across the script - don't mark everything "serious."

7. VISUAL_CONCEPT: This is NOT a literal keyword for stock footage search. Describe the underlying feeling or metaphor (e.g. for a sentence about people taking advantage of you, use "quiet resentment building" or "one-sided giving," not "hands" or "gift"). This field will be used to select from a curated abstract footage library, so favor mood and metaphor over literal nouns in the sentence.

8. PAUSE_AFTER_MS: Set to 300-500 for beats that need a beat of silence for impact (right after a punchline or before a reveal). Default to 0 otherwise. Never use more than two long pauses in one script.

9. LENGTH: Total spoken script should run 25-40 seconds at natural speaking pace (~2.5 words/second), which is roughly 65-100 words total across all beats.

10. VOICE: Write like you're telling a friend something slightly shocking about human psychology, not narrating a textbook. Second-person ("you") where possible. Confident, direct, zero hedging language ("might," "could," "some studies suggest").

## EXAMPLE OF A GOOD OUTPUT

{
  "title": "ben_franklin_effect",
  "hook_strength_notes": "Opens with a reversal of common wisdom, creates immediate curiosity gap",
  "beats": [
    {"text": "Doing favors makes people like you less, not more.", "emotion": "intense", "emphasis_words": ["like you less"], "visual_concept": "quiet imbalance, one side giving", "pause_after_ms": 400},
    {"text": "It's called the Ben Franklin effect.", "emotion": "curious", "emphasis_words": ["Ben Franklin effect"], "visual_concept": "old handwriting, a written note", "pause_after_ms": 0},
    {"text": "Franklin got a rival to like him by asking to borrow a book.", "emotion": "curious", "emphasis_words": ["borrow a book"], "visual_concept": "hand reaching for something small", "pause_after_ms": 0},
    {"text": "Not by doing the rival a favor. By asking for one.", "emotion": "playful", "emphasis_words": ["asking for one"], "visual_concept": "role reversal, unexpected turn", "pause_after_ms": 300},
    {"text": "Your brain justifies effort by deciding it must like the person.", "emotion": "serious", "emphasis_words": ["justifies effort"], "visual_concept": "internal conflict, mental rationalizing", "pause_after_ms": 0},
    {"text": "So next time you want someone to like you,", "emotion": "urgent", "emphasis_words": [], "visual_concept": "anticipation, a pause before action", "pause_after_ms": 200},
    {"text": "stop helping them. Ask them to help you.", "emotion": "triumphant", "emphasis_words": ["Ask them to help you"], "visual_concept": "quiet confidence, reversal of power", "pause_after_ms": 0}
  ],
  "cta": "Try it once this week - and watch what happens."
}

## TOPIC FOR THIS SCRIPT:
{{TOPIC}}

## REFERENCE MATERIAL (if provided):
{{SOURCE_MATERIAL}}"""


def build_user_prompt_v2(topic: str, source_material: str = "") -> str:
    src = (source_material or "").strip() or "(none provided)"
    return PROMPT_V2_TEMPLATE.replace("{{TOPIC}}", topic.strip()) \
                             .replace("{{SOURCE_MATERIAL}}", src)


def hook_strength_score(hook_text: str) -> tuple[float, list[str], list[str]]:
    """Heuristic 0-10 score for a hook line + (reasons, hard-failure list).

    Fails when: banned opener, length outside 3-18 words, or a final score
    below the gate. The score is printed into the run log so scripts can be
    reviewed before rendering."""
    text = (hook_text or "").strip()
    low = text.lower()
    wc = count_words(text)
    score, reasons, fails = 0.0, [], []

    if wc == 0:
        return 0.0, [], ["hook is empty"]

    # length (ideal 8-12 spoken words = under 3 s)
    if HOOK_IDEAL_MIN <= wc <= HOOK_IDEAL_MAX:
        score += 3
    elif HOOK_MIN_WORDS <= wc <= HOOK_MAX_WORDS:
        score += 1.5
        reasons.append(f"{wc} words is outside the 8-12 sweet spot")
    else:
        fails.append(f"hook is {wc} words (hard bounds {HOOK_MIN_WORDS}-{HOOK_MAX_WORDS}, ideal 8-12)")

    # throat-clearing openers are an instant fail
    for opener in BANNED_OPENERS:
        if low.startswith(opener):
            fails.append(f"banned opener: starts with '{opener.strip()}'")
            score -= 3
            break

    # hedging language
    hedges = [h for h in HEDGE_PATTERNS if h in low]
    if hedges:
        score -= 2
        reasons.append(f"hedging language ({hedges[0]}) weakens the claim")

    # direct question aimed at the viewer
    if text.rstrip().endswith(("?", "?\u201d", "?\"")):
        score += 2
    # second-person address
    if re.search(r"\b(you|your|you're|yourself)\b", low):
        score += 2
    # provocative vocabulary
    if any(m in low for m in PROVOCATIVE_MARKERS):
        score += 1
    # imperative opener (command -> claim)
    if low.split()[0].strip(",.") in IMPERATIVE_VERBS:
        score += 1

    score = max(0.0, min(10.0, round(score, 1)))
    return score, reasons, fails


def _normalize_beat(raw, index: int) -> dict | None:
    text = clean_script_text(str(raw.get("text", "")))
    if not text:
        return None
    emotion = str(raw.get("emotion", "serious")).strip().lower()
    if emotion not in EMOTIONS:
        emotion = "serious"
    emphasis = [re.sub(r"\s+", " ", str(w)).strip()
                for w in (raw.get("emphasis_words") or [])]
    emphasis = [e for e in emphasis if e][:3]
    try:
        pause = int(raw.get("pause_after_ms") or 0)
    except (TypeError, ValueError):
        pause = 0
    return {
        "index": index,
        "text": text,
        "emotion": emotion,
        "emphasis_words": emphasis,
        "visual_concept": clean_script_text(str(raw.get("visual_concept", "")))[:60],
        "pause_after_ms": max(0, min(800, pause)),
    }


def parse_beats_payload(raw: str) -> dict | None:
    """Parse Gemini's v2 reply into a normalized beats payload.

    Returns {"title", "hook_strength_notes", "beats": [...], "cta",
    "hook_text"} with the CTA appended as the final spoken beat, or None if
    the reply contains no usable script at all."""
    text = (raw or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", text).strip()
    data = None
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return None

    if isinstance(data.get("beats"), list) and data["beats"]:
        beats = []
        for raw_beat in data["beats"]:
            if isinstance(raw_beat, dict):
                beat = _normalize_beat(raw_beat, len(beats))
                if beat:
                    beats.append(beat)
        if not beats:
            return None
    elif isinstance(data.get("script"), str) and data["script"].strip():
        # classic-shaped reply - synthesize beats so v2 can still proceed
        beats = build_beats_from_classic(data["script"])
    else:
        return None

    cta = clean_script_text(str(data.get("cta", "")))
    if cta:
        beats.append(_normalize_beat({"text": cta, "emotion": "triumphant",
                                      "emphasis_words": [], "pause_after_ms": 0},
                                     len(beats)))
    return {
        "title": clean_script_text(str(data.get("title", "")))[:80],
        "hook_strength_notes": clean_script_text(
            str(data.get("hook_strength_notes", "")))[:200],
        "beats": beats,
        "cta": cta,
        "hook_text": beats[0]["text"],
    }


def sentences_of(text: str) -> list[str]:
    """Split spoken text into sentences (keeps terminal punctuation)."""
    parts = re.split(r"(?<=[.!?])\s+", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def build_beats_from_classic(script_text: str) -> list[dict]:
    """Turn a plain script string into neutral beats (<= 12 words each).

    Used when v2 mode is fed a plain --script-file, and as a safety net if
    the model answers in the old {script, search_queries} shape."""
    beats: list[dict] = []
    for sentence in sentences_of(clean_script_text(script_text)):
        words = sentence.split()
        while len(words) > 12:
            beats.append(_normalize_beat({"text": " ".join(words[:9])}, len(beats)))
            words = words[9:]
        if words:
            beats.append(_normalize_beat({"text": " ".join(words)}, len(beats)))
    return [b for b in beats if b]


def clean_concept_queries(concepts: list[str]) -> list[str]:
    """visual_concept strings -> short Pexels-friendly queries (compat layer)."""
    queries: list[str] = []
    for concept in concepts:
        words = re.findall(r"[A-Za-z0-9']+", (concept or "").lower())
        words = [w for w in words if w not in
                 ("the", "a", "an", "of", "and", "with", "in", "on", "one",
                  "side", "something", "small", "its")]
        if not words:
            continue
        q = " ".join(words[:3])
        if q not in queries:
            queries.append(q)
    return queries[:6]


def mark_emphasis_words(words: list[dict], beats: list[dict]) -> int:
    """Flag words whose text belongs to a beat's emphasis_words (in place).

    Handles multi-word phrases ("like you less") by scanning each beat's
    words for a case-insensitive match. Returns the number of flagged words."""
    flagged = 0
    for beat in beats:
        beat_words = [w for w in words if w.get("beat") == beat["index"]]
        for phrase in beat.get("emphasis_words", []):
            tokens = [re.sub(r"[^a-z0-9']", "", t.lower())
                      for t in phrase.split()]
            tokens = [t for t in tokens if t]
            n = len(tokens)
            if n == 0:
                continue
            for i in range(len(beat_words) - n + 1):
                seq = [re.sub(r"[^a-z0-9']", "", w["word"].lower())
                       for w in beat_words[i:i + n]]
                if seq == tokens:
                    for w in beat_words[i:i + n]:
                        w["emphasis"] = True
                        flagged += 1
                    break
    return flagged


def generate_script_beats_with_gemini(topic: str, cfg: Config,
                                      source_material: str = "",
                                      hook_gate: bool = True,
                                      hook_min_score: float = 5.0) -> dict:
    """V2 script generation: beats JSON + hook-strength gate with retry."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Missing dependency 'google-genai'. Run:  pip install -r requirements.txt")

    client = genai.Client(
        api_key=cfg.api_key,
        http_options=types.HttpOptions(timeout=90000),
    )
    problems: list[dict] = []
    feedback = ""

    for model in cfg.models:
        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            user_prompt = build_user_prompt_v2(topic, source_material)
            if feedback:
                user_prompt += "\n\n## CORRECTION REQUIRED (previous attempt rejected)\n" + feedback
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=1.0,
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                    ),
                )
                raw = extract_text(response)
                if not raw:
                    raise RuntimeError("empty response (possibly blocked by safety filters)")
                payload = parse_beats_payload(raw)
                if not payload:
                    feedback = ("Your reply was not usable JSON matching the schema. "
                                "Return ONE bare JSON object with title, "
                                "hook_strength_notes, beats[], cta.")
                    problems.append({"model": model, "why": "unparseable"})
                    continue

                total_words = sum(count_words(b["text"]) for b in payload["beats"])
                score, reasons, fails = hook_strength_score(payload["hook_text"])

                if attempt < MAX_API_ATTEMPTS and (fails or total_words < 45
                                                   or total_words > 150):
                    if fails:
                        feedback = (f"Your hook line '{payload['hook_text']}' was "
                                    f"REJECTED: {'; '.join(fails)}. Rewrite beats[0] "
                                    f"as a provocative claim, uncomfortable truth, "
                                    f"or direct question of {HOOK_IDEAL_MIN}-"
                                    f"{HOOK_IDEAL_MAX} words with zero setup. ")
                    if total_words < 45:
                        feedback += (f"The whole script is only {total_words} words; "
                                     "expand to 65-100 spoken words across 6-10 beats. ")
                    elif total_words > 150:
                        feedback += (f"The script is {total_words} words; cut it to "
                                     "65-100 spoken words across 6-10 beats. ")
                    feedback += "Return the full corrected JSON object only."
                    print(f"      gate: retrying ({'; '.join(fails) or f'{total_words} words'})")
                    continue

                if hook_gate and fails:
                    raise RuntimeError(
                        "hook gate failed after "
                        f"{MAX_API_ATTEMPTS} attempts: {payload['hook_text']!r} -> "
                        + "; ".join(fails))
                if hook_gate and score < hook_min_score:
                    # soft threshold: too-weak hook but no hard failure
                    print(f"      WARNING: hook score {score} < {hook_min_score} "
                          f"({'; '.join(reasons) or 'weak markers'})")

                payload["hook_score"] = score
                payload["hook_reasons"] = reasons
                payload["model"] = model
                return payload
            except RuntimeError as exc:
                # includes the deliberate hook-gate failure - do not swallow
                if "hook gate failed" in str(exc):
                    raise
                if is_permanent_model_error(exc):
                    print(f"      {model}: skipped ({_short_error(exc)})")
                    problems.append({"model": model, "why": _short_error(exc)})
                    break
                if attempt < MAX_API_ATTEMPTS:
                    wait = 2 ** attempt
                    print(f"      Gemini attempt {attempt} failed: {_short_error(exc)}")
                    print(f"      retrying in {wait}s ...")
                    time.sleep(wait)
                    continue
                problems.append({"model": model, "why": _short_error(exc)})
                break
            except Exception as exc:
                if is_permanent_model_error(exc):
                    print(f"      {model}: skipped ({_short_error(exc)})")
                    problems.append({"model": model, "why": _short_error(exc)})
                    break
                if attempt < MAX_API_ATTEMPTS:
                    wait = 2 ** attempt
                    print(f"      Gemini attempt {attempt} failed: {_short_error(exc)}")
                    print(f"      retrying in {wait}s ...")
                    time.sleep(wait)
                    continue
                problems.append({"model": model, "why": _short_error(exc)})
                break

    tried = "; ".join(f"{p['model']} ({p['why']})" for p in problems)
    raise RuntimeError(f"no Gemini model could generate a beats script. Tried: {tried}")


def load_environment(env_file: str | None) -> None:
    """Load secrets from .env without ever overriding real environment vars."""
    if env_file:
        path = Path(env_file)
        if path.is_file():
            load_dotenv(dotenv_path=path, override=False)
        else:
            print(f"NOTE: env file not found at {path} - continuing with current environment")
        return
    # Load EVERY candidate (override=False: real env vars / CI secrets always
    # win). Loading all of them means a partial .env in one location can no
    # longer shadow keys that live in another location.
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if candidate.is_file():
            load_dotenv(dotenv_path=candidate, override=False)


def resolve_models() -> list[str]:
    """Model selection: GEMINI_MODEL (single) > GEMINI_MODELS (chain) > built-in chain."""
    single = os.getenv("GEMINI_MODEL", "").strip()
    if single:
        return [single]
    chain = os.getenv("GEMINI_MODELS", "").strip()
    if chain:
        models = [m.strip() for m in chain.split(",") if m.strip()]
        if models:
            return models
    return list(DEFAULT_MODEL_CHAIN)


def resolve_api_key(required: bool) -> str | None:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        if required:
            sys.exit(
                "ERROR: GEMINI_API_KEY not found.\n"
                "  1. Get a free key at https://aistudio.google.com/apikey\n"
                "  2. Copy .env.example to .env and paste the key into it\n"
                "  (or run with --script-file to test the TTS stage without Gemini)"
            )
        return None
    if key.lower().startswith(("your_", "xxx", "paste", "example")):
        if required:
            sys.exit(
                "ERROR: GEMINI_API_KEY in your .env still looks like a placeholder.\n"
                "Get a real key at https://aistudio.google.com/apikey"
            )
        print("NOTE: GEMINI_API_KEY looks like a placeholder - running in TTS-only mode")
        return None
    return key


# ---------------------------------------------------------------------------
# Stage 1: script generation with Gemini
# ---------------------------------------------------------------------------

def extract_text(response) -> str | None:
    """Pull plain text out of a GenerateContentResponse defensively."""
    try:
        if response.text and response.text.strip():
            return response.text
    except Exception:
        pass
    try:
        parts = "".join(p.text or "" for p in response.candidates[0].content.parts)
        if parts.strip():
            return parts
    except Exception:
        pass
    return None


def is_permanent_model_error(exc: Exception) -> bool:
    """True = this model will not work in this environment; skip to the next one.
    (404 retired/unknown model, region-blocked model, or free-tier limit of 0.)"""
    text = str(exc)
    if "NOT_FOUND" in text or " is not found" in text or "no longer available" in text:
        return True
    if "FAILED_PRECONDITION" in text or "location is not supported" in text:
        return True
    if "RESOURCE_EXHAUSTED" in text and "limit: 0" in text:
        return True
    return False


def _short_error(exc: Exception) -> str:
    text = str(exc)
    if "location is not supported" in text:
        return "model not available in this region"
    if "RESOURCE_EXHAUSTED" in text and "limit: 0" in text:
        return "not included in the free tier (billing required)"
    if "no longer available" in text:
        return "model retired for this account"
    return text.split(chr(10))[0][:90]


def generate_script_with_gemini(topic: str, cfg: Config) -> tuple[str, list[str], str]:
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        sys.exit("Missing dependency 'google-genai'. Run:  pip install -r requirements.txt")

    # Hard per-request timeout so a black-holed connection can never hang the
    # pipeline for the whole job (GitHub Actions killed a 23-min silent run).
    client = genai.Client(
        api_key=cfg.api_key,
        http_options=types.HttpOptions(timeout=90000),  # 90 s per request
    )
    problems: list[str] = []

    for model in cfg.models:
        user_prompt = build_user_prompt(topic)
        for attempt in range(1, MAX_API_ATTEMPTS + 1):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=1.0,
                        max_output_tokens=8192,
                        response_mime_type="application/json",
                    ),
                )
                raw = extract_text(response)
                if not raw:
                    raise RuntimeError("empty response (possibly blocked by safety filters)")
                script, queries = parse_script_payload(raw)
                wc = count_words(script)
                if WORDS_MIN <= wc <= WORDS_MAX:
                    return script, queries, model
                if attempt < MAX_API_ATTEMPTS:
                    print(f"      draft had {wc} words (target {WORDS_MIN}-{WORDS_MAX}) - retrying")
                    user_prompt = (
                        build_user_prompt(topic)
                        + f"\nCRITICAL: your previous \"script\" field was {wc} words. "
                        f"The new \"script\" field MUST be {WORDS_MIN}-{WORDS_MAX} words. "
                        "Reply with the corrected JSON object only."
                    )
                    continue
                print(f"      WARNING: final draft is {wc} words (target {WORDS_MIN}-{WORDS_MAX})")
                return script, queries, model
            except Exception as exc:
                if is_permanent_model_error(exc):
                    reason = _short_error(exc)
                    print(f"      {model}: skipped ({reason})")
                    problems.append(f"{model} ({reason})")
                    break
                if attempt < MAX_API_ATTEMPTS:
                    wait = 2 ** attempt
                    print(f"      Gemini attempt {attempt} failed: {_short_error(exc)}")
                    print(f"      retrying in {wait}s ...")
                    time.sleep(wait)
                    continue
                problems.append(f"{model} ({_short_error(exc)})")
                break

    raise RuntimeError(
        "no Gemini model could generate the script. Tried: " + "; ".join(problems)
    )


# ---------------------------------------------------------------------------
# Stage 2+3: voiceover synthesis + word-level timing capture (edge-tts)
# ---------------------------------------------------------------------------

async def synthesize_voiceover(text: str, cfg: Config, out_dir: Path):
    try:
        import edge_tts
    except ImportError:
        sys.exit("Missing dependency 'edge-tts'. Run:  pip install -r requirements.txt")

    audio_path = out_dir / "voiceover.mp3"
    raw_timings: list[dict] = []

    # edge-tts >= 7.2 emits SentenceBoundary events by default; we explicitly
    # request WordBoundary so we get one timing entry per WORD. Older versions
    # (<= 7.1) always emit WordBoundary and have no 'boundary' parameter.
    kwargs = {"rate": cfg.rate}
    if cfg.pitch:
        kwargs["pitch"] = cfg.pitch
    if "boundary" in inspect.signature(edge_tts.Communicate.__init__).parameters:
        kwargs["boundary"] = "WordBoundary"
    communicate = edge_tts.Communicate(text, cfg.voice, **kwargs)
    with audio_path.open("wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                raw_timings.append(chunk)

    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise RuntimeError("edge-tts produced an empty audio file")
    return audio_path, raw_timings


def _normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def restore_punctuation(words: list[dict], script_text: str) -> None:
    """Best-effort pass: edge-tts boundary text is often bare (no punctuation).

    Replace each timed word with the matching word from the original script so
    later subtitles look natural. Never raises - on any mismatch the original
    TTS word is simply kept, so this can never break the timings."""
    try:
        script_words = script_text.split()
        normalized = [_normalize(w) for w in script_words]
        cursor = 0
        for entry in words:
            target = _normalize(entry["word"])
            if not target:
                continue
            j = cursor
            while j < len(normalized) and normalized[j] != target:
                j += 1
            if j < len(normalized):
                entry["word"] = script_words[j]
                cursor = j + 1
    except Exception:
        pass


def build_word_records(raw_timings: list[dict], script_text: str) -> list[dict]:
    records = []
    for chunk in raw_timings:
        word = str(chunk.get("text", "")).strip()
        if not word:
            continue
        offset = int(chunk.get("offset", 0))
        duration = int(chunk.get("duration", 0))
        records.append({
            "word": word,
            "start": round(offset / TICKS_PER_SECOND, 3),
            "end": round((offset + duration) / TICKS_PER_SECOND, 3),
            "offset_100ns": offset,
            "duration_100ns": duration,
        })
    restore_punctuation(records, script_text)
    for index, record in enumerate(records):
        record["index"] = index
    return records


def save_word_timings(json_path: Path, script_text: str, topic: str, used_model: str,
                      cfg: Config, words: list[dict],
                      search_queries: list[str] | None = None,
                      beats: list[dict] | None = None,
                      title: str = "", cta: str = "",
                      hook_score: float | None = None,
                      hook_notes: str = "", tts_backend: str = "edge",
                      mode: str = "classic") -> None:
    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "topic": topic,
        "model": used_model,
        "mode": mode,
        "tts_backend": tts_backend,
        "voice": cfg.voice,
        "rate": cfg.rate,
        "pitch": cfg.pitch,
        "audio_file": "voiceover.mp3",
        "audio_duration_seconds": round(words[-1]["end"], 3) if words else 0.0,
        "audio_duration_note": "approximate (end of last spoken word)",
        "word_count": len(words),
        "search_queries": list(search_queries or []),
    }
    if mode == "beats":
        meta.update({
            "title": title,
            "cta": cta,
            "hook_score": hook_score,
            "hook_strength_notes": hook_notes,
        })
    payload = {
        "meta": meta,
        "script": script_text,
        "search_queries": list(search_queries or []),
        "words": words,
    }
    if beats is not None:
        payload["beats"] = beats
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

async def run_pipeline(args, cfg: Config) -> None:
    run_id = args.run_id or datetime.now().strftime("run_%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    mode = (getattr(args, "mode", "") or os.getenv("SCRIPT_MODE", "")
            or "beats").strip().lower()
    if mode not in ("beats", "classic"):
        mode = "beats"
    tts_backend = (os.getenv("TTS_BACKEND", "edge") or "edge").strip().lower()

    # Stage 1 - the script ----------------------------------------------------
    search_queries: list[str] = []
    beats_payload: dict | None = None
    beats: list[dict] = []
    if args.beats_file:
        source = Path(args.beats_file)
        if not source.is_file():
            sys.exit(f"ERROR: beats file not found: {source}")
        beats_payload = parse_beats_payload(source.read_text(encoding="utf-8"))
        if not beats_payload:
            sys.exit(f"ERROR: no usable beats/script found in {source}")
        topic = f"custom beats (from {source.name})"
        used_model = "n/a (provided beats)"
        mode = "beats"
        print(f"[1/3] Using provided beats file ({len(beats_payload['beats'])} beats)")
    elif args.script_file:
        source = Path(args.script_file)
        if not source.is_file():
            sys.exit(f"ERROR: script file not found: {source}")
        raw_text = clean_script_text(source.read_text(encoding="utf-8"))
        topic = f"custom (from {source.name})"
        used_model = "n/a (provided script)"
        if mode == "beats":
            beats_payload = {"title": "", "hook_strength_notes": "",
                             "beats": build_beats_from_classic(raw_text),
                             "cta": "", "hook_text": raw_text.split(".")[0]}
            print(f"[1/3] Using provided script as {len(beats_payload['beats'])} "
                  "neutral beats")
        else:
            script_text = raw_text
            print(f"[1/3] Using provided script ({count_words(script_text)} words)")
    else:
        topic = args.topic or random.choice(TOPICS)
        source_material = (os.getenv("SOURCE_MATERIAL", "") or "").strip()
        if mode == "beats":
            print("[1/3] Generating beats script with Gemini (v2) ...")
            print(f"      topic: {topic}")
            hook_gate = (os.getenv("HOOK_GATE", "on") or "on").strip().lower() \
                not in ("0", "off", "none", "false")
            try:
                hook_min = float(os.getenv("HOOK_MIN_SCORE", "5") or 5)
            except ValueError:
                hook_min = 5.0
            beats_payload = generate_script_beats_with_gemini(
                topic, cfg, source_material=source_material,
                hook_gate=hook_gate, hook_min_score=hook_min)
            used_model = beats_payload.get("model", "?")
            print(f"      model: {used_model}")
            print(f"      title: {beats_payload.get('title') or '(untitled)'}")
            print(f"      hook score: {beats_payload.get('hook_score')}/10 "
                  f"- {beats_payload.get('hook_strength_notes', '')[:90]}")
        else:
            print("[1/3] Generating script + visual search queries with Gemini ...")
            print(f"      topic: {topic}")
            script_text, search_queries, used_model = generate_script_with_gemini(topic, cfg)
            print(f"      model: {used_model}")
            print(f"      search queries: {search_queries or '(none - static background fallback)'}")

    if beats_payload is not None:
        beats = beats_payload["beats"]
        script_text = " ".join(b["text"] for b in beats)
        search_queries = clean_concept_queries(
            [b["visual_concept"] for b in beats if b.get("visual_concept")])
        print(f"      {len(beats)} beats, {count_words(script_text)} spoken words")
        print(f"      concept queries: {search_queries or '(none - static background fallback)'}")
        print("      script ready:")
        print(textwrap.fill(script_text, width=64,
                            initial_indent="      | ",
                            subsequent_indent="      | "))
    if not script_text:
        sys.exit("ERROR: the script text is empty - nothing to synthesize")
    (out_dir / "script.txt").write_text(script_text + "\n", encoding="utf-8")

    # Stage 2 - voiceover -----------------------------------------------------
    if mode == "beats":
        print(f"[2/3] Synthesizing voiceover ({tts_backend} backend, "
              "per-beat emotion prosody) ...")
        print(f"      voice: {cfg.voice} | base rate: {cfg.rate}")
        import tts_engine
        result = tts_engine.generate_voiceover(
            beats, backend=tts_backend, out_dir=out_dir,
            voice=cfg.voice, rate=cfg.rate, pitch=cfg.pitch)
        audio_path = result["audio_path"]
        words = result["words"]
        beats = result["beats"]
        tts_backend = result["backend"]
        flagged = mark_emphasis_words(words, beats)
        print(f"      audio written ({audio_path.stat().st_size / 1024:.0f} KB, "
              f"{len(words)} timed words, {flagged} emphasis words flagged)")
    else:
        print("[2/3] Synthesizing voiceover with edge-tts ...")
        print(f"      voice: {cfg.voice} | rate: {cfg.rate}")
        audio_path, raw_timings = await synthesize_voiceover(script_text, cfg, out_dir)
        print(f"      audio written ({audio_path.stat().st_size / 1024:.0f} KB, "
              f"{len(raw_timings)} word-boundary events)")

        # Stage 3 - word timing metadata ---------------------------------------
        print("[3/3] Extracting word-level timing metadata ...")
        words = build_word_records(raw_timings, script_text)
        if not words:
            print("      WARNING: no word boundaries captured - run: pip install -U edge-tts")

    duration = words[-1]["end"] if words else 0.0
    save_word_timings(out_dir / "word_timings.json", script_text, topic,
                      used_model, cfg, words, search_queries,
                      beats=beats if mode == "beats" else None,
                      title=(beats_payload or {}).get("title", ""),
                      cta=(beats_payload or {}).get("cta", ""),
                      hook_score=(beats_payload or {}).get("hook_score"),
                      hook_notes=(beats_payload or {}).get("hook_strength_notes", ""),
                      tts_backend=tts_backend, mode=mode)

    print()
    print("=" * 60)
    print(f"  run folder         : {out_dir}")
    print(f"  mode               : {mode} (tts: {tts_backend})")
    if mode == "beats":
        print(f"  hook score         : {(beats_payload or {}).get('hook_score', 'n/a')}/10")
    print(f"  script.txt         : {count_words(script_text)} words")
    print(f"  voiceover.mp3      : {audio_path.stat().st_size / 1024:.0f} KB")
    print(f"  word_timings.json  : {len(words)} timed words, ~{duration:.1f} s of speech")
    print(f"  search queries     : {search_queries or '(none - static background)'}")
    print("=" * 60)
    if mode == "classic" and duration and not (35 <= duration <= 52):
        print("  tip: set EDGE_TTS_RATE in .env (e.g. \"+10%\") to move the read toward 40 s")
    print("  next: feed word_timings.json into step 2 for perfectly synced subtitles")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Step 1: Gemini script -> edge-tts voiceover -> word-level timing JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python step1_generate.py\n"
            "  python step1_generate.py --topic \"the shadow self\"\n"
            "  python step1_generate.py --script-file sample_script.txt --run-id sample_run\n"
        ),
    )
    parser.add_argument("--topic",
                        help="topic for today's video (default: random from built-in list)")
    parser.add_argument("--list-topics", action="store_true",
                        help="list built-in topic ideas and exit")
    parser.add_argument("--script-file",
                        help="use a ready-made text file instead of calling Gemini")
    parser.add_argument("--beats-file",
                        help="use a ready-made beats JSON file instead of calling "
                             "Gemini (implies beats mode)")
    parser.add_argument("--mode", choices=("beats", "classic"),
                        help="script style: 'beats' (v2: emotion/emphasis/visual "
                             "metadata, hook gate) or 'classic' (legacy). "
                             "Default: beats, or SCRIPT_MODE env")
    parser.add_argument("--run-id",
                        help="output folder name (default: run_<timestamp>)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"base output directory (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--env-file",
                        help="path to a .env file (default: .env near this script)")
    args = parser.parse_args()

    if args.list_topics:
        print("Built-in topic ideas (or pass your own with --topic):")
        for i, topic in enumerate(TOPICS, 1):
            print(f"  {i:2d}. {topic}")
        return 0

    if sys.platform == "win32":
        # Avoids a known edge-tts/aiohttp event-loop issue on Windows.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    load_environment(args.env_file)
    cfg = Config(
        models=resolve_models(),
        voice=(os.getenv("EDGE_TTS_VOICE", DEFAULT_VOICE).strip() or DEFAULT_VOICE),
        rate=(os.getenv("EDGE_TTS_RATE", DEFAULT_RATE).strip() or DEFAULT_RATE),
        pitch=(os.getenv("EDGE_TTS_PITCH", DEFAULT_PITCH).strip() or ""),
        api_key=resolve_api_key(required=args.script_file is None
                                and args.beats_file is None),
    )

    print("=" * 60)
    print("  FACELESS CHANNEL PIPELINE - STEP 1")
    print("  Gemini script -> edge-tts voiceover -> word timings JSON")
    print("=" * 60)

    try:
        asyncio.run(run_pipeline(args, cfg))
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
