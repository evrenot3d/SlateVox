"""BS-PolarFormer (ZFTurbo, Music-Source-Separation-Training v1.0.20) — bağımsız PyTorch çıkarımı.

BSRoformer + PoPE (polar konum gömmesi). Ağırlık adları özgün `models/bs_roformer/bs_roformer.py` ile
aynıdır; böylece özgün checkpoint doğrudan yüklenir. Yalnızca çıkarım için gereken kısımlar var
(beartype / rotary / triton gerekmez). PoPE dikkatinin hesabı PoPE-pytorch'un triton'suz yolu ile aynıdır.
"""
from __future__ import annotations

from math import pi

import torch
import torch.nn.functional as F
from torch import nn

CKPT_URL = ("https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/download/v1.0.20/"
            "model_bs_polarformer_float16.ckpt")
CKPT_NAME = "model_bs_polarformer_float16.ckpt"

FREQS_PER_BANDS = (2,) * 24 + (4,) * 12 + (12,) * 8 + (24,) * 8 + (48,) * 8 + (128, 129)
CONFIG = dict(dim=256, depth=12, stereo=True, num_stems=1, time_transformer_depth=1, freq_transformer_depth=1,
              freqs_per_bands=FREQS_PER_BANDS, dim_head=64, heads=8, mask_estimator_depth=2,
              mlp_expansion_factor=4)
STFT = dict(n_fft=2048, hop_length=512, win_length=2048, normalized=False)
SR = 44100
CHUNK = 882000            # 20 sn
OVERLAP = 2               # %50 örtüşme
TIME_GROUP = 12           # zaman dikkatinde aynı anda işlenen bant sayısı (8 GB kartlar için)


class RMSNorm(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.scale = dim ** 0.5
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        return F.normalize(x, dim=-1) * self.scale * self.gamma


class FeedForward(nn.Module):
    def __init__(self, dim, mult=4):
        super().__init__()
        inner = int(dim * mult)
        self.net = nn.Sequential(RMSNorm(dim), nn.Linear(dim, inner), nn.GELU(), nn.Dropout(0.0),
                                 nn.Linear(inner, dim), nn.Dropout(0.0))

    def forward(self, x):
        return self.net(x)


class PoPE(nn.Module):
    def __init__(self, dim, heads, theta=10000):
        super().__init__()
        self.register_buffer("inv_freqs", theta ** -(torch.arange(dim).float() / dim))
        self.bias = nn.Parameter(torch.zeros(heads, dim))

    def forward(self, n):
        pos = torch.arange(n, device=self.inv_freqs.device, dtype=torch.float32)
        freqs = pos[:, None] * self.inv_freqs.float()[None, :]                 # (n, d)
        return freqs, self.bias.float().clamp(-2.0 * pi, 0.0)                  # (h, d)


def _sdpa(q, k, v, scale):
    """q,k: (b,h,n,2d) · v: (b,h,n,d). Bellek dostu çekirdeklerle; v, q ile aynı boya doldurulur."""
    dv = v.shape[-1]
    if v.shape[-1] != q.shape[-1]:
        v = F.pad(v, (0, q.shape[-1] - dv))
    try:
        from torch.nn.attention import SDPBackend, sdpa_kernel
        backends = [SDPBackend.CUDNN_ATTENTION, SDPBackend.EFFICIENT_ATTENTION, SDPBackend.FLASH_ATTENTION]
        with sdpa_kernel(backends):
            out = F.scaled_dot_product_attention(q, k, v, scale=scale)
    except Exception:                                    # uygun çekirdek yoksa parça parça klasik dikkat
        outs = []
        step = max(1, int(2 ** 28 // (q.shape[1] * q.shape[2] * k.shape[2] * 4)))
        for i in range(0, q.shape[0], step):
            outs.append(F.scaled_dot_product_attention(q[i:i + step], k[i:i + step], v[i:i + step], scale=scale))
        out = torch.cat(outs)
    return out[..., :dv]


class Attention(nn.Module):
    def __init__(self, dim, heads, dim_head, pope_embed):
        super().__init__()
        self.heads = heads
        self.scale = dim_head ** -0.5
        inner = heads * dim_head
        self.pope_embed = pope_embed
        self.norm = RMSNorm(dim)
        self.to_qkv = nn.Linear(dim, inner * 3, bias=False)
        self.to_gates = nn.Linear(dim, heads)
        self.to_out = nn.Sequential(nn.Linear(inner, dim, bias=False), nn.Dropout(0.0))

    def forward(self, x):
        x = self.norm(x)
        b, n, _ = x.shape
        q, k, v = self.to_qkv(x).view(b, n, 3, self.heads, -1).permute(2, 0, 3, 1, 4)   # (b,h,n,d) ×3
        freqs, bias = self.pope_embed(n)
        with torch.autocast("cuda", enabled=False):
            qf, kf = F.softplus(q.float()), F.softplus(k.float())
            fk = freqs[None] + bias[:, None, :]                                          # (h,n,d)
            q2 = torch.stack((qf * freqs.cos(), qf * freqs.sin()), dim=-1).flatten(-2)    # (d two)
            k2 = torch.stack((kf * fk.cos(), kf * fk.sin()), dim=-1).flatten(-2)
        dt = v.dtype
        out = _sdpa(q2.to(dt), k2.to(dt), v, self.scale)
        gates = self.to_gates(x)                                                         # (b,n,h)
        out = out * gates.permute(0, 2, 1).unsqueeze(-1).sigmoid()
        out = out.permute(0, 2, 1, 3).reshape(b, n, -1)
        return self.to_out(out)


class Transformer(nn.Module):
    def __init__(self, dim, depth, heads, dim_head, pope_embed):
        super().__init__()
        self.layers = nn.ModuleList([nn.ModuleList([Attention(dim, heads, dim_head, pope_embed), FeedForward(dim)])
                                     for _ in range(depth)])
        self.norm = nn.Identity()

    def forward(self, x):
        for attn, ff in self.layers:
            x = attn(x) + x
            x = ff(x) + x
        return self.norm(x)


class BandSplit(nn.Module):
    def __init__(self, dim, dim_inputs):
        super().__init__()
        self.dim_inputs = dim_inputs
        self.to_features = nn.ModuleList([nn.Sequential(RMSNorm(d), nn.Linear(d, dim)) for d in dim_inputs])

    def forward(self, x):
        return torch.stack([f(s) for s, f in zip(x.split(self.dim_inputs, dim=-1), self.to_features)], dim=-2)


class MaskEstimator(nn.Module):
    def __init__(self, dim, dim_inputs, depth, mlp_expansion_factor=4):
        super().__init__()
        hidden = dim * mlp_expansion_factor
        nets = []
        for d in dim_inputs:
            dims = (dim, *((hidden,) * (depth - 1)), d * 2)
            layers = []
            for i, (a, b) in enumerate(zip(dims[:-1], dims[1:])):
                layers.append(nn.Linear(a, b))
                if i < len(dims) - 2:
                    layers.append(nn.Tanh())
            nets.append(nn.Sequential(nn.Sequential(*layers), nn.GLU(dim=-1)))
        self.to_freqs = nn.ModuleList(nets)

    def forward(self, x):
        return torch.cat([m(b) for b, m in zip(x.unbind(dim=-2), self.to_freqs)], dim=-1)


class PolarFormer(nn.Module):
    def __init__(self, dim, depth, stereo, num_stems, time_transformer_depth, freq_transformer_depth,
                 freqs_per_bands, dim_head, heads, mask_estimator_depth, mlp_expansion_factor):
        super().__init__()
        self.audio_channels = 2 if stereo else 1
        time_pope, freq_pope = PoPE(dim_head, heads), PoPE(dim_head, heads)   # özgündeki gibi katmanlar arası ortak
        self.layers = nn.ModuleList([
            nn.ModuleList([Transformer(dim, time_transformer_depth, heads, dim_head, time_pope),
                           Transformer(dim, freq_transformer_depth, heads, dim_head, freq_pope)])
            for _ in range(depth)])
        self.final_norm = RMSNorm(dim)
        dims = tuple(2 * f * self.audio_channels for f in freqs_per_bands)
        self.band_split = BandSplit(dim, dims)
        self.mask_estimators = nn.ModuleList([MaskEstimator(dim, dims, mask_estimator_depth, mlp_expansion_factor)
                                              for _ in range(num_stems)])

    def core(self, x):
        """x: (b, t, f*c) → maske (b, n, f, t, 2)"""
        x = self.band_split(x)                                            # (b,t,bands,d)
        for time_t, freq_t in self.layers:
            b, t, f, d = x.shape
            xt = x.permute(0, 2, 1, 3).reshape(b * f, t, d)
            xt = torch.cat([time_t(g) for g in xt.split(TIME_GROUP)])     # bellek: bantlar küçük gruplar halinde
            x = xt.view(b, f, t, d).permute(0, 2, 1, 3)
            x = freq_t(x.reshape(b * t, f, d)).view(b, t, f, d)
        x = self.final_norm(x)
        m = torch.stack([fn(x) for fn in self.mask_estimators], dim=1)    # (b,n,t,f*c)
        return m.unflatten(-1, (-1, 2)).permute(0, 1, 3, 2, 4)


def load(ckpt_path: str, device) -> PolarFormer:
    model = PolarFormer(**CONFIG)
    sd = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    if isinstance(sd, dict) and "state_dict" in sd:
        sd = sd["state_dict"]
    sd = {k.replace("module.", ""): v for k, v in sd.items()}
    model.load_state_dict(sd, strict=True)
    return model.to(device).eval()


@torch.no_grad()
def separate(model: PolarFormer, audio, device, on=None):
    """audio: numpy (2, T) 44.1 kHz → vokal numpy (2, T)"""
    import numpy as np
    T = audio.shape[1]
    step = CHUNK // OVERLAP
    win = torch.hann_window(STFT["win_length"], device=device)
    out = np.zeros((2, T), dtype=np.float32)
    cnt = np.zeros(T, dtype=np.float32)
    starts = list(range(0, max(1, T - step), step)) or [0]
    if starts[-1] + CHUNK < T:
        starts.append(max(0, T - CHUNK))
    use_amp = device.type == "cuda"
    for i, s0 in enumerate(starts):
        e0 = min(s0 + CHUNK, T)
        ch = np.zeros((2, CHUNK), dtype=np.float32)
        ch[:, : e0 - s0] = audio[:, s0:e0]
        raw = torch.from_numpy(ch).to(device)
        spec = torch.view_as_real(torch.stft(raw, **STFT, window=win, return_complex=True))      # (2,F,Tf,2)
        Fq, Tf = spec.shape[1], spec.shape[2]
        spec_fs = spec.permute(1, 0, 2, 3).reshape(Fq * 2, Tf, 2)                                # (f s) t c
        feat = spec_fs.permute(1, 0, 2).reshape(1, Tf, Fq * 4)
        with torch.autocast("cuda", dtype=torch.float16, enabled=use_amp):
            mask = model.core(feat)
        mask = mask.float().reshape(Fq * 2, Tf, 2)
        m = torch.view_as_complex(spec_fs.contiguous()) * torch.view_as_complex(mask.contiguous())
        m = m.reshape(Fq, 2, Tf).permute(1, 0, 2).contiguous()                                   # (2,F,Tf)
        m[:, 0, :] = 0
        rec = torch.istft(m, **STFT, window=win, length=CHUNK).float().cpu().numpy()
        out[:, s0:e0] += rec[:, : e0 - s0]
        cnt[s0:e0] += 1
        if on:
            on((i + 1) / len(starts))
    return out / np.maximum(cnt, 1.0)
