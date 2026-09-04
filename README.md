# InnerLogic Automation

Fully automated faceless YouTube Shorts / Instagram Reels pipeline
(philosophy & dark psychology niche) — runs **entirely on GitHub's servers**
once a day, no computer of your own needed.

```
Gemini (v2 beats script: hook gate, emotion, emphasis, visual concepts)
  ──> tts_engine (edge | elevenlabs | playht, per-beat emotion prosody + pauses)
        └──> MoviePy assembly (curated footage beats + zoom pulse + ducked mood
              music + whoosh/ding SFX + emphasis captions + hook overlay)
              └──> final_short.mp4
                        ├──> YouTube Shorts  (Data API v3)
                        └──> Instagram Reels (instagrapi)
Gemini also writes the title + 3-5 niche hashtags used by both uploads.
```

---

## Folder structure

```
InnerLogicAutomation/
├── .github/
│   └── workflows/
│       └── daily_video.yml       # GitHub Actions: daily 15:00 UTC run
├── assets/
│   └── background.mp4            # default animated dark gradient (20 s, loops)
├── tools/
│   └── yt_refresh_token.py       # ONE-TIME: create the YouTube refresh token
├── output/                       # generated runs (git-ignored)
│   └── run_YYYYMMDD_HHMMSS/
│       ├── script.txt            #   the voiceover script
│       ├── voiceover.mp3         #   the synthesized voiceover
│       ├── word_timings.json     #   word-by-word timing metadata
│       ├── metadata.json         #   AI title + hashtags used for both uploads
│       ├── _background_9x16.mp4  #   pre-rendered background cache
│       ├── thumbnails/           #   generated thumbnail variants + manifest
│       └── final_short.mp4       #   THE FINISHED VIDEO (1080x1920)
├── main.py                       # one-command pipeline (what CI runs)
├── step1_generate.py             # Gemini beats script -> voiceover -> timings
├── tts_engine.py                 # swappable TTS backends + emotion prosody
├── step2_render_video.py         # MoviePy: beats assembly + SFX + captions
├── step_thumbnail.py             # auto thumbnails (3 variants, 2 sizes each)
├── step3_upload.py               # publish: YouTube Shorts + Instagram Reels
├── curated_library.py            # tagged abstract-clip matcher + reuse tracker
├── curated_library/
│   ├── library.json              #   32 hand-tagged abstract clips (themes)
│   ├── reuse_log.json            #   per-video clip usage (committed back by CI)
│   └── cache/                    #   downloaded clips (git-ignored)
├── sfx_gen.py                    # synthesized whoosh (cuts) + ding (emphasis)
├── music_gen.py                  # procedural mood-reactive dark ambient bed
├── pexels_bg.py                  # Pexels search/download (library + fallback)
├── requirements.txt
├── sample_script.txt             # demo script to test without Gemini
├── sample_beats.json             # demo beats fixture (v2 mode, no Gemini needed)
├── .env.example                  # template for local secrets
├── .env                          # YOUR local key (never committed)
└── .gitignore
```

---

## One-time setup on GitHub (2 minutes)

### 1. Add your Gemini API key as a Secret

1. Open your repo on github.com → **Settings** (top bar)
2. Left sidebar → **Secrets and variables** → **Actions**
3. On the **Secrets** tab click **New repository secret**
4. Name:  `GEMINI_API_KEY`
5. Secret: paste your key from <https://aistudio.google.com/apikey>
6. Click **Add secret**

That's the only *required* secret. Everything below is optional — with no
upload secrets the workflow still renders the video and the publish step is
skipped gracefully.

### 1b. (Optional) Enable dynamic Pexels backgrounds

By default each Short uses one static gradient background. Add this secret
and the video becomes fully contextual:

1. Create a free API key at <https://www.pexels.com/api/> (30 seconds)
2. Add it as a Secret named `PEXELS_API_KEY`

Now every run works like this:

1. **Gemini returns structured beats** (v2 `{title, beats[], cta}`): every
   beat carries `emotion` (drives TTS pitch/rate + music mood),
   `emphasis_words` (drives caption color-pop + SFX dings),
   `visual_concept` (drives footage matching) and `pause_after_ms` (real
   silence in the audio). beats[0] is the hook, gated by a strength
   heuristic (length, banned openers, provocative markers) — weak hooks
   retry with corrective feedback; `HOOK_GATE=off` disables.
2. **Footage is curated first**: each beat's visual_concept is matched
   against `curated_library/library.json` (32 abstract clips tagged with
   themes like mirrors, brains, crowds, embers, puppet strings). Unmatched
   beats fall back to a concept-refined live Pexels search, then to the
   static gradient. A reuse tracker (`curated_library/reuse_log.json`)
   keeps clips rotating across videos (CI commits it back after each run).
3. **The voice is dynamic**: every beat is synthesized with its own
   prosody profile (faster/higher for excitement, slower/lower for serious
   lines) and real pauses between beats. Backends are swappable per run
   (`TTS_BACKEND=edge | elevenlabs | playht` — premium ones need keys in
   `.env`/Secrets and fall back to edge on failure).
4. **Scene cuts land exactly on beat starts** (no more even-split
   approximation), each cut has a short **zoom pulse** (+5% easing back
   over ~0.9 s) on top of the punch-in ladder, and a soft synthesized
   **whoosh** leads into it. Emphasized words get a quiet **ding**
   (whole SFX layer ~-16..-20 dBFS; `SFX=off` to disable).
5. **A mood-matched music bed** is synthesized per script (intense adds a
   pulse, curious brightens the shimmer, triumphant swells...) and
   **ducked to 45% whenever the voice speaks** (`MUSIC_DUCKING=off` to
   disable; `MUSIC_LEVEL` still scales the base volume).
6. **Enhanced captions**: bundled **Anton** typeface, ALL CAPS, the
   spoken word pops in yellow ~10% larger — and words from the script's
   `emphasis_words` metadata pop harder in red (`--emphasis-color`) with a
   thicker glow. Caption groups follow beat boundaries (1-6 words) and sit
   in a **platform-aware safe zone**: pick the export target with
   `--target-platform youtube|instagram|tiktok|generic` and the renderer
   moves the caption anchor to that platform's preset (youtube 52% of
   frame height, instagram 62%, tiktok 60%, generic 58%) and keeps the
   bottom `bottom_clearance_pct` (28%/20%/22%/24%) completely clear —
   because YouTube Shorts' bottom UI (channel + 2-line title + views) is
   taller than Instagram/TikTok's chrome. Horizontally text stays inside
   8% left / 18% right margins and never exceeds **84% of frame width**
   (`CAPTION_MAX_WIDTH_PCT`), so the right-side icon rail can never cover
   a word. Font sizes scale with resolution:
   `CAPTION_FONT_SIZE_PCT=0.065` (70 px at 1080 wide, a 4-5 word line
   spans ~70-80% of the frame) and `TITLE_CARD_FONT_SIZE_PCT=0.075`.
7. **The hook gets its own title card** (default `HOOK_STYLE=title_card`):
   bold yellow hook text alone for ~1.4 s — no captions underneath — then
   a quick fade hands off to the normal word-by-word captions in the same
   safe zone. The card follows the same platform preset and font-size cap
   as the captions. `HOOK_STYLE=word_by_word` renders beat[0] as bigger
   captions with a 110%→100% scale-in instead; `HOOK_STYLE=both` keeps the
   legacy overlay-plus-captions look. Verify placement with a debug
   render: `--safe-zone-guides` draws translucent platform-UI rectangles
   sized to the selected target platform.

### 2. (Optional) Add Variables to pin behavior

Same page → **Variables** tab → **New repository variable**:

| Name               | Example value                  | Purpose                                        |
|--------------------|--------------------------------|------------------------------------------------|
| `VIDEO_TOPIC`      | `the shadow self`              | fixed daily topic; **empty = random topic**    |
| `SCRIPT_MODE`      | `classic`                      | A/B: `beats` (v2 default) or `classic`         |
| `TTS_BACKEND`      | `elevenlabs`                   | voice backend: edge/elevenlabs/playht          |
| `EDGE_TTS_RATE`    | `+15%`                         | speaking rate; keeps the read near 40 s        |
| `EDGE_TTS_VOICE`   | `en-US-AndrewMultilingualNeural`      | any Microsoft neural voice                     |
| `GEMINI_MODEL`     | `gemini-flash-latest`          | force one model; empty = built-in fallback     |
| `BACKGROUND_VIDEO` | `assets/my_bg.mp4`             | path to your own background video in the repo  |
| `YT_PRIVACY`       | `public`                       | YouTube upload privacy: public/unlisted/private |
| `SFX`              | `off`                          | disable the whoosh/ding layer                  |
| `HOOK_GATE`        | `off`                          | accept weak hooks without retry                |
| `CURATED_LIB`      | `off`                          | skip the curated library, go straight to Pexels |
| `MUSIC_DUCKING`    | `off`                          | constant music volume (no sidechain ducking)   |
| `HOOK_OVERLAY`     | `off`                          | disable the hook title card / overlay          |
| `HOOK_STYLE`       | `word_by_word`                 | hook presentation: title_card/word_by_word/both |
| `TARGET_PLATFORM`  | `youtube`                      | safe-zone preset: youtube/instagram/tiktok/generic |
| `CAPTION_Y_PCT`    | `0.70`                         | EXPLICIT anchor override (empty = platform preset) |
| `BOTTOM_CLEARANCE_PCT` | `0.30`                     | EXPLICIT bottom-clearance override (empty = preset) |
| `CAPTION_FONT_SIZE_PCT` | `0.06`                     | caption size as fraction of frame width (default 0.065) |
| `TITLE_CARD_FONT_SIZE_PCT` | `0.08`                 | title-card size as fraction of width (default 0.075) |
| `CAPTION_MAX_WIDTH_PCT` | `0.80`                     | hard cap on caption width (default 0.84) |
| `RENDER_SAFE_ZONE_GUIDES` | `on`                    | debug render with platform-UI guide rectangles  |

**A/B testing per video:** run the workflow manually — the dispatch form
asks for `mode` (beats/classic), `tts_backend`, `sfx`, `hook_style`
(title_card/word_by_word/both), `target_platform`
(youtube/instagram/tiktok/generic), an optional explicit `caption_y`
override and `safe_zone_guides` so you can compare old vs new on a
per-video basis without touching any config.

---

### 3. (Optional) YouTube Shorts auto-upload

Unattended CI can't open a login page, so the upload authenticates with a
long-lived **OAuth 2.0 refresh token**. You create it once with the bundled
helper, paste 3 secrets, done forever:

1. Create a project at console.cloud.google.com → **APIs & Services →
   Library** → enable **YouTube Data API v3**
2. **OAuth consent screen** → External → add yourself as a **Test user**
3. **Credentials → Create credentials → OAuth client ID → Desktop app**
4. On any computer with a browser:
   ```bash
   pip install google-auth-oauthlib
   python tools/yt_refresh_token.py --client-id "XXXX" --client-secret "YYYY"
   ```
   Sign in with the channel's Google account ("Advanced → Go to app" is fine
   — it's your own app) and the **refresh token is printed**.
5. Add these **Secrets**:

   | Secret             | Value                              |
   |--------------------|------------------------------------|
   | `YT_CLIENT_ID`     | the OAuth client ID                |
   | `YT_CLIENT_SECRET` | the OAuth client secret            |
   | `YT_REFRESH_TOKEN` | the printed refresh token          |

The daily run refreshes the token automatically. Optional Variable
`YT_PRIVACY` = `public` (default) / `unlisted` / `private`.

> **Good to know:** each upload costs 1,600 quota units of your 10,000/day
> default (~6 uploads/day). Until Google audits your API project, YouTube
> may lock API uploads to *private* — flip them public in Studio once, or
> request an audit. Videos ≥ 60 s should stay Shorts-sized (~40 s reads).

### 4. (Optional) Instagram Reels auto-upload

Add these **Secrets**:

| Secret             | Value                                                        |
|--------------------|--------------------------------------------------------------|
| `IG_SESSION_JSON`  | **Best** - full session made once at home (see below)         |
| `IG_USERNAME`      | Instagram username (fallback)                                |
| `IG_PASSWORD`      | Instagram password (fallback)                                |

**Why a home-made session:** Instagram aggressively challenges logins from
datacenter IPs (GitHub's runners get error **467**). A password login from
CI will fail. The fix is free: create the session ONCE on your own computer
(your home IP is trusted), then CI reuses it forever:

```bash
pip install instagrapi
python tools/ig_make_session.py        # enter username + password once
# copy the printed JSON into the IG_SESSION_JSON secret
```

(Alternatively grab the browser `sessionid` cookie into the `IG_SESSIONID`
secret, or paste username/password - but those two are the ones that hit
verification challenges.) Use a **dedicated creator account** for automation.
`step3_upload.py` prefers `IG_SESSION_JSON` → `IG_SESSIONID` → password.

---

## Daily operation

- **Automatic:** every day at **15:00 UTC** the workflow renders, saves the
  artifact, **auto-publishes to YouTube Shorts + Instagram Reels** (Gemini
  writes the title + 3–5 niche hashtags), and prints both URLs in the run log.
- **Manual:** repo → **Actions** tab → **Daily Short Video** → **Run workflow**.
- **Download the video:** open the finished run → **Artifacts** section (bottom
  of the run summary) → download `final_short_<run_number>` → unzip →
  `final_short.mp4` is ready to post from your phone.
- Artifacts are kept for **14 days**.
- Publishing without secrets? The publish step prints `SKIPPED` and the run
  still succeeds — add secrets whenever you're ready.

### Clearing the channel (purge)

**Actions → “Purge Channel” → Run workflow**, type `PURGE` in the confirm
box. It lists every video on the uploads playlist and deletes each one
(audited in the log; optionally keep specific IDs via the `keep` input).
This is **permanent** - API deletes bypass the trash.

> The purge needs a refresh token with the `youtube.force-ssl` scope
> (delete is more than upload). The current upload-only token will be
> refused on the first delete with exact instructions: re-run
> `python tools/yt_refresh_token.py` (it now requests upload + force-ssl)
> and update the `YT_REFRESH_TOKEN` secret - a one-time 2-minute fix.

> GitHub disables scheduled workflows after 60 days without repo activity.
> Any commit (or a manual re-enable in the Actions tab) keeps it alive.

---

## Local usage (optional, for testing)

```bash
pip install -r requirements.txt
cp .env.example .env            # paste your GEMINI_API_KEY into .env

python main.py                          # full run, random topic (v2 beats mode)
python main.py --topic "amor fati"      # pick a topic
python main.py --mode classic           # legacy script style (A/B)
python main.py --tts-backend elevenlabs # premium voice (needs key in .env)
python main.py --beats-file sample_beats.json   # v2 fixture, no Gemini needed
python main.py --script-file sample_script.txt  # classic fixture
python main.py --limit 8                # fast 8-second preview render

tools/seed_curated_library.py           # pre-download all 32 curated clips

# publish the newest render locally (reads the same vars from .env):
python step3_upload.py                  # YouTube + Instagram
python step3_upload.py --dry-run        # title + hashtags only, no upload
python step3_upload.py --skip-instagram # YouTube only
```

Put your own `background.mp4` (any resolution, any length) in the project root
to replace the default gradient — landscape videos are center-cropped to 9:16,
short clips are looped.

---

## Automatic thumbnails (step 2.6)

Every run generates **three creative AI-painted thumbnail variants** from the
same script data (no extra script-generation call) and auto-attaches one at
upload time. **Cost: $0** - the artwork comes from the free Pollinations.ai
Flux endpoint (no API key, no account, no billing), and the headline is
typeset locally in the channel's Anton font:

| variant | artwork | text |
|---|---|---|
| `hook` (default) | AI scene matching beat[0]'s concept + emotion | the hook text, shortened to 3-6 punchy ALL-CAPS words (quick Gemini text call when longer than 6 words; local emphasis heuristic offline) |
| `midpoint` | AI scene from the strongest mid-video emotional beat | that beat's text, shortened the same way |
| `clean` | pure atmospheric AI artwork | none |

Design details:

- **No video frames.** Every thumbnail is freshly painted artwork. The model
  renders the scene TEXT-FREE (with clean negative space up top), then the
  caption is typeset deterministically: Anton ALL CAPS, yellow `#F5D90A`
  (hook) or white (midpoint), black stroke + drop shadow + soft scrim,
  auto-fit to <=3 lines and <=84% width - perfect spelling and legibility
  at phone-feed size, no AI-text guessing.
- **Two sizes per variant, cropped not stretched:** `1280x720` (YouTube
  spec, JPG under 2 MB) and `1080x1920` (Instagram Reels cover), each
  generated in its own native aspect ratio.
- **Providers (`THUMB_IMAGE_PROVIDER`):** `auto` (default) = Pollinations
  flux -> local brand-artwork fallback (the pipeline never breaks);
  `pollinations` / `gemini` (paid, if billing is ever enabled) / `local`.
- **Review folder:** `output/<run_id>/thumbnails/` holds all variants plus
  `thumbnail_manifest.json` (per-variant model + caption provenance) and
  tiny `preview_mobile_*` images. CI uploads them as the
  `thumbnails_<run_number>` artifact.
- **Upload wiring:** YouTube gets the 1280x720 via `thumbnails.set` right
  after upload (channel must be phone-verified - one-time fix at
  youtube.com/verify). Instagram gets the 1080x1920 cover via
  `clip_upload(thumbnail=...)`.
- **Per-video config:** `thumbnail_variant` (hook | midpoint | clean) and
  `auto_upload_thumbnail` (true | false - generate + save for review but do
  not attach) are on the workflow-dispatch form and in `.env.example`. To use
  a different variant after review, run step 3 again with
  `--thumbnail path/to/file.jpg`.

---

## Customizing the captions

All of these work on `main.py` or `step2_render_video.py`:

```bash
--mixed-case           # keep normal casing (ALL CAPS is the default)
--highlight none       # plain white captions (no karaoke accent/pop)
--highlight "#00E5FF"  # different karaoke accent color
--emphasis-color "#FF3D2E"  # color for emphasis_words metadata pops (v2)
--max-words 3          # 2-4 words per screen (default 4; beats force splits)
--target-platform youtube   # safe-zone preset: youtube/instagram/tiktok/generic
--caption-y-pct 0.70   # EXPLICIT anchor override (default = platform preset)
--bottom-clearance-pct 0.30  # EXPLICIT bottom-clearance override (default = preset)
--caption-font-size-pct 0.06    # caption size vs frame width (default 0.065)
--title-card-font-size-pct 0.08 # title-card size vs frame width (default 0.075)
--caption-max-width-pct 0.80    # hard width cap vs frame width (default 0.84)
--subtitle-y 0.72      # legacy alias for --caption-y-pct
--font /path/font.ttf  # custom bold font (default: bundled Anton)
--sfx off              # disable whoosh/ding layer
--hook-style word_by_word   # beat[0] without the title card (scale-in captions)
--hook-style both           # legacy: overlay + captions together
--no-hook-overlay      # no hook title card / overlay at all
--safe-zone-guides     # debug: draw platform-UI safe-zone rectangles
--no-punchins          # flat background (no zoom ladder / beat pulse)
```

In CI, add the flag to the `run: python main.py` line of
`.github/workflows/daily_video.yml`.

---

## Cost

GitHub Actions: **free** for public repositories (~2,000 free minutes/month on
private repos — one render uses roughly 8–12 minutes). Gemini free tier and
edge-tts are free.
