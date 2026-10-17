<p align="center"><img src="ui/kurulum_logo.png" width="260" alt="Doğru mu Samet? logosu"></p>

<h1 align="center">"Doğru mu Samet?"</h1>
<p align="center"><b>İngilizce videoları Türkçe dublajla izleyin.</b><br>
Bütün yapay zekâ modelleri kendi bilgisayarınızda çalışır; videolarınız hiçbir sunucuya gönderilmez.</p>

<p align="center">
  <a href="https://github.com/evrenot3d/dogru-mu-samet/releases/latest"><b>⬇ Son sürümü indir</b></a>
</p>

---

## Nedir?

"Doğru mu Samet?", İngilizce bir videoyu açtığınızda konuşmaları ayırır, yazıya döker, Türkçeye çevirir ve
Türkçe seslendirir. Arka plandaki müzik ve efektler korunur; dublaj videoyla eş zamanlı çalar.

- **Hızlı mod:** kadın, erkek ya da otomatik (kadınlar kadın, erkekler erkek sesiyle) hazır Türkçe sesler
- **Orta ve Güçlü mod:** VoxCPM2 ile her cümle konuşanın kendi sesinden kopyalanır; vurgu, hız ve duygu taşınır
  (hazır ayarlar: Orta 10 adım, Güçlü 20 adım)
- **Gelişmiş mod:** ses ayırma, çeviri ve ses modeli (VoxCPM2 · EMA Lightning · Antalia-2 Mini · Ahenk Cep · Ahenk)
  ile adım sayısı serbestçe seçilir; seçilen ses modeli bütün cümleleri okur. Deneysel **Ahenk / Ahenk Cep**'te her
  cümlenin duygusu (kızgın, neşeli, üzgün, sakin, şaşkın, şefkatli, gülerek) İngilizce metinden ve orijinal sesten
  bulunup modele bildirilir
- **Parça parça işleme:** ilk bölüm hazır olur olmaz izlemeye başlarsınız, kalanı arkada hazırlanır
- **YouTube bağlantısı:** video indirilmeden izlenir, dublaj için yalnız ses indirilir
- Türkçe / İngilizce altyazı, konuşmacı noktaları, ses karıştırıcı, dışa aktarma (MP4 / WAV)
- Program içinden güncelleme

## Ekran görüntüleri

<p align="center"><img src="docs/ekran-dublaj.jpg" alt="YouTube videosu açık, VoxCPM2 seçili" width="900"></p>
<p align="center"><i>YouTube bağlantısıyla açılmış bir video · dublajlanacak aralık seçimi · VoxCPM2 ile dublaj</i></p>

<p align="center"><img src="docs/ekran-acilis.jpg" alt="Açılış ekranı" width="900"></p>
<p align="center"><i>Açılış ekranı · video açın, sürükleyip bırakın ya da YouTube bağlantısı yapıştırın</i></p>

## Kurulum

1. [Releases](https://github.com/evrenot3d/dogru-mu-samet/releases/latest) sayfasından
   **`DogruMuSamet-Kurulum-x.y.z.exe`** dosyasını indirip çalıştırın. Yönetici izni gerekmez.
2. Program ilk açıldığında kendi ekranında **"Gerekli bileşenler"** sayfası çıkar.
   **Bileşenleri İndir** deyin; yapay zekâ altyapısı (yaklaşık 5 GB) indirilir.
   Yarıda kalırsa kaldığı yerden devam eder.
3. Bitince program kendiliğinden açılır.
4. **Yapay zekâ modelleri ilk dublajda** indirilir — yalnızca seçtiğiniz ayarların gerektirdikleri, yalnızca
   bir kez. Sonraki dublajlarda bekleme olmaz ve internet gerekmez.

> **Windows / antivirüs uyarısı:** Kurulum dosyası dijital olarak imzalı değildir (imza sertifikası ücretlidir).
> Bu yüzden Windows *"Windows kişisel bilgisayarınızı korudu"* diyebilir ya da antivirüsünüz yeni bir dosya olduğu
> için kısa bir tarama yapabilir. **Ek bilgi → Yine de çalıştır** ile devam edebilirsiniz.
> Kurulumun içinde yalnızca bu sayfadaki açık kaynak kod ve resmî (imzalı) Python bulunur.

**ZIP ile kurulum (alternatif):** Releases sayfasındaki ZIP'i bir klasöre çıkarıp `BASLAT.bat` dosyasına çift
tıklayın. İlk seferde gerekli her şey bir kurulum penceresinde indirilir.

### Gereksinimler

- Windows 10 / 11 (64 bit)
- NVIDIA ekran kartı önerilir (en az 6–8 GB). Kart yoksa işlemciyle çalışır ama çok yavaştır.
- En az 25 GB boş disk alanı; internet bağlantısı (yalnız ilk kurulumda)

## Ticari değildir

Bu program **kişisel kullanım** için, **hobi amaçlı ve deneysel** bir proje olarak yapıldı.
**Ücretsizdir ve hiçbir zaman ücretli olmayacak.** Ticari kullanım için tasarlanmadı ve uygun değildir;
programın kullandığı her modelin kendi lisans koşulları vardır.

YouTube'dan ses indirme özelliği yalnızca kişisel kullanım içindir; indirdiğiniz içerikleri paylaşmayın.

## Açık kaynak

Projenin kaynak kodu [MIT lisansı](LICENSE) ile açıktır. Dilediğiniz gibi kullanabilir, inceleyebilir,
değiştirip geliştirebilirsiniz — gönül rahatlığıyla, tepe tepe kullanın!

Bu program, Anthropic'in yapay zekâ asistanı **Claude** ile birlikte geliştirildi.

## Sırada neler var?

- Yeni ve daha iyi bir **ses temizleme** özelliği
- **Tamamen Türkçe konuşan, hafif bir ses modeli** — bunun için kendi modelimi eğitmeyi düşünüyorum
- Hızlı modda **çok daha fazla konuşmacı sesi**

## Kullanılan modeller ve kütüphaneler

Program bu modelleri ilk kurulumda kendi kaynaklarından indirir; hiçbiri bu depoda dağıtılmaz.
Her birinin kendi lisansı geçerlidir.

| Görev | Model / kütüphane |
|---|---|
| Ses ayırma | [UVR-MDX-NET Voc FT](https://github.com/nomadkaraoke/python-audio-separator) · [BandIt v2](https://huggingface.co/jsoncx/bandit-v2) · [TIGER-DnR](https://huggingface.co/JusperLee/TIGER-DnR) |
| İngilizce metin | [Whisper Large V3 Turbo](https://huggingface.co/openai/whisper-large-v3-turbo) · [faster-whisper](https://github.com/SYSTRAN/faster-whisper) |
| Kadın / erkek ayırma | [voice-gender-classifier](https://huggingface.co/Alice-Sabrina-Ivy/voice-gender-classifier-onnx-q8-v2) · [WeSpeaker ResNet34-LM](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM) |
| Türkçe çeviri | [MiLMMT-46 4B](https://huggingface.co/mradermacher/MiLMMT-46-4B-v1.0-GGUF) · [Hy-MT2 7B](https://huggingface.co/tencent/Hy-MT2-7B-GGUF) · [llama.cpp](https://github.com/ggml-org/llama.cpp) |
| Türkçe ses | [EMA Lightning](https://pypi.org/project/ema-lightning/) · [Antalia-2 Mini](https://pypi.org/project/antalia-mini/) · [VoxCPM2](https://huggingface.co/openbmb/VoxCPM2) · [Ahenk](https://huggingface.co/ozgurguler/ahenk-tts-tr) · [Ahenk Cep](https://huggingface.co/ozgurguler/ahenk-cep-tr) (Özgür Güler, CC BY 4.0) |
| Türkçe metin düzeltme | [trnorm](https://github.com/ozguradmin/ahenk-turkish-tts) (Apache-2.0; `trnorm/` klasörüne kopyalandı) |
| Duygu algılama | [Emotion English DistilRoBERTa-base](https://huggingface.co/j-hartmann/emotion-english-distilroberta-base) (Jochen Hartmann, 2022) · [ONNX kopyası](https://huggingface.co/onnx-community/emotion-english-distilroberta-base-ONNX) |
| Diğer | [PyTorch](https://pytorch.org) · [pywebview](https://github.com/r0x0r/pywebview) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · [Deno](https://deno.com) |

## İletişim

**Evren TUĞ** · ✉ [evrtug@gmail.com](mailto:evrtug@gmail.com) · Instagram: [@evrenot3d](https://www.instagram.com/evrenot3d/)

Öneri ve istekleriniz için bana e-posta gönderebilirsiniz; her mesajı okumaya çalışıyorum.
Sağlık sorunlarım nedeniyle zaman zaman e-postalara geri dönemeyebilirim; şimdiden anlayışınız için teşekkür ederim.

---

<details>
<summary>Geliştiriciler için</summary>

- `baslat.py` — başlatıcı ve ilk kurulum ekranı (`ui/kurulum.html`)
- `app.py` — masaüstü penceresi (pywebview) · `server.py` — yerel arayüz sunucusu
- `jobs.py` — parça parça dublaj işleyicisi · `engine.py` — modeller
- `youtube.py` — YouTube sesi · `guncelle.py` — GitHub'dan güncelleme
- `kurulum/EXE_OLUSTUR.bat` — Windows kurulum dosyasını oluşturur (Inno Setup)

Yeni sürüm: `engine.py` içindeki `VERSION` artırılır, GitHub'da `vX.Y.Z` etiketli bir Release yayınlanır.
Programlar açılışta bunu görür ve "Yeni güncelleme var" der.
</details>
