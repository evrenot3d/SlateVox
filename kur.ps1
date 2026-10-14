# Doğru mu Samet - Windows kurulum + model indirme + başlatma (masaüstü)
# Her adım kurulum.log dosyasına yazılır. Tekrar çalıştırılırsa kurulu olanları atlar.
param([switch]$BaslatmaYok)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
Set-Location -LiteralPath $PSScriptRoot
$Log = Join-Path $PSScriptRoot "kurulum.log"
"" | Out-File -LiteralPath $Log -Encoding utf8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
$env:HF_HUB_DISABLE_SYMLINKS_WARNING = "1"

function Say([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $m
    Write-Host $line -ForegroundColor Yellow
    Add-Content -LiteralPath $Log -Value $line -Encoding utf8
}
function Run([string]$exe, [string[]]$argv) {
    Add-Content -LiteralPath $Log -Value ("> " + $exe + " " + ($argv -join " ")) -Encoding utf8
    & $exe @argv 2>&1 | ForEach-Object {
        $s = "$_"
        Write-Host $s
        Add-Content -LiteralPath $Log -Value $s -Encoding utf8
    }
    $code = $LASTEXITCODE
    Add-Content -LiteralPath $Log -Value ("(cikis kodu " + $code + ")") -Encoding utf8
    return $code
}
function Fail([string]$m) {
    Say "HATA: $m"
    Say "KURULUM_BASARISIZ"
    Read-Host "Kapatmak icin Enter"
    exit 1
}

Say "Dogru mu Samet kurulumu basladi"

# ---------- 1) Python 3.11-3.13
function Find-Python {
    foreach ($v in @("3.12", "3.11", "3.13")) {
        try {
            $p = & py "-$v" -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { return "$p".Trim() }
        } catch {}
    }
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
                     "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
                     "C:\Program Files\Python312\python.exe",
                     "C:\Program Files\Python311\python.exe")) {
        if (Test-Path -LiteralPath $c) { return $c }
    }
    try {
        $v = & python -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v -in @("3.11", "3.12", "3.13")) { return (& python -c "import sys;print(sys.executable)").Trim() }
    } catch {}
    return $null
}

$Venv = Join-Path $PSScriptRoot ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Py)) {
    $Base = Find-Python
    if (-not $Base) {
        Say "Python 3.12 bulunamadi, winget ile kuruluyor..."
        Run "winget" @("install", "-e", "--id", "Python.Python.3.12", "--scope", "user", "--silent",
                       "--accept-package-agreements", "--accept-source-agreements") | Out-Null
        $Base = Find-Python
    }
    if (-not $Base) { Fail "Python kurulamadi. python.org'dan Python 3.12 kurup tekrar deneyin." }
    Say "Python: $Base"
    if ((Run $Base @("-m", "venv", $Venv)) -ne 0) { Fail "Sanal ortam olusturulamadi" }
}
Say ("venv python: " + (& $Py -c "import sys;print(sys.version)"))
Run $Py @("-m", "pip", "install", "-q", "--progress-bar", "off", "-U", "pip", "wheel", "setuptools") | Out-Null

function PipOk([string]$mod) {
    & $Py -c "import $mod" 2>$null
    return ($LASTEXITCODE -eq 0)
}
function Pip([string[]]$pkgs) {
    $code = Run $Py (@("-m", "pip", "install", "--progress-bar", "off") + $pkgs)
    if ($code -ne 0) { Fail ("pip kurulumu basarisiz: " + ($pkgs -join " ")) }
}

$Gpu = $null -ne (Get-Command nvidia-smi -ErrorAction SilentlyContinue)
if ($Gpu) { Say ("NVIDIA bulundu: " + ((& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader) -join "; ")) }
else { Say "NVIDIA bulunamadi, CPU ile calisacak" }

# ---------- 2) PyTorch
$torchOk = $false
if (PipOk "torch") {
    $cuda = & $Py -c "import torch;print(torch.cuda.is_available())" 2>$null
    $torchOk = (-not $Gpu) -or ("$cuda".Trim() -eq "True")
}
if (-not $torchOk) {
    Say "PyTorch kuruluyor (buyuk indirme, birkac dakika surebilir)"
    if ($Gpu) { Pip @("torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu128") }
    else { Pip @("torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cpu") }
}
Say ("torch: " + (& $Py -c "import torch;print(torch.__version__, 'cuda=', torch.cuda.is_available())"))

# ---------- 3) Diğer paketler
if (-not (PipOk "audio_separator")) {
    Say "audio-separator kuruluyor"
    if ($Gpu) { Pip @("audio-separator[gpu]") } else { Pip @("audio-separator[cpu]") }
}
if ($Gpu -and -not (PipOk "nvidia.cudnn")) {
    Say "cuDNN / cuBLAS (Whisper GPU icin) kuruluyor"
    Pip @("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
}
Say "Uygulama paketleri kuruluyor"
Pip @("-r", "requirements.txt")

# torch'un CPU surumuyle degistirilmedigini kontrol et (bazi paketler PyPI'daki CPU torch'u ceker)
if (PipOk "torchaudio") { Run $Py @("-m", "pip", "uninstall", "-y", "torchaudio") | Out-Null }
if ($Gpu) {
    # onnxruntime (CPU) paketi onnxruntime-gpu'yu ezer -> GPU surumunu geri yukle
    & $Py -c "import onnxruntime as o,sys;sys.exit(0 if 'CUDAExecutionProvider' in o.get_available_providers() else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "onnxruntime-gpu duzeltiliyor"
        Run $Py @("-m", "pip", "uninstall", "-y", "onnxruntime", "onnxruntime-gpu") | Out-Null
        Pip @("--no-deps", "onnxruntime-gpu")
    }
    # torch + torchvision CUDA ve birbiriyle uyumlu olmali
    Say "torch / torchvision CUDA kontrolu"
    if ((Run $Py @("-u", "torch_duzelt.py")) -ne 0) { Fail "torch CUDA surumu kurulamadi" }
    Say ("torch: " + (& $Py -c "import torch,torchvision;print(torch.__version__, torchvision.__version__, 'cuda=', torch.cuda.is_available(), torch.cuda.get_device_name(0))"))
}

# ---------- 4) Modeller (eksik olanlar)
Say "Modeller kontrol ediliyor / indiriliyor"
if ((Run $Py @("-u", "modelleri_indir.py")) -ne 0) { Fail "Bazi modeller indirilemedi (yukaridaki hataya bakin)" }

Say "KURULUM_TAMAM"

# ---------- 5) Baslat
if (-not $BaslatmaYok) {
    Say "Uygulama aciliyor"
    $Pyw = Join-Path $Venv "Scripts\pythonw.exe"
    Start-Process -FilePath $Pyw -ArgumentList ('"' + (Join-Path $PSScriptRoot "app.py") + '"') -WorkingDirectory $PSScriptRoot
}
