#!/usr/bin/env python3
"""
curated_library.py - theme-tagged abstract footage library + reuse tracker.

Why
---
Live Pexels keyword search makes footage feel random: the sentence says
"favors" and you get close-ups of hands shaking. This module instead matches
each script beat's *visual_concept* (abstract feeling / metaphor written by
Gemini at script time) against a hand-curated library of 32 abstract clips
(brains, mirrors, crowds, puppet strings, statues, embers...) tagged with
themes and emotion affinities. Only beats with no good library match fall
back to a refined live Pexels search.

Reuse tracking (curated_library/reuse_log.json)
-----------------------------------------------
    - the same entry is never used twice inside one video
      (unless the library is exhausted - then spaced >= 4 beats apart)
    - entries used in >= 3 of the last 5 videos are deprioritized
    - the log keeps the last 30 videos; CI commits it back after a win

Clip resolution
---------------
Entry -> clip is resolved through Pexels using the entry's hand-picked
search query, cached under curated_library/cache/<entry_id>.mp4 so each
clip is downloaded once per machine. Pre-download everything with
    python tools/seed_curated_library.py

Everything here is best-effort: any failure returns fewer clips and the
caller falls back (concept-refined Pexels search -> static background).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent / "curated_library"
LIB_FILE = LIB_DIR / "library.json"
CACHE_DIR = LIB_DIR / "cache"
REUSE_FILE = LIB_DIR / "reuse_log.json"

MIN_SCORE = 1.0          # require at least one tag hit to accept an entry
EMOTION_BONUS = 0.75
RECENT_WINDOW, RECENT_MAX = 5, 3          # used in >=3 of last 5 videos -> cool down
SPACING_WITHIN_VIDEO = 4                  # beats between forced repeats
HISTORY_KEEP = 30

_token_cache: dict[str, list[str]] = {}


def _tokens(text: str) -> list[str]:
    if text in _token_cache:
        return _token_cache[text]
    toks = [t for t in re.findall(r"[a-z0-9']+", (text or "").lower())
            if t not in ("the", "a", "an", "of", "and", "with", "in", "on",
                         "is", "its", "it's", "one", "side")]
    _token_cache[text] = toks
    return toks


def load_entries() -> list[dict]:
    """Read the curated manifest (empty list on any problem)."""
    try:
        data = json.loads(LIB_FILE.read_text(encoding="utf-8"))
        entries = [e for e in data.get("entries", []) if e.get("id") and e.get("query")]
        return entries
    except Exception as exc:
        print(f"      curated library: manifest unreadable ({str(exc)[:60]})")
        return []


_SUFFIXES = ("izations", "ization", "izations", "ings", "ingly", "ing",
             "edly", "ed", "ies", "es", "ers", "er", "ly", "s")


def _stem(word: str) -> str:
    """Crude English stemmer - enough for tag matching (rationalizing ->
    rationaliz ~= rationalize, thoughts -> thought, giving -> giving)."""
    w = word.strip("'").lower()
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) - len(suf) >= 5:
            base = w[:-len(suf)]
            if suf == "ies":
                base += "y"
            return base
    return w


def score_entry(entry: dict, concept: str, emotion: str) -> tuple[float, list[str]]:
    """Tag-overlap score between a beat's concept and a library entry."""
    concept_tokens = _tokens(concept)
    if not concept_tokens:
        return 0.0, []
    tags = [t.lower() for t in entry.get("tags", [])]
    hit_tags: list[str] = []
    for tag in tags:
        tag_stem = tag.split()[0].lower()
        ts = _stem(tag_stem)
        for tok in concept_tokens:
            ks = _stem(tok)
            if (tok == tag_stem
                    or (min(len(ts), len(ks)) >= 6
                        and (ts.startswith(ks) or ks.startswith(ts)))):
                hit_tags.append(tag)
                break
    score = float(len(hit_tags))
    if emotion and emotion in entry.get("emotions", []):
        score += EMOTION_BONUS
    return score, hit_tags


def _load_reuse() -> dict:
    try:
        return json.loads(REUSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"videos": {}, "totals": {}}


def _save_reuse(reuse: dict) -> None:
    try:
        LIB_DIR.mkdir(parents=True, exist_ok=True)
        REUSE_FILE.write_text(json.dumps(reuse, indent=2, sort_keys=True),
                              encoding="utf-8")
    except Exception as exc:
        print(f"      curated library: could not save reuse log ({str(exc)[:60]})")


def _recent_video_count(reuse: dict, entry_id: str) -> int:
    """How many of the last RECENT_WINDOW videos used this entry."""
    videos = reuse.get("videos", {})
    recent = list(videos.keys())[-RECENT_WINDOW:]
    return sum(1 for v in recent if entry_id in videos.get(v, {}))


def pick_entries_for_beats(beats: list[dict]) -> tuple[dict[int, dict], list[int]]:
    """Choose a library entry per beat.

    Returns ({beat_index: entry}, [beat indexes with no good match])."""
    entries = load_entries()
    if not entries:
        return {}, list(range(len(beats)))
    reuse = _load_reuse()
    totals = reuse.setdefault("totals", {})
    chosen: dict[int, dict] = {}
    used_ids: set[str] = set()

    for beat in beats:
        concept = beat.get("visual_concept", "")
        emotion = beat.get("emotion", "")
        scored = []
        for entry in entries:
            score, hits = score_entry(entry, concept, emotion)
            if score < MIN_SCORE or not hits:
                continue
            scored.append((score, entry, hits))
        if not scored:
            continue
        # sort: best score first, then least-recently-used, then least-total
        scored.sort(key=lambda s: (-s[0], _recent_video_count(reuse, s[1]["id"]),
                                   totals.get(s[1]["id"], 0), s[1]["id"]))
        pick = None
        for score, entry, hits in scored:
            eid = entry["id"]
            if eid not in used_ids and _recent_video_count(reuse, eid) < RECENT_MAX:
                pick = entry
                break
        if pick is None:                      # all good candidates cooled down
            for score, entry, hits in scored:
                if entry["id"] not in used_ids:
                    pick = entry
                    break
        if pick is None and (beats.index(beat) if beat in beats else 0) >= 0:
            # library exhausted for this video: allow a spaced repeat
            used_list = sorted(used_ids)
            if len(used_list) < len(entries):
                pass                          # should not happen (used_ids subset)
            for score, entry, hits in scored:
                idx = beat.get("index", 0)
                last_use = max((i for i, c in chosen.items()
                                if c["id"] == entry["id"]), default=-10**9)
                if idx - last_use >= SPACING_WITHIN_VIDEO:
                    pick = entry
                    break
        if pick is not None:
            chosen[beat.get("index", len(chosen))] = pick
            used_ids.add(pick["id"])

    missing = [b.get("index", 0) for b in beats
               if b.get("index", 0) not in chosen]
    return chosen, missing


def resolve_clip(entry: dict, used_pexels_ids: set[int]) -> Path | None:
    """Materialize a library entry as a local clip file (cached)."""
    try:
        import pexels_bg
    except Exception:
        return None
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{entry['id']}.mp4"
    if dest.is_file() and dest.stat().st_size > 50_000:
        return dest
    # one shared download helper from pexels_bg (handles search + picking)
    got = pexels_bg.search_and_download(entry["query"], dest, used_pexels_ids)
    return got


def gemini_visual_keywords(beats: list[dict], api_key: str | None,
                           models: list[str] | None = None) -> dict[int, str]:
    """Batched Gemini call: abstract visual keywords for beats that need them.

    Returns {beat_index: "2-3 keyword query"}. Best-effort - empty dict on
    any failure (caller then falls back to cleaning visual_concept)."""
    if not api_key or not beats:
        return {}
    prompt = (
        "For each numbered item, extract 2-3 short visual keywords that "
        "represent the item's underlying concept or mood - NOT the literal "
        "nouns in the sentence. The keywords describe filmable but abstract "
        "footage (e.g. 'quiet resentment building' -> 'one-sided tension, "
        "slow pressure'). Reply with ONE bare JSON object: "
        '{"items": [{"id": <same id>, "keywords": "<2-3 words>"}]}\n\n'
        + "\n".join(f'{b.get("index", i)}. concept: {b.get("visual_concept", "")} '
                    f'| emotion: {b.get("emotion", "")}'
                    for i, b in enumerate(beats))
    )
    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=api_key,
                              http_options=types.HttpOptions(timeout=30000))
        chain = models or ["gemini-flash-latest", "gemini-2.5-flash"]
        for model in chain:
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.3, max_output_tokens=1024,
                        response_mime_type="application/json"))
                text = (resp.text or "").strip()
                data = json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0)
                                  if not text.startswith("{") else text)
                out: dict[int, str] = {}
                for item in data.get("items", []):
                    try:
                        out[int(item.get("id"))] = str(item.get("keywords", ""))[:40]
                    except (TypeError, ValueError):
                        continue
                if out:
                    return out
            except Exception:
                continue
    except Exception:
        pass
    return {}


def ensure_clips_for_beats(beats: list[dict], run_key: str,
                           api_key: str | None = None,
                           models: list[str] | None = None
                           ) -> tuple[dict[int, Path], dict[int, str]]:
    """Full curated-library pipeline for a video.

    Returns ({beat_index: clip_path}, {beat_index: refined_query}) - the
    second dict covers beats the library could not serve (refined queries
    for the live-Pexels fallback; empty strings mean 'no refinement')."""
    chosen, missing = pick_entries_for_beats(beats)
    clips: dict[int, Path] = {}
    refined: dict[int, str] = {}
    used_pexels_ids: set[int] = set()

    if chosen:
        print(f"      curated library: {len(chosen)}/{len(beats)} beats matched "
              f"({', '.join(e['id'] for e in list(chosen.values())[:6])}"
              f"{'...' if len(chosen) > 6 else ''})")
    for idx, entry in chosen.items():
        got = resolve_clip(entry, used_pexels_ids)
        if got:
            clips[idx] = got
        else:
            missing.append(idx)
    if missing:
        # refine fallback queries via Gemini concept extraction, else clean tags
        kw = gemini_visual_keywords([b for b in beats
                                     if b.get("index") in missing],
                                    api_key, models)
        for idx in missing:
            beat = next((b for b in beats if b.get("index") == idx), None)
            concept = (beat or {}).get("visual_concept", "")
            refined[idx] = kw.get(idx) or " ".join(_tokens(concept)[:3])
        print(f"      curated library: {len(missing)} beat(s) unmatched - "
              f"live search fallback for {sorted(refined)}")

    _commit_reuse(run_key, {idx: e["id"] for idx, e in chosen.items()
                            if idx in clips})
    return clips, refined


def _commit_reuse(run_key: str, used: dict[int, str]) -> None:
    if not used:
        return
    reuse = _load_reuse()
    counts: dict[str, int] = {}
    for eid in used.values():
        counts[eid] = counts.get(eid, 0) + 1
    reuse.setdefault("videos", {})[run_key] = counts
    totals = reuse.setdefault("totals", {})
    for eid, n in counts.items():
        totals[eid] = totals.get(eid, 0) + n
    videos = reuse["videos"]
    if len(videos) > HISTORY_KEEP:
        for old_key in sorted(videos.keys())[:-HISTORY_KEEP]:
            for eid, n in videos.pop(old_key).items():
                totals[eid] = max(0, totals.get(eid, 0) - n)
    _save_reuse(reuse)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    entries = load_entries()
    print(f"library: {len(entries)} entries")
    assert len(entries) >= 20, "expected 20-40 curated entries"

    test_beats = [
        {"index": 0, "text": "Doing favors makes people like you less.",
         "emotion": "intense", "visual_concept": "quiet imbalance, one side giving"},
        {"index": 1, "text": "Your brain decides it must like them.",
         "emotion": "serious", "visual_concept": "internal conflict, mental rationalizing"},
        {"index": 2, "text": "Stop helping them. Ask them to help you.",
         "emotion": "triumphant", "visual_concept": "quiet confidence, reversal of power"},
        {"index": 3, "text": "It's called the Ben Franklin effect.",
         "emotion": "curious", "visual_concept": "old handwriting, a written note"},
    ]
    chosen, missing = pick_entries_for_beats(test_beats)
    for idx, e in sorted(chosen.items()):
        score, hits = score_entry(e, test_beats[idx]["visual_concept"],
                                  test_beats[idx]["emotion"])
        print(f"  beat {idx} -> {e['id']:<16} score={score:.2f} tags={hits[:4]}")
    print(f"  unmatched beats: {missing}")
    assert 0 in chosen, "imbalance/giving beat should match weighing_scale"
    assert 1 in chosen, "rationalizing beat should match brain_neurons"
    assert 3 in chosen, "handwriting beat should match hands_writing"
    assert 2 in chosen, "'reversal of power' should match chess_strategy"
    print(f"  (missing beats: {missing or 'none'})")
    print("SELF-TEST OK (no network used)")
