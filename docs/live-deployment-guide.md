# Ayrı Windows laptopta Live gözlemci kurulumu

Bu sürüm **Live trading açmaz**. Yeni servis salt okunur `live-observer` olarak
çalışır; OKX tarafında yalnız izinli GET uçlarını kullanır. Emir oluşturma/iptal,
transfer, kaldıraç veya TP/SL değiştirme yeteneği yoktur. `paused=true` ve
`LIVE_TRADING_ENABLED=false` sabittir; restart, `/resume` veya iki aşamalı onayla
bile gerçek emir kapısı açılamaz. Mevcut Demo emir kilitleri korunur.

Kullanıcı Live GET kontrolünün Futures (2), net mode, sıfır bakiye/pozisyon/emir
ile başarıyla geçtiğini bildirdi. Bu geliştirmede secret dosyası okunmadı, gerçek
API çağrısı veya emir gönderilmedi ve Live container başlatılmadı.

## Ayrı altyapı

| Bileşen | Live karşılığı |
| --- | --- |
| Compose / proje | `docker-compose.live.yml` / `telegram-okx-live` |
| Image | `telegram-okx-live-observer:local`, ayrı `Dockerfile.live` |
| PostgreSQL | `postgres-live`, DB/kullanıcı `okx_live`, PostgreSQL 17 |
| Kalıcı DB volume | `telegram-okx-live-postgres-data` |
| HTTP | Yalnız `127.0.0.1:8011`; yönetim raporları bearer korumalı |
| Veri / session | `%LOCALAPPDATA%\OKXTrading\live\data` |
| Log / backup | `%LOCALAPPDATA%\OKXTrading\live\logs` ve `live\backups` |
| Hesap kimliği | `%LOCALAPPDATA%\OKXTrading\live\identity\account.json` |
| Public ayarlar | `config/live.toml` |

Demo Compose, image tanımı, DB ve volume'ları değiştirilmedi. Live DB'nin dışarı
portu yoktur; yalnız internal backend ağındadır. Gözlemcinin ayrı egress ağı
OKX/Telegram'a erişir. Non-root UID 10001, salt okunur root ve session mount'u,
capability kaldırma ve dönen loglar kullanılır.

**Daima `scripts/live_compose.ps1` kullanın.** Wrapper ayrı Compose/proje ile açıkça
credentials içermeyen `config/live.compose.env` seçer; Demo `.env` devralınmaz.
Project override ve volume silme seçenekleri reddedilir. `config` yalnız quiet
çalışır. Profile'lı kimlik/session/migration araçları normal `up` ile başlamaz.

## 1. Yeni Windows laptop

1. Docker'ın güncel desteklediği Windows, BIOS/UEFI sanallaştırması, güncel Windows
   ve WSL2 gerekir. Yönetici PowerShell'de `wsl --install`, restart sonrası
   `wsl --update` ve `wsl --version` uygulayın. [Microsoft WSL rehberi](https://learn.microsoft.com/en-us/windows/wsl/install).
2. [Git for Windows](https://git-scm.com/downloads/win) ve [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
   kurun. WSL2 engine, Linux containers ve gereken distro integration'ı seçin.
   [Docker WSL2 belgesi](https://docs.docker.com/desktop/features/wsl/) en az WSL
   2.1.5 ister; güncel WSL tercih edilir. `docker compose version` ile doğrulayın.
3. Kaynakları yeni kullanıcı dizinine aktarın. Mevcut klasörde Git deposu yoksa
   clone varmış gibi davranmayın; kaynak paketi kullanın. Geliştirme ortamında:

   ```powershell
   .\.venv\Scripts\python.exe scripts/export_live_source.py --output .artifacts\okx-live-source.zip
   ```

   Paket yalnız source/config/docs/tests içerir; `.env`, secret, session, DB,
   yedek ve `.venv` içermez. Güvenilir kanalla taşıyıp ayrı dizine açın. Git repo
   oluşturulursa ignore ve `git status` kontrolünden sonra commit yapın; credential
   veya session hiçbir zaman push edilmez. Runtime dosyaları repo dışında kalır.

## 2. Harici secret ve Telegram session

Proje dizininde:

```powershell
.\scripts\prepare_live.ps1
```

Bu script yalnız boş dizinleri oluşturur; dosya/secret okumaz veya değiştirmez ve
servis başlatmaz. Host diski/yedekleri şifreleyin; Windows ACL'lerini bakım
kullanıcısı ve gerekli sistem erişimiyle sınırlayın. Docker Desktop/UID 10001'in
mount okuma/yazma izinleri hedef laptopta ayrıca doğrulanmalıdır.

| `%LOCALAPPDATA%` altındaki dosya | Container içi / tüketen servis |
| --- | --- |
| `OKXTrading\secrets\live.env` | `/run/secrets/okx_live_env`; gözlemci + kimlik aracı |
| `OKXTrading\secrets\telegram-live.env` | `/run/secrets/telegram_live_env`; gözlemci + session aracı |
| `OKXTrading\secrets\postgres-live-password.txt` | `/run/secrets/postgres_password`; yalnız Live DB/migration/gözlemci |

Compose secrets dosyaları salt okunur mount eder; API anahtarları environment,
Docker ARG/ENV veya image layer'a konmaz. File-source secrets otomatik şifreli
kasa değildir; [Docker secrets belgesi](https://docs.docker.com/compose/how-tos/use-secrets/).
`.dockerignore`, `.gitignore` ve kaynak export allowlist'i ayrı koruma sağlar.

Mevcut `live.env` içeriğine dokunulmaz. Şema [Live GET rehberindedir](live-read-only-connection.md).
`OKX_BASE_URL`/`OKX_LIVE_BASE_URL` açıkça hesabın kayıt bölgesine uygun olmalı;
Demo header'ı, region tahmini, redirect/proxy devralma veya domain fallback yoktur.
Kimlik bağı domaine bağlıdır. `LIVE_TRADING_ENABLED=false` kalmalıdır.

PostgreSQL için en az 24 karakter ayrı rastgele parola üretip güvenli editör/parola
yöneticisiyle `postgres-live-password.txt` dosyasına UTF-8/BOM'suz, tek satır
kaydedin. Terminale yazdırmayın; Demo parolasını kullanmayın. Mevcut volume'da
dosyayı değiştirmek DB parolasını otomatik değiştirmez; rotasyon ayrı bakım ister.

`telegram-live.env` yalnız şu alanları içerir; gerçek değerler yerelde doldurulur:

```dotenv
TELEGRAM_API_ID=<Telegram uygulama ID>
TELEGRAM_API_HASH=<Telegram uygulama hash>
TELEGRAM_BOT_TOKEN=<yalnız Live gözlemciye ait ayrı BotFather token>
TELEGRAM_ADMIN_ID=<doğrulanmış yönetici ID>
ADMIN_API_TOKEN=<en az 32 karakter ayrı rastgele token>
```

Demo admin bot token'ını yeniden kullanmayın; getUpdates tüketicileri çakışabilir.
Sinyal okuyan Telegram kullanıcı hesabı gruba üye olmalıdır. Kanal
`-1003769687656`, topic `23576` Live config'te sabittir.

```powershell
.\scripts\live_compose.ps1 config
.\scripts\live_compose.ps1 build live-observer
```

Build container başlatmaz. Config doğrulaması yalnız Compose şemasını doğrular;
gerçek secret içeriği veya mount izinlerinin çalıştığını kanıtlamaz.

Çalışan Demo session'ını kopyalayıp eşzamanlı kullanmayın. Yeni ayrı session,
OKX/DB credential'ı bağlanmayan interaktif araçla oluşturulur:

```powershell
.\scripts\live_compose.ps1 run --rm --no-deps live-session --telegram-env-file /run/secrets/telegram_live_env --output /app/live-data/telegram.session-string
```

Telefon/kod/2FA kullanıcı terminalinde girilir; StringSession basılmaz. Var olan
dosya üzerine yazılmaz. Zaten bağımsız bir Live session varsa eski Live gözlemci
durdurulduktan sonra şifreli kanalla `live\data\telegram.session-string` konumuna
aktarılabilir. Demo session/volume'una dokunmayın. Sızıntıda Telegram Devices
ekranından ilgili oturumu iptal edin.

## 3. TelegramBot1 ve endpoint bağı

`GET /account/config` içindeki `uid != mainUid` alt hesap ayrımını gösterir;
`label` API etiketi olabilir, `TelegramBot1` adının kanıtı değildir.
[OKX account config sözleşmesi](https://www.okx.com/docs-v5/en/#trading-account-rest-api-get-account-configuration).

Operatör doğru bölgesel OKX web/app ekranında **TelegramBot1** alt hesabını açıp
anahtarın bu alt hesaba ait olduğunu, Futures/net modunu, Read iznini, Withdrawal
izni olmadığını ve sabit çıkış IP allowlist'ini doğrular. Bu aşamada Trade izni
gerekmez; Read-only tercih edilir. Değerleri/ekran görüntülerini paylaşmayın.
Bu eşleşme doğrulanamıyorsa **durun**, manifest oluşturmayın ve servis başlatmayın.

Manuel kontrol gerçekten tamamlandıktan sonra:

```powershell
.\scripts\live_compose.ps1 run --rm --no-deps live-identity enroll --manual-subaccount TelegramBot1
```

Araç yalnız config GET yapar; Futures/net ve alt hesabı doğrular. Ham UID yerine
`sha256("live:" + uid)` ve domaini 30 günlük, manuel teyitli `identity\account.json`
dosyasına bağlar. API key/UID basılmaz. Manifest yoksa/süresi dolmuşsa, UID veya
domain değişmişse başlangıç reddedilir; her polling turunda bağ tekrar kontrol
edilir. Çalışırken doğrulama hatası gözlemi sağlıksız yapar ve kalıcı emergency
kilidini etkinleştirir; sonraki GET denemeleri kilidi açmaz.
Bu **operatör teyidi + teknik UID/domain bağıdır**, otomatik dost adı doğrulaması
iddiası değildir. Dosya varsa üzerine yazılmaz. Yenilemede gözlemciyi durdurun,
eski manifesti güvenli arşivleyin, OKX ekranını yeniden doğrulayıp enroll edin.

## 4. Güvenli migration ve ilk açılış

Demo dump, bakiye, slot döngüsü veya işlem geçmişi Live DB'ye taşınmaz.

```powershell
# Yalnız yeni Live PostgreSQL:
.\scripts\live_compose.ps1 up -d postgres-live
# Ayrı migration; açık bakım onayı şart:
.\scripts\live_compose.ps1 run --rm --no-deps live-migrate migrate --confirm-live-database
# Kimlik, session ve schema hazır olduktan sonra:
.\scripts\live_compose.ps1 up -d live-observer
.\scripts\live_compose.ps1 ps
Invoke-RestMethod http://127.0.0.1:8011/health/live
Invoke-RestMethod http://127.0.0.1:8011/health/ready
```

Bu başlatma/migration komutları geliştirme sırasında **çalıştırılmadı**. `serve`
migration yapmaz; `0004` hazır değilse durur. Migration yalnız
`postgres-live:5432/okx_live`, kullanıcı `okx_live` hedefini kabul eder. Aktif
worker lease'i, Demo environment bağı veya işlem geçmişi varsa migration durur.
Fresh runtime satırı ve her başlangıç `paused=true` olur; emergency sıfırlanmaz.

Güncelleme: yalnız Live gözlemciyi durdur → Live backup → izole restore provası →
ayrı migration → schema/kimlik kontrolü → paused gözlemci. Live için
`scripts/start.py` kullanılmaz; o mevcut Demo yaşam döngüsüdür.

## 5. Sermaye, strateji ve risk

İlk pozitif **Live USDT equity** (`details[USDT].eq`) ve kullanılabilir bakiyeyle,
pozisyonsuz/emirsiz doğrulanmış hesapta Live cycle açılır: 3 slot, hedef E/3.
USD totalEq, başka currency veya Demo bakiyesi USDT sermayesi yerine kullanılmaz.
Sıfır hesap normaldir; fonlanana kadar cycle bekler. Cycle her GET'te yeniden
hesaplanmaz. Gözlemci slot rezerve etmez veya emir niyeti oluşturmaz.

Aynı ChannelSignalParser LONG/SHORT ve aralık × 0.6 seviyelerini üretir; sinyaller
kaydedilir/dedup edilir ve execution disabled ile reddedilir. Mevcut Demo gerçek
fill TP/SL, sahiplik, PnL, iki aşamalı onay ve emir tekrar koruması kodları korunur;
Live executor'a açılmış sayılmaz. `/status`, `/balance`, `/allocation`, `/positions`,
`/pnl`, `/daily`, `/trades`, `/history`, `/limits` gözlem özeti verir. Günlük bot
PnL bu sürüm Live işlem üretmediği için 0'dır; manuel hareketler bot getirisi gibi
gösterilmez. Günlük mesaj güncel gözlem özetidir, geçmiş gün kapanış bakiyesi değildir.

**%40 toplam SL riski ve 10 USDT rezerv Live için aşırı gevşektir.** 1.500 USDT
örneğinde %40 = 600 USDT planlanan stop zararı; 10 USDT yalnız %0,67 rezervdir.
Gap, kayma, likidasyon ve ücretler tahmini stop kaybını aşabilir. 10x zararları da
büyütür; [OKX kaldıraç risk açıklaması](https://www.okx.com/en-sg/help/understanding-leverage-futures-and-margin).

Ayrı Live config en çok %3 toplam SL riski, en çok %1 günlük kayıp ve
`max(50 USDT, equity × %10)` rezerv uygular. Bunlar garanti değil, yeniden
incelenmesi gereken muhafazakâr hazırlık sınırlarıdır. 3 slotun eşzamanlı dolması
garanti edilmez; E/3 bütçeleri, fee/MMR ve rezerv sonraki slotu reddedebilir.
Kanal TP/SL'si riske uydurularak daraltılmaz; mevcut RiskManager işlemi reddeder.
Bu politika mock testle doğrulanır; gözlemcide bütün emir girişleri zaten kapalıdır.

Hesapta dış pozisyon/emir/algo görülürse emergency kalıcı olur; gözlemci bunları
iptal/kapatma/koruma değiştirme işlemi yapmaz. TP/SL kayıt sayısı koruma yeterliliği
veya stop'un gerçekten tetiklendiği kanıtı değildir. Bu aşamada emir testi yapılmaz.

## 6. Restart, internet ve tek instance

Docker Desktop'ta Windows oturum açılınca başlama seçeneğini etkinleştirin. WSL2
Linux containers için yalnız Windows servisinin boot etmesine güvenmeyin;
kullanıcı oturumu ve Docker Desktop hazır olmalıdır. [Windows izin belgesi](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/).
Uyku/hibernation, elektrik, disk, NTP ve internet izlenmelidir. Docker unhealthy
container'ı tek başına restart etmez; dış sağlık takibi gerekir.

`unless-stopped` ile geri gelse bile gözlemci her açılışta paused, gerçek emir
kapısı kapalıdır. Task Scheduler'a resume/aktivasyon komutu eklenmez. Süresi
dolmuş kimlik pin'i açılışı da reddeder. API veya Telegram kaybı emergency'yi
kalıcı yapar; yeniden bağlantı raporlamayı geri getirebilir, kilitleri açmaz.
DB lease kaybında süreç durur, kaybolan lock sessizce yeniden kazanılmış sayılmaz.

Aynı Docker daemon'da sabit `telegram-okx-live-observer` container adı ikinci
kopyayı engeller. Aynı Live DB'de PostgreSQL advisory lock ikinci listener/admin
poller'ı reddeder. Tek worker/replica vardır. **İki farklı hosttaki ayrı DB'ler
ortak lock kullanmaz.** Şu an Live executor yoktur; ileride gerçek executor
öncesinde merkezi lease veya doğrulanmış operasyonel fencing zorunludur.

Taşımada eski Live worker'ı ve Windows başlangıç görevlerini durdurun; eski Live
OKX anahtarını iptal edip yeni host/IP'ye ait anahtar üretin. Gerekirse ayrı Live
bot token'ını yenileyin. Session eşzamanlı kullanılmaz; Demo'ya dokunulmaz. Sabit
dış IP için ISP/VPN çıkışını doğrulayın; CGNAT/değişken IP varsaymayın. IP hatasını
allowlist'i kaldırarak aşmayın. Fencing doğrulanamıyorsa ikinci host başlatılmaz.

## 7. Backup, restore ve taşıma

```powershell
.\scripts\backup_live.ps1
.\scripts\restore_live_check.ps1 -BackupFile '<şifreli diskteki Live dump yolu>'
```

Backup container içinde custom-format pg_dump üretip binary dosyayı `docker cp`
ile alır; PowerShell `>` kullanmaz. Restore helper yeni
`okx_live_restore_<uuid>` DB oluşturur, `--exit-on-error --no-owner` ile yükler,
schema/paused/emergency'yi kontrol eder. Aktif `okx_live`/Demo DB üzerine yazmaz
ve bot başlatmaz. Prova DB'leri inceleme için kalır; retention silmesi ayrı bakımdır.
Prova ayrıca DB'nin `environment=live` bağına sahip olduğunu, cycle'ların Live
olduğunu ve gözlemci sürümünde işlem kaydı bulunmadığını doğrular. Boş/bağlanmamış
veya Demo yedeği promotion için kabul edilmez.
Dump'ları tarihli, şifreli diskte ve ayrı cihazda saklayın. Session/secrets dump'ta
yoktur; ayrı şifreli aktarılır. Kaynak zip runtime yedeği değildir.

Taşıma sırası: eski Live hostu fence et → tutarlı Live dump/session yedeği →
yeni laptopta kaynak ve hosta özel credential/IP → manuel UID pin → yalnız yeni
Live DB → **boş hedef DB'ye** kontrollü restore → ayrı migration → paused gözlemci.
Yeni hedefte, `postgres-live` açıldıktan sonra fakat migration/gözlemci başlamadan:

```powershell
.\scripts\restore_live_target.ps1 -BackupFile '<doğrulanacak Live dump yolu>' -ConfirmEmptyTarget
.\scripts\live_compose.ps1 run --rm --no-deps live-migrate migrate --confirm-live-database
```

Target helper gözlemci çalışıyorsa veya `okx_live` içinde herhangi public tablo
varsa durur. Önce ayrı DB'de restore/provenance doğrular, dosya hash'ini tekrar
kontrol eder ve yalnız boş hedefe restore eder. Drop/clean/overwrite yapmaz.
Kısmi restore hatasında servis başlatılmaz; eski kaynak/yedek korunur ve ayrı
yeni hedef hazırlanır. Migration yapılmış mevcut DB'de bu helper kullanılmaz.
Yedekten sonraki borsa hareketleri ayrıca uzlaştırılır. [pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html).

## 8. Rollback

1. `live_compose.ps1 stop live-observer` ile yalnız Live gözlemciyi durdurun.
2. Live dump ve kullanılan image ID/tag'ini saklayın. Başarılı image'i güncelleme
   öncesi benzersiz tag ile koruyun; `:local` tek başına rollback arşivi değildir.
3. Önceki image/kaynağı aynı Live config/mount'larla seçin, schema uyumunu kontrol
   edin; kimlik pin'i ve emergency korunarak yalnız paused gözlemciyi başlatın.
4. Schema downgrade veya eski dump ile aktif DB overwrite normal rollback
   değildir. Gerekirse ayrı kurtarma DB/volume'u kullanın; mevcut volume'u koruyun.
   `down -v`, volume prune/rm veya Demo Compose kullanılmaz.

## 9. Gerçek para öncesi eksikler ve kalan riskler

- Live executor yoktur; env veya Telegram onayıyla açılamaz. Ayrı kod incelemesi
  ve açık aktivasyon çalışması gerekir. Mevcut Demo hard gate'leri kaldırılmaz.
- Yetkili ayrı Demo kabulünde gerçek IOC/kısmi fill, attached OCO kabul/state,
  trigger/size, fill sonrası TP/SL, reduceOnly/net, fee/funding, yalnız bot
  sahipliği ve günlük PnL uçtan uca doğrulanmalıdır. ACK/sayı tek başına yeterli değildir.
- Gerçek OKX TP/SL kabul/ret ve uygulama kapalıyken tetikleme Live'da denenmedi.
  Bu aşama hiçbir gerçek veya Demo emri göndermeye yetki vermez.
- Yeni laptopta image build/start, secret mount izinleri, Telegram topic, Windows
  restart, ağ/DB/disk kesintisi ve backup/restore provaları eksiktir. Compose
  doğrulaması container çalıştırma veya disk kalıcılığı kabulü değildir.
- Yerel PostgreSQL testleri 16 üzerinde, hedef Compose 17'dir; hedef sürümde tam
  image/restore/restart provası ayrıca gerekir. Mevcut Demo DB testlerde kullanılmaz.
- Dost adı manuel teyitlidir. Yerel manifest/secrets erişimi güven sınırıdır;
  pin 30 günde yenilenir. Windows oturum/uyku bağımlılığı, iki bağımsız host için
  ortak lease eksikliği, dinamik IP, session hırsızlığı, şifresiz backup ve mutable
  image tag'leri kalan operasyonel risklerdir.

Güncel doğrulama komutları ve sonuçları [teslimat raporunda](live-deployment-verification-report.md)
yer alır. Hiçbir
restart veya yönetici komutu bu teslimattaki gerçek emir kapısını açmaz.
