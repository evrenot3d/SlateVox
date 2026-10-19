"""İngilizce adların Türkçe okunuşu (Gelişmiş · Ahenk, Ahenk Cep, EMA Lightning, Antalia-2 Mini).

Çeviride İngilizce kalan adlar (Michael, Jennifer, Brooklyn...) Türkçe ses modellerine olduğu gibi gidince harf
harf ya da yanlış okunuyordu. Bu modül onları, insanların söylediği gibi Türkçe yazımla yeniden yazar:
    "Michael'ın arabası" → "Maykıl'ın arabası"

Hangi kelimeler değişir (Türkçe kelimelere dokunmamak için üç şart birden):
  1. Kelime cümlenin İNGİLİZCE aslında da aynen geçiyor (çevrilmemiş: ad, marka, yer adı).
  2. Yazılışı İngilizceye özgü (w, q, x, j, c, th, sh, ee, oo, sonda sessiz e, sonda -er ...). Türkçe yazımıyla
     zaten doğru okunan Paris, Tesla, Amazon, Anna, Daniel gibi adlar olduğu gibi kalır.
  3. Kısaltma değil (KVKK, FBI ... trnorm ve ses modelleri kendisi okur).

Okunuş nereden gelir:
  - trnorm'un marka sözlüğü (iPhone → ayfon, YouTube → yutub ...) önce bakılır.
  - CMU Pronouncing Dictionary (Carnegie Mellon, ~135 bin kelime, BSD lisanslı): İngilizce telaffuz → Türkçe harfler.
  - g2p_en (Kyubyong Park, Apache-2.0) yapay zekâ modeli: sözlükte olmayan kelimenin telaffuzunu tahmin eder
    (küçük bir GRU ağı; ağırlıkları numpy dosyası, işlemcide çalışır, ek paket gerekmez).
Yöntem, dönüşüm tablosu ve hazır terim listesi: sewox/turkish-neural-tts (Apache-2.0, src/pronounce.rs)
— İngilizce telaffuz (CMU) → Türkçe yazım; birleşik kelime bölme (SonicWall, onboarding).
"""
from __future__ import annotations

import re
import threading
import urllib.request
from pathlib import Path

import numpy as np

CMU_URL = "https://raw.githubusercontent.com/cmusphinx/cmudict/master/cmudict.dict"
G2P_URL = "https://github.com/Kyubyong/g2p/raw/master/g2p_en/checkpoint20.npz"

_lock = threading.Lock()
_cmu: dict | None = None
_g2p = None
_names = None

TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
WORD_RE = re.compile(r"(?<![\w@./])([A-Za-zÇĞİÖŞÜçğıöşü]+)((?:['’][a-zçğıöşü]+)?)(?![\w@]|[./:]\w)")
# İngilizceye özgü yazılış: bunlardan biri yoksa kelime Türkçe yazımıyla zaten yaklaşık doğru okunur
ENGLISH_RE = re.compile(
    r"[wqxj]|c(?!h)|th|sh|ch|ph|gh|ck|wh|kn|wr|ee|oo|ea|ou|oa|ai|ay|ey|ie|ue|au|aw|ew|ow|oy"
    r"|[^aeiou]y(?:[^aeiou]|$)|[^aeiou]e$|er$|tion$|ight", re.I)


# ------------------------------------------------------------------ telaffuz kaynakları
def _download(url: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "SlateVox"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 16)
            if not b:
                break
            f.write(b)
    tmp.replace(dst)
    return dst


def load_cmu(models: Path) -> dict:
    """kelime (küçük harf) → ARPAbet sesleri (ilk okunuş)"""
    global _cmu
    with _lock:
        if _cmu is None:
            p = models / "cmudict" / "cmudict.dict"
            if not p.exists():
                _download(CMU_URL, p)
            d = {}
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                w, *ph = line.split()
                if "(" in w:                          # "word(2)": ikinci okunuş, ilki yeter
                    continue
                d.setdefault(w, ph)
            _cmu = d
        return _cmu


class G2P:
    """g2p_en'in sözlük dışı kelime tahmincisi (GRU kodlayıcı-çözücü), numpy ile; ağırlıklar checkpoint20.npz"""
    GRAPHEMES = ["<pad>", "<unk>", "</s>"] + list("abcdefghijklmnopqrstuvwxyz")
    PHONEMES = ["<pad>", "<unk>", "<s>", "</s>"] + [
        "AA0", "AA1", "AA2", "AE0", "AE1", "AE2", "AH0", "AH1", "AH2", "AO0", "AO1", "AO2", "AW0", "AW1", "AW2",
        "AY0", "AY1", "AY2", "B", "CH", "D", "DH", "EH0", "EH1", "EH2", "ER0", "ER1", "ER2", "EY0", "EY1", "EY2",
        "F", "G", "HH", "IH0", "IH1", "IH2", "IY0", "IY1", "IY2", "JH", "K", "L", "M", "N", "NG", "OW0", "OW1",
        "OW2", "OY0", "OY1", "OY2", "P", "R", "S", "SH", "T", "TH", "UH0", "UH1", "UH2", "UW", "UW0", "UW1",
        "UW2", "V", "W", "Y", "Z", "ZH"]

    def __init__(self, npz: Path):
        v = np.load(str(npz), allow_pickle=False)
        self.v = {k: v[k].astype(np.float32) for k in v.files}
        self.g2i = {g: i for i, g in enumerate(self.GRAPHEMES)}

    @staticmethod
    def _cell(x, h, w_ih, w_hh, b_ih, b_hh):
        gi = x @ w_ih.T + b_ih
        gh = h @ w_hh.T + b_hh
        k = gi.shape[-1] // 3
        r = 1 / (1 + np.exp(-(gi[:, :k] + gh[:, :k])))
        z = 1 / (1 + np.exp(-(gi[:, k:2 * k] + gh[:, k:2 * k])))
        n = np.tanh(gi[:, 2 * k:] + r * gh[:, 2 * k:])
        return (1 - z) * n + z * h

    def __call__(self, word: str) -> list:
        v = self.v
        ids = [self.g2i.get(c, 1) for c in word.lower() if c.isascii()] + [2]
        h = np.zeros((1, v["enc_w_hh"].shape[1]), np.float32)
        for i in ids:
            h = self._cell(v["enc_emb"][[i]], h, v["enc_w_ih"], v["enc_w_hh"], v["enc_b_ih"], v["enc_b_hh"])
        dec, out = v["dec_emb"][[2]], []
        for _ in range(24):
            h = self._cell(dec, h, v["dec_w_ih"], v["dec_w_hh"], v["dec_b_ih"], v["dec_b_hh"])
            p = int((h @ v["fc_w"].T + v["fc_b"]).argmax())
            if p == 3:                                 # </s>
                break
            out.append(self.PHONEMES[p])
            dec = v["dec_emb"][[p]]
        return [p for p in out if not p.startswith("<")]


def load_g2p(models: Path) -> G2P:
    global _g2p
    with _lock:
        if _g2p is None:
            p = models / "g2p" / "checkpoint20.npz"
            if not p.exists():
                _download(G2P_URL, p)
            _g2p = G2P(p)
        return _g2p


def _brand_names() -> dict:
    """trnorm'un marka / ürün sözlüğü (iPhone → ayfon ...): yazılış → okunuş"""
    global _names
    if _names is None:
        try:
            from trnorm.normalizer import _NAMES
            _names = dict(_NAMES)
        except Exception:  # noqa: BLE001
            _names = {}
    return _names


# ------------------------------------------------------------------ İngilizce sesler → Türkçe harfler
# sewox/turkish-neural-tts'in (Apache-2.0, src/pronounce.rs) dönüşüm tablosu ve kuralları
# Hazır okunuşlar (büyük/küçük harf fark etmez; "Türkiye'deki ofislerde söylendiği gibi", sewox listesi)
DEFAULT_LEXICON = {
    "jira": "jira", "java": "cava", "javascript": "cava skript", "typescript": "tayp skript", "linux": "linuks",
    "python": "paytın", "azure": "ejır", "github": "git hab", "gitlab": "git lab", "docker": "dakır",
    "kubernetes": "kubernetis", "teams": "tims", "slack": "slek", "zoom": "zum", "excel": "eksel",
    "outlook": "autluk", "google": "gugıl", "microsoft": "maykrosoft", "notion": "noşın", "confluence": "konfluens",
    "sonicwall": "sonik vol", "salesforce": "seylsfors", "whatsapp": "vatsap", "linkedin": "linkdin",
    "youtube": "yutub", "iphone": "ayfon", "macbook": "mekbuk", "windows": "vindovs",
}
PHONE = {"AA": "a", "AE": "e", "AH": "a", "AO": "o", "AW": "av", "AY": "ay", "B": "b", "CH": "ç", "D": "d",
         "DH": "d", "EH": "e", "ER": "ır", "EY": "ey", "F": "f", "G": "g", "HH": "h", "IH": "i", "IY": "i",
         "JH": "c", "K": "k", "L": "l", "M": "m", "N": "n", "NG": "ng", "OW": "o", "OY": "oy", "P": "p", "R": "r",
         "S": "s", "SH": "ş", "T": "t", "TH": "t", "UH": "u", "UW": "u", "V": "v", "W": "v", "Y": "y", "Z": "z",
         "ZH": "j"}


def arpabet_to_tr(phones: list, spelled: str = "") -> str:
    """ARPAbet → Türkçe yazım (sewox kuralları + iki küçük ek: "n" önünde "ng" → "n", üç aynı harf olmaz)"""
    low = spelled.lower()
    o_not_a = "o" in low and "a" not in low           # Amerikan "a"sı, "o" ile yazılmışsa "o" (sonic, Notion)
    out = []
    for i, p in enumerate(phones):
        base = p.rstrip("012")
        if p == "AH0":                                # vurgusuz ünlü
            out.append("ı")
        elif base == "AA" and o_not_a:
            out.append("o")
        elif base == "NG":                            # "Duncan" → dankın (dangkın değil)
            nxt = phones[i + 1].rstrip("012") if i + 1 < len(phones) else ""
            out.append("n" if nxt in ("K", "G") else "ng")
        else:
            out.append(PHONE.get(base, ""))
    if out and out[-1] == "z" and low.endswith("s"):  # yazılı çoğul "s" "s" kalır (teams → tims)
        out[-1] = "s"
    return re.sub(r"(.)\1{2,}", r"\1\1", "".join(out))


def _camel_parts(word: str) -> list:
    """SonicWall → [Sonic, Wall]"""
    parts = []
    for c in word:
        if not parts or (c.isupper() and parts[-1][-1:].islower()):
            parts.append("")
        parts[-1] += c
    return parts


def _from_dict(word: str, cmu: dict | None, g2p) -> str | None:
    """Bir kelime parçasının Türkçe yazımı: CMU → (bölünürse iki CMU kelimesi) → yapay zekâ tahmini"""
    low = word.lower()
    if cmu is not None:
        if low in cmu:
            return arpabet_to_tr(cmu[low], word)
        for i in range(len(low) - 2, 1, -1):          # onboarding → on + boarding (iki sözlük kelimesi)
            a, b = low[:i], low[i:]
            if a in cmu and b in cmu:
                return arpabet_to_tr(cmu[a], a) + arpabet_to_tr(cmu[b], b)
    if g2p is not None:
        ph = g2p(low)
        if ph:
            return arpabet_to_tr(ph, word)
    return None


BACK, FRONT = set("aıou"), set("eiöü")


def harmonize(root: str, suffix: str) -> str:
    """Ekin ünlülerini yeni okunuşun son ünlüsüne uydurur: Brooklyn'de → Bruklın'da, Peter'e → Pitır'a"""
    last = next((c for c in reversed(root.lower()) if c in BACK | FRONT), None)
    if not last or not suffix:
        return suffix
    out = []
    for c in suffix:
        if c in "ae":
            c = "a" if last in BACK else "e"
        elif c in "ıiuü":
            c = {"a": "ı", "ı": "ı", "o": "u", "u": "u", "e": "i", "i": "i", "ö": "ü", "ü": "ü"}[last]
        if c in BACK | FRONT:
            last = c
        out.append(c)
    return "".join(out)


def _cap(word: str, like: str) -> str:
    if like[:1].isupper():
        f = word[:1]
        return ({"i": "İ", "ı": "I"}.get(f) or f.upper()) + word[1:]
    return word


# ------------------------------------------------------------------ cümle
def candidates(tr: str, en: str) -> list:
    """(başlangıç, bitiş, kök, ek) — çeviride kalan İngilizce kelimeler (yukarıdaki üç şart)"""
    en_words = {w.lower() for w in re.findall(r"[A-Za-z]+", en or "")}
    out = []
    for m in WORD_RE.finditer(tr or ""):
        root, suf = m.group(1), m.group(2)
        if (len(root) < 2 or not root.isascii() or root.lower() not in en_words
                or (root.isupper() and len(root) >= 2)):           # kısaltma: trnorm / ses modeli okur
            continue
        out.append((m.start(), m.end(), root, suf))
    return out


def transform(tr: str, en: str, models: Path, use_cmu: bool = True, use_g2p: bool = False) -> tuple[str, list]:
    """Çeviri cümlesindeki İngilizce adları Türkçe okunuşla yazar. Dönüş: (yeni metin, [(eski, yeni), ...])"""
    if not (use_cmu or use_g2p) or not tr:
        return tr, []
    cands = candidates(tr, en)
    if not cands:
        return tr, []
    brands = _brand_names()
    cmu = load_cmu(models) if use_cmu else None
    g2p = load_g2p(models) if use_g2p else None
    parts, pos, changes = [], 0, []
    for a, b, root, suf in cands:
        new = None
        camel = len(_camel_parts(root)) > 1
        if root in brands:                                 # trnorm marka sözlüğü (iPhone, YouTube ...)
            new = brands[root]
        elif root.lower() in DEFAULT_LEXICON:              # sewox hazır okunuşları (Python, Docker ...)
            new = DEFAULT_LEXICON[root.lower()]
        elif camel or ENGLISH_RE.search(root):
            got = [_from_dict(p, cmu, g2p) for p in _camel_parts(root)]
            new = " ".join(got) if all(got) else None
        if not new or new.lower() == root.lower().translate(TR_LOWER):
            continue
        new = _cap(new, root)
        nsuf = suf[:1] + harmonize(new, suf[1:]) if suf else ""
        parts += [tr[pos:a], new + nsuf]
        pos = b
        changes.append([root + suf, new + nsuf])
    parts.append(tr[pos:])
    return "".join(parts), changes
