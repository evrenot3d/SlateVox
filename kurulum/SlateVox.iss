; SlateVox kurulum betiği (Inno Setup 6)
; Oluşturmak için: kurulum\EXE_OLUSTUR.bat
;
; Antivirüs dostu olması için bilerek sade tutuldu:
;   * Yaygın ve güvenilir Inno Setup kullanılır; ek paketleyici (UPX vb.) ya da gizleme yoktur
;   * Yönetici izni istemez, yalnız kullanıcı klasörüne kurar; sistem ayarlarına dokunmaz
;   * İçindeki tek çalıştırılabilir dosya, Python Yazılım Vakfı imzalı resmî Python'dur; gerisi açık kaynak kod
;   * Bileşenler gizlice değil, programın kendi ekranında, kullanıcı "Bileşenleri İndir" deyince indirilir
;   * Sürüm, yayıncı ve açıklama bilgileri eksiksiz doldurulur

#ifndef AppVer
  #define AppVer "1.3.3"
#endif

[Setup]
AppId={{8F4C2A1E-5B7D-4E9A-9C3F-2D6B1A0E7F45}
AppName=SlateVox
AppVersion={#AppVer}
AppVerName=SlateVox {#AppVer}
AppPublisher=Evren TUĞ
AppPublisherURL=https://www.instagram.com/evrenot3d/
AppSupportURL=https://www.instagram.com/evrenot3d/
AppComments=İngilizce → Türkçe video dublaj · hobi amaçlı, açık kaynak, kişisel kullanım
VersionInfoVersion={#AppVer}.0
VersionInfoCompany=Evren TUĞ
VersionInfoDescription=SlateVox kurulumu
VersionInfoProductName=SlateVox
VersionInfoProductVersion={#AppVer}
VersionInfoCopyright=Evren TUĞ · açık kaynak
DefaultDirName={localappdata}\SlateVox
DefaultGroupName=SlateVox
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=cikti
OutputBaseFilename=SlateVox-Kurulum-{#AppVer}
SetupIconFile=logo.ico
UninstallDisplayIcon={app}\logo.ico
UninstallDisplayName=SlateVox
InfoBeforeFile=bilgi.txt
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
UsePreviousAppDir=yes
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "tr"; MessagesFile: "compiler:Languages\Turkish.isl"

[Tasks]
Name: "desktopicon"; Description: "Masaüstüne kısayol oluştur"; GroupDescription: "Kısayollar:"

[Files]
Source: "..\baslat.py";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\app.py";            DestDir: "{app}"; Flags: ignoreversion
Source: "..\server.py";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\jobs.py";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\engine.py";         DestDir: "{app}"; Flags: ignoreversion
Source: "..\youtube.py";        DestDir: "{app}"; Flags: ignoreversion
Source: "..\guncelle.py";       DestDir: "{app}"; Flags: ignoreversion
Source: "..\sepmodels.py";      DestDir: "{app}"; Flags: ignoreversion
Source: "..\polarformer.py";    DestDir: "{app}"; Flags: ignoreversion
Source: "..\vox_worker.py";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\telaffuz.py";       DestDir: "{app}"; Flags: ignoreversion
Source: "..\ses_isleme.py";     DestDir: "{app}"; Flags: ignoreversion
Source: "..\modelleri_indir.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\torch_duzelt.py";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\requirements.txt";  DestDir: "{app}"; Flags: ignoreversion
Source: "..\guncelleme.json";   DestDir: "{app}"; Flags: ignoreversion
Source: "..\BASLAT.bat";        DestDir: "{app}"; Flags: ignoreversion
Source: "..\DURDUR.bat";        DestDir: "{app}"; Flags: ignoreversion
Source: "..\KUR.bat";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\kur.ps1";           DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";         DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\LICENSE";           DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\ui\*";              DestDir: "{app}\ui"; Flags: ignoreversion recursesubdirs
Source: "..\trnorm\*";          DestDir: "{app}\trnorm"; Excludes: "__pycache__"; Flags: ignoreversion recursesubdirs
Source: "logo.ico";             DestDir: "{app}"; Flags: ignoreversion
; kuruluma gömülü Python (EXE_OLUSTUR.bat bir kez indirir): başlatıcı ve kurulum ekranı bununla çalışır
Source: "python\*";             DestDir: "{app}\python"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\SlateVox"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\baslat.py"""; WorkingDir: "{app}"; IconFilename: "{app}\logo.ico"; Comment: "İngilizce → Türkçe video dublaj"
Name: "{autodesktop}\SlateVox"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\baslat.py"""; WorkingDir: "{app}"; IconFilename: "{app}\logo.ico"; Comment: "İngilizce → Türkçe video dublaj"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\baslat.py"""; WorkingDir: "{app}"; Description: "SlateVox'u başlat"; Flags: postinstall nowait skipifsilent

[InstallDelete]
; eski adlı (Doğru mu Samet) kısayollar kaldırılır; AppId aynı kaldığı için eski kurulumun üzerine kurulur
Type: files; Name: "{autoprograms}\Doğru mu Samet.lnk"
Type: files; Name: "{autodesktop}\Doğru mu Samet.lnk"

[UninstallRun]
Filename: "{app}\DURDUR.bat"; WorkingDir: "{app}"; Flags: runhidden; RunOnceId: "Durdur"

[UninstallDelete]
; indirilen modeller, Python ortamları ve çalışma dosyaları da kaldırılır
Type: filesandordirs; Name: "{app}"
