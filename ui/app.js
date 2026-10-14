/* "Doğru mu Samet?" — masaüstü arayüzü
   Video sol tarafta oynar; Türkçe dublaj parça parça hazırlandıkça Web Audio ile videoya kilitlenerek çalınır.
   Uyarlamalı buffering: ölçülen işlem hızına göre, takılmadan izlenebilecek ana kadar beklenir. */
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const store = {
  get(k, d) { try { const v = localStorage.getItem("dms." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("dms." + k, JSON.stringify(v)); } catch {} },
};
const KEEP_DECODED = 3;           // bellekte tutulacak çözülmüş parça sayısı
const DRIFT = 0.12;               // sn — bu kadar kayarsa ses yeniden hizalanır

/* ================= oynatıcı: yerel video ya da YouTube — ikisi de aynı arayüzle (currentTime, paused, play…) =========
   YouTube'da görüntü indirilmez, YouTube'un kendi oynatıcısıyla akar; zaman ölçümü ara değerlenir, atlamalar beklenir. */
let ytApiP = null;
function ytApi() {
  if (window.YT?.Player) return Promise.resolve(window.YT);
  if (!ytApiP) ytApiP = new Promise((res, rej) => {
    const prev = window.onYouTubeIframeAPIReady;
    window.onYouTubeIframeAPIReady = () => { try { prev?.(); } catch {} res(window.YT); };
    const sc = document.createElement("script");
    sc.src = "https://www.youtube.com/iframe_api";
    sc.onerror = () => { ytApiP = null; rej(new Error("YouTube oynatıcısı yüklenemedi (internet bağlantısı?)")); };
    document.head.appendChild(sc);
  });
  return ytApiP;
}
class Player extends EventTarget {
  constructor(el) {
    super();
    this.el = el; this.mode = "html"; this.yt = null; this.onFallback = null;
    this._t = 0; this._at = 0; this._state = -1; this._seekTo = null; this._want = false; this._hold = false;
    this._muted = false; this._dur = 0; this._lastRt = null; this._tuAt = 0;
    for (const ev of ["play", "pause", "seeking", "seeked", "timeupdate", "ended", "loadedmetadata", "ratechange", "error"])
      el.addEventListener(ev, () => { if (this.mode === "html") this.dispatchEvent(new Event(ev)); });
    setInterval(() => this._poll(), 100);
  }
  _emit(ev) { this.dispatchEvent(new Event(ev)); }
  get isYT() { return this.mode === "yt"; }
  get currentTime() {
    if (!this.isYT) return this.el.currentTime;
    if (this._seekTo != null) return this._seekTo;
    return this._state === 1 ? this._t + (performance.now() - this._at) / 1000 : this._t;
  }
  set currentTime(t) {
    if (!this.isYT) { this.el.currentTime = t; return; }
    t = Math.max(0, Math.min(+t || 0, (this._dur || 1e9) - 0.05));
    this._seekTo = t; this._t = t; this._at = performance.now();
    if (!this._want) this._hold = true;                 // duraklatılmışken atlama: YouTube kendiliğinden oynamasın
    this._emit("seeking");
    try { this.yt?.seekTo(t, true); } catch {}
    clearTimeout(this._seekT); this._seekT = setTimeout(() => this._seekDone(), 1500);
  }
  _seekDone() {
    if (this._seekTo == null) return;
    clearTimeout(this._seekT);
    this._t = this._seekTo; this._at = performance.now(); this._seekTo = null;
    this._emit("seeked"); this._emit("timeupdate");
  }
  get seeking() { return this.isYT ? this._seekTo != null : this.el.seeking; }
  get paused() { return this.isYT ? this._state !== 1 : this.el.paused; }      // YouTube yüklenirken de "duraklamış"
  get wantPlay() { return this.isYT ? this._want : !this.el.paused; }          // kullanıcı oynatmak istiyor mu
  get duration() { return this.isYT ? (this._dur || 0) : this.el.duration; }
  get muted() { return this.isYT ? this._muted : this.el.muted; }
  set muted(m) {
    if (!this.isYT) { this.el.muted = m; return; }
    m = !!m; if (m === this._muted) return;
    this._muted = m; try { m ? this.yt?.mute() : this.yt?.unMute(); } catch {}
  }
  get playbackRate() { return this.isYT ? 1 : this.el.playbackRate; }
  set playbackRate(r) { if (!this.isYT) this.el.playbackRate = r; }
  play() {
    if (!this.isYT) return this.el.play();
    this._want = true; this._hold = false;
    try { this.yt?.playVideo(); } catch {}
    return Promise.resolve();
  }
  pause() {
    if (!this.isYT) return this.el.pause();
    const was = this._state === 1; this._want = false;
    if (was) { this._t = this.currentTime; this._at = performance.now(); this._state = 2; }
    try { this.yt?.pauseVideo(); } catch {}
    if (was) this._emit("pause");
  }
  _poll() {
    if (!this.isYT || !this.yt?.getCurrentTime) return;
    let rt; try { rt = this.yt.getCurrentTime(); } catch { return; }
    if (typeof rt !== "number") return;
    if (this._seekTo != null) {
      if (Math.abs(rt - this._seekTo) < 0.5 && this._state !== 3 && this._state !== -1) this._seekDone();
      return;
    }
    if (rt !== this._lastRt) {                         // ara değerleme: yalnız belirgin sapmada düzelt (titreme olmasın)
      this._lastRt = rt;
      if (this._state !== 1 || Math.abs(rt - this.currentTime) > 0.3) { this._t = rt; this._at = performance.now(); }
    }
    if (this._state === 1 && performance.now() - this._tuAt > 250) { this._tuAt = performance.now(); this._emit("timeupdate"); }
  }
  _onState(e) {
    const s = e.data, prev = this._state, now = performance.now();
    let rt = 0; try { rt = this.yt.getCurrentTime() || 0; } catch {}
    if (s === 1 && (this._hold || !this._want)) {      // istenmeden oynamaya geçti (atlama sonrası) → durdur
      this._hold = false; this._state = 2;
      try { this.yt.pauseVideo(); } catch {}
      this._t = this._seekTo ?? rt; this._at = now;
      if (this._seekTo != null) this._seekDone();
      return;
    }
    this._state = s;
    if (s === 1) {
      this._t = this._seekTo ?? rt; this._at = now;
      if (this._seekTo != null) this._seekDone();
      if (prev !== 1) this._emit("play");
    } else if (s === 2) {
      this._t = rt; this._at = now;
      if (this._seekTo != null) this._seekDone();
      if (prev === 1) this._emit("pause");
    } else if (s === 3) {
      if (prev === 1) { this._t += (now - this._at) / 1000; this._at = now; }
    } else if (s === 0) {
      this._t = this._dur || rt; this._at = now; this._want = false;
      this._emit("pause"); this._emit("ended");
    }
  }
  _destroyYT() {
    try { this.yt?.destroy(); } catch {}
    this.yt = null; $("#ytBox").innerHTML = '<div id="ytPlayer"></div>';
  }
  loadHtml(url) {
    this._destroyYT(); this.mode = "html"; canvas.classList.remove("yt");
    this.el.src = url;
  }
  async loadYouTube(id, dur) {
    this.el.pause(); this.el.removeAttribute("src"); this.el.load();
    this._destroyYT(); this.mode = "yt"; canvas.classList.add("yt");
    Object.assign(this, { _dur: dur || 0, _state: -1, _t: 0, _at: performance.now(), _seekTo: null, _want: false, _hold: false, _muted: false, _lastRt: null });
    const token = (this._token = (this._token || 0) + 1);
    let YTns;
    try { YTns = await ytApi(); } catch (e) { this.onFallback?.(e.message); return; }
    if (token !== this._token || !this.isYT) return;
    this.yt = new YTns.Player("ytPlayer", {
      videoId: id, host: "https://www.youtube.com", width: "100%", height: "100%",
      playerVars: { autoplay: 0, controls: 0, disablekb: 1, fs: 0, iv_load_policy: 3, modestbranding: 1, rel: 0,
                    playsinline: 1, cc_load_policy: 0, enablejsapi: 1, origin: location.origin, widget_referrer: location.origin },
      events: {
        onReady: () => {
          if (token !== this._token) return;
          try { this._dur = this.yt.getDuration() || this._dur; if (this._muted) this.yt.mute(); else this.yt.unMute(); } catch {}
          this._noCC();
          this._emit("loadedmetadata");
        },
        onStateChange: (e) => { if (token === this._token) { this._noCC(); this._onState(e); } },
        onApiChange: () => { if (token === this._token) this._noCC(); },
        onError: (e) => { if (token === this._token) this.onFallback?.(e.data); },
      },
    });
  }
  // YouTube'un kendi altyazısını kapat (kullanıcı tercihi açık olsa bile; altyazı modülü oynatmada yeniden yüklenebilir)
  _noCC() {
    const y = this.yt;
    if (!y) return;
    for (const m of ["captions", "cc"]) {
      try { y.setOption?.(m, "track", {}); } catch {}
      try { y.unloadModule?.(m); } catch {}
    }
  }
}
const canvas = $("#canvas"), subList = $("#subList");
const vid = new Player($("#vid"));
const S = {
  video: null,                    // {video_id, name, duration, url}
  mode: store.get("mode", "hizli"), gender: store.get("gender", "erkek"), sep: store.get("sep", "uvr"), mt: store.get("mt", "milmmt4"), tts: store.get("tts", "std"),
  av: null,                       // videoya bağlı (çalan) dublaj varyantı
  status: null, chunks: new Map(),// k -> {info, buf, loading, used}
  segs: [], active: -1, lang: "tr", cc: true,
  playing: new Map(), anchor: null, lastK: -1, bufPaused: false, gate: true, skipGate: false,
  order: [], cmap: new Map(), bufOn: true,
  userScroll: 0, polling: false, info: null,
};
window.__dms = S;
/* varyant: "<mod>[-<ayırma>][~<çeviri modeli>]" — sunucu dosyaları buna göre ayırır */
const MT_DEFAULT = "milmmt4";                          // varsayılan çeviri modeli (Hızlı da bunu kullanır)
const variant = () => S.mode === "detayli"
  ? "detayli" + (S.sep && S.sep !== "uvr" ? "-" + S.sep : "") + (S.mt && S.mt !== MT_DEFAULT ? "~" + S.mt : "") +
    (S.tts === "vox" ? "~vox" : "")
  : "hizli-" + S.gender;
const parseV = (v) => { const [base, ...opts] = String(v || "").split("~"); return { base, det: base.startsWith("detayli"),
  sep: base.startsWith("detayli-") ? base.split("-")[1] : "uvr", mt: opts.find((o) => o !== "vox") || MT_DEFAULT,
  tts: opts.includes("vox") ? "vox" : "std" }; };
const fmt = (t) => { t = Math.max(0, t | 0); const h = (t / 3600) | 0, m = ((t % 3600) / 60) | 0, s = t % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : String(m).padStart(2, "0")) + ":" + String(s).padStart(2, "0"); };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let toastT;
function toast(msg, err = false) {
  const t = $("#toast"); t.textContent = msg; t.classList.toggle("err", err); t.classList.add("show");
  clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), err ? 6000 : 3000);
}
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.ok === false) throw new Error(j.error || "İstek başarısız (" + r.status + ")");
  return j;
}

/* ================= ses motoru ================= */
const ctx = new (window.AudioContext || window.webkitAudioContext)();
const master = ctx.createGain(); master.connect(ctx.destination);
const gains = {}; for (const ch of ["dub", "bg", "en"]) { gains[ch] = ctx.createGain(); gains[ch].connect(master); }

/* parçalar dinamik planlanır: kimlik (i) zaman sırası değildir */
const cmeta = (id) => S.cmap.get(id);
function chunkAt(t) {
  for (const c of S.order) if (t >= c.start - 0.08 && t < c.end) return c.i;   // yuvarlama artığı boşluklar sayılmaz
  return -1;
}
function nextOf(id) {
  const c = cmeta(id); if (!c) return -1;
  const n = S.order.find((x) => x.i !== id && Math.abs(x.start - c.end) < 0.08);
  return n ? n.i : -1;
}
function indexStatus(st) {
  S.order = st.chunks; S.cmap = new Map(st.chunks.map((c) => [c.i, c]));
}
/* çalan parça: {srcs, live} */
function killEntry(e) {
  for (const s of e.srcs) { try { s.onended = null; s.stop(); } catch {} }
}
function stopAll() {
  for (const e of S.playing.values()) killEntry(e);
  S.playing.clear(); S.anchor = null;
}
function startChunk(k) {
  const c = S.chunks.get(k), meta = cmeta(k);
  if (!c?.buf || !meta || S.playing.has(k)) return;
  const now = ctx.currentTime;
  let when = S.anchor + meta.start, off = 0;
  if (when < now) { off = now - when; when = now; }
  if (off >= c.buf.dub.duration - 0.02) return;
  const e = { k, srcs: [], live: 0 };
  const play = (b, dst) => playSrc(e, b, dst, when, off);
  play(c.buf.dub, gains.dub); play(c.buf.bg, gains.bg); play(c.buf.en, gains.en);
  S.playing.set(k, e); c.used = performance.now();
}
/* tüm sesleri bitince parça "çalan" listesinden düşer */
function playSrc(e, b, dst, when, off) {
  if (!b || off >= b.duration) return null;
  const s = ctx.createBufferSource(); s.buffer = b; s.connect(dst); s.start(when, off);
  e.srcs.push(s); e.live++;
  s.onended = () => { if (--e.live <= 0 && S.playing.get(e.k) === e) { S.playing.delete(e.k); killEntry(e); } };
  return s;
}
function sync() {
  const now = ctx.currentTime, t = vid.currentTime;
  if (S.anchor == null || Math.abs(now - S.anchor - t) > DRIFT) { stopAll(); S.anchor = now - t; }
  const k = chunkAt(t);
  startChunk(k);
  const n = nextOf(k);
  if (n >= 0 && cmeta(n).start - t < 4) startChunk(n);
}

async function loadChunkInfo(k) {
  let c = S.chunks.get(k);
  if (!c) { c = { info: null, buf: null, loading: null, used: 0 }; S.chunks.set(k, c); }
  if (c.info || c.infoLoading) return c;
  c.infoLoading = true;
  const av = S.av;
  try {
    const j = await api(`/api/chunk/${S.video.video_id}/${k}?variant=${av}`);
    if (av !== S.av) return c;
    c.info = j; addSegments(j);
  } catch { /* hazır değil */ } finally { c.infoLoading = false; }
  return c;
}
async function decodeChunk(k) {
  const c = await loadChunkInfo(k);
  if (!c.info || c.buf || c.loading) return;
  const av = S.av;
  c.loading = (async () => {
    const get = async (u) => (u ? ctx.decodeAudioData(await (await fetch(u)).arrayBuffer()) : null);
    const a = c.info.audio;
    const [dub, bg, en] = await Promise.all([get(a.dub), get(a.bg), get(a.en)]);
    if (av !== S.av) return;
    c.buf = { dub, bg, en }; c.used = performance.now();
    evict();
  })().catch((e) => { console.error(e); toast("Parça sesi yüklenemedi: " + e.message, true); })
    .finally(() => { c.loading = null; tick(); });
}
function evict() {
  const t = vid.currentTime, cur = chunkAt(t), nx = nextOf(cur);
  const dec = [...S.chunks.entries()].filter(([, c]) => c.buf);
  if (dec.length <= KEEP_DECODED) return;
  const dist = (k) => { const m = cmeta(k); return m ? Math.min(Math.abs(m.start - t), Math.abs(m.end - t)) : 1e9; };
  dec.sort((a, b) => dist(b[0]) - dist(a[0]));
  for (const [k, c] of dec.slice(0, dec.length - KEEP_DECODED)) {
    if (k === cur || k === nx) continue;
    const e = S.playing.get(k); if (e) { killEntry(e); S.playing.delete(k); }
    c.buf = null;
  }
}
function wantChunks() {
  if (!S.av || !S.status) return;
  const k = chunkAt(vid.currentTime);
  for (const j of [k, nextOf(k)]) if (j >= 0 && cmeta(j)?.state === "hazir") decodeChunk(j);
}

const playable = () => S.av && S.status && (S.status.buffering || S.status.complete);
const srvRange = () => S.status?.range || [0, dur()];
const inRange = (t) => { const [a, b] = srvRange(); return t >= a - 0.01 && t < b; };

/* ================= oynatma döngüsü ================= */
function hold(msg) {
  if (vid.wantPlay) { S.bufPaused = true; vid.pause(); }
  stopAll(); setBuffer(true, msg);
}
function tick() {
  if (S.scrub) { drawSeek(); $("#tCur").textContent = fmt(S.scrub.t); setBuffer(false); return; }
  enforceRange();
  const t = vid.currentTime || 0;
  drawSeek(t); updateSubs(t); voiceTalk(t);
  $("#tCur").textContent = fmt(t);
  $("#playBtn").classList.toggle("playing", vid.wantPlay || S.bufPaused);
  if (!S.av || !S.status) { setBuffer(false); return; }
  if (!playable()) {                 // buffering kapalı: dublaj hepsi bitince bağlanır, şimdilik özgün ses
    setMixer(false); stopAll(); setBuffer(false);
    if (S.bufPaused) { S.bufPaused = false; playVideo(); }
    return;
  }
  if (S.autoplay) {                  // dublaj başlatıldı: hazır olunca kendiliğinden oynar
    S.autoplay = false;
    if (!vid.wantPlay && !S.bufPaused) {
      const [a, b] = bounds();
      if (t >= b - 0.1) vid.currentTime = a;
      S.bufPaused = true; S.gate = true; S.skipGate = false;
    }
  }
  if (!inRange(t)) {                 // seçili aralığın dışı: özgün ses
    setMixer(false); stopAll(); setBuffer(false);
    if (S.bufPaused) { S.bufPaused = false; playVideo(); }
    return;
  }
  setMixer(true);
  const k = chunkAt(t);
  if (k !== S.lastK) { S.lastK = k; postFocus(t); wantChunks(); }
  const c = k >= 0 ? S.chunks.get(k) : null;
  if (!c?.buf) {
    S.gate = true;
    const meta = k >= 0 ? cmeta(k) : null;
    let msg = "Dublaj hazırlanıyor";
    if (meta?.state === "hazir") msg = "Ses yükleniyor…";
    else if (meta && S.status.current?.chunk === k) {
      const cs = S.status.current.stages.find((x) => x.status === "calisiyor" || x.status === "indiriliyor");
      if (cs) msg += ` · ${cs.name}${cs.progress ? " %" + Math.round(cs.progress) : ""}`;
    } else if (!S.status.running) msg = S.status.stopped ? "Dublaj durduruldu" : "Bu bölüm henüz dublajlanmadı";
    hold(msg);
    return;
  }
  setBuffer(false);
  if (S.bufPaused) { S.bufPaused = false; playVideo(); return; }
  if (vid.paused || vid.seeking) { stopAll(); return; }
  sync();
}
function setBuffer(on, msg) {
  $("#buffering").hidden = !on;
  if (on) $("#bufText").textContent = msg;
}
async function playVideo() {
  if (ctx.state === "suspended") await ctx.resume().catch(() => {});
  vid.play().catch(() => {});
}
function togglePlay() {
  if (!S.video) return;
  S.autoplay = false;
  if (!vid.wantPlay && !S.bufPaused) {
    const [a, b] = bounds();
    if (S.range && vid.currentTime >= b - 0.1) vid.currentTime = a;    // bitiş çubuğunda: başlangıçtan oynat
    S.gate = true; playVideo();
  }
  else { S.bufPaused = false; vid.pause(); stopAll(); tick(); }
}
let focusT;
function postFocus(t) {
  if (!S.av || !S.video) return;
  clearTimeout(focusT);
  focusT = setTimeout(() => api("/api/focus", { video_id: S.video.video_id, variant: S.av, t }).catch(() => {}), 150);
}
setInterval(tick, 200);
vid.addEventListener("play", tick);
vid.addEventListener("pause", () => { stopAll(); tick(); });
vid.addEventListener("seeking", () => { stopAll(); });
vid.addEventListener("timeupdate", () => enforceRange());
vid.addEventListener("seeked", () => {
  if (S.scrub) {                                           // tutamaç sürükleniyor: son konuma yetiş
    if (S.scrub.to != null && Math.abs(vid.currentTime - S.scrub.to) > 0.04) vid.currentTime = S.scrub.to;
    return;
  }
  S.gate = true; S.skipGate = false; postFocus(vid.currentTime); wantChunks(); S.userScroll = 0; tick(); });
vid.addEventListener("ratechange", () => { if (vid.playbackRate !== 1) vid.playbackRate = 1; });
vid.addEventListener("ended", () => { stopAll(); S.bufPaused = false; tick(); });
vid.addEventListener("loadedmetadata", () => { $("#tDur").textContent = fmt(vid.duration); drawRanges(); });
vid.addEventListener("error", () => toast("Video bu pencerede oynatılamıyor (kodek desteklenmiyor olabilir).", true));

/* ================= arama çubuğu ================= */
const seek = $("#seek");
const dur = () => S.video?.duration || vid.duration || 0;
/* çubuklar sınırdır: oynatma noktası (yuvarlak) her zaman başlangıç–bitiş çubuklarının arasında kalır */
const bounds = () => { const d = dur(); return S.range ? [S.range[0], S.range[1]] : [0, d]; };
/* buffering açıkken dublaj sürerken yuvarlak yalnız hazır (render alınmış) kısımlarda gezebilir */
const lockToReady = () => !!(S.av && S.status && S.status.variant === S.av && S.status.buffering && !S.status.complete);
function readySpans() {
  const [a, b] = bounds(), out = [];
  for (const c of S.order) {
    if (c.state !== "hazir") continue;
    const s0 = Math.max(a, c.start), e0 = Math.min(b, c.end);
    if (e0 - s0 < 0.1) continue;
    const last = out[out.length - 1];
    if (last && s0 - last[1] < 0.1) last[1] = Math.max(last[1], e0); else out.push([s0, e0]);
  }
  return out;
}
const clampR = (t) => { const d = dur(), [a, b] = bounds(); return Math.max(a, Math.min(t, b, d - 0.05)); };
function clampT(t) {
  t = clampR(t);
  if (!lockToReady()) return t;
  const sp = readySpans();
  if (!sp.length) return t;                               // henüz hiçbir parça hazır değil: serbest
  let best = t, bd = Infinity;
  for (const [s0, e0] of sp) {
    const hi = Math.max(s0, e0 - 0.05);
    if (t >= s0 && t <= hi) return t;
    const c = t < s0 ? s0 : hi, dd = Math.abs(c - t);
    if (dd < bd) { bd = dd; best = c; }
  }
  return best;
}
function enforceRange() {
  if (!S.video || S.scrub || !S.range || vid.seeking) return;
  const [a, b] = bounds(), t = vid.currentTime;
  if (t < a - 0.05) { vid.currentTime = a; return; }
  if (t >= b - 0.02) {
    if (vid.wantPlay || S.bufPaused) { S.bufPaused = false; vid.pause(); stopAll(); }
    if (t > b + 0.05) vid.currentTime = b;
  }
}
function drawSeek(t) {
  if (S.scrub) t = S.scrub.t;
  const p = dur() ? Math.min(100, (t / dur()) * 100) : 0;
  $("#seekPlayed").style.width = p + "%"; $("#seekThumb").style.left = p + "%";
}
function drawRanges() {
  const box = $("#seekRanges"), d = dur();
  if (!S.status || !d || !S.av) { box.innerHTML = ""; return; }
  box.innerHTML = S.order.filter((c) => c.state !== "bekliyor").map((c) =>
    `<i class="${c.state === "isleniyor" ? "proc" : ""}" style="left:${(c.start / d) * 100}%;width:${((c.end - c.start) / d) * 100}%"></i>`).join("");
}
function seekTo(ev) {
  const r = seek.getBoundingClientRect(), d = dur(); if (!d) return;
  vid.currentTime = clampT(((ev.clientX - r.left) / r.width) * d); drawSeek(vid.currentTime);
}
seek.addEventListener("pointerdown", (e) => { if (!S.video) return; seek.setPointerCapture(e.pointerId); seekTo(e);
  const mv = (ev) => seekTo(ev), up = () => { seek.removeEventListener("pointermove", mv); seek.removeEventListener("pointerup", up); };
  seek.addEventListener("pointermove", mv); seek.addEventListener("pointerup", up); });

/* ================= aralık (başlangıç / bitiş çubukları) =================
   Çubuk sürüklenirken video o anki sahneyi gösterir; bırakınca yuvarlağın yerine döner.
   Çubuk yuvarlağa gelirse yuvarlağı da önüne katar (yuvarlak hiçbir zaman çubukların dışına çıkmaz). */
S.range = null;                                          // null: tüm video
const rangeOrNull = () => (S.range && (S.range[0] > 0.5 || S.range[1] < dur() - 0.5) ? [S.range[0], S.range[1]] : null);
function drawRange() {
  const d = dur(), [a, b] = S.range || [0, d];
  const pa = d ? (a / d) * 100 : 0, pb = d ? (b / d) * 100 : 100;
  $("#rngA").style.left = pa + "%"; $("#rngB").style.left = pb + "%";
  $("#rngL").style.width = pa + "%"; $("#rngR").style.left = pb + "%"; $("#rngR").style.width = (100 - pb) + "%";
  const full = !rangeOrNull();
  $("#rangeRow").hidden = !S.video;
  $("#rangeTxt").textContent = full ? `Tüm video · ${fmt(d)}` : `${fmt(a)} – ${fmt(b)} · ${fmt(b - a)}`;
  $("#rangeAll").hidden = full;
  $("#rngA").dataset.t = fmt(a); $("#rngB").dataset.t = fmt(b);
}
function scrubTo(t) { S.scrub.to = t; if (!vid.seeking) vid.currentTime = t; }
function rangeDrag(which, e) {
  if (!S.video) return;
  e.preventDefault(); e.stopPropagation();
  const h = e.currentTarget; h.setPointerCapture(e.pointerId); h.classList.add("drag");
  const d = dur(); if (!S.range) S.range = [0, d];
  const orig = clampT(vid.currentTime), wasPlaying = vid.wantPlay || S.bufPaused;
  S.bufPaused = false;
  if (vid.wantPlay) vid.pause();
  stopAll();
  S.scrub = { t: orig, to: null };
  const place = () => { S.scrub.t = Math.max(S.range[0], Math.min(orig, S.range[1], d - 0.05)); drawSeek(); };
  let moved = false;
  const mv = (ev) => {
    const r = seek.getBoundingClientRect();
    const t = Math.max(0, Math.min(d, ((ev.clientX - r.left) / r.width) * d));
    if (which === 0) S.range[0] = Math.max(0, Math.min(t, S.range[1] - 10));
    else S.range[1] = Math.min(d, Math.max(t, S.range[0] + 10));
    moved = true; place(); drawRange();
    scrubTo(Math.min(which === 0 ? S.range[0] : S.range[1], d - 0.05));
  };
  const up = () => {
    h.removeEventListener("pointermove", mv); h.removeEventListener("pointerup", up); h.removeEventListener("pointercancel", up);
    h.classList.remove("drag");
    const target = moved ? clampT(S.scrub.t) : S.scrub.t; S.scrub = null;
    if (moved) vid.currentTime = target;
    drawSeek(target); tick();
    if (wasPlaying) { S.gate = true; playVideo(); }
    if (!rangeOrNull()) S.range = null;
    if (moved) rangeChanged();
  };
  h.addEventListener("pointermove", mv); h.addEventListener("pointerup", up); h.addEventListener("pointercancel", up);
  place();
}
$("#rngA").addEventListener("pointerdown", (e) => rangeDrag(0, e));
$("#rngB").addEventListener("pointerdown", (e) => rangeDrag(1, e));
$("#rangeAll").addEventListener("click", () => { S.range = null; drawRange(); rangeChanged(); });
/* Aralık değişince hemen işlenmez: düğme “Dublajı Başlat” olur, basınca seçili aralık baştan dublajlanır. */
function rangeChanged() {
  drawRange(); drawDubPanel();
  if (rangeDirty()) $("#pDub").classList.remove("folded");     // “Dublajı Başlat” görünsün
}
const fullRange = () => (S.range ? [S.range[0], S.range[1]] : [0, dur()]);
function rangeDirty() {
  if (!S.av || !S.status?.range) return false;
  const [a, b] = fullRange(), [sa, sb] = S.status.range;
  return Math.abs(a - sa) > 0.05 || Math.abs(b - sb) > 0.05;
}
function adoptRange(st) {                                 // sunucu cümle boşluğuna oturttu: çubuklar oraya gelir
  if (!st?.range) return;
  const d = dur(), [a, b] = st.range;
  S.range = a > 0.5 || b < d - 0.5 ? [a, b] : null;
  drawRange(); enforceRange();
}

/* ================= mikser ================= */
const mix = { dub: 70, bg: 150, en: 0, mute: {} };   // her açılışta standart seviyeler
for (const el of $$(".ch")) {
  const ch = el.dataset.ch, r = $("input", el), o = $("output", el);
  const apply = () => {
    const v = +r.value; r.style.setProperty("--p", (v / 150) * 100 + "%"); o.textContent = v + "%";
    const muted = !!mix.mute[ch]; el.classList.toggle("muted", muted);
    gains[ch].gain.setTargetAtTime(muted ? 0 : v / 100, ctx.currentTime, 0.02);
    mix[ch] = v;
  };
  r.value = mix[ch] ?? r.value;
  r.addEventListener("input", apply);
  $(".chbtn", el).addEventListener("click", () => { mix.mute[ch] = !mix.mute[ch]; apply(); });
  apply();
}
function setMixer(on) { $("#mixer").dataset.off = on ? "0" : "1"; vid.muted = !!on; }
$("#ccBtn").addEventListener("click", () => { S.cc = !S.cc; $("#ccBtn").classList.toggle("on", S.cc); S.active = -2; tick(); });

/* ================= altyazılar ================= */
function addSegments(info) {
  S.segs = S.segs.filter((s) => s.k !== info.i).concat(info.segments.map((s, j) => ({ ...s, k: info.i, j, spk: spkOf(info.i, j, s.g) })));
  S.segs.sort((a, b) => a.start - b.start);
  renderSubs();
}
function renderSubs() {
  drawVoices();
  if (!S.segs.length) {
    subList.innerHTML = `<div class="empty">${S.av ? "İlk parça hazırlanıyor; metin hazır oldukça burada akacak." : "Dublaj hazırlandıkça metin burada video ile birlikte akar."}</div>`;
    S.active = -1; return;
  }
  const det = !!S.av && (S.av.startsWith("detayli") || S.av === "hizli-oto");
  const sel = voiceFilter();                          // seçili ses(ler): yalnız onların cümleleri
  let html = "", prevK = null;
  S.segs.forEach((s, i) => {
    if (sel && !sel.has(s.spk)) return;
    if (prevK != null && s.k !== prevK) {
      const a = cmeta(prevK), b = cmeta(s.k);
      if (!a || !b || Math.abs(a.end - b.start) > 0.01) html += `<div class="subsep">· · ·</div>`;
    }
    prevK = s.k;
    const sg = spkInfo(s.spk)?.g || s.g;
    const g = det && sg ? `<span class="g ${sg}" style="color:${spkColor(s.spk) || ""}">${sg === "kadin" ? "♀" : "♂"}</span>` : "";
    html += `<div class="sub" data-i="${i}"><span class="t">${fmt(s.start)}${g}</span><span class="x">${esc(S.lang === "tr" ? (s.tr || s.en) : s.en)}</span></div>`;
  });
  if (!html) html = `<div class="empty">Seçili seste cümle yok.</div>`;
  if (S.status && !S.status.complete) html += `<div class="subsep">Sonraki bölümler hazırlanıyor…</div>`;
  const keep = subList.scrollTop;
  subList.innerHTML = html; subList.scrollTop = keep;
  S.active = -2;
}
/* ================= Sesler: kişi noktaları (Detaylı, dublaj bitince) =================
   Her kadın için bir pembe, her erkek için bir mavi nokta (en çok 10'ar); en çok konuşandan en aza sıralı.
   Kişi ayırma yapılamazsa yalnız iki nokta: kadın / erkek. */
S.voiceSel = new Set();
S.spk = null;                                            // {speakers:[{id,g,rank,words,n}], assign:{k:[id]}}
const SPK_COLORS = {
  kadin: ["#ff8fc7", "#d6337f", "#ffbfdf", "#a3195b", "#f06fb0", "#ff5c9e", "#e5a1c4", "#c2477f", "#ffd6ea", "#7d1046"],
  erkek: ["#6fb0ff", "#2470d6", "#a8d0ff", "#0f4fa8", "#4f9cf5", "#87c3ff", "#3d7fc4", "#1c5fb8", "#cfe5ff", "#0a3a7a"],
};
const spkInfo = (id) => S.spk?.speakers?.find((x) => x.id === id) || (id === "kadin" || id === "erkek" ? { id, g: id, rank: 1 } : null);
const spkColor = (id) => { const x = spkInfo(id); return x ? SPK_COLORS[x.g][x.rank - 1] : ""; };
function spkOf(k, j, g) {                                 // cümlenin kişisi (kişi ayırma yoksa cinsiyeti)
  const a = S.spk?.assign?.[String(k)];
  return (a && a[j]) || g || null;
}
const voicesOn = () => !!(S.av && (S.av.startsWith("detayli") || S.av === "hizli-oto") && S.status?.variant === S.av && S.status.complete && S.segs.some((s) => s.g));
const voiceFilter = () => (S.voiceSel.size && voicesOn() ? S.voiceSel : null);
async function loadSpeakers() {
  if (S.spkLoading || S.spk) return;
  S.spkLoading = true;
  const av = S.av, vidId = S.video?.video_id;
  try {
    const r = await api(`/api/speakers/${vidId}?variant=${av}`);
    if (av !== S.av || vidId !== S.video?.video_id) return;
    S.spk = r.speakers ? r : { speakers: null };
    for (const s of S.segs) s.spk = spkOf(s.k, s.j, s.g);
    renderSubs();
  } catch { S.spk = { speakers: null }; } finally { S.spkLoading = false; }
}
function drawVoices() {
  const p = $("#pVoices"), on = voicesOn();
  p.hidden = !on;
  if (!on) return;
  if (!S.spk) { loadSpeakers(); return; }
  const list = S.spk.speakers?.length ? S.spk.speakers
    : ["kadin", "erkek"].filter((g) => S.segs.some((s) => s.g === g)).map((g) => ({ id: g, g, rank: 1, n: S.segs.filter((s) => s.g === g).length }));
  const ids = new Set(list.map((x) => x.id));
  for (const id of [...S.voiceSel]) if (!ids.has(id)) S.voiceSel.delete(id);
  const key = list.map((x) => x.id).join(",");
  const box = $(".vbody", p);
  if (box.dataset.key !== key) {                          // yalnız kişi listesi değişince yeniden çiz
    box.dataset.key = key;
    box.innerHTML = list.map((x) => {
      const who = (x.g === "kadin" ? "Kadın" : "Erkek") + (S.spk.speakers ? " " + x.rank : "");
      return `<button type="button" class="vdot ${x.g}" data-s="${x.id}" style="--vc:${SPK_COLORS[x.g][x.rank - 1]}" title="${who} · ${x.n} cümle"><i></i></button>`;
    }).join("");
  }
  for (const b of $$(".vdot", box)) b.classList.toggle("sel", S.voiceSel.has(b.dataset.s));
  $("#voiceSum").textContent = S.voiceSel.size ? "Seçimi kaldırmak için çembere tıklayın" : "Bir kişiye tıklayın: yalnız onun cümleleri";
}
function voiceTalk(t) {                                 // konuşan kişinin noktası yanıp söner
  const p = $("#pVoices");
  if (p.hidden) return;
  const s = !vid.paused ? S.segs[segAt(t)] : null;
  const id = s && t >= (s.ds ?? s.start) && t <= (s.de ?? s.end) ? s.spk : null;
  for (const b of $$(".vdot", p)) b.classList.toggle("talk", b.dataset.s === id);
}
$("#pVoices").addEventListener("click", (e) => {
  const b = e.target.closest(".vdot"); if (!b) return;
  const id = b.dataset.s;
  if (S.voiceSel.has(id)) S.voiceSel.delete(id); else S.voiceSel.add(id);
  subList.scrollTop = 0; S.userScroll = 0;
  drawVoices(); renderSubs(); tick();
});
function resetVoices() { S.voiceSel.clear(); S.spk = null; $("#pVoices .vbody").dataset.key = ""; drawVoices(); }
function segAt(t) {
  let lo = 0, hi = S.segs.length - 1, ans = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (S.segs[m].start <= t) { ans = m; lo = m + 1; } else hi = m - 1; }
  if (ans < 0) return -1;
  const s = S.segs[ans], end = Math.max(s.end, s.de ?? 0);
  return t <= end + 0.4 ? ans : -1;
}
function updateSubs(t) {
  const i = S.av ? segAt(t) : -1;
  const cap = $("#caption");
  if (i !== S.active) {
    $(".sub.on", subList)?.classList.remove("on");
    const el = i >= 0 ? subList.querySelector(`.sub[data-i="${i}"]`) : null;
    if (el) {
      el.classList.add("on");
      if (performance.now() - S.userScroll > 4000) {
        const top = el.offsetTop - subList.clientHeight / 2 + el.offsetHeight / 2;
        subList.scrollTo({ top, behavior: Math.abs(subList.scrollTop - top) > subList.clientHeight * 2 ? "auto" : "smooth" });
      }
    }
    const s = S.segs[i];
    cap.textContent = s ? (S.lang === "tr" ? (s.tr || s.en) : s.en) : "";
    cap.classList.toggle("show", !!s && S.cc);
    S.active = i;
  }
}
subList.addEventListener("wheel", () => { S.userScroll = performance.now(); }, { passive: true });
subList.addEventListener("click", (e) => {
  const el = e.target.closest(".sub"); if (!el) return;
  const s = S.segs[+el.dataset.i]; if (!s) return;
  vid.currentTime = clampT(s.start + 0.01); S.userScroll = 0;
  if (!vid.wantPlay && !S.bufPaused) { S.autoplay = false; S.gate = true; playVideo(); }     // o cümleden oynat
});
for (const b of $$("#pSubs .ptab")) b.addEventListener("click", () => {
  S.lang = b.dataset.lang; $$("#pSubs .ptab").forEach((x) => x.classList.toggle("on", x === b)); renderSubs(); tick();
});

/* ================= dublaj paneli ================= */
const SEP_MODELS = {
  uvr: { name: "UVR-MDX-NET Voc FT", note: "varsayılan · hızlı", slow: false },
  bandit2: { name: "BandIt v2 · DnR Multi", note: "yavaş çalışır", slow: true },
  tiger: { name: "TIGER-DnR", note: "yavaş çalışır", slow: true },
};
if (!SEP_MODELS[S.sep]) S.sep = "uvr";
/* buffering: Hızlı'da açık, Detaylı'da kapalı başlar; her mod kendi ayarını tutar */
S.bufMode = { hizli: true, detayli: false };
Object.defineProperty(S, "bufOn", { configurable: true, get: () => !!S.bufMode[S.mode], set: (v) => { S.bufMode[S.mode] = !!v; } });
S.autoplay = false;                                    // dublaj başlatılınca: hazır olunca kendiliğinden oynat
S.scrub = null;                                        // çubuk sürüklenirken {t: yuvarlağın yeri, to: önizleme}
/* çeviri modelleri: tahmini kaliteye göre iyiden kötüye */
const MT_MODELS = {                                    // sıra: önerilen önce
  milmmt4: { name: "MiLMMT-46 4B", note: "önerilen · ~2,5 GB", rec: true },
  hymt7: { name: "Hy-MT2 7B", note: "~4,7 GB" },
};
if (!MT_MODELS[S.mt]) S.mt = MT_DEFAULT;
if (!["kadin", "oto", "erkek"].includes(S.gender)) S.gender = "oto";
/* Türkçe ses (Detaylı) */
const TTS_MODELS = {                                   // sıra: önerilen önce
  vox: { name: "VoxCPM2", note: "önerilen · orijinal sesi kopyalar, duyguyu ve hızı taşır · yavaş", rec: true },
  std: { name: "EMA Lightning + Antalia-2 Mini", note: "hazır kadın / erkek sesleri · hızlı" },
};
S.tts = "vox";                                         // Detaylı'da her açılışta VoxCPM2 (Hızlı'da hep EMA / Antalia)
/* aşama → modelin hız istatistiğindeki anahtarı (sunucudaki stage_model ile aynı) */
const statKey = (st, v) => { const p = parseV(v);
  return { separate: "sep:" + p.sep, transcribe: "asr:whisper-turbo", gender: "gender:vgc", translate: "mt:" + p.mt,
           dub: p.tts === "vox" ? "tts:vox:" + S.voxSteps : p.det || v === "hizli-oto" ? "tts:ema+antalia" : v.endsWith("kadin") ? "tts:ema" : "tts:antalia" }[st]; };
const fmtSec = (x) => (x < 10 ? `${x.toFixed(1).replace(".", ",")} sn` : x < 60 ? `${Math.round(x)} sn`
  : `${Math.floor(x / 60)} dk ${String(Math.round(x % 60)).padStart(2, "0")} sn`);
const perMin = (key) => S.stats?.models?.find((m) => m.key === key)?.per_min;
function modelRows() {
  const det = S.mode === "detayli";
  const auto = !det && S.gender === "oto";
  const tts = det ? TTS_MODELS[S.tts].name : auto ? "EMA Lightning + Antalia-2 Mini" : S.gender === "kadin" ? "EMA Lightning" : "Antalia-2 Mini";
  const sm = det ? SEP_MODELS[S.sep] : SEP_MODELS.uvr;
  const mm = det ? MT_MODELS[S.mt] : MT_MODELS[MT_DEFAULT];
  const rows = [
    { k: det ? "sep" : "", st: "separate", stage: "Ses ayırma", model: sm.name, tag: sm.slow ? "yavaş çalışır" : "" },
    { st: "transcribe", stage: "İngilizce metin", model: "Whisper Large V3 Turbo" },
  ];
  if (det || auto) rows.push({ st: "gender", stage: "Kadın / erkek", model: "Voice Gender Classifier" });
  rows.push({ k: det ? "mt" : "", st: "translate", stage: "Türkçe çeviri", model: mm.name, tag: det && mm.slow ? "yavaş" : "" },
            { k: det ? "tts" : "", st: "dub", stage: "Türkçe ses", model: tts, tag: det && S.tts === "vox" ? "yavaş" : "" });
  return rows;
}
/* Detaylı: her modelin bu dublajda harcadığı süre adının sağında */
function stageTime(st) {
  const s = S.status;
  if (S.mode !== "detayli" || !s || s.variant !== variant() || !s.timing?.[st]) return "";
  const sec = s.timing[st], vs = s.timing_video?.[st] || 0;
  const pm = vs > 0 ? ` · 1 dk video ≈ ${Math.round((sec / vs) * 60)} sn` : "";
  return `<i class="tm" title="Bu dublajda ${fmtSec(sec)}${pm}">${fmtSec(sec)}</i>`;
}
function optList(k) {
  if (k === "tts") return Object.entries(TTS_MODELS).map(([id, m]) => {
    const pm = perMin(id === "vox" ? "tts:vox:" + S.voxSteps : "tts:ema+antalia");
    const inst = id === "vox" && S.info && !S.info.vox ? " · ilk kullanımda kurulur (~8 GB indirme)" : "";
    return `<button type="button" class="mopt${S.tts === id ? " on" : ""}${m.rec ? " rec" : ""}" data-tts="${id}"><span>${m.name}</span>` +
      `<em>${m.note}${inst}${pm != null ? ` · 1 dk ≈ ${Math.round(pm)} sn` : ""}</em></button>`;
  }).join("") + `<div class="mnote">VoxCPM2 her cümleyi videodaki kişinin kendi sesinden kopyalar; kadın / erkek ayrımına gerek kalmaz.</div>`;
  if (k === "sep") return Object.entries(SEP_MODELS).map(([id, m]) =>
    `<button type="button" class="mopt${S.sep === id ? " on" : ""}${m.slow ? " slow" : ""}" data-sep="${id}"><span>${m.name}</span><em>${m.note}</em></button>`).join("");
  return Object.entries(MT_MODELS).map(([id, m], i) => {
    const pm = perMin("mt:" + id);
    return `<button type="button" class="mopt${S.mt === id ? " on" : ""}${m.rec ? " rec" : ""}" data-mt="${id}"><span>${i + 1}. ${m.name}</span>` +
      `<em>${m.note}${pm != null ? ` · 1 dk ≈ ${Math.round(pm)} sn` : ""}</em></button>`;
  }).join("") + `<div class="mnote">Model ilk seçildiğinde bir kez indirilir.</div>`;
}
function drawModels() {
  const box = $("#modelList");
  const open = S.mode === "detayli" ? box.dataset.open || "" : "";
  box.innerHTML = modelRows().map((r) => {
    const name = `<span>${esc(r.model)}${r.tag ? ` <em>${r.tag}</em>` : ""}</span>${stageTime(r.st)}`;
    if (r.k) return `<button type="button" class="mrow pick${open === r.k ? " open" : ""}" data-k="${r.k}" title="${r.stage} modelini değiştir">
         <span class="ms">${r.stage}</span><span class="mm">${name}</span>
         <svg viewBox="0 0 24 24"><path d="m6 9 6 6 6-6"/></svg></button>` + (open === r.k ? `<div class="mopts">${optList(r.k)}</div>` : "");
    return `<div class="mrow"><span class="ms">${r.stage}</span><span class="mm">${name}</span></div>`;
  }).join("");
}
$("#modelList").addEventListener("click", (e) => {
  const box = $("#modelList");
  const opt = e.target.closest(".mopt");
  if (opt) {
    if (opt.dataset.sep) { S.sep = opt.dataset.sep; store.set("sep", S.sep); }
    if (opt.dataset.mt) { S.mt = opt.dataset.mt; store.set("mt", S.mt); }
    if (opt.dataset.tts) { S.tts = opt.dataset.tts; store.set("tts", S.tts); }
    box.dataset.open = ""; drawDubPanel(); return;
  }
  const row = e.target.closest(".mrow.pick");
  if (row) { box.dataset.open = box.dataset.open === row.dataset.k ? "" : row.dataset.k; drawModels(); }
});
$("#bufTg").addEventListener("change", (e) => { S.bufOn = e.target.checked; drawDubPanel(); });
/* parça süresi: Detaylı'da 1–5 dk arası seçilir; Hızlı'da sabit 1:30 */
S.bufLen = 90;
const QUICK_LEN = 90;
const fmtLen = (sec) => `${Math.floor(sec / 60)}:${String(Math.round(sec % 60)).padStart(2, "0")}`;
function drawBufLen() {
  const r = $("#bufLen");
  r.value = S.bufLen; r.style.setProperty("--p", ((S.bufLen - 60) / 240) * 100 + "%");
  $("#bufLenO").textContent = fmtLen(S.bufLen) + " dk";
}
$("#bufLen").addEventListener("input", (e) => { S.bufLen = Math.max(60, Math.min(300, +e.target.value)); drawBufLen(); drawDubPanel(); });
const bufParams = () => ({ buffering: S.bufOn, chunk_len: S.mode === "detayli" ? S.bufLen : QUICK_LEN, vox_steps: S.voxSteps });
/* VoxCPM2 adım sayısı: 4–30, standart 10 */
S.voxSteps = 10;                                       // 5 · 10 (önerilen) · 20 · 30 (yavaş)
function drawVoxSteps() {
  $("#voxField").hidden = !(S.mode === "detayli" && S.tts === "vox");
  for (const b of $$("#voxSteps button")) {
    const n = +b.dataset.s, pm = perMin("tts:vox:" + n);
    b.classList.toggle("on", n === S.voxSteps);
    b.title = pm != null ? `Ölçülen: 1 dk video ≈ ${Math.round(pm)} sn` : "Henüz ölçülmedi";
  }
  $("#voxNote").textContent = S.voxSteps >= 20
    ? "Çok adım sesi daha doğal ve ayrıntılı yapar ama dublaj belirgin şekilde yavaşlar."
    : S.voxSteps <= 5 ? "Az adım dublajı hızlandırır; ses biraz daha az doğal olabilir."
    : "Hız ve doğallık dengesi için önerilen ayar.";
}
$("#voxSteps").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; S.voxSteps = +b.dataset.s; drawVoxSteps(); drawEta(); });
/* her “Dublajı Başlat / Tekrar Dublajla”da: yuvarlak başlangıç çubuğuna gider, işlem oradan başlar */
function toRangeStart() {
  S.autoplay = false;
  if (vid.wantPlay || S.bufPaused) { S.bufPaused = false; vid.pause(); }
  stopAll();
  vid.currentTime = fullRange()[0]; drawSeek(vid.currentTime);
}
const isDone = () => S.av && S.status && S.status.complete;
/* tahmini süre: Modeller menüsündeki ölçümler (1 dk video kaç sn) × dublajlanacak süre; ölçülmemiş modelde
   bu bilgisayarda gözlenen yaklaşık değerler kullanılır */
const ETA_GUESS = { "sep:uvr": 12.5, "sep:bandit2": 62, "sep:tiger": 62, "asr:whisper-turbo": 6.5, "gender:vgc": 0.5,
  "mt:milmmt4": 4.5, "mt:hymt7": 6.5, "tts:ema": 1.7, "tts:antalia": 1.7, "tts:ema+antalia": 1.8,
  "tts:vox:5": 50, "tts:vox:10": 72, "tts:vox:20": 115, "tts:vox:30": 160 };
function etaInfo() {
  if (!S.video) return null;
  const v = variant(), p = parseV(v), [a, b] = fullRange();
  const same = S.av === v && S.status?.variant === v && !rangeDirty();
  if (same && S.status.complete && !S.status.running) {        // “Tekrar Dublajla”: yalnız seslendirme
    return { keys: [statKey("dub", v)], sec: b - a, redo: true };
  }
  const left = same ? Math.max(0, b - a - (S.status.ready_dur || 0)) : b - a;
  const keys = [statKey("separate", v), statKey("transcribe", v)];
  if (p.det || v === "hizli-oto") keys.push("gender:vgc");
  keys.push(statKey("translate", v), statKey("dub", v));
  return { keys, sec: left, running: same && S.status.running };
}
function drawEta() {
  const el = $("#goEta"), e = etaInfo();
  if (!e || e.sec < 1) { el.hidden = true; return; }
  const v = variant(), p = parseV(v);
  let t = 0, guess = false;
  for (const k of e.keys) { let pm = perMin(k); if (pm == null) { pm = ETA_GUESS[k] ?? 10; guess = true; } t += (pm * e.sec) / 60; }
  const L = S.bufOn ? (S.mode === "detayli" ? S.bufLen : QUICK_LEN) : 150;
  t = t * 1.1 + Math.ceil(e.sec / L) * (p.tts === "vox" ? 20 : 6);       // model yükleme payı (parça başına)
  const txt = t < 60 ? "<1 dk" : t < 3600 ? `~${Math.round(t / 60)} dk` : `~${Math.floor(t / 3600)} sa ${Math.round((t % 3600) / 60)} dk`;
  el.innerHTML = `${txt}<small>${e.running ? "kalan · tahmini" : "tahmini"}</small>`;
  el.title = `Seçili ${fmt(e.sec)} için, Modeller menüsündeki hız ölçümlerine göre` +
    (guess ? " (bazı modeller henüz ölçülmedi; yaklaşık değer kullanıldı)" : "");
  el.hidden = false;
}
function drawDubPanel() {
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.m === S.mode));
  $$(".voice").forEach((b) => b.classList.toggle("on", b.dataset.g === S.gender));
  $("#voiceField").hidden = S.mode !== "hizli";
  drawModels();
  drawVoxSteps();
  const L = S.mode === "detayli" ? S.bufLen : QUICK_LEN;
  $("#lenRow").hidden = S.mode !== "detayli";            // Hızlı: süre ayarı yok, 1:30'luk parçalar
  $("#bufTg").checked = S.bufOn;
  $("#lenRow").classList.toggle("off", !S.bufOn);
  drawBufLen();
  $("#bufNote").textContent = S.bufOn
    ? `Video yaklaşık ${fmtLen(L)} dakikalık parçalara bölünür (cümle ortasından bölünmez). İlk parça hazır olunca oynatma kendiliğinden başlar; sonrakiler siz izlerken hazırlanır.`
    : "Önce seçili aralığın tamamı hazırlanır, bitince dublajlı oynatma kendiliğinden başlar. Yavaş modeller için önerilir.";
  const v = variant(), go = $("#goBtn"), hint = $("#goHint");
  const same = S.av === v && S.status?.variant === v;
  const done = same && isDone();
  const running = same && S.status.running;
  const stopped = same && S.status.stopped;
  const dirty = rangeDirty();
  go.disabled = !S.video || (running && !dirty);
  go.textContent = dirty ? "Dublajı Başlat" : done ? "Tekrar Dublajla" : running ? "Dublaj Sürüyor…" : stopped ? "Devam Et" : "Dublajı Başlat";
  go.dataset.redo = done && !dirty ? "1" : "";
  hint.textContent = !S.video ? "Önce bir video açın" : dirty ? "Aralık değişti — seçili aralık baştan dublajlanır"
    : done ? "Aynı ayarlarla yeni bir okuma yapılır" : S.av && S.av !== v ? "Mevcut dublaj bu seçimle değiştirilecek" : "";
  $("#dubSummary").textContent = S.av ? labelOf(S.av) : labelOf(v);
  $("#pDub").classList.toggle("done", !!(S.av && isDone() && !dirty));
  drawEta();
}
const labelOf = (v) => { const p = parseV(v);
  if (!p.det) return v === "hizli-kadin" ? "Hızlı · Kadın" : v === "hizli-oto" ? "Hızlı · Otomatik" : "Hızlı · Erkek";
  return "Detaylı" + (p.sep !== "uvr" ? " · " + (SEP_MODELS[p.sep]?.name.split(" ·")[0] || "") : "") +
    (p.mt !== MT_DEFAULT ? " · " + (MT_MODELS[p.mt]?.name || "") : "") + (p.tts === "vox" ? " · VoxCPM2" : ""); };
$("#modeSeg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (!b) return; S.mode = b.dataset.m; store.set("mode", S.mode); drawDubPanel(); });
$$(".voice").forEach((b) => b.addEventListener("click", () => { S.gender = b.dataset.g; store.set("gender", S.gender); drawDubPanel(); }));
$("#pDub .ptabs").addEventListener("click", () => $("#pDub").classList.toggle("folded"));
$("#goBtn").addEventListener("click", () => ($("#goBtn").dataset.redo ? redoDub() : startDub()));

function attach(v, st, force = false) {
  if (S.av !== v || force) { stopAll(); S.chunks.clear(); S.segs = []; S.lastK = -1; S.active = -2; }
  S.av = v; S.status = st; indexStatus(st); S.gate = true; S.skipGate = false;
  renderSubs(); drawRanges(); drawJob(); drawDubPanel(); wantChunks(); tick();
}
async function startDub() {
  if (!S.video) return;
  const v = variant();
  try {
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const fresh = rangeDirty();                             // aralık değişti: önceki parçalar atılır
    toRangeStart();
    const st = await api("/api/start", { video_id: S.video.video_id, variant: v, t: 0, ...bufParams(),
                                         range: fullRange(), fresh });
    resetVoices();                                         // yeni render: ses seçimi sıfırlanır
    attach(v, st, fresh);
    adoptRange(st);
    if (st.range) { vid.currentTime = st.range[0]; drawSeek(st.range[0]); }
    S.autoplay = true;
    $("#pDub").classList.add("folded");
    poll();
  } catch (e) { toast("Dublaj başlatılamadı: " + e.message, true); }
}
async function redoDub() {
  if (!S.video) return;
  const v = variant();
  try {
    toRangeStart();
    const st = await api("/api/redo", { video_id: S.video.video_id, variant: v, t: 0, ...bufParams(),
                                        range: fullRange() });
    resetVoices();
    attach(v, st, true);
    adoptRange(st);
    if (st.range) { vid.currentTime = st.range[0]; drawSeek(st.range[0]); }
    S.autoplay = true;
    $("#pDub").classList.add("folded");
    toast("Dublaj yeniden hazırlanıyor");
    poll();
  } catch (e) { toast("Tekrar dublajlanamadı: " + e.message, true); }
}
async function stopDub() {
  if (!S.video || !S.av) return;
  try {
    const st = await api("/api/stop", { video_id: S.video.video_id, variant: S.av });
    S.status = st; indexStatus(st);
    drawJob(); drawDubPanel(); status();
    toast("Dublaj durduruluyor — o anki adım bitince durur");
  } catch (e) { toast("Durdurulamadı: " + e.message, true); }
}
$("#stopBtn").addEventListener("click", stopDub);

/* ================= işlem paneli + durum sorgulama ================= */
const pct = (st) => { if (!st) return 0; const [a, b] = st.range || [0, st.duration]; return b > a ? Math.min(100, Math.round((st.ready_dur / (b - a)) * 100)) : 0; };
function drawJob() {
  drawVoices();
  const st = S.status, p = $("#pJob");
  if (!st || !S.av) { p.hidden = true; return; }
  p.hidden = st.complete && !st.error;
  $("#stopBtn").hidden = !st.running;
  let sum = `%${pct(st)} hazır`;
  if (st.buffering && st.chunk_len) sum += ` · parça ${fmtLen(st.chunk_len)} dk`;
  $("#jobSummary").textContent = sum;
  const d = st.duration || 1, fk = chunkAt(vid.currentTime);
  $("#chunkMap").innerHTML = S.order.map((c) =>
    `<i class="${c.state}${c.i === fk ? " focus" : ""}" data-k="${c.i}" style="left:${(c.start / d) * 100}%;width:${((c.end - c.start) / d) * 100}%" title="${fmt(c.start)}–${fmt(c.end)}"></i>`).join("");
  const cur = st.current;
  let html = "";
  if (cur) {
    html += `<div class="stg"><span></span><span class="nm" style="color:var(--text)">Parça ${fmt(cur.start)}–${fmt(cur.end)}</span><span class="pc">${Math.round(cur.end - cur.start)} sn</span></div>`;
    for (const s of cur.stages) {
      const run = s.status === "calisiyor" || s.status === "indiriliyor";
      const pc = run ? (s.progress ? Math.round(s.progress) + "%" : "") : s.status === "onbellek" ? "önbellek" : s.status === "tamam" ? "✓" : s.status === "hata" ? "!" : "";
      html += `<div class="stg ${s.status}" title="${esc(s.detail || "")}"><span class="dot"></span><span class="nm">${esc(s.name)}${run && s.detail ? ` <small style="color:var(--dim)">· ${esc(s.detail)}</small>` : ""}</span><span class="pc">${pc}</span>` +
        (run ? `<div class="bar"><b style="width:${s.progress || 0}%"></b></div>` : "") + `</div>`;
    }
    const dl = cur.stages.find((s) => s.status === "indiriliyor" && / indiriliyor/.test(s.detail || ""));
    if (dl) {
      html += `<div class="dlnote"><b>⬇ Model indiriliyor — yalnızca bir kez.</b> Bu model ilk kullanımda bilgisayarınıza indirilir; ` +
        `sonraki dublajlarda hazırdır, bir daha beklemezsiniz.</div>`;
      if (!S.dlToast) { S.dlToast = true; toast("Gerekli model indiriliyor — yalnızca ilk kullanımda, bir kez iner."); }
    }
  } else if (st.stopped) html = `<div class="stg"><span></span><span class="nm">Durduruldu — kaldığı yerden sürdürmek için “Devam Et”</span><span></span></div>`;
  else if (!st.complete && !st.running) html = `<div class="stg"><span></span><span class="nm">Bekliyor</span><span></span></div>`;
  if (!st.buffering && !st.complete) html += `<div class="stg"><span></span><span class="nm" style="color:var(--dim)">Buffering kapalı: tamamı bitince dublajlı izlenir</span><span></span></div>`;
  if (st.error) html += `<div class="joberr">Hata: ${esc(st.error)}</div>`;
  $("#stages").innerHTML = html;
}
$("#chunkMap").addEventListener("click", (e) => { const i = e.target.closest("i"); const m = i && cmeta(+i.dataset.k); if (m) vid.currentTime = clampT(m.start + 0.01); });

async function poll() {
  if (S.polling) return;
  S.polling = true;
  try {
    while (S.video && S.av) {
      const vidId = S.video.video_id, av = S.av;
      let st;
      try { st = await api(`/api/status/${vidId}?variant=${av}`); } catch { await sleep(1500); continue; }
      if (!S.video || S.video.video_id !== vidId || S.av !== av) continue;
      const prev = S.status; S.status = st; indexStatus(st);
      const key = (x) => x ? x.chunks.map((c) => c.i + c.state + c.end + (c.done ? "d" : "")).join(",") + x.complete : "";
      if (!prev || key(prev) !== key(st)) {
        if (prev && !prev.complete && st.complete) {
          loadStats().then(() => drawModels());
          toast(st.buffering ? "Dublajın tamamı hazır" : "Dublaj hazır — artık dublajlı izleyebilirsiniz");
          $("#pDub").classList.add("folded");
        }
        for (const c of st.chunks) if (c.state === "hazir") await loadChunkInfo(c.i);
        drawRanges(); wantChunks(); renderSubs();
      }
      drawJob(); drawDubPanel(); status();
      if (!st.running) break;
      await sleep(1000);
    }
  } finally { S.polling = false; }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function status() {
  const l = $("#sbLeft"), r = $("#sbRight");
  if (!S.video) { l.textContent = "Video yok"; r.textContent = S.info?.gpu ? "GPU: " + S.info.gpu : ""; return; }
  l.textContent = `${S.video.name} · ${fmt(S.video.duration)}`;
  if (S.av && S.status) {
    const st = S.status;
    r.textContent = `${labelOf(S.av)} · %${pct(st)} hazır` + (st.running ? " · arka planda işleniyor" : st.stopped ? " · durduruldu" : "");
  } else r.textContent = "Dublaj yok";
  if (S.export?.running) r.textContent = `Dışa aktarılıyor: ${S.export.label} · %${Math.round(S.export.progress || 0)}`;
}

/* ================= video açma ================= */
async function setVideo(v) {
  stopAll(); S.av = null; S.status = null; S.chunks.clear(); S.segs = []; S.bufPaused = false; S.lastK = -1; S.autoplay = false;
  S.voiceSel.clear(); S.spk = null;
  S.order = []; S.cmap = new Map(); S.gate = true; S.skipGate = false;
  setMixer(false);
  S.video = v; canvas.classList.add("has");
  vid.onFallback = (why) => ytFallback(v, why);
  if (v.youtube && v.embed !== false) vid.loadYouTube(v.youtube, v.duration);
  else if (v.youtube) ytFallback(v, "yerleştirme kapalı");
  else vid.loadHtml(v.url);
  $("#playBtn").disabled = false; $("#tDur").textContent = fmt(v.duration);
  S.range = null; drawRange();
  $("#pDub").classList.remove("folded");
  renderSubs(); drawJob(); drawDubPanel(); status(); drawRanges();
}
async function openPath(path) {
  if (!path) return;
  try { toast("Video açılıyor…"); await setVideo(await api("/api/open", { path })); }
  catch (e) { toast("Video açılamadı: " + e.message, true); }
}
async function uploadFile(file) {
  if (file.pywebviewFullPath) return openPath(file.pywebviewFullPath);
  try {
    toast("Video aktarılıyor…");
    const r = await fetch("/api/upload", { method: "POST", headers: { "X-File-Name": encodeURIComponent(file.name) }, body: file });
    const j = await r.json(); if (!r.ok || j.ok === false) throw new Error(j.error || r.status);
    await setVideo(j);
  } catch (e) { toast("Video aktarılamadı: " + e.message, true); }
}
async function openDialog() {
  const a = window.pywebview?.api;
  if (a?.pick_video) { try { return openPath(await a.pick_video()); } catch (e) { console.warn(e); } }
  $("#fileInput").click();
}
/* ================= YouTube bağlantısı: yalnız ses indirilir, görüntü YouTube'dan akar ================= */
async function ytFallback(v, why) {                     // YouTube oynatıcısı açılamadı → doğrudan akış (görüntü + ses, 360p)
  if (S.video !== v) return;
  try {
    const r = await api(`/api/yt_stream/${v.video_id}`);
    if (S.video !== v) return;
    if (!r.url) throw new Error("akış adresi yok");
    const t = vid.currentTime || 0;
    vid.loadHtml(r.url);
    vid.addEventListener("loadedmetadata", () => { try { vid.currentTime = t; } catch {} }, { once: true });
    toast("Bu video uygulama içindeki YouTube oynatıcısında açılamadı; doğrudan akış kullanılıyor (düşük kalite).");
  } catch (e) {
    toast("YouTube videosu oynatılamadı (" + why + "): " + e.message, true);
  }
}
function ytStatus(msg, pct) {
  const el = $("#ytState"), on = !!msg;
  el.hidden = !on || canvas.classList.contains("has");
  if (on) {
    el.innerHTML = `<span>${esc(msg)}${pct != null && pct > 0 ? ` · %${Math.round(pct)}` : ""}</span>` +
      (pct != null ? `<i><b style="width:${Math.max(2, Math.min(100, pct || 0))}%"></b></i>` : "");
    $("#sbLeft").textContent = msg + (pct ? ` · %${Math.round(pct)}` : "");
  } else status();
}
async function openUrl(url) {
  url = String(url || "").trim();
  if (!url) return;
  if (S.ytBusy) return toast("YouTube indirmesi sürüyor", true);
  S.ytBusy = true; $("#ytGo").disabled = true;
  try {
    await api("/api/open_url", { url });
    ytStatus("YouTube bağlantısı açılıyor", 0);
    for (let n = 0; ; n++) {
      await sleep(400);
      const st = await api("/api/open_url_status");
      if (st.error) throw new Error(st.error);
      if (st.video) { ytStatus(null); $("#ytUrl").value = ""; await setVideo(st.video); toast("YouTube videosu açıldı: " + st.video.name); return; }
      if (!st.running) throw new Error("indirme durdu");
      ytStatus(st.stage, st.progress);
      if (canvas.classList.contains("has") && n % 8 === 0) toast(`${st.stage}${st.progress ? " · %" + Math.round(st.progress) : ""}`);
    }
  } catch (e) {
    ytStatus(null); toast(e.message, true);
  } finally { S.ytBusy = false; $("#ytGo").disabled = false; }
}
$("#ytGo").addEventListener("click", () => openUrl($("#ytUrl").value));
$("#ytUrl").addEventListener("keydown", (e) => { if (e.key === "Enter") openUrl(e.target.value); });
function askUrl() {
  showModal("YouTube bağlantısı", `<p class="dim">Bağlantıyı yapıştırın. Yalnız ses indirilir (dublaj için); görüntü YouTube'dan akar.</p>
    <div class="wlink in"><input type="text" id="ytUrl2" placeholder="https://www.youtube.com/watch?v=…" spellcheck="false" autocomplete="off">
    <button type="button" class="btn primary" id="ytGo2">Uygula</button></div>`);
  const go = () => { const u = $("#ytUrl2").value; $("#modal").hidden = true; openUrl(u); };
  $("#ytGo2").addEventListener("click", go);
  $("#ytUrl2").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  setTimeout(() => $("#ytUrl2")?.focus(), 30);
}
$("#fileInput").addEventListener("change", (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) uploadFile(f); });
$("#wOpen").addEventListener("click", openDialog);
$("#playBtn").addEventListener("click", togglePlay);
let clickT;
$("#vClick").addEventListener("click", () => { clearTimeout(clickT); clickT = setTimeout(togglePlay, 220); });
$("#vClick").addEventListener("dblclick", () => clearTimeout(clickT));

let dragN = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragN++; canvas.classList.add("drag"); });
window.addEventListener("dragleave", () => { if (--dragN <= 0) { dragN = 0; canvas.classList.remove("drag"); } });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragN = 0; canvas.classList.remove("drag");
  const f = e.dataTransfer?.files?.[0]; if (f) setTimeout(() => uploadFile(f), 30);   // pywebviewFullPath sonradan eklenir
});

/* ================= görünüm / dock ================= */
for (const b of $$(".rbtn")) b.addEventListener("click", () => {
  const p = $("#" + b.dataset.p); const off = !p.classList.toggle("off"); b.classList.toggle("on", off);
});

window.addEventListener("keydown", (e) => {
  if (e.ctrlKey && (e.key === "o" || e.key === "O")) { e.preventDefault(); openDialog(); return; }
  if (e.target.matches("input[type=text],textarea")) return;
  if (e.target.matches("input[type=range]") && e.key.startsWith("Arrow")) return;   // kaydırıcıyı oynatırken video atlamasın
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowRight" && S.video) { vid.currentTime = clampT(vid.currentTime + 5); }
  else if (e.key === "ArrowLeft" && S.video) { vid.currentTime = clampT(vid.currentTime - 5); }
  else if (e.key === "c" || e.key === "C") { $("#ccBtn").click(); }
});
for (const r of $$("input[type=range]")) r.addEventListener("keydown", (e) => { if (e.code === "Space") e.preventDefault(); });

/* ================= üst menü ================= */
function closeMenus() { $$(".mi.open").forEach((m) => m.classList.remove("open")); }
$("#menu").addEventListener("click", (e) => {
  const top = e.target.closest("button[data-menu]");
  if (top) { const mi = top.parentElement, was = mi.classList.contains("open"); closeMenus(); if (!was) mi.classList.add("open"); e.stopPropagation(); return; }
  const it = e.target.closest("button[data-act]"); if (!it) return;
  closeMenus();
  const act = it.dataset.act;
  if (act === "open") openDialog();
  else if (act === "openurl") askUrl();
  else if (act === "export") exportAs(it.dataset.kind);
  else if (act === "models") showModels();
  else if (act === "info") showAbout();
  else if (act === "update") showUpdate();
});
/* ================= hakkında ================= */
const LINK = (url, text) => `<a href="#" class="xlink" data-url="${esc(url)}">${esc(text)}</a>`;
function showAbout() {
  showModal("Hakkında", `<div class="about">
    <div class="ahead"><b>"Doğru mu Samet?"</b><span>Sürüm ${esc(S.info?.version || "")} · İngilizce → Türkçe video dublaj</span></div>
    <p>"Doğru mu Samet?", İngilizce videoları Türkçe dublajla izleyebilmek için hazırlanmış, <b>hobi amaçlı ve deneysel</b>
      bir projedir. Bütün yapay zekâ modelleri kendi bilgisayarınızda çalışır; videolarınız hiçbir sunucuya gönderilmez.</p>
    <h4>Ticari değildir</h4>
    <p>Program kişisel kullanım için yapıldı. <b>Ücretsizdir ve hiçbir zaman ücretli olmayacak.</b> Ticari kullanım için
      tasarlanmadı ve uygun değildir; kullanılan her modelin kendi lisans koşulları vardır.</p>
    <h4>Açık kaynak</h4>
    <p>Proje açık kaynaklıdır. Dilediğiniz gibi kullanabilir, inceleyebilir, değiştirip geliştirebilirsiniz.
      Gönül rahatlığıyla, tepe tepe kullanın!</p>
    <h4>Claude ile geliştirildi</h4>
    <p>Bu program, Anthropic'in yapay zekâ asistanı <b>Claude</b> ile birlikte geliştirildi.</p>
    <h4>Sırada neler var?</h4>
    <p>Proje gelişmeye devam ediyor. İlerleyen sürümlerde eklemeyi planladıklarım:</p>
    <ul>
      <li>Yeni ve daha iyi bir <b>ses temizleme</b> özelliği</li>
      <li>Yeni bir konuşma modeli: <b>tamamen Türkçe konuşan, hafif bir ses modeli</b> — bunun için kendi modelimi eğitmeyi düşünüyorum</li>
      <li>Hızlı modda <b>çok daha fazla konuşmacı sesi</b></li>
    </ul>
    <h4>İletişim</h4>
    <div class="acard">
      <div><b>Evren TUĞ</b></div>
      <div>E-posta: ${LINK("mailto:evrtug@gmail.com", "evrtug@gmail.com")}</div>
      <div>Instagram: ${LINK("https://www.instagram.com/evrenot3d/", "@evrenot3d")}</div>
    </div>
    <p>Öneri ve istekleriniz için bana e-posta gönderebilirsiniz; her mesajı okumaya çalışıyorum.
      Sağlık sorunlarım nedeniyle zaman zaman e-postalara geri dönemeyebilirim; şimdiden anlayışınız için teşekkür ederim.</p>
  </div>`, true);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a.xlink"); if (!a) return;
  e.preventDefault();
  api("/api/open_external", { url: a.dataset.url }).catch(() => toast("Bağlantı açılamadı", true));
});
/* ================= güncelleme ================= */
S.upd = null;
function markUpdate(u) {
  S.upd = u;
  const on = !!(u && u.newer);
  $('[data-menu="mAbout"]').classList.toggle("dot", on);
  $('[data-act="update"]').classList.toggle("dot", on);
}
async function checkUpdate(force = false) {
  const u = await api("/api/update/check" + (force ? "?force=1" : ""));
  if (u.ok) markUpdate(u);
  return u;
}
const notesHtml = (t) => esc(t || "").replace(/^#+\s*(.*)$/gm, "<b>$1</b>").replace(/^\s*[-*]\s+(.*)$/gm, "• $1").replace(/\n/g, "<br>");
async function showUpdate() {
  showModal("Güncelleme", `<p class="dim">Denetleniyor…</p>`);
  let u;
  try { u = await checkUpdate(true); } catch (e) { u = { ok: false, error: e.message }; }
  const cur = `<p>Kurulu sürüm: <b>${esc(u.current || S.info?.version || "")}</b></p>`;
  if (!u.ok) return showModal("Güncelleme", cur + `<p class="err">${esc(u.error || "Denetlenemedi")}</p>
    <div class="mbtns"><button type="button" class="btn" id="updRetry">Yeniden dene</button></div>`);
  if (!u.configured) return showModal("Güncelleme", cur + `<p class="dim">Güncelleme adresi henüz ayarlanmadı.
    Program GitHub'da yayınlandığında güncellemeler buradan denetlenecek.</p>`);
  if (!u.newer) return showModal("Güncelleme", cur + `<p>✓ Program güncel.</p>
    ${u.latest ? `<p class="dim">GitHub'daki son sürüm: ${esc(u.latest)}${u.date ? " · " + esc(u.date) : ""}</p>` : ""}
    <div class="mbtns"><button type="button" class="btn" id="updRetry">Yeniden denetle</button></div>`);
  showModal("Güncelleme", cur + `<p class="ok">Yeni sürüm var: <b>${esc(u.latest)}</b>${u.date ? ` <span class="dim">· ${esc(u.date)}</span>` : ""}</p>
    ${u.notes ? `<div class="notes">${/^\s*#/.test(u.notes) ? "" : `<div class="dim">Yenilikler</div>`}${notesHtml(u.notes)}</div>` : ""}
    <p class="dim">Yalnız program dosyaları güncellenir; modelleriniz, çalışmalarınız ve ayarlarınız olduğu gibi kalır.
      Eski dosyalar "yedek" klasörüne alınır.</p>
    <div class="updprog" id="updProg" hidden><div class="bar"><i></i></div><span></span></div>
    <div class="mbtns"><button type="button" class="btn" data-a="close">Sonra</button>
      <button type="button" class="btn primary" id="updGo">Güncelle</button></div>`, true);
}
$("#mBody").addEventListener("click", async (e) => {
  if (e.target.id === "updRetry") return showUpdate();
  if (e.target.dataset.a === "close") { $("#modal").hidden = true; return; }
  if (e.target.id === "updRestart") {
    e.target.disabled = true; e.target.textContent = "Yeniden başlatılıyor…";
    try { await api("/api/update/restart", {}); } catch {}
    return;
  }
  if (e.target.id !== "updGo") return;
  const btn = e.target; btn.disabled = true; btn.textContent = "Güncelleniyor…";
  const box = $("#updProg"); box.hidden = false;
  try {
    await api("/api/update/install", {});
    for (;;) {
      await new Promise((r) => setTimeout(r, 500));
      const st = await api("/api/update/status");
      box.querySelector("i").style.width = (st.progress || 0) + "%";
      box.querySelector("span").textContent = st.stage || "";
      if (st.error) throw new Error(st.error);
      if (!st.running && st.done) break;
    }
    box.querySelector("span").textContent = `Sürüm ${S.upd?.latest || ""} kuruldu. Değişikliklerin geçerli olması için program yeniden başlatılacak.`;
    btn.id = "updRestart"; btn.disabled = false; btn.textContent = "Yeniden Başlat";
  } catch (err) {
    box.querySelector("span").textContent = "Güncellenemedi: " + err.message;
    btn.disabled = false; btn.textContent = "Tekrar dene";
  }
});
$("#menu").addEventListener("mouseover", (e) => {
  const top = e.target.closest("button[data-menu]");
  if (top && $(".mi.open") && !top.parentElement.classList.contains("open")) { closeMenus(); top.parentElement.classList.add("open"); }
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".mi")) closeMenus();
  if (!e.target.closest(".ccgrp")) $("#ccPop").hidden = true;
});
/* evet / hayır sorusu (pencere içinde) */
function askConfirm(title, html, yes = "Evet", no = "Vazgeç") {
  return new Promise((resolve) => {
    showModal(title, `<p>${html}</p><div class="mbtns"><button type="button" class="btn" data-a="0">${esc(no)}</button>` +
      `<button type="button" class="btn primary" data-a="1">${esc(yes)}</button></div>`);
    const box = $("#modal");
    const done = (v) => { box.hidden = true; box.removeEventListener("click", onClick); $("#mClose").removeEventListener("click", onClose); resolve(v); };
    const onClick = (e) => { const b = e.target.closest(".mbtns [data-a]"); if (b) done(b.dataset.a === "1"); else if (e.target === box) done(false); };
    const onClose = () => done(false);
    box.addEventListener("click", onClick); $("#mClose").addEventListener("click", onClose);
  });
}
function showModal(title, html, wide = false) {
  $("#mTitle").textContent = title; $("#mBody").innerHTML = html; $(".mbox").classList.toggle("wide", wide); $("#modal").hidden = false;
}
/* ================= Modeller: ortalama hızlar (kalıcı, tüm videolar) ================= */
const KNOWN_MODELS = {
  separate: [["sep:uvr", "UVR-MDX-NET Voc FT"], ["sep:bandit2", "BandIt v2 · DnR Multi"], ["sep:tiger", "TIGER-DnR"]],
  transcribe: [["asr:whisper-turbo", "Whisper Large V3 Turbo"]],
  gender: [["gender:vgc", "Voice Gender Classifier"]],
  translate: Object.entries(MT_MODELS).map(([k, m]) => ["mt:" + k, m.name]),
  dub: [["tts:ema", "EMA Lightning"], ["tts:antalia", "Antalia-2 Mini"], ["tts:ema+antalia", "EMA Lightning + Antalia-2 Mini"],
        ["tts:vox:5", "VoxCPM2 · 5 adım"], ["tts:vox:10", "VoxCPM2 · 10 adım"], ["tts:vox:20", "VoxCPM2 · 20 adım"],
        ["tts:vox:30", "VoxCPM2 · 30 adım"]],
};
const STAGE_TITLES = [["separate", "Ses ayırma"], ["transcribe", "İngilizce metin"], ["gender", "Kadın / erkek ayırma"],
                      ["translate", "Türkçe çeviri"], ["dub", "Türkçe ses"]];
async function loadStats() { try { S.stats = await api("/api/stats"); } catch { /* sunucu yoksa boş */ } return S.stats; }
async function showModels() {
  const st = (await loadStats()) || { models: [] };
  const vid = (m) => `${m.video_min.toFixed(1).replace(".", ",")} dk video`;
  let html = `<p class="dim">1 dakikalık videoyu ortalama kaç saniyede işlediği. Her dublajda güncellenir ve bütün videoların
    birlikte ortalaması alınır (ilk indirme / yükleme beklemeleri sayılmaz). Her grupta en hızlıdan en yavaşa sıralıdır.</p>`;
  for (const [g, title] of st.groups?.length ? st.groups : STAGE_TITLES) {
    const meas = st.models.filter((m) => m.stage === g).sort((a, b) => a.per_min - b.per_min);
    const rest = (KNOWN_MODELS[g] || []).filter(([k]) => !meas.some((m) => m.key === k));
    const max = Math.max(1, ...meas.map((m) => m.per_min));
    html += `<div class="sgrp"><div class="sgh">${esc(title)}</div>` +
      meas.map((m, i) => `<div class="srow" title="${m.n} ölçüm"><span class="sn"><b>${i + 1}.</b> ${esc(m.name)}</span>` +
        `<span class="sbar"><i style="width:${Math.max(3, (m.per_min / max) * 100)}%"></i></span><span class="sv">${fmtSec(m.per_min)}</span>` +
        `<span class="ss">${vid(m)}</span></div>`).join("") +
      rest.map(([, n]) => `<div class="srow none"><span class="sn">${esc(n)}</span><span class="sbar"></span><span class="sv">—</span><span class="ss">ölçülmedi</span></div>`).join("") +
      `</div>`;
  }
  showModal("Modeller · 1 dk video kaç saniyede", html, true);
}
$("#mClose").addEventListener("click", () => ($("#modal").hidden = true));
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") $("#modal").hidden = true; });

/* ================= dışa aktarma ================= */
const EXPORT_NAMES = { video: "_dublaj.mp4", dub: "_dublaj_ses.wav", en: "_ingilizce_ses.wav", bg: "_arkaplan_ses.wav", all: "_tum_sesler.wav" };
async function exportAs(kind) {
  if (!S.video) return toast("Önce bir video açın", true);
  if (!S.av) return toast("Önce dublajı başlatın", true);
  const st = S.status, needDub = ["video", "dub", "all"].includes(kind);
  if (needDub && st && !st.complete) return toast(`Dublaj henüz bitmedi (%${pct(st)} hazır)`, true);
  const gains = {};
  for (const ch of ["dub", "bg", "en"]) gains[ch] = mix.mute[ch] ? 0 : (mix[ch] ?? 100) / 100;
  const base = S.video.name.replace(/\.[^.]+$/, "");
  let path = null;
  try { path = await window.pywebview?.api?.save_file(base + EXPORT_NAMES[kind], kind); } catch (e) { console.warn(e); }
  if (!path) return;
  try {
    await api("/api/export", { video_id: S.video.video_id, variant: S.av, kind, gains, path });
    pollExport();
  } catch (e) { toast("Dışa aktarılamadı: " + e.message, true); }
}
async function pollExport() {
  for (;;) {
    let st; try { st = await api("/api/export_status"); } catch { break; }
    S.export = st; status();
    if (!st.running) {
      if (st.error) toast("Dışa aktarma başarısız: " + st.error, true);
      else if (st.done) toast("Kaydedildi: " + st.path);
      S.export = null; status(); break;
    }
    await sleep(500);
  }
}

/* ================= altyazı görünümü ================= */
const CAP_DEF = { size: 75, shadow: false, color: "#ffffff", stroke: false, strokeColor: "#000000" };
const cap = Object.assign({}, CAP_DEF);               // her açılışta standart altyazı
function applyCap() {
  const c = $("#caption").style;
  c.setProperty("--cap-scale", cap.size / 100);
  c.setProperty("--cap-color", cap.color);
  c.setProperty("--cap-bg", cap.shadow ? "rgba(0,0,0,.62)" : "transparent");
  c.setProperty("--cap-tshadow", cap.shadow ? "0 1px 3px #000" : "none");
  c.setProperty("--cap-stroke", cap.stroke ? `${Math.max(1, cap.size / 70).toFixed(1)}px ${cap.strokeColor}` : "0 transparent");
  $("#csSize").value = cap.size; $("#csSizeO").textContent = cap.size + "%";
  $("#csSize").style.setProperty("--p", ((cap.size - 60) / 140) * 100 + "%");
  $("#csShadow").checked = cap.shadow; $("#csColor").value = cap.color;
  $("#csStroke").checked = cap.stroke; $("#csStrokeColor").value = cap.strokeColor;
  $("#csStrokeColor").disabled = !cap.stroke;
}
$("#csSize").addEventListener("input", (e) => { cap.size = +e.target.value; applyCap(); });
$("#csShadow").addEventListener("change", (e) => { cap.shadow = e.target.checked; applyCap(); });
$("#csColor").addEventListener("input", (e) => { cap.color = e.target.value; applyCap(); });
$("#csStroke").addEventListener("change", (e) => { cap.stroke = e.target.checked; applyCap(); });
$("#csStrokeColor").addEventListener("input", (e) => { cap.strokeColor = e.target.value; applyCap(); });
$("#csReset").addEventListener("click", () => { Object.assign(cap, CAP_DEF); applyCap(); });
const togglePop = () => { $("#ccPop").hidden = !$("#ccPop").hidden; };
$("#ccMore").addEventListener("click", (e) => { e.stopPropagation(); togglePop(); });
$("#ccBtn").addEventListener("contextmenu", (e) => { e.preventDefault(); e.stopPropagation(); $("#ccPop").hidden = false; });
$("#ccPop").addEventListener("click", (e) => e.stopPropagation());
applyCap();

/* ================= tam ekran ================= */
let fs = false;
async function setFs(on) {
  fs = on; document.documentElement.classList.toggle("fs", on);
  try { await window.pywebview?.api?.set_fullscreen(on); } catch {}
  if (!window.pywebview?.api) { try { on ? await document.documentElement.requestFullscreen() : await document.exitFullscreen(); } catch {} }
}
$("#fsBtn").addEventListener("click", () => setFs(!fs));
$("#vClick").addEventListener("dblclick", () => setFs(!fs));
window.addEventListener("keydown", (e) => {
  if (e.key === "F11") { e.preventDefault(); setFs(!fs); }
  else if (e.key === "Escape") { if (fs) setFs(false); closeMenus(); $("#ccPop").hidden = true; $("#modal").hidden = true; }
});

/* ================= başlangıç ================= */
api("/api/info").then((i) => {
  S.info = i;
  const chip = $("#gpuChip");
  chip.textContent = i.demo ? "DEMO" : i.gpu || "GPU yok";
  chip.classList.toggle("ok", !!i.gpu);
  if (i.version) $("#verTag").textContent = "V. " + i.version;
  if (!i.demo || new URLSearchParams(location.search).has("upd")) {      // açılışta sessizce güncelleme denetimi
    setTimeout(() => checkUpdate().then((u) => {
      if (u.newer) toast(`Yeni güncelleme var: sürüm ${u.latest} — Hakkında › Güncelleme`);
    }).catch(() => {}), 2500);
  }
  loadStats().then(() => drawModels());
  drawDubPanel(); status();
}).catch(() => {});
drawDubPanel(); status();
if (location.protocol === "file:") toast("Uygulamayı BASLAT.bat ile açın.", true);
})();
