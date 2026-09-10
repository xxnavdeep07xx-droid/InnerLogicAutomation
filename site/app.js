/* InnerLogic Daily Drop — site logic
   data: site/data/manifest.json (committed by the daily workflow)
   animations: Motion (vanilla animate, WAAPI transform springs)             */

const RAW_MANIFEST =
  'https://raw.githubusercontent.com/xxnavdeep07xx-droid/InnerLogicAutomation' +
  '/main/site/data/manifest.json';

const IST = 'Asia/Kolkata';
const fmtDay = new Intl.DateTimeFormat('en-IN',
  { day: 'numeric', month: 'short', year: 'numeric', timeZone: IST });
const fmtFull = new Intl.DateTimeFormat('en-IN',
  { day: 'numeric', month: 'short', year: 'numeric',
    hour: '2-digit', minute: '2-digit', timeZone: IST });

const $ = (id) => document.getElementById(id);
const store = {
  get() { try { return JSON.parse(localStorage.getItem('il_posted') || '{}'); }
          catch { return {}; } },
  set(v) { localStorage.setItem('il_posted', JSON.stringify(v)); },
};

let MANIFEST = { videos: [] };
let CURRENT = 0;
let motion = null;
try { ({ animate: motion } = await import(
  'https://cdn.jsdelivr.net/npm/motion@11.11.17/+esm')); } catch { /* CSS-only */ }
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------- tiny helpers ---------- */
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('on'), 1600);
}

async function copyText(text, label) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text; document.body.appendChild(ta);
    ta.select(); document.execCommand('copy'); ta.remove();
  }
  toast((label || 'Copied') + ' ✓');
}

function dateKey(iso) {                    // IST calendar day, YYYY-MM-DD
  const d = new Date(iso);
  return d.toLocaleDateString('en-CA', { timeZone: IST });
}
function todayKey() { return dateKey(new Date().toISOString()); }

function postedFor(tag) { return store.get()[tag] || {}; }

/* ---------- motion helpers (skill: prefer transform => WAAPI) ---------- */
function entrance(els, baseDelay = 0, step = 0.06) {
  if (!motion || REDUCED || !els.length) return;
  for (let i = 0; i < els.length; i++) {
    motion(els[i],
      { transform: ['translateY(26px) scale(.985)', 'translateY(0px) scale(1)'],
        opacity: [0, 1] },
      { type: 'spring', bounce: 0.18, visualDuration: 0.5,
        delay: baseDelay + i * step });
  }
}

/* ---------- manifest ---------- */
async function loadManifest() {
  const r = await fetch('data/manifest.json?t=' + Date.now(),
                        { cache: 'no-store' });
  if (!r.ok) throw new Error('manifest HTTP ' + r.status);
  MANIFEST = await r.json();
  if (!Array.isArray(MANIFEST.videos)) MANIFEST.videos = [];
}

/* ---------- hero ---------- */
function videoBlock(v) {
  return {
    title: v.title || 'Untitled drop',
    desc: v.description || '',
    tags: v.hashtags && v.hashtags.length ? v.hashtags
          : (v.tags || []).map(t => '#' + String(t).replace(/\s+/g, '')),
    ig: v.ig_caption || '',
  };
}

function select(i, scroll) {
  const vids = MANIFEST.videos;
  if (!vids.length) return;
  CURRENT = Math.max(0, Math.min(i, vids.length - 1));
  const v = vids[CURRENT], b = videoBlock(v);

  const player = $('player');
  player.src = v.video_url || '';
  if (v.thumb_url) player.poster = v.thumb_url;
  $('playerEmpty').classList.remove('on');

  $('videoTitle').textContent = b.title;
  $('videoDesc').textContent = b.desc || 'No description for this drop.';
  $('dropDate').textContent = v.created_at
    ? 'drop · ' + fmtFull.format(new Date(v.created_at))
    : 'drop';
  $('downloadBtn').href = v.video_url || '#';

  const tags = $('videoTags');
  tags.innerHTML = '';
  for (const t of b.tags.slice(0, 12)) {
    const s = document.createElement('span');
    s.className = 'tag'; s.textContent = t;
    tags.appendChild(s);
  }

  const p = postedFor(v.tag);
  $('markYT').checked = !!p.yt;
  $('markIG').checked = !!p.ig;

  for (const [k, el] of [['yt', $('markYT')], ['ig', $('markIG')]]) {
    el.dataset.tag = v.tag; el.dataset.kind = k;
  }

  document.querySelectorAll('.card').forEach((c, idx) =>
    c.classList.toggle('active', idx === CURRENT));

  if (scroll) $('hero').scrollIntoView({ behavior: 'smooth', block: 'start' });
  entrance([$('meta')], 0, 0);
}

/* ---------- status chip + countdown ---------- */
function setStatus() {
  const chip = $('statusChip'), txt = $('statusText');
  const vids = MANIFEST.videos;
  if (!vids.length) {
    chip.className = 'chip chip-wait';
    txt.textContent = 'first drop rendering…';
    return;
  }
  const latest = vids[0];
  if (dateKey(latest.created_at) === todayKey()) {
    chip.className = 'chip chip-ready';
    txt.textContent = "today's drop is ready";
    return;
  }
  chip.className = 'chip chip-wait';
  txt.textContent = countdownText();
  clearInterval(setStatus._h);
  setStatus._h = setInterval(() => {
    if (dateKey(MANIFEST.videos?.[0]?.created_at || '') !== todayKey())
      txt.textContent = countdownText();
    else setStatus();
  }, 30000);
}

function countdownText() {
  const now = new Date();
  const next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
    now.getUTCDate(), 15, 0, 0));
  if (now.getUTCHours() >= 15) next.setUTCDate(next.getUTCDate() + 1);
  const ms = next - now;
  const h = Math.floor(ms / 3.6e6), m = Math.ceil((ms % 3.6e6) / 6e4);
  return `next drop in ${h}h ${m}m (8:30 PM IST)`;
}

/* ---------- stats ---------- */
function renderStats() {
  const vids = MANIFEST.videos;
  const marks = store.get();
  let yt = 0, ig = 0;
  const byDay = {};
  for (const v of vids) {
    const day = v.created_at ? dateKey(v.created_at) : null;
    if (day && !byDay[day]) byDay[day] = v.tag;
    const p = marks[v.tag] || {};
    yt += p.yt ? 1 : 0;
    ig += p.ig ? 1 : 0;
  }
  $('stTotal').textContent = vids.length;
  $('stYt').textContent = yt;
  $('stIg').textContent = ig;

  // streak: consecutive IST days (ending today or yesterday) with a video
  // posted anywhere
  let streak = 0;
  const d = new Date();
  if (!(marks[byDay[dateKey(d.toISOString())]]?.yt ||
        marks[byDay[dateKey(d.toISOString())]]?.ig)) {
    d.setUTCDate(d.getUTCDate() - 1);          // today not posted yet - grace
  }
  for (let i = 0; i < 366; i++) {
    const key = dateKey(d.toISOString());
    const tag = byDay[key];
    const p = tag ? (marks[tag] || {}) : {};
    if (tag && (p.yt || p.ig)) { streak++; d.setUTCDate(d.getUTCDate() - 1); }
    else break;
  }
  $('stStreak').textContent = streak;

  // 30-day calendar
  const cal = $('cal');
  cal.innerHTML = '';
  const today = new Date();
  for (let i = 29; i >= 0; i--) {
    const dd = new Date(today);
    dd.setUTCDate(dd.getUTCDate() - i);
    const key = dateKey(dd.toISOString());
    const tag = byDay[key];
    const cell = document.createElement('div');
    cell.className = 'cell' + (tag ? ' has' : '') +
      (i === 0 ? ' today' : '');
    cell.title = key + (tag ? ' — video ready' : '');
    const p = tag ? (marks[tag] || {}) : {};
    if (p.yt || p.ig) cell.appendChild(document.createElement('i'));
    cal.appendChild(cell);
  }
}

/* ---------- archive ---------- */
function renderGrid() {
  const grid = $('grid');
  grid.innerHTML = '';
  const vids = MANIFEST.videos;
  if (!vids.length) {
    grid.innerHTML = '<p style="color:var(--mut)">Nothing here yet — the ' +
      'first drop appears right after the daily render finishes.</p>';
    return;
  }
  vids.forEach((v, idx) => {
    const p = postedFor(v.tag);
    const card = document.createElement('button');
    card.className = 'card' + (idx === CURRENT ? ' active' : '');
    card.innerHTML =
      `<img class="card-thumb" loading="lazy" alt="" src="${v.thumb_url || ''}">` +
      `<div class="card-body"><div class="card-title"></div>` +
      `<div class="card-date">${v.created_at ? fmtDay.format(new Date(v.created_at)) : ''}</div>` +
      `<div class="card-dots"><i class="yt ${p.yt ? 'on' : ''}"></i>` +
      `<i class="ig ${p.ig ? 'on' : ''}"></i></div></div>`;
    card.querySelector('.card-title').textContent = v.title || 'Untitled';
    card.addEventListener('click', () => select(idx, true));
    grid.appendChild(card);
  });
}

/* ---------- actions ---------- */
function wireActions() {
  const cur = () => videoBlock(MANIFEST.videos[CURRENT] || {});

  $('ytBtn').addEventListener('click', async () => {
    const b = cur();
    const pack = `${b.title}\n\n${b.desc}` +
      (b.tags.length ? `\n\n${b.tags.join(' ')}` : '');
    await copyText(pack, 'YouTube package copied');
    window.open('https://studio.youtube.com/upload', '_blank');
  });

  $('igBtn').addEventListener('click', async () => {
    const b = cur();
    const cap = b.ig || `${b.title}\n\n${b.tags.join(' ')}`;
    await copyText(cap, 'Instagram caption copied');
    window.open('https://www.instagram.com/', '_blank');
  });

  $('copyTitle').addEventListener('click', () => copyText(cur().title, 'Title'));
  $('copyDesc').addEventListener('click', () => copyText(cur().desc, 'Description'));
  $('copyTags').addEventListener('click', () =>
    copyText(cur().tags.join(', '), 'Tags'));
  $('copyIg').addEventListener('click', () =>
    copyText(cur().ig || cur().title, 'IG caption'));

  for (const el of [$('markYT'), $('markIG')]) {
    el.addEventListener('change', () => {
      const tag = el.dataset.tag;
      if (!tag) return;
      const all = store.get();
      const p = all[tag] || {};
      p[el.dataset.kind] = el.checked;
      if (el.checked) p.at = new Date().toISOString();
      all[tag] = p;
      store.set(all);
      renderStats(); renderGrid();
      toast(el.checked ? 'marked as posted ✓' : 'mark removed');
    });
  }
}

/* ---------- bookmarklets ---------- */
function ytFillBody(URL) {
  fetch(URL + '?t=' + Date.now()).then(function (r) { return r.json(); })
    .then(function (m) {
      var v = (m.videos || [])[0];
      if (!v) { alert('InnerLogic: manifest has no videos yet.'); return; }
      var boxes = document.querySelectorAll('#textbox');
      if (!boxes.length) {
        alert('InnerLogic: open the YouTube Studio upload page AFTER adding ' +
          'your file (studio.youtube.com/upload -> drag the video in), ' +
          'then click this bookmark again.');
        return;
      }
      function put(el, t) {
        el.focus();
        var rng = document.createRange();
        rng.selectNodeContents(el);
        var sel = window.getSelection();
        sel.removeAllRanges(); sel.addRange(rng);
        document.execCommand('insertText', false, t);
      }
      put(boxes[0], v.title || '');
      if (boxes[1]) {
        var d = v.description || '';
        if (v.hashtags && v.hashtags.length) d += '\n\n' + v.hashtags.join(' ');
        put(boxes[1], d);
      }
      alert('InnerLogic: title' + (boxes[1] ? ' + description' : '') +
        ' filled. Review and press Next.');
    })
    .catch(function (e) { alert('InnerLogic: fetch failed - ' + e); });
}

function igFillBody(URL) {
  fetch(URL + '?t=' + Date.now()).then(function (r) { return r.json(); })
    .then(function (m) {
      var v = (m.videos || [])[0];
      if (!v) { alert('InnerLogic: manifest has no videos yet.'); return; }
      var cap = v.ig_caption ||
        ((v.title || '') + '\n\n' + (v.hashtags || []).join(' '));
      var ta = document.querySelector('textarea[placeholder]') ||
        document.querySelector('div[role="textbox"]') ||
        document.querySelector('textarea');
      if (!ta) {
        alert('InnerLogic: open the caption step of the Instagram create ' +
          'dialog (where you type the caption), then click this bookmark.');
        return;
      }
      ta.focus();
      if (ta.tagName === 'TEXTAREA') {
        var st = Object.getOwnPropertyDescriptor(
          window.HTMLTextAreaElement.prototype, 'value').set;
        st.call(ta, cap);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
      } else {
        document.execCommand('insertText', false, cap);
      }
      alert('InnerLogic: caption filled - hit Share!');
    })
    .catch(function (e) { alert('InnerLogic: fetch failed - ' + e); });
}

function toBookmarklet(fn) {
  return 'javascript:' + encodeURIComponent(
    '(' + fn.toString() + ')(' + JSON.stringify(RAW_MANIFEST) + ');');
}

function wireBookmarklets() {
  $('bmYT').href = toBookmarklet(ytFillBody);
  $('bmIG').href = toBookmarklet(igFillBody);
  $('bmYT').addEventListener('click', (e) => {
    e.preventDefault();
    toast('Drag me to your bookmarks bar ↑');
  });
  $('bmIG').addEventListener('click', (e) => {
    e.preventDefault();
    toast('Drag me to your bookmarks bar ↑');
  });
}

/* ---------- boot ---------- */
async function boot() {
  wireActions();
  wireBookmarklets();
  try {
    await loadManifest();
  } catch (e) {
    $('statusText').textContent = 'manifest not found - waiting for first render';
    $('playerEmpty').classList.add('on');
    renderStats(); renderGrid();
    return;
  }
  const vids = MANIFEST.videos;
  if (!vids.length) {
    $('statusText').textContent = 'first drop rendering…';
    $('playerEmpty').classList.add('on');
    renderStats(); renderGrid();
    return;
  }
  select(0, false);
  setStatus();
  renderStats();
  renderGrid();
  entrance(
    [$('playerWrap'), $('meta'), $('bookmarklets'), $('stats'),
     document.querySelector('.archive .sec-head'), $('grid')],
    0, 0.07);
  const cards = [...document.querySelectorAll('.card')].slice(0, 10);
  entrance(cards, 0.35, 0.045);
}

boot();
