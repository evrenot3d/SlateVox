"""Parça parça dublaj işleyicisi (uyarlamalı buffering).

Parçalar baştan sabit değildir; işlendikçe planlanır:
  * İlk parça ~90 sn. İşlenme süresi ölçülür (hız = işlem sn / video sn).
  * Sonraki parça, bir önceki parça oynarken bitecek uzunlukta seçilir: (önceki − 15 sn pay) / hız
    (45–180 sn arası). Hız 1'in üstündeyse (gerçek zamandan yavaş) parçalar 90 sn kalır ve oynatıcı,
    takılmadan izlenebilecek kadar ön hazırlık bitene dek bekler (arayüz bunu hesaplar).
  * Kesim noktası cümle ortasına düşmez: konuşma algılayıcı (Silero VAD) ile hedefin ±12 sn çevresindeki
    konuşmasız boşluklar bulunur, en uygun boşluğun en sessiz anından kesilir.
  * Buffering kapalıysa seçili aralığın sesi TEK PARÇA olarak işlenir; bittiğinde yalnızca oynatma kolaylığı için
    cümle aralarından ~2 dakikalık dilimlere ayrılır (yeniden işlenmez). Dublaj hepsi bitince izlenir.
  * Aralık: kullanıcı oynatma çubuğunda başlangıç/bitiş seçebilir; yalnızca o aralık işlenir.
  * Veriler uygulama kapanana dek tutulur.
Her aşama diske yazılır. Video her açılışta sıfırdan başlar.

Varyantlar:  hizli-kadin · hizli-erkek · hizli-oto            (Hızlı)
             detayli · detayli-bandit2 · detayli-tiger  (kadın/erkek otomatik; ayırma modeli)
             ses modeli eki: ~vox · ~ema · ~antalia · ~ahenkcep · ~ahenk (Gelişmiş; seçilen model bütün cümleleri okur,
             ek yoksa eski EMA + Antalia çifti). Ahenk'lilerde "Duygu Algılama" aşaması da çalışır (her cümlenin
             duygusu modele talimat olarak gider)
"""
from __future__ import annotations

import json
import os
import random
import shutil
import threading
import time
import traceback
import uuid
import wave
from pathlib import Path

import numpy as np

import engine as E

CHUNK_LEN, CHUNK_MIN, CHUNK_MAX, NOBUF_LEN = 120.0, 60.0, 1200.0, 150.0   # buffering parça süresi (2·5·10·20 dk)
VOX_STEPS_MIN, VOX_STEPS_MAX = 4, 32   # difüzyon adımı (VoxCPM2 4–30, varsayılan 10; Ahenk / Ahenk Cep varsayılan 16)
SEARCH = 12.0                     # parça sonu: hedefin ± bu kadar sn içindeki en uygun konuşma boşluğu
SEPS = {"uvr": "", "bandit2": "BandIt v2", "tiger": "TIGER-DnR"}
SLOW_SEPS = {"tiger"}
FIXED_SEP = "bandit2"                     # Hızlı · Orta · Güçlü'nün ses ayırma modeli (ölçümlerde en hızlısı)
BASES = ("hizli-kadin", "hizli-erkek", "hizli-oto", "detayli", "detayli-bandit2", "detayli-tiger")
KEEP = {"_webview"}                       # calisma/ içinde silinmeyenler
# Varyant: "<mod>[-<ayırma>][~<çeviri modeli>][~<ses modeli>]"  ör. "detayli-tiger~hymt7~vox";
# varsayılan çeviri (MiLMMT) ve varsayılan ses (EMA + Antalia) için ek yok
# Gelişmiş'te ses modeli tek başına (bütün cümleleri o okur) ya da kadın / erkek çifti olarak seçilir
# (çiftlerde kadınları ilki, erkekleri Antalia-2 Mini okur; ek yoksa EMA Lightning + Antalia-2 Mini çifti)
TTS_KEYS = {"vox": "VoxCPM2", "ema": "EMA Lightning", "antalia": "Antalia-2 Mini", "ahenkcep": "Ahenk Cep",
            "ahenk": "Ahenk", "cepant": "Ahenk Cep + Antalia-2 Mini", "ahenkant": "Ahenk + Antalia-2 Mini"}
AHENK_TTS = {"ahenkcep", "ahenk", "cepant", "ahenkant"}   # algılanan duyguyla okur
TTS_VOICE = {"ema": "kadin", "antalia": "erkek"}   # tek sesli hazır modeller (engine.dub'daki ses adı)
STEP_DEFAULT = {"vox": E.VOX_STEPS, **{t: E.AHENK_STEPS for t in AHENK_TTS}}
# Hızlı: Kadın = Ahenk Cep, Otomatik = Ahenk Cep (kadın) + Antalia (erkek), Erkek = Antalia; Ahenk Cep 16 adım sabit
HIZLI_TTS = {"hizli-kadin": "ahenkcep", "hizli-oto": "cepant", "hizli-erkek": "antalia"}
# Hazır modlar (ayarları kilitli): UVR + MiLMMT + VoxCPM2, sabit adım sayısıyla. Varyant: "detayli~vox~orta"
PRESETS = {"orta": 10, "guclu": 20}


def preset_of(variant: str):
    return next((o for o in variant.split("~")[1:] if o in PRESETS), None)


def parse_variant(variant: str) -> tuple:
    base, *opts = variant.split("~")
    mt = next((o for o in opts if o in E.MT_MODELS), E.MT_DEFAULT)
    tts = next((o for o in opts if o in TTS_KEYS), "std")
    return base, mt, tts, opts


def split_variant(variant: str) -> tuple:
    base, mt, _, _ = parse_variant(variant)
    return base, mt


def tts_of(variant: str) -> str:
    return parse_variant(variant)[2]


def dub_tts(variant: str) -> str:
    """dublajda gerçekten çalışan ses modeli (Hızlı'nın sesleri dahil; "std" = EMA + Antalia çifti)"""
    return HIZLI_TTS.get(variant.split("~")[0]) or tts_of(variant)


def uses_emotion(variant: str) -> bool:
    return dub_tts(variant) in AHENK_TTS


def fixed_steps(variant: str):
    """hazır modlarda adım sayısı sabit: Orta / Güçlü (VoxCPM2), Hızlı (Ahenk Cep 16)"""
    pre = preset_of(variant)
    if pre:
        return PRESETS[pre]
    return E.AHENK_STEPS if variant.startswith("hizli") else None


def valid_variant(variant: str) -> bool:
    base, mt, tts, opts = parse_variant(variant)
    pre = [o for o in opts if o in PRESETS]
    return (base in BASES and len(opts) == len(set(opts))
            and all(o in E.MT_MODELS or o in TTS_KEYS or o in PRESETS for o in opts)
            and sum(o in TTS_KEYS for o in opts) <= 1
            and (base.startswith("detayli") or (mt == E.MT_DEFAULT and tts == "std"))
            and (not pre or (len(pre) == 1 and base == "detayli" and tts == "vox" and mt == E.MT_DEFAULT)))


def is_det(variant: str) -> bool:
    return variant.startswith("detayli")


def uses_gender(variant: str) -> bool:
    """kadın / erkek ayırma çalışır mı: Detaylı ve Hızlı · Otomatik"""
    return is_det(variant) or variant.split("~")[0] == "hizli-oto"


def sep_of(variant: str) -> str:
    """ses ayırma modeli: Gelişmiş'te seçilen ("detayli" = UVR); Hızlı ve hazır modlar (Orta, Güçlü) BandIt v2"""
    base = split_variant(variant)[0]
    if base.startswith("detayli-"):
        return base.split("-", 1)[1]
    return FIXED_SEP if base.startswith("hizli") or preset_of(variant) else "uvr"


def mt_of(variant: str) -> str:
    return split_variant(variant)[1]


def mtsfx(variant: str) -> str:
    mt = mt_of(variant)
    return "" if mt == E.MT_DEFAULT else "~" + mt


def sfx(variant: str) -> str:
    """Ayırma modeline göre dosya eki (ayırmadan sonraki her şey o ayırmaya bağlıdır)."""
    s = sep_of(variant)
    return "" if s == "uvr" else "_" + s


def fname(variant: str, base: str) -> str:
    """ayırmaya bağlı dosya (ses, metin, kadın/erkek)"""
    stem, ext = base.rsplit(".", 1)
    return f"{stem}{sfx(variant)}.{ext}"


def fname_mt(variant: str, base: str) -> str:
    """ayırmaya ve çeviri modeline bağlı dosya (çeviri)"""
    stem, ext = base.rsplit(".", 1)
    return f"{stem}{sfx(variant)}{mtsfx(variant)}.{ext}"


def fname_dub(variant: str, base: str) -> str:
    """ayırmaya, çeviri modeline ve ses modeline bağlı dosya (dublaj)"""
    stem, ext = base.rsplit(".", 1)
    t, pre = tts_of(variant), preset_of(variant)
    return f"{stem}{sfx(variant)}{mtsfx(variant)}{'' if t == 'std' else '~' + t}{'~' + pre if pre else ''}.{ext}"


def stages_for(variant: str) -> list:
    sep = SEPS.get(sep_of(variant), "")
    st = [("separate", "Ses Ayırma" + (f" ({sep})" if sep else "")), ("transcribe", "İngilizce Metin")]
    if uses_gender(variant):
        st.append(("gender", "Kadın / Erkek Ayırma"))
    t = dub_tts(variant)
    if uses_emotion(variant):
        st.append(("emotion", "Duygu Algılama"))
    mt = mt_of(variant)
    st += [("translate", "Türkçe Çeviri" + (f" ({E.MT_MODELS[mt]['name']})" if mt != E.MT_DEFAULT else "")),
           ("dub", "Türkçe Dublaj" + (f" ({TTS_KEYS[t]})" if t in TTS_KEYS else ""))]
    return st


def _rmtree_later(path: Path, tries: int = 20) -> None:
    def run():
        for _ in range(tries):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
            time.sleep(3)
    threading.Thread(target=run, daemon=True).start()


def load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(p: Path, obj) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def build_sentences(words: list) -> list:
    out, cur = [], []
    import re
    abbrev = re.compile(r"^(mr|mrs|ms|dr|st|vs|etc|jr|sr|e\.g|i\.e|u\.s)\.$", re.I)

    def flush():
        if cur:
            text = " ".join("".join(w[2] for w in cur).split())
            if text:
                out.append({"start": round(cur[0][0], 3), "end": round(cur[-1][1], 3), "en": text})
            cur.clear()

    for w in words:
        if cur and w[0] - cur[-1][1] > 1.0:
            flush()
        cur.append(w)
        dur = cur[-1][1] - cur[0][0]
        tok = w[2].strip()
        if tok.endswith((".", "?", "!", "…")) and not abbrev.match(tok) and dur >= 0.8:
            flush()
        elif dur > 9 and tok.endswith((",", ";", ":")):
            flush()
        elif dur > 14:
            flush()
    flush()
    return out


# ------------------------------------------------------------------ model hızları (kalıcı)
STATS_FILE = E.BASE / ("model_hizlari_demo.json" if E.DEMO else "model_hizlari.json")
STAGE_GROUPS = [("separate", "Ses ayırma"), ("transcribe", "İngilizce metin"), ("gender", "Kadın / erkek ayırma"),
                ("emotion", "Duygu algılama"), ("translate", "Türkçe çeviri"), ("dub", "Türkçe ses")]
SEP_NAMES = {"uvr": "UVR-MDX-NET Voc FT", "bandit2": "BandIt v2 · DnR Multi", "tiger": "TIGER-DnR"}


def stage_model(stage: str, variant: str) -> tuple:
    """aşamada çalışan modelin (anahtar, ad) bilgisi"""
    if stage == "separate":
        s = sep_of(variant)
        return "sep:" + s, SEP_NAMES.get(s, s)
    if stage == "transcribe":
        return "asr:whisper-turbo", "Whisper Large V3 Turbo"
    if stage == "gender":
        return "gender:vgc", "Voice Gender Classifier"
    if stage == "emotion":
        return "emo:jh", "Emotion DistilRoBERTa + ses şiddeti"
    if stage == "translate":
        mt = mt_of(variant)
        return "mt:" + mt, E.MT_MODELS[mt]["name"]
    if stage == "dub":
        t = dub_tts(variant)
        if t in TTS_KEYS:
            return "tts:" + t, TTS_KEYS[t]
        return "tts:ema+antalia", "EMA Lightning + Antalia-2 Mini"
    return stage, stage


class SpeedStats:
    """Her modelin işlediği video süresi ve harcadığı süre; uygulama kapansa da saklanır.
    Ortalama = toplam işlem süresi / toplam video süresi (tüm videolar birlikte)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = load_json(STATS_FILE, {}) or {}
        self.data.setdefault("models", {})
        old = self.data["models"].pop("tts:vox", None)
        if old:                              # önceki ölçümler 10 adımla yapılmıştı
            old.update(name="VoxCPM2 · 10 adım")
            self.data["models"].setdefault("tts:vox:10", old)
        gone = [k for k in self.data["models"] if (k.startswith("mt:") and k[3:] not in E.MT_MODELS)
                or k.startswith("vf:")]
        for k in gone:                       # kaldırılan modellerin ölçümleri (eski çeviri modelleri, VoiceFixer)
            self.data["models"].pop(k, None)
        if gone or old:
            try:
                save_json(STATS_FILE, self.data)
            except OSError:
                pass

    def add(self, stage: str, key: str, name: str, video_s: float, proc_s: float) -> None:
        if video_s < 3 or proc_s <= 0:
            return
        with self.lock:
            m = self.data["models"].setdefault(key, {"sec": 0.0, "video": 0.0, "n": 0})
            m.update(stage=stage, name=name)
            m["sec"] = round(m["sec"] + proc_s, 2)
            m["video"] = round(m["video"] + video_s, 2)
            m["n"] += 1
            try:
                save_json(STATS_FILE, self.data)
            except OSError as e:
                print("[istatistik]", e, flush=True)

    def summary(self) -> dict:
        with self.lock:
            rows = [{"key": k, "stage": m.get("stage"), "name": m.get("name", k), "n": m["n"],
                     "video_min": round(m["video"] / 60, 2), "per_min": round(m["sec"] / m["video"] * 60, 1)}
                    for k, m in self.data["models"].items() if m.get("video", 0) > 0]
        return {"ok": True, "groups": STAGE_GROUPS, "models": rows}


# ------------------------------------------------------------------ video
class Video:
    def __init__(self, vid: str, path: Path, name: str):
        self.id = vid
        self.path = path
        self.name = name
        self.dir = E.WORK / vid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.duration = E.probe_duration(path)
        if not E.has_audio(path):
            raise RuntimeError("Videoda ses kanalı yok.")
        self.chunks: list[dict] = []       # {"i", "start", "end"} — zamana göre sıralı tutulur
        self.next_id = 0
        self.lock = threading.Lock()
        self.range = (0.0, self.duration)  # son kullanılan işlem aralığı (cümle sınırına oturtulmuş)
        save_json(self.dir / "meta.json", {"name": name, "path": str(path), "duration": self.duration})

    def sorted(self) -> list:
        with self.lock:
            return sorted(self.chunks, key=lambda c: c["start"])

    def by_id(self, i: int) -> dict:
        for c in self.chunks:
            if c["i"] == i:
                return c
        raise KeyError(i)

    def at(self, t: float):
        for c in self.chunks:
            if c["start"] <= t < c["end"]:
                return c
        return None

    def gap_at(self, t: float):
        """t'yi içeren planlanmamış aralık (başlangıç, bitiş)"""
        lo, hi = 0.0, self.duration
        for c in self.chunks:
            if c["end"] <= t + 1e-6:
                lo = max(lo, c["end"])
            elif c["start"] > t:
                hi = min(hi, c["start"])
        return lo, hi

    def add(self, a: float, b: float) -> dict:
        with self.lock:
            c = {"i": self.next_id, "start": round(a, 3), "end": round(b, 3)}
            self.next_id += 1
            self.chunks.append(c)
            return c

    def new_id(self) -> int:
        with self.lock:
            self.next_id += 1
            return self.next_id - 1

    def covered(self, lo: float = 0.0, hi: float | None = None) -> bool:
        hi = self.duration if hi is None else hi
        t = lo
        for c in self.sorted():
            if c["end"] <= t + 0.06:
                continue
            if c["start"] > t + 0.06:
                return False
            t = max(t, c["end"])
            if t >= hi - 0.06:
                return True
        return t >= hi - 0.06

    def cdir(self, k: int) -> Path:
        d = self.dir / f"c{k:03d}"
        d.mkdir(exist_ok=True)
        return d


def dub_file(variant: str) -> str:
    """varyantın dublaj dosyasının adı"""
    if is_det(variant):
        return fname_dub(variant, "dub_oto.wav")
    g = variant.split("~")[0].split("-")[1]
    if g == "oto":                                   # Hızlı · Otomatik: kadınları EMA, erkekleri Antalia okur
        return "dub_auto.wav"
    return f"dub_{g}.wav"


def chunk_ready(v: Video, k: int, variant: str) -> bool:
    """oynatılabilir: dublaj hazır"""
    d = v.dir / f"c{k:03d}"
    raw = dub_file(variant)
    return ((d / raw).exists() and (d / (raw[:-4] + ".json")).exists()
            and (d / fname(variant, "vocals.wav")).exists())


chunk_done = chunk_ready


# ------------------------------------------------------------------ işleyici
class Job:
    def __init__(self, video: Video, variant: str, buffering: bool = True):
        self.video = video
        self.variant = variant
        self.buffering = buffering
        self.focus = 0.0
        self.cancel = False
        self.stopped = False
        self.error = None
        self.current = None          # {"chunk", "stage", "start", "end", "t0"}
        self.chunk_t0 = None
        self.lock = threading.Lock()
        self.stage_state = {}        # parça no → {aşama: (durum, ilerleme, ayrıntı)}
        self.seed = 0                # "Tekrar Dublajla" yeni tohumla farklı okuma üretir
        self.chunk_len = CHUNK_LEN   # buffering açıkken parça süresi (cümle sınırına oturtulur)
        self.vox_steps = STEP_DEFAULT.get(dub_tts(variant), 10)  # ses modelinin adım sayısı (az: hızlı, çok: doğal)
        self.timing = {}             # aşama → bu dublajda harcanan sn (önbellekten gelenler hariç)
        self.timing_video = {}       # aşama → işlenen video sn
        self.range = (0.0, video.duration)

    def next_len(self) -> float:
        return float(self.chunk_len) if self.buffering else NOBUF_LEN


class Manager:
    def __init__(self):
        self.engine = E.Engine()
        self.stats = SpeedStats()
        self.videos: dict[str, Video] = {}
        self.job: Job | None = None
        self.cv = threading.Condition()
        self.export_state = {"running": False}
        for p in E.WORK.iterdir():               # önceki oturumların kayıtları: her açılış sıfırdan
            if p.name not in KEEP:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---- videolar
    def open_path(self, path: Path, name: str | None = None) -> Video:
        path = Path(path)
        if not path.is_file():
            raise RuntimeError("Dosya bulunamadı.")
        with self.cv:                            # açık olan işi bırak, eski kayıtları sil
            if self.job:
                self.job.cancel = True
                self.job = None
            old = list(self.videos.values())
            self.videos.clear()
            self.cv.notify_all()
        for o in old:
            _rmtree_later(o.dir)
            if o.path.parent in (E.WORK / "_yuklenen", E.WORK / "_youtube") and o.path != path:
                threading.Thread(target=lambda p=o.path: (time.sleep(5), p.unlink(missing_ok=True)), daemon=True).start()
        vid = uuid.uuid4().hex[:16]
        v = Video(vid, path, name or path.name)
        self.videos[vid] = v
        return v

    def get(self, vid: str) -> Video:
        v = self.videos.get(vid)
        if v is None:
            raise KeyError(vid)
        return v

    # ---- iş kontrolü
    def _snap_range(self, v: Video, rng) -> tuple:
        """Kullanıcının seçtiği aralığı cümle ortasını bölmeyecek noktalara oturtur."""
        D = v.duration
        if not rng:
            return (0.0, D)
        a, b = max(0.0, float(rng[0])), min(D, float(rng[1]))
        if b - a < 5:
            b = min(D, a + 5)
        pa, pb = v.range                         # değişmeyen uç yeniden oturtulmaz (her seferinde kaymasın)
        if abs(a - pa) >= 0.05:
            a = 0.0 if a < 1.0 else E.find_pause(v.path, max(0.0, a - 6), min(D, a + 1), a)
        if abs(b - pb) >= 0.05:
            b = D if b > D - 1.0 else E.find_pause(v.path, max(0.0, b - 1), min(D, b + 6), b)
        if b <= a + 3:
            b = min(D, a + 5)
        return (round(a, 3), round(b, 3))

    def _apply(self, job: Job, v: Video, variant: str, rng):
        if rng is not None:
            v.range = self._snap_range(v, rng)
        job.range = v.range

    def _reset_chunks(self, v: Video) -> None:
        """Aralık değişti: önceki tüm parçalar atılır, seçili aralık baştan işlenir."""
        with v.lock:
            old, v.chunks = v.chunks, []
        for c in old:
            _rmtree_later(v.dir / f"c{c['i']:03d}")

    def start(self, vid: str, variant: str, focus: float = 0.0, buffering: bool = True, rng=None,
              fresh: bool = False, chunk_len=None, vox_steps=None) -> Job:
        if not valid_variant(variant):
            raise ValueError("geçersiz mod")
        v = self.get(vid)
        with self.cv:
            same = (self.job and not self.job.cancel and self.job.video.id == vid and self.job.variant == variant
                    and not fresh)
            if self.job and not same:
                self.job.cancel = True
            if fresh:
                self._reset_chunks(v)
            if not same:
                old = self.job if self.job and self.job.video.id == vid and self.job.variant == variant else None
                self.job = Job(v, variant, buffering)
                if old:                           # durdurup devam: ölçülen hız korunur
                    self.job.seed, self.job.chunk_len, self.job.vox_steps = old.seed, old.chunk_len, old.vox_steps
                    if not fresh:                 # durdurup devam: süreler birikmeye devam eder
                        self.job.timing, self.job.timing_video = old.timing, old.timing_video
            self.job.buffering = bool(buffering)
            if chunk_len:
                self.job.chunk_len = float(np.clip(float(chunk_len), CHUNK_MIN, CHUNK_MAX))
            if vox_steps:
                self.job.vox_steps = int(np.clip(int(vox_steps), VOX_STEPS_MIN, VOX_STEPS_MAX))
            if fixed_steps(variant):                   # hazır mod: adım sayısı sabit
                self.job.vox_steps = fixed_steps(variant)
            self._apply(self.job, v, variant, rng)
            if not self.job.buffering:
                self._single_unit(v, variant)
            self.job.focus = focus
            self.job.error = None
            self.cv.notify_all()
            return self.job

    def _single_unit(self, v: Video, variant: str) -> None:
        """Buffering kapalı: aralığın bu modda bitmemiş kısmı TEK PARÇA işlensin. Önceki çalışmalardan (buffering açıkken
        ya da başka bir modelle) kalan parça planı videoya bağlı olduğu için yeniden kullanılırsa iş parça parça
        yürüyordu. Bitmemiş parçalar atılır, boşluk _plan_next'te tek parça olarak planlanır (bitmiş parçalar kalır)."""
        ra, rb = self.job.range
        with v.lock:
            drop = [c for c in v.chunks if c["end"] > ra and c["start"] < rb and not chunk_done(v, c["i"], variant)]
            if not drop:
                return
            v.chunks = [c for c in v.chunks if c not in drop]
        cur = self.job.current
        if cur and any(c["i"] == cur["chunk"] for c in drop):   # o anda işlenen parça atıldı: işi yeniden başlat
            old = self.job
            old.cancel = True
            self.job = Job(v, variant, False)
            self.job.seed, self.job.chunk_len, self.job.vox_steps = old.seed, old.chunk_len, old.vox_steps
            self.job.timing, self.job.timing_video, self.job.range = old.timing, old.timing_video, old.range
        for c in drop:
            _rmtree_later(v.dir / f"c{c['i']:03d}")

    def stop(self, vid: str, variant: str) -> None:
        with self.cv:
            if self.job and self.job.video.id == vid and self.job.variant == variant:
                self.job.cancel = True
                self.job.stopped = True
                self.cv.notify_all()

    def redo(self, vid: str, variant: str, focus: float = 0.0, buffering: bool = True, rng=None,
             chunk_len=None, vox_steps=None) -> Job:
        """Aynı ayarlarla yeniden dublaj: çeviri dahil önceki aşamalar korunur, ses yeni tohumla yeniden okunur."""
        if not valid_variant(variant):
            raise ValueError("geçersiz mod")
        v = self.get(vid)
        with self.cv:
            old = self.job
            if self.job:
                self.job.cancel = True
            raw = dub_file(variant)
            for c in v.sorted():
                d = v.dir / f"c{c['i']:03d}"
                for f in (raw, raw[:-4] + ".json"):
                    (d / f).unlink(missing_ok=True)
            self.job = Job(v, variant, buffering)
            if old and old.video.id == vid and old.variant == variant:
                self.job.chunk_len, self.job.vox_steps = old.chunk_len, old.vox_steps
            if chunk_len:
                self.job.chunk_len = float(np.clip(float(chunk_len), CHUNK_MIN, CHUNK_MAX))
            if vox_steps:
                self.job.vox_steps = int(np.clip(int(vox_steps), VOX_STEPS_MIN, VOX_STEPS_MAX))
            if fixed_steps(variant):                   # hazır mod: adım sayısı sabit
                self.job.vox_steps = fixed_steps(variant)
            self.job.seed = random.randint(1, 1_000_000)
            self._apply(self.job, v, variant, rng)
            self.job.focus = focus
            self.cv.notify_all()
            return self.job

    def set_range(self, vid: str, variant: str, rng) -> None:
        v = self.get(vid)
        v.range = self._snap_range(v, rng)
        with self.cv:
            if self.job and self.job.video.id == vid and self.job.variant == variant:
                self.job.range = v.range
                self.cv.notify_all()

    def set_focus(self, vid: str, variant: str, t: float):
        with self.cv:
            if self.job and self.job.video.id == vid and self.job.variant == variant:
                self.job.focus = float(t)
                self.cv.notify_all()

    def status(self, vid: str, variant: str) -> dict:
        v = self.get(vid)
        j = self.job if (self.job and self.job.video.id == vid and self.job.variant == variant) else None
        job = j if j and not j.cancel else None
        ra, rb = (j.range if j else v.range)
        chunks, ready_dur = [], 0.0
        cur_j = job.current if job else None
        for c in v.sorted():
            busy = bool(cur_j and cur_j["chunk"] == c["i"])
            # işlenirken dublajı hazırsa çalınabilir; dilimlenecek tek parça hariç
            split = busy and not job.buffering and c["end"] - c["start"] > NOBUF_LEN
            ready = chunk_ready(v, c["i"], variant) and not split
            st = "hazir" if ready else ("isleniyor" if busy else "bekliyor")
            done = ready and not busy and chunk_done(v, c["i"], variant)
            if done:
                ready_dur += max(0.0, min(c["end"], rb) - max(c["start"], ra))
            chunks.append({"i": c["i"], "start": c["start"], "end": c["end"], "state": st, "done": done})
        complete = v.covered(ra, rb) and all(c["done"] for c in chunks if c["end"] > ra and c["start"] < rb)
        cur = None
        if job and job.current:
            cur = dict(job.current)
            cur["elapsed"] = round(time.time() - (job.chunk_t0 or time.time()), 1)
            cur["stages"] = [{"key": key, "name": name, **dict(zip(("status", "progress", "detail"),
                              job.stage_state.get(cur["chunk"], {}).get(key, ("bekliyor", 0, ""))))}
                             for key, name in stages_for(variant)]
        return {
            "ok": True, "video_id": vid, "variant": variant, "duration": v.duration,
            "chunks": chunks, "ready": len([c for c in chunks if c["state"] == "hazir"]), "total": len(chunks),
            "ready_dur": round(ready_dur, 2), "complete": complete,
            "running": bool(job and not job.error and not complete),
            "stopped": bool(j and j.stopped and not complete),
            "buffering": bool((job or j).buffering) if (job or j) else True,
            "chunk_len": (job or j).chunk_len if (job or j) else CHUNK_LEN,
            "timing": {k: round(x, 1) for k, x in (j.timing.items() if j else [])},
            "timing_video": {k: round(x, 1) for k, x in (j.timing_video.items() if j else [])},
            "current": cur, "error": job.error if job else None,
            "range": [ra, rb],
            "stages": [{"key": k, "name": n} for k, n in stages_for(variant)],
        }

    def speakers(self, vid: str, variant: str) -> dict:
        """Videodaki kişiler: tüm cümlelerin ses izleri birlikte kümelenir (kadın / erkek en çok 10'ar kişi)."""
        v = self.get(vid)
        ra, rb = v.range
        chunks = [c for c in v.sorted() if c["end"] > ra and c["start"] < rb and chunk_ready(v, c["i"], variant)]
        key = (variant, tuple((c["i"], c["start"], c["end"]) for c in chunks))
        cache = getattr(self, "_spk_cache", {})
        self._spk_cache = cache
        if vid in cache and cache[vid][0] == key:
            return cache[vid][1]
        items = []
        for c in chunks:
            d = v.dir / f"c{c['i']:03d}"
            segs = (load_json(d / fname(variant, "asr.json"), {}) or {}).get("segments", [])
            gj = load_json(d / fname(variant, "gender.json"), {}) or {}
            emb = (load_json(d / fname(variant, "spk.json"), {}) or {}).get("emb", [])
            lb, sc = gj.get("labels", []), gj.get("scores", [])
            for j, s in enumerate(segs):
                e = emb[j] if j < len(emb) else None
                items.append({"k": c["i"], "j": j, "dur": s["end"] - s["start"], "words": len(s.get("en", "").split()),
                              "g": lb[j] if j < len(lb) else None, "p": sc[j] if j < len(sc) else 0.5,
                              "e": np.asarray(e, dtype=np.float64) if e else None})
        res = cluster_speakers(items)
        cache[vid] = (key, res)
        return res

    def chunk_info(self, vid: str, variant: str, k: int) -> dict:
        v = self.get(vid)
        if not chunk_ready(v, k, variant):
            return {"ok": False}
        c = v.by_id(k)
        d = v.dir / f"c{k:03d}"
        a, b = c["start"], c["end"]
        asr = load_json(d / fname(variant, "asr.json"), {}) or {}
        tr = (load_json(d / fname_mt(variant, "tr.json"), {}) or {}).get("tr", [])
        raw = dub_file(variant)
        dj = load_json(d / (raw[:-4] + ".json"), {}) or {}
        voices, timings, emos = dj.get("voices", []), dj.get("timings", []), dj.get("emotions", [])
        segs = []
        for i, s in enumerate(asr.get("segments", [])):
            t = timings[i] if i < len(timings) else None
            segs.append({"start": round(a + s["start"], 3), "end": round(a + s["end"], 3), "en": s["en"],
                         "tr": tr[i] if i < len(tr) else "", "g": voices[i] if i < len(voices) else None,
                         "ds": round(a + t[0], 3) if t else None, "de": round(a + t[1], 3) if t else None,
                         **({"emo": emos[i]} if i < len(emos) else {})})
        base = f"/media/{vid}/c{k:03d}/"
        audio = {"dub": f"{base}{raw}?v={int((d / raw).stat().st_mtime)}", "bg": f"{base}{fname(variant, 'inst.wav')}",
                 "en": f"{base}{fname(variant, 'vocals.wav')}"}
        return {"ok": True, "i": k, "start": a, "end": b, "segments": segs, "audio": audio}

    # ---- dışa aktarma
    EXPORT_KINDS = {"video": "Video (MP4)", "dub": "Yalnız dublaj sesi", "en": "Yalnız İngilizce ses",
                    "bg": "Yalnız arka plan sesi", "all": "Tüm sesler"}

    def export(self, vid: str, variant: str, kind: str, gains: dict, out: str) -> None:
        if kind not in self.EXPORT_KINDS:
            raise ValueError("geçersiz dışa aktarma türü")
        if kind == "video" and getattr(self.get(vid), "youtube", None):
            raise RuntimeError("YouTube videolarında görüntü indirilmediği için video (MP4) dışa aktarılamaz; "
                               "sesleri WAV olarak dışa aktarabilirsiniz.")
        if self.export_state.get("running"):
            raise RuntimeError("Önceki dışa aktarma sürüyor.")
        v = self.get(vid)
        need_dub = kind in ("video", "dub", "all")
        ra, rb = v.range
        if not v.covered(ra, rb):
            raise RuntimeError("Önce dublajın tamamlanması gerekiyor.")
        for c in v.sorted():
            if c["end"] <= ra or c["start"] >= rb:
                continue
            k = c["i"]
            d = v.dir / f"c{k:03d}"
            if need_dub and not chunk_ready(v, k, variant):
                raise RuntimeError("Önce dublajın tamamlanması gerekiyor.")
            if not (d / fname(variant, "vocals.wav")).exists():
                raise RuntimeError("Ses ayırma henüz tamamlanmadı.")
        st = {"running": True, "kind": kind, "label": self.EXPORT_KINDS[kind], "progress": 0.0, "error": None,
              "path": out, "done": False}
        self.export_state = st
        threading.Thread(target=self._export_run, args=(v, variant, kind, gains, Path(out), st),
                         daemon=True).start()

    def _export_run(self, v: Video, variant: str, kind: str, gains: dict, out: Path, st: dict):
        try:
            if kind == "video":
                g = {c: float(gains.get(c, 1.0)) for c in ("dub", "bg", "en")}
                tmp = v.dir / "_export_mix.wav"
                self._mix_to_wav(v, variant, g, tmp, lambda p: st.update(progress=p * 0.7))
                st["progress"] = 75.0
                part = out.with_name(out.stem + ".part" + out.suffix)
                ra, rb = v.range
                full = ra < 0.5 and rb > v.duration - 0.5
                enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
                src = ["-i", v.path] if full else ["-ss", f"{ra:.3f}", "-t", f"{rb - ra:.3f}", "-i", v.path]
                tail = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", part]
                maps = ["-map", "0:v:0", "-map", "1:a:0"]
                if full:
                    try:
                        E.ffmpeg(*src, "-i", tmp, *maps, "-c:v", "copy", *tail)
                    except RuntimeError:
                        st["label"] = "Video (MP4) · görüntü yeniden kodlanıyor"
                        E.ffmpeg(*src, "-i", tmp, *maps, *enc, *tail)
                else:                                     # seçili aralık: kare doğruluğu için yeniden kodlanır
                    st["label"] = "Video (MP4) · seçili aralık kodlanıyor"
                    E.ffmpeg(*src, "-i", tmp, *maps, *enc, *tail)
                os.replace(part, out)
                tmp.unlink(missing_ok=True)
            else:
                g = {"dub": {"dub": 1.0}, "en": {"en": 1.0}, "bg": {"bg": 1.0}}.get(kind)
                if g is None:
                    g = {c: float(gains.get(c, 1.0)) for c in ("dub", "bg", "en")}
                part = out.with_name(out.stem + ".part.wav")
                self._mix_to_wav(v, variant, g, part, lambda p: st.update(progress=p * 0.99))
                os.replace(part, out)
            st.update(progress=100.0, done=True)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            st["error"] = str(e)
        finally:
            st["running"] = False

    def _mix_to_wav(self, v: Video, variant: str, gains: dict, dst: Path, prog) -> None:
        """Seçili aralıktaki parça seslerini 48 kHz stereo tek dosyada birleştirir (bellekte tek parça tutar).
        Dublaj parçası bir sonraki parçaya taşabilir; taşan kısım bir sonrakine eklenir."""
        SR = 48000
        ra, rb = v.range
        files = {"bg": fname(variant, "inst.wav"), "en": fname(variant, "vocals.wav")}
        base = int(round(ra * SR))
        total = int(round(rb * SR)) - base
        plan = [c for c in v.sorted() if c["end"] > ra and c["start"] < rb]
        n = len(plan)
        carry = np.zeros((0, 2), dtype=np.float32)

        def load(path: Path) -> np.ndarray:
            x, sr = E.read_wav(path)
            x = E.resample(x, sr, SR).astype(np.float32)
            if x.ndim == 1:
                x = x[:, None]
            if x.shape[1] == 1:
                x = np.repeat(x, 2, axis=1)
            return x[:, :2]

        written = 0
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            for idx, c in enumerate(plan):
                k, a, b = c["i"], c["start"], c["end"]
                n0 = int(round(max(a, ra) * SR)) - base
                n1 = total if idx == n - 1 else int(round(min(b, rb) * SR)) - base
                n0 = max(n0, written)
                L = max(0, n1 - n0)
                off = int(round(max(a, ra) * SR)) - int(round(a * SR))     # parçanın aralık öncesi kısmı atlanır
                seg = np.zeros((L, 2), dtype=np.float32)
                m = min(L, len(carry))
                seg[:m] += carry[:m]
                carry = carry[m:]
                d = v.dir / f"c{k:03d}"
                for ch, g in gains.items():
                    if g <= 0:
                        continue
                    x = load(d / (dub_file(variant) if ch == "dub" else files[ch]))[off:] * g
                    m = min(L, len(x))
                    seg[:m] += x[:m]
                    if ch == "dub" and len(x) > L and idx < n - 1:   # dublaj kuyruğu sonraki parçaya
                        over = x[L:]
                        if len(carry) < len(over):
                            carry = np.concatenate([carry, np.zeros((len(over) - len(carry), 2), np.float32)])
                        carry[: len(over)] += over
                w.writeframes((np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes())
                written = n0 + L
                prog((idx + 1) / n * 100)

    # ---- parça planlama
    def _plan_next(self, job: Job):
        """Sıradaki işlenecek parça (gerekirse yeni parça planlar). İşlenecek bir şey kalmadıysa None.
        Yalnızca job.range içinde çalışır. Buffering kapalıysa boşluğun tamamı tek parça olur."""
        v = job.video
        ra, rb = job.range
        start_at = job.focus if job.buffering else ra
        start_at = min(max(ra, start_at), max(ra, rb - 0.05))
        passes = [(start_at, rb)] + ([(ra, start_at)] if start_at > ra else [])
        for lo, hi in passes:
            t = lo
            while t < hi - 0.01:
                c = v.at(t)
                if c:
                    if not chunk_done(v, c["i"], job.variant):
                        return c
                    t = c["end"]
                    continue
                g0, g1 = v.gap_at(t)
                g0, g1 = max(g0, ra), min(g1, rb)
                if g1 - g0 < 0.05:                # yuvarlama artığı: kapsanmış sayılır (covered ile aynı pay)
                    t = max(t + 0.05, g1)
                    continue
                if not job.buffering:             # tek parça: aralığın boş kısmının tamamı
                    return v.add(g0, g1)
                s0 = g0
                if t - g0 > 20.0:                 # izleyici boşluğun ortasında: oradan başla (cümle sınırında)
                    s0 = max(g0, E.find_pause(v.path, max(g0, t - 10.0), t, t))
                    if s0 - g0 < 1.0:             # önünde küçük boşluk kalmasın
                        s0 = g0
                L = job.next_len()
                tail = min(30.0, L / 3)           # sona bundan kısa parça kalmasın: son parçaya katılır
                srch = min(SEARCH, L / 6)
                target = s0 + L
                if g1 - target < tail:
                    e0 = g1
                else:
                    lo2 = max(s0 + L * 0.6, target - srch)
                    hi2 = min(g1 - tail * 0.5, target + srch)
                    e0 = E.find_pause(v.path, lo2, hi2, target) if hi2 > lo2 + 1 else target
                    if g1 - e0 < tail * 0.5:
                        e0 = g1
                if e0 <= s0 + 5.0:
                    e0 = min(g1, s0 + L)
                return v.add(s0, e0)
        return None

    # ---- tek parça işlenen sesi oynatma için dilimleme (yeniden işleme yok)
    def _split_unit(self, v: Video, c: dict, variant: str, piece: float = 120.0) -> None:
        k, a, b = c["i"], c["start"], c["end"]
        d = v.dir / f"c{k:03d}"
        dur = b - a
        asr = load_json(d / fname(variant, "asr.json"), {}) or {}
        segs = asr.get("segments", [])
        raw = dub_file(variant)
        tim = (load_json(d / (raw[:-4] + ".json"), {}) or {}).get("timings", [])
        cands = []                                   # cümle aralarındaki boşlukların ortası (parça içi sn)
        for i in range(len(segs) - 1):
            e = max(segs[i]["end"], (tim[i][1] if i < len(tim) and tim[i] else 0.0))
            s_ = min(segs[i + 1]["start"], (tim[i + 1][0] if i + 1 < len(tim) and tim[i + 1] else 1e9))
            if s_ - e > 0.15:
                cands.append((e + s_) / 2)
        cuts, pos = [], 0.0
        while dur - pos > piece * 1.25:
            near = [x for x in cands if pos + piece * 0.5 <= x <= pos + piece * 1.5]
            cut = min(near, key=lambda x: abs(x - pos - piece)) if near else pos + piece
            cuts.append(cut)
            pos = cut
        if not cuts:
            return
        bounds = [0.0] + cuts + [dur]
        parts = [{"i": v.new_id(), "start": round(a + bounds[j], 3), "end": round(a + bounds[j + 1], 3)}
                 for j in range(len(bounds) - 1)]
        dirs = [v.dir / f"c{p['i']:03d}" for p in parts]
        for dd in dirs:
            dd.mkdir(exist_ok=True)
        last = len(parts) - 1
        # sesler: her dosya kendi örnekleme hızıyla, sınırlarda örnek kaybı olmadan
        for f in d.glob("*.wav"):
            if f.name == "mix.wav":
                continue
            sr, frames = _wav_info(f)
            for j, dd in enumerate(dirs):
                s0 = int(round(bounds[j] * sr))
                s1 = frames if j == last else int(round(bounds[j + 1] * sr))
                x = _wav_slice(f, min(s0, frames), min(s1, frames))
                E.write_wav(dd / f.name, x, sr)
        # metinler: cümle başlangıcına göre dilime atanır, zamanlar dilim başına kaydırılır
        def owner(t):
            for j in range(len(bounds) - 1):
                if t < bounds[j + 1] or j == last:
                    return j
            return last
        for f in d.glob("asr*.json"):
            sx = f.stem[3:]                                      # "" ya da "_bandit2" vb.
            aj = load_json(f, {}) or {}
            sg = aj.get("segments", [])
            own = [owner(x["start"]) for x in sg]
            gname = f"gender{sx}.json"
            gj = load_json(d / gname)
            sname = f"spk{sx}.json"
            spj = load_json(d / sname)
            ename = f"emotion{sx}.json"
            emj = load_json(d / ename)
            # çeviri ve dublaj dosyaları çeviri modeline göre ayrı olabilir: "tr<ayırma>[~<model>].json"
            trs = {g.name: (load_json(g, {}) or {}) for g in d.glob("tr*.json") if g.stem[2:].partition("~")[0] == sx}
            # Gelişmiş / Orta / Güçlü: "dub_oto<ayırma>[~...]" · Hızlı: dub_kadin / dub_auto / dub_erkek (BandIt v2)
            dubs = [g for g in d.glob("dub_*.json")
                    if (g.stem.startswith("dub_oto") and g.stem[7:].partition("~")[0] == sx)
                    or (not g.stem.startswith("dub_oto") and sx == "_" + FIXED_SEP)]
            dubj = {g.name: load_json(g, {}) or {} for g in dubs}
            for j, dd in enumerate(dirs):
                idx = [i for i, o in enumerate(own) if o == j]
                off = bounds[j]
                save_json(dd / f.name, {"segments": [{**sg[i], "start": round(sg[i]["start"] - off, 3),
                                                      "end": round(sg[i]["end"] - off, 3)} for i in idx]})
                for name, trj in trs.items():
                    tr = trj.get("tr", [])
                    save_json(dd / name, {"tr": [tr[i] if i < len(tr) else "" for i in idx]})
                if spj is not None:
                    em = spj.get("emb", [])
                    save_json(dd / sname, {"emb": [em[i] if i < len(em) else None for i in idx]})
                for gn, js in ((gname, gj), (ename, emj)):
                    if js is not None:
                        lb, sc = js.get("labels", []), js.get("scores", [])
                        save_json(dd / gn, {"labels": [lb[i] for i in idx if i < len(lb)],
                                            "scores": [sc[i] for i in idx if i < len(sc)]})
                for name, dj in dubj.items():
                    vo, tm, em = dj.get("voices", []), dj.get("timings", []), dj.get("emotions")
                    save_json(dd / name, {"voices": [vo[i] for i in idx if i < len(vo)],
                                          "timings": [([round(tm[i][0] - off, 3), round(tm[i][1] - off, 3)]
                                                       if i < len(tm) and tm[i] else None) for i in idx],
                                          **({"emotions": [em[i] for i in idx if i < len(em)]} if em else {})})
        with v.lock:
            alive = any(x["i"] == k for x in v.chunks)      # bu arada aralık değiştiyse (parçalar atıldıysa) ekleme
            if alive:
                v.chunks = [x for x in v.chunks if x["i"] != k] + parts
        if not alive:
            for dd in dirs:
                _rmtree_later(dd)
            return
        _rmtree_later(d)

    # ---- arka plan döngüsü
    def _loop(self):
        while True:
            with self.cv:
                while True:
                    job = self.job
                    if job and not job.cancel and not job.error:
                        break
                    self.cv.wait(timeout=1.0)
            try:
                c = self._plan_next(job)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                job.error = f"Parça planlanamadı: {e}"
                continue
            if c is None:
                job.current = None
                with self.cv:
                    if self.job is job:
                        self.cv.wait(timeout=1.0)
                continue
            k = c["i"]
            try:
                self._process(job, k)
            except _Cancelled:
                pass
            except Exception as e:  # noqa: BLE001
                if job.cancel or self.job is not job:   # iptal edilen işin artığı (parçası atılmış olabilir)
                    continue
                traceback.print_exc()
                job.error = f"Parça {fmt_t(c['start'])}–{fmt_t(c['end'])}: {e}"
                if job.current:
                    job.stage_state.setdefault(k, {})[job.current.get("stage") or "separate"] = ("hata", 0, str(e)[:300])
            finally:
                E.free_gpu()

    def _process(self, job: Job, k: int):
        v, variant = job.video, job.variant
        d = v.cdir(k)
        ch = v.by_id(k)
        a, b = ch["start"], ch["end"]
        dur = b - a
        ss = job.stage_state.setdefault(k, {})
        job.chunk_t0 = time.time()

        def check():
            if job.cancel or self.job is not job:
                raise _Cancelled()

        tinfo = {}                           # aşama süresi: indirme / yükleme beklemesi hariç

        def stage(key):
            E.free_gpu()                     # 8 GB'lık kartlarda modeller arası boş tampon belleği geri ver
            job.current = {"chunk": k, "stage": key, "start": a, "end": b, "t0": time.time()}
            ti = tinfo[key] = {"t0": time.time(), "dl": 0.0, "last": None}

            def on(status, progress, detail=""):
                if job.cancel:
                    raise _Cancelled()
                now = time.time()
                if ti["last"] and ti["last"][0] == "indiriliyor":
                    ti["dl"] += now - ti["last"][1]
                ti["last"] = (status, now)
                ss[key] = (status, float(progress), detail)
            ss[key] = ("calisiyor", 0, "")
            return on

        def done(key, cached=False, detail=""):
            ss[key] = ("onbellek" if cached else "tamam", 100.0, detail)
            ti = tinfo.get(key)
            if cached or not ti:
                return
            now = time.time()
            dl = ti["dl"] + (now - ti["last"][1] if ti["last"] and ti["last"][0] == "indiriliyor" else 0.0)
            sec = max(0.0, now - ti["t0"] - dl)
            job.timing[key] = job.timing.get(key, 0.0) + sec
            job.timing_video[key] = job.timing_video.get(key, 0.0) + dur
            mk, name = stage_model(key, variant)
            if mk[4:] in STEP_DEFAULT:                # adım sayısı seçilebilen modeller: adıma göre ayrı tutulur
                mk, name = f"{mk}:{job.vox_steps}", f"{name} · {job.vox_steps} adım"
            self.stats.add(key, mk, name, dur, sec)

        F = lambda base: d / fname(variant, base)          # noqa: E731
        FM = lambda base: d / fname_mt(variant, base)      # noqa: E731
        voc_f, inst_f = F("vocals.wav"), F("inst.wav")

        # 1) separation
        check()
        on = stage("separate")
        if voc_f.exists() and inst_f.exists():
            done("separate", True)
        else:
            on("calisiyor", 2, "Ses çıkarılıyor")
            mix = d / "mix.wav"
            E.ffmpeg("-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", v.path, "-vn", "-ac", "2", "-ar", "44100",
                     "-c:a", "pcm_s16le", mix)
            t0 = time.time()
            self.engine.separate(mix, voc_f, inst_f, on, model=sep_of(variant))
            mix.unlink(missing_ok=True)
            done("separate", detail=f"{time.time() - t0:.0f} sn")

        # 2) transcription
        check()
        on = stage("transcribe")
        asr = load_json(F("asr.json"))
        if asr is not None:
            done("transcribe", True)
        else:
            t0 = time.time()
            words = self.engine.transcribe(voc_f, dur, on)
            asr = {"segments": build_sentences(words)}
            save_json(F("asr.json"), asr)
            done("transcribe", detail=f"{len(asr['segments'])} cümle · {time.time() - t0:.0f} sn")
        segs = asr["segments"]

        # 2b) gender (Detaylı ve Hızlı · Otomatik)
        voices = None
        if uses_gender(variant):
            check()
            on = stage("gender")
            gj = load_json(F("gender.json"))
            if gj is not None and len(gj.get("labels", [])) == len(segs):
                done("gender", True)
            else:
                prior = {"kadin": 0.0, "erkek": 0.0}
                for cj in v.sorted():
                    if cj["start"] >= a:
                        continue
                    pj = load_json(v.dir / f"c{cj['i']:03d}" / fname(variant, "gender.json"))
                    for lab, sc in zip((pj or {}).get("labels", []), (pj or {}).get("scores", [])):
                        prior[lab] += abs(sc - 0.5) * 2
                t0 = time.time()
                labels, scores = self.engine.classify(voc_f, segs, prior, on) if segs else ([], [])
                gj = {"labels": labels, "scores": scores}
                save_json(F("gender.json"), gj)
                kc = sum(1 for x in labels if x == "kadin")
                done("gender", detail=f"{kc} kadın, {len(labels) - kc} erkek · {time.time() - t0:.0f} sn")
            voices = gj["labels"]
            sj = load_json(F("spk.json"))                 # konuşmacı ses izleri (kişi ayırma için)
            if not (sj and len(sj.get("emb", [])) == len(segs)):
                try:
                    save_json(F("spk.json"), {"emb": self.engine.speaker_embeddings(voc_f, segs) if segs else []})
                except Exception as e:  # noqa: BLE001 — kişi ayırma olmazsa kadın / erkek noktaları gösterilir
                    print("[konuşmacı]", e, flush=True)
        else:
            voices = [variant.split("-")[1]] * len(segs)

        # 2c) duygu (Ahenk modelleri): İngilizce metin + orijinal sesin şiddeti / perdesi
        emotions = None
        if uses_emotion(variant):
            check()
            on = stage("emotion")
            ej = load_json(F("emotion.json"))
            if ej is not None and len(ej.get("labels", [])) == len(segs):
                done("emotion", True)
            else:
                t0 = time.time()
                labels, scores = self.engine.emotions(voc_f, segs, voices, on) if segs else ([], [])
                ej = {"labels": labels, "scores": scores}
                save_json(F("emotion.json"), ej)
                cnt = {}
                for x in labels:
                    cnt[x] = cnt.get(x, 0) + 1
                top = ", ".join(f"{n} {E.EMO_TR.get(x, x)}" for x, n in sorted(cnt.items(), key=lambda kv: -kv[1])[:3])
                done("emotion", detail=f"{top} · {time.time() - t0:.0f} sn")
            emotions = ej["labels"]

        # 3) translation
        check()
        on = stage("translate")
        trj = load_json(FM("tr.json"))
        if trj is not None and len(trj.get("tr", [])) == len(segs):
            done("translate", True)
        else:
            t0 = time.time()
            tr = self.engine.translate([s["en"] for s in segs], on, model=mt_of(variant)) if segs else []
            save_json(FM("tr.json"), {"tr": tr})
            done("translate", detail=f"{time.time() - t0:.0f} sn")
            trj = {"tr": tr}
        tr = trj["tr"]

        # 4) dubbing
        check()
        on = stage("dub")
        raw = dub_file(variant)
        rawj = d / (raw[:-4] + ".json")
        if (d / raw).exists() and rawj.exists():
            done("dub", True)
        else:
            t0 = time.time()
            x, sr = E.read_wav(voc_f)
            target = E.active_rms(x.mean(axis=1), sr) or 0.08
            items = [{"start": s["start"], "end": s["end"], "tr": tr[i] if i < len(tr) else "", "en": s.get("en", "")}
                     for i, s in enumerate(segs)]
            t = dub_tts(variant)
            if t == "vox":                             # her cümle kendi İngilizce sesinden kopyalanır (duygu dahil)
                track, timings = self.engine.dub_vox(items, voc_f, voices, dur, target, on, seed=job.seed, work=d,
                                                     steps=job.vox_steps)
            elif t in AHENK_TTS:                       # Ahenk algılanan duyguyla; çiftlerde erkekleri Antalia okur
                pair = t in ("cepant", "ahenkant")
                track, timings = self.engine.dub_ahenk(items, voc_f, emotions or [], dur, target, on,
                                                       seed=job.seed, work=d, steps=job.vox_steps,
                                                       big=t in ("ahenk", "ahenkant"), voices=voices if pair else None)
            else:                                      # EMA / Antalia tek başına ya da (eski) kadın-erkek çifti
                who = [TTS_VOICE[t]] * len(voices) if t in TTS_VOICE else voices
                track, timings = self.engine.dub(items, who, dur, target, on, seed=job.seed)
            E.write_wav(d / raw, track, E.DUB_SR)
            save_json(rawj, {"voices": voices, "timings": timings, **({"emotions": emotions} if emotions else {})})
            done("dub", detail=f"{time.time() - t0:.0f} sn")

        if not job.buffering and dur > 150.0:          # tek parça işlendi → oynatma için dilimle
            check()
            try:
                self._split_unit(v, ch, variant)
            except Exception:  # noqa: BLE001 — dilimleme olmazsa tek büyük parça olarak kalır
                traceback.print_exc()
        job.current = None


# ------------------------------------------------------------------ kişi ayırma (ses izlerini kümeleme)
SPK_SAME, SPK_MAX, SPK_MIN_DUR = 0.38, 10, 4.0     # aynı kişi benzerlik eşiği, cinsiyet başına en çok kişi, en az konuşma


def cluster_speakers(items: list) -> dict:
    """items: [{k, j, dur, words, g, p (kadın olasılığı), e (ses izi ya da None)}]
    1) ≥1 sn cümlelerin izleri ortalama bağlantılı kümelemeyle birleştirilir (benzerlik ≥ SPK_SAME)
    2) her kümenin cinsiyeti, cümlelerin kadın olasılıklarının süreyle ağırlıklı oyu
    3) 4 sn'den az konuşan küçük kümeler aynı cinsiyetteki en benzer kümeye katılır
    4) kısa / izsiz cümleler en benzer kümeye ya da yakınındaki aynı cinsiyetli cümlenin kişisine verilir
    5) kişiler kelime sayısına göre sıralanır; cinsiyet başına 10'dan fazlası 10. kişiye katılır"""
    base = [i for i, it in enumerate(items) if it["e"] is not None and it["dur"] >= 1.0]
    if len(base) < 2:
        return {"ok": True, "speakers": None}
    X = np.stack([items[i]["e"] for i in base])
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    n = len(X)
    S = X @ X.T
    np.fill_diagonal(S, -np.inf)
    size = np.ones(n)
    members = [[i] for i in range(n)]
    while n > 1:
        a, b = divmod(int(np.argmax(S)), n)
        if not np.isfinite(S[a, b]) or S[a, b] < SPK_SAME:
            break
        na, nb = size[a], size[b]
        S[a, :] = (na * S[a, :] + nb * S[b, :]) / (na + nb)
        S[:, a] = S[a, :]
        S[a, a] = -np.inf
        S[b, :] = -np.inf
        S[:, b] = -np.inf
        size[a] += nb
        members[a] += members[b]
        members[b] = []
    groups = [[base[i] for i in m] for m in members if m]

    def stats(g):
        c = np.mean([items[i]["e"] for i in g], axis=0)
        c = c / (np.linalg.norm(c) + 1e-9)
        dur = sum(items[i]["dur"] for i in g)
        vote = sum((items[i]["p"] - 0.5) * items[i]["dur"] for i in g)
        return {"m": g, "c": c, "dur": dur, "g": "kadin" if vote > 0 else "erkek"}

    cl = [stats(g) for g in groups]
    changed = True
    while changed:                                         # küçük kümeleri büyüklere kat
        changed = False
        cl.sort(key=lambda x: x["dur"])
        for x in cl:
            if x["dur"] >= SPK_MIN_DUR:
                continue
            big = [y for y in cl if y is not x and y["dur"] >= SPK_MIN_DUR and y["g"] == x["g"]]
            if not big:
                continue
            y = max(big, key=lambda y: float(x["c"] @ y["c"]))
            cl = [z for z in cl if z is not x and z is not y] + [stats(y["m"] + x["m"])]
            changed = True
            break
    who = {}
    for ci, x in enumerate(cl):
        for i in x["m"]:
            who[i] = ci
    order = sorted(range(len(items)), key=lambda i: (items[i]["k"], items[i]["j"]))
    for i in order:                                        # kısa ya da izsiz cümleler
        if i in who:
            continue
        it = items[i]
        if it["e"] is not None:
            sims = [float(it["e"] @ x["c"]) for x in cl]
            best = int(np.argmax(sims))
            if sims[best] >= 0.25:
                who[i] = best
                continue
    for i in order:
        if i in who:
            continue
        it = items[i]
        pos = order.index(i)
        near = None
        for dd in (-1, 1, -2, 2):                          # yakınındaki aynı cinsiyetli cümlenin kişisi
            q = pos + dd
            if 0 <= q < len(order) and order[q] in who and cl[who[order[q]]]["g"] == (it["g"] or cl[who[order[q]]]["g"]):
                near = who[order[q]]
                break
        if near is None:
            same = [ci for ci, x in enumerate(cl) if x["g"] == it["g"]] or list(range(len(cl)))
            near = max(same, key=lambda ci: cl[ci]["dur"])
        who[i] = near
    words = [0] * len(cl)
    cnt = [0] * len(cl)
    for i, ci in who.items():
        words[ci] += items[i]["words"]
        cnt[ci] += 1
    ids, speakers = {}, []
    for g, pre in (("kadin", "k"), ("erkek", "e")):
        cis = sorted([ci for ci in range(len(cl)) if cl[ci]["g"] == g and cnt[ci]], key=lambda ci: -words[ci])
        for rank, ci in enumerate(cis):
            r = min(rank, SPK_MAX - 1)                     # 10'dan fazlası son (10.) kişiye
            ids[ci] = f"{pre}{r + 1}"
            if rank < SPK_MAX:
                speakers.append({"id": ids[ci], "g": g, "rank": r + 1, "words": words[ci], "n": cnt[ci]})
            else:
                sp = speakers[-1] if speakers and speakers[-1]["id"] == ids[ci] else next(x for x in speakers if x["id"] == ids[ci])
                sp["words"] += words[ci]
                sp["n"] += cnt[ci]
    assign = {}
    for i, it in enumerate(items):
        assign.setdefault(str(it["k"]), {})[it["j"]] = ids.get(who.get(i))
    assign = {k: [m.get(j) for j in range(max(m) + 1)] for k, m in assign.items()}
    return {"ok": True, "speakers": speakers, "assign": assign}


def _wav_info(path: Path):
    try:
        import soundfile as sf
        i = sf.info(str(path))
        return i.samplerate, i.frames
    except ImportError:
        with wave.open(str(path), "rb") as w:
            return w.getframerate(), w.getnframes()


def _wav_slice(path: Path, s0: int, s1: int) -> np.ndarray:
    """[s0, s1) örnekleri float32 (n, kanal) — dosyanın tamamını belleğe almadan"""
    try:
        import soundfile as sf
        x, _ = sf.read(str(path), start=s0, stop=s1, dtype="float32", always_2d=True)
        return x
    except ImportError:
        with wave.open(str(path), "rb") as w:
            ch, sw = w.getnchannels(), w.getsampwidth()
            w.setpos(s0)
            raw = w.readframes(max(0, s1 - s0))
        if sw != 2:
            raise RuntimeError("yalnızca 16 bit WAV destekleniyor")
        return (np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768).reshape(-1, ch)


def fmt_t(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


class _Cancelled(Exception):
    pass
