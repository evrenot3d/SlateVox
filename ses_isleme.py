"""Ses işleme (stüdyo miksi) — Gelişmiş, deneysel. Yapay zekâ değil, dublaj stüdyolarının klasik son işleme adımları.

Her parçanın işlenmemiş dublaj sesi (dub_..._ham değil, ayarsız hâli) saklanır; bu adımlar onun üzerine uygulanır,
böylece anahtarlar açılıp kapanınca seslendirme yeniden yapılmaz. Sıra:

  1. mixm · Ses rengini eşleme + hafif ortam yankısı
       - Eşleme (match EQ): dublaj sesinin uzun süreli frekans dağılımı, aynı parçadaki orijinal (İngilizce)
         konuşmanınkine yaklaştırılır (100 Hz – 12 kHz, en çok ±6 dB, yarı yarıya). Yapay ses "stüdyoda okunmuş" değil,
         sahnedeki kişinin mikrofonundan geliyormuş gibi duyulur.
       - Ortam yankısı: kısa (≈0,3 sn), hafif (%12) bir oda yankısı; kuru sesin "üstüne yapıştırılmış" hissini azaltır.
  2. mixd · De-esser, EQ ve kompresör (ffmpeg)
       - 80 Hz altı kesilir (uğultu), sert "s/ş/ç" sesleri kısılır (de-esser), yapay seslerdeki sert 3,5 kHz
         parlaklığı hafifçe azaltılır, kompresörle cümleler arası seviye dengelenir.
  3. mixl · Loudness standardı
       - Dublajın algılanan ses yüksekliği (LUFS, ITU-R BS.1770) orijinal konuşmayla eşlenir, tepeler −1 dB'de
         sınırlanır (patlama yok). Dışa aktarmada son karışım −16 LUFS'a (internet / YouTube standardı) getirilir.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

import engine as E

FLAGS = {"mixm": "Ses rengi eşleme + ortam yankısı", "mixd": "De-esser, EQ ve kompresör", "mixl": "Loudness standardı"}
SR = 48000
EXPORT_LUFS = "loudnorm=I=-16:TP=-1.5:LRA=11"


# ------------------------------------------------------------------ ölçüm: algılanan ses yüksekliği (LUFS)
def lufs(x: np.ndarray, sr: int = SR) -> float:
    """ITU-R BS.1770-4 bütünleşik ses yüksekliği (K ağırlıklı, 400 ms bloklar, −70 / −10 LU kapıları)."""
    from scipy.signal import lfilter
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    if sr != SR:
        x = E.resample(x.astype(np.float32), sr, SR).astype(np.float64)
    if len(x) < SR // 2:
        return -70.0
    x = lfilter([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585], x)
    x = lfilter([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621], x)
    blk, hop = int(0.4 * SR), int(0.1 * SR)
    n = 1 + (len(x) - blk) // hop
    if n <= 0:
        return -70.0
    ms = np.array([np.mean(x[i * hop:i * hop + blk] ** 2) for i in range(n)])
    lk = -0.691 + 10 * np.log10(ms + 1e-12)
    g = ms[lk > -70]
    if not len(g):
        return -70.0
    rel = -0.691 + 10 * np.log10(g.mean()) - 10
    g = ms[lk > max(-70, rel)]
    return float(-0.691 + 10 * np.log10(g.mean() + 1e-12)) if len(g) else -70.0


# ------------------------------------------------------------------ 1) ses rengi eşleme + ortam yankısı
def match_eq(dub: np.ndarray, ref: np.ndarray, sr: int = SR, amount: float = 0.5, limit_db: float = 6.0) -> np.ndarray:
    from scipy.signal import fftconvolve, firwin2, welch
    if len(ref) < sr or len(dub) < sr:
        return dub
    f, pd = welch(dub, sr, nperseg=4096)
    _, pr = welch(ref, sr, nperseg=4096)
    band = (f >= 100) & (f <= 12000)
    diff = 10 * np.log10((pr + 1e-14) / (pd + 1e-14))
    diff -= np.average(diff[band], weights=pd[band] + 1e-14)             # yalnız biçim (genel seviye ayrı eşlenir)
    sm = np.copy(diff)                                                    # üçte bir oktav yumuşatma
    for i, fc in enumerate(f):
        if fc <= 0:
            continue
        m = (f >= fc / 2 ** (1 / 6)) & (f <= fc * 2 ** (1 / 6))
        sm[i] = diff[m].mean()
    g_db = np.clip(sm * amount, -limit_db, limit_db)
    g_db[~band] = 0.0
    g_db[f > 12000] = 0.0
    gains = 10 ** (g_db / 20)
    fir = firwin2(1025, f / (sr / 2), gains)
    return fftconvolve(dub, fir, mode="same").astype(np.float32)


def room(dub: np.ndarray, sr: int = SR, rt60: float = 0.3, wet: float = 0.12, seed: int = 7) -> np.ndarray:
    from scipy.signal import butter, fftconvolve, sosfilt
    n = int(sr * rt60 * 1.2)
    t = np.arange(n) / sr
    ir = np.random.default_rng(seed).standard_normal(n) * np.exp(-6.91 * t / rt60)
    ir = sosfilt(butter(2, 6000, "lowpass", fs=sr, output="sos"), ir)
    ir = np.concatenate([np.zeros(int(0.012 * sr)), ir])                  # 12 ms ön gecikme
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-12
    tail = fftconvolve(dub, ir)[: len(dub)]
    return (dub + wet * tail).astype(np.float32)


# ------------------------------------------------------------------ 2) de-esser, EQ, kompresör · 3) sınırlayıcı (ffmpeg)
def _ffmpeg(x: np.ndarray, af: str, tmp: Path) -> np.ndarray:
    i, o = tmp / "_mx_in.wav", tmp / "_mx_out.wav"
    E.write_wav(i, np.clip(x, -1, 1), SR)
    E.ffmpeg("-i", i, "-af", af, "-ar", str(SR), "-ac", "1", o)
    y, ysr = E.read_wav(o)
    y = y.mean(axis=1) if y.ndim > 1 else y
    i.unlink(missing_ok=True)
    o.unlink(missing_ok=True)
    return E.resample(y, ysr, SR) if ysr != SR else y


DYN_AF = ("highpass=f=80,deesser=i=0.4:m=0.5:f=0.5:s=o,equalizer=f=3500:t=q:w=1.2:g=-1.5,"
          "acompressor=threshold=0.1:ratio=3:attack=8:release=120:makeup=1")
LIMIT_AF = "alimiter=limit=0.891:attack=5:release=60:level=disabled"


def process(track: np.ndarray, ref: np.ndarray, ref_sr: int, flags: list, tmp: Path) -> np.ndarray:
    """track: 48 kHz mono işlenmemiş dublaj · ref: aynı parçanın orijinal konuşması (ses ayırmadan)"""
    x = np.asarray(track, dtype=np.float32).reshape(-1)
    if not np.any(x):
        return x
    ref = np.asarray(ref, dtype=np.float32)
    ref = ref.mean(axis=1) if ref.ndim > 1 else ref
    ref = E.resample(ref, ref_sr, SR) if ref_sr != SR else ref
    l0 = lufs(x)
    tmp.mkdir(parents=True, exist_ok=True)
    if "mixm" in flags:
        x = room(match_eq(x, ref))
    if "mixd" in flags:
        x = _ffmpeg(x, DYN_AF, tmp)
    if "mixl" in flags:
        target = lufs(ref)
        target = target if target > -60 else l0
        cur = lufs(x)
        x = x * float(10 ** (np.clip(target - cur, -12, 12) / 20))
        x = _ffmpeg(x, LIMIT_AF, tmp)
    else:                                         # işleme ses seviyesini değiştirmesin (mixer dengesi korunur)
        cur = lufs(x)
        if cur > -60 and l0 > -60:
            x = x * float(10 ** (np.clip(l0 - cur, -12, 12) / 20))
    peak = float(np.abs(x).max() or 1.0)
    if peak > 0.98:
        x = x * (0.98 / peak)
    return x.astype(np.float32)
