# Live Docker hazırlığı — doğrulama ve teslimat

Tarih: 22 Ekim 2026. Hedef: ayrı Windows laptopta çalışabilecek, gerçek emir
gönderimi kapalı bir Live ortamı. Kurulum rehberi:
[live-deployment-guide.md](live-deployment-guide.md).

## Teslim edilen davranış

Yeni `live-observer`, OKX Live hesabını izinli GET uçlarıyla okur; Telegram
sinyallerini kaydeder ve yöneticiye bakiye/slot/günlük gözlem raporu verir.
`paused=true` ve `LIVE_TRADING_ENABLED=false` kalıcıdır. Emir oluşturma, iptal,
pozisyon kapatma, transfer, kaldıraç veya TP/SL değiştirme yolu yoktur. Telegram
`/resume`, `/confirm`, `/close` ve yönetim HTTP yazma uçları aktivasyon sağlayamaz.

Bu teslimat Live trading executor içermez. Mevcut Demo işlem motoru ve 3 slotlu
strateji korunur. Live'da gerçek işlem açabilmek sonraki ayrı geliştirme ve kabul
çalışmasıdır; yalnızca environment bayrağını değiştirmek yeterli değildir.

Gerçek `live.env` ve proje kökündeki Demo `.env` içerikleri okunmadı, değiştirilmedi
ve kopyalanmadı. Gerçek OKX/Telegram bağlantısı veya mesajı, Demo/Live emri,
Docker build/start/exec ve mevcut DB üzerinde migration yapılmadı. Docker yalnızca
Compose şema doğrulaması için kullanıldı; wrapper argüman testleri sahte Docker
programıyla yapıldı. PostgreSQL testleri yeni, geçici, loopback cluster ve yeni
test veritabanlarında çalıştı; test cluster'ları kapatıldı ve inceleme için bırakıldı.

## Dosyalar

Yeni dosyalar:

- `Dockerfile.live`, `docker-compose.live.yml`: ayrı image, proje, DB, volume,
  ağlar, dosya secrets, non-root gözlemci ve bakım profilleri.
- `config/live.toml`, `config/live.compose.env`: public Live ayarları ve Demo `.env`
  keşfini önleyen açık Compose interpolation dosyası.
- `src/okx_bot/live_deployment.py`: dosyadan yapılandırma, sabit güvenlik sınırları,
  manuel alt hesap teyidi ve UID/domain bağı.
- `src/okx_bot/live_observer.py`: salt okunur hesap gözlemi, Live sermaye döngüsü,
  sinyal dedup, kalıcı emergency ve günlük rapor kuyruğu.
- `src/okx_bot/live_service.py`, `scripts/live_service.py`: Demo uygulamasından
  bağımsız lifecycle, tek instance, health ve kapalı yönetim kapıları.
- `src/okx_bot/live_migration.py`: sabit Live DB hedefi, aktif worker ve Demo veri
  kontrolleri, açık migration; fresh runtime satırı paused oluşturulur.
- `scripts/live_compose.ps1`, `scripts/prepare_live.ps1`: sabit proje seçimi,
  güvenli argüman aktarımı ve yalnız boş harici dizin hazırlığı.
- `scripts/create_live_session.py`: ayrı Live Telegram StringSession; session
  terminale basılmaz, var olan dosya üzerine yazılmaz.
- `scripts/backup_live.ps1`, `scripts/restore_live_check.ps1`,
  `scripts/restore_live_target.ps1`: binary dump, yeni DB'de restore/provenance
  kontrolü ve yalnız boş Live hedefe kontrollü restore.
- `scripts/export_live_source.py`: secret/session/DB içermeyen source zip.
- `tests/test_live_deployment.py`, `tests/test_live_deployment_postgres.py`:
  mock ve izole PostgreSQL kabul kontrolleri.
- `docs/live-deployment-guide.md`, bu rapor.

Güncellenen dosyalar:

- `migrations/env.py`: açıkça verilen SQLAlchemy connection/DATABASE_URL ile
  migration; Demo ayarlarını yüklemeden izole test ve Live bakım imkânı.
- `scripts/check_local_postgres.py`: yeni Live PostgreSQL testlerinin mevcut
  disposable cluster prosedürüne eklenmesi.
- `.gitignore`: iç içe secret/env dosyalarının dışlanması; yalnız public
  `config/live.compose.env` ve örnek dosyalar için istisna.
- `README.md`, `docs/live-trading-readiness.md`,
  `docs/live-docker-deployment-plan.md`, `docs/live-read-only-connection.md`:
  güncel kuruluma yönlendirme; eski planın tarihsel olarak işaretlenmesi.

Demo `docker-compose.yml`, `Dockerfile`, emir motoru, OKX mutasyon istemcisi,
allocator ve migration revision dosyaları değiştirilmedi. Çalışan Demo
container/volume/veritabanlarına müdahale edilmedi.

## Doğrulama komutları ve sonuçlar

Komutlar proje kökünde, yerel `.venv` Python 3.12 ile çalıştırıldı.

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_live_deployment.py --tb=short --show-capture=no --junitxml=.artifacts/live-deployment-unit-tests.xml
.venv\Scripts\python.exe -m pytest -q --tb=short --show-capture=no --junitxml=.artifacts/live-deployment-regression-tests.xml
.venv\Scripts\python.exe scripts/check_local_postgres.py --bin "C:\Program Files\PostgreSQL\16\bin"
.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
.venv\Scripts\python.exe -m compileall -q src scripts migrations
uv lock --check --offline
```

| Kontrol | Sonuç |
| --- | --- |
| Yeni Live odaklı testler | 35 geçti, 0 başarısız (13,499 sn) |
| Tam mock/regresyon | 541 geçti, 0 başarısız; 11 PostgreSQL testi ayrı koşu için deselected (179,11 sn) |
| İzole PostgreSQL | 11 geçti, 0 başarısız (13,92 sn) |
| Alembic | upgrade head, drift check, downgrade base ve tekrar upgrade başarılı |
| Ruff lint | Başarılı |
| Ruff format | 73 dosya uygun |
| compileall | Başarılı |
| Offline dependency lock | Başarılı, 45 paket |
| Docker Compose | Tüm profiller şema kontrolünden geçti; container başlatılmadı |
| PowerShell | Live script AST/sözdizimi ve 6 wrapper argüman/ret testi geçti |
| Telegram mock tekrar kontrolü | 14 geçti; thread uyarısı hata sayılarak da başarılı (5,24 sn) |

Tam regresyonda bir `aiosqlite` worker thread'i test event loop'u kapandıktan
sonra sonuç teslim etmeye çalıştığı için `PytestUnhandledThreadExceptionWarning`
görüldü. Test başarısızlığı yoktu. Uyarının ilişkilendirildiği Telegram mock
dosyası, uyarıyı hata sayan şu kontrolle ayrıca geçti; aralıklı test teardown
uyarısı rapordan gizlenmedi ve giderildiği iddia edilmiyor:

```powershell
.venv\Scripts\python.exe -m pytest -q tests/test_telegram_observability.py --tb=short --show-capture=no -W error::pytest.PytestUnhandledThreadExceptionWarning --junitxml=.artifacts/live-deployment-telegram-recheck.xml
```

35 odaklı test tam 541 testin alt kümesidir; toplam sayıya ikinci kez eklenmez.
PostgreSQL'deki 11 test, mevcut 7 ve yeni 4 testi kapsar. XML kayıtları
`.artifacts/live-deployment-*-tests.xml` ve
`.artifacts/live-deployment-telegram-recheck.xml`; PostgreSQL logu
`.artifacts/live-deployment-postgres-validation.log` içindedir.

Compose testinde `LOCALAPPDATA` gerçek secret dizinine yönlendirilmedi; geçici
test yolu ve sahte fixture dosyaları kullanıldı. `config` dosya içeriklerini
okuyarak credential doğrulaması yapmaz. Gerçek Docker CLI doğrulaması şu sabit
dosyaları ve quiet modu kullandı:

```powershell
docker compose --project-directory C:\Users\halit\Desktop\OKX --env-file config/live.compose.env --file docker-compose.live.yml --project-name telegram-okx-live --profile maintenance --profile tools config --quiet
```

Bu komut test sürecinde geçici `LOCALAPPDATA` environment değeriyle çalıştırıldı.
Sahte Docker testleri `up -d`, migration onayı ve `pg_dump -d/-Fc/-f` argümanlarının
aynen iletildiğini; Demo proje/file override ve `down -v` işlemlerinin
reddedildiğini doğruladı. Bu testler gerçek `up`, dump veya restore çalıştırmadı.

İlk geliştirme turunda PostgreSQL'de eksik fresh runtime satırı nedeniyle iki
başarısızlık görüldü; migration'a paused satır oluşturma eklenerek giderildi.
İki mock fixture beklentisi de düzeltildi. Son kontrollerdeki PowerShell `-d`
argüman tüketme sorunu wrapper'ın native argümanları korumasıyla giderildi ve
regresyon testi eklendi. Bu eski başarısızlıklar kalan hata değildir.

## Kullanıcının uygulayacağı başlatma ve migration sırası

Bu adımlar burada uygulanmadı. Önce rehberdeki WSL2/Docker/Git kurulumu, harici
secret dosyaları, yeni Telegram session ve OKX UI'da TelegramBot1 teyidi tamamlanır.

1. `scripts/prepare_live.ps1`; harici secret/session dizinlerinin ACL ve disk
   şifrelemesi hazırlanır. Gerçek dosyalar source dizinine taşınmaz.
2. `scripts/live_compose.ps1 config`; ardından `build live-observer`.
3. Rehberdeki `live-session` aracıyla yeni ayrı session; `live-identity enroll
   --manual-subaccount TelegramBot1` ile yalnız GET kullanan manuel kimlik bağı.
4. `scripts/live_compose.ps1 up -d postgres-live`; yalnız ayrı Live DB açılır.
5. `scripts/live_compose.ps1 run --rm --no-deps live-migrate migrate
   --confirm-live-database`; fresh şema `0004`, runtime paused. Startup migration yoktur.
6. `scripts/live_compose.ps1 up -d live-observer`; localhost 8011 health kontrolleri.

Güncellemede yalnız Live observer durdurulur, dump alınır, ayrı restore provası
yapılır, açık migration çalıştırılır ve tekrar paused başlatılır. Aktif DB lease'i
veya Demo provenance varsa migration reddedilir. Emergency sıfırlanmaz.

## Secret, bölge ve alt hesap

Docker harici `%LOCALAPPDATA%\OKXTrading\secrets\live.env` dosyasını
`/run/secrets/okx_live_env` olarak salt okunur bağlar. Image/Git/ARG/ENV'e credential
eklenmez. Telegram ve PostgreSQL için ayrı harici secret dosyaları kullanılır.
Root Demo `.env` Live yapılandırmasını belirlemez.

Mevcut GET istemcisi açık Live ortamı, izinli bölgesel host, Futures/net, eksik veya
çakışan credential grupları ve GET endpoint allowlist kontrollerini korur.
Simulated header, redirect/proxy devralma ve domain fallback yoktur. Bu geliştirmede
gerçek endpoint/credential okunmadığı için kullanıcının hesabının bölgesi yeniden
doğrulanmadı; operatör hesap kayıt bölgesini teyit eder ve manifest o domaine bağlanır.

API config yanıtı alt hesap UID'sini ana hesaptan ayırabilir; dost adı otomatik
kanıtlayamaz. TelegramBot1 eşleşmesi OKX UI'da manuel teyit edilmezse servis
başlatılmaz. Araç ham UID yerine tek yönlü hash ve domain kaydeder; pin 30 günlüktür.

## Sermaye ve risk

Yeni Live DB'ye Demo veri kopyalanmaz. Pozitif Live **USDT equity**, kullanılabilir
bakiye ve boş hesap koşuluyla 3 sabit slotlu E/3 sermaye döngüsü oluşturulur.
Sıfır bakiyede hata veya Demo bakiye fallback'i yoktur.

%40 toplam SL limiti ve 10 USDT serbest rezerv Live için aşırı gevşektir. 1.500
USDT'de planlanan 600 USDT stop kaybı ve %0,67 rezerv anlamına gelir. Ayrı Live
ayarları en çok %3 toplam SL, en çok %1 günlük kayıp ve `max(50 USDT, %10 equity)`
rezervle sınırlandırıldı. Kanalın aralık × 0.6 TP/SL seviyeleri risk için
daraltılmaz; mevcut RiskManager risk aşımını reddeder. Bunlar güvenli getiri veya
maksimum zarar garantisi değildir. Bu gözlemci tüm emir niyetlerini zaten reddeder.

Bot dışı/kimliği belirsiz pozisyon, emir veya algo gözlemi kalıcı emergency
oluşturur; hiçbirine müdahale edilmez. Kopma acil durum kilidini etkinleştirir; yeniden bağlantı
emir yeteneği veya resume sağlamaz. TP/SL sayısı gerçek koruma kabul testi değildir.

## Tek instance, ikinci laptop ve restart

Sabit container adı aynı daemon'da ikinci kopyayı; PostgreSQL advisory lock aynı
DB'de ikinci listener/admin poller'ı engeller. DB lease kaybında süreç sonlanır.
Tek worker kullanılır. **Farklı hostlardaki bağımsız DB'ler için küresel lock yoktur.**
Bu teslimatta Live executor yoktur; gelecekte gerçek executor'dan önce merkezi
lease/fencing ayrıca tamamlanmalıdır.

Source-only zip yeni laptopa taşınır. Eski Live host ve başlangıç görevleri
durdurulur; eski Live API anahtarı iptal edilip yeni host/IP için ayrı anahtar,
gerekirse Live bot token rotasyonu yapılır. Session/secrets ayrı şifreli kanaldan
aktarılır; Demo session eşzamanlı kopyalanmaz. Live dump yeni boş hedefe kontrollü
restore edilir; migration ve paused startup uygulanır. Fencing kanıtlanamıyorsa
ikinci host açılmaz.

Docker Desktop kullanıcı oturumunda başlatılır; restart policy bile gerçek emir
kapısını açmaz. Task Scheduler'a resume eklenmez. Uyku, elektrik, disk, saat ve
IP allowlist izlenir. Reconnect emergency'yi otomatik temizlemez.

## Backup ve rollback

`backup_live.ps1` binary custom dump'ı `docker cp` ile alır; PowerShell binary
redirect kullanılmaz. `restore_live_check.ps1` her defasında yeni DB'ye restore
eder ve Live provenance kontrol eder. `restore_live_target.ps1 -ConfirmEmptyTarget`
yalnız durmuş observer ve tamamen boş hedefte, başarılı prova ve dosya hash
kontrolünden sonra restore eder. Demo ve mevcut hedefler overwrite edilmez.

Rollback: yalnız `live-observer` durdurulur; Live dump ve önceki benzersiz image
tag/ID korunur. Önceki image schema uyumuyla aynı Live mount'larda paused açılır.
Schema downgrade/aktif DB üstüne eski dump uygulanmaz; gerekirse ayrı kurtarma
DB/volume'u kullanılır. `down -v` ve volume prune yoktur. Demo ortamı korunur.

## Eksik kabul testleri ve kalan riskler

- Gerçek yeni laptopta image build/start, UID 10001 mount/ACL, PostgreSQL 17,
  secret ve session erişimi, Telegram topic, Windows reboot ve ağ/DB/disk kesintisi
  testleri yapılmadı. Yerel PostgreSQL test sürümü 16'dır.
- Gerçek API/Telegram doğrulaması ve Docker üzerinde backup/restore provası kullanıcı
  ortamında bekliyor. Kullanıcı önceki Live GET testini başarılı bildirdi.
- Live executor, global iki-host lease/fencing ve gerçek fill/OCO yaşam döngüsü
  kabulü tamamlanmadan gerçek emir yeteneği eklenmemeli.
- Sonradan ayrıca yetkilendirilmiş Demo kabulünde kısmi fill, reduceOnly/net,
  TP/SL kabul/ret/trigger/state/size, fill sonrası koruma, fee/funding, sahiplik,
  günlük PnL ve tekrar koruması uçtan uca sınanmalı. Bu görevde emir testi yapılmadı.
- Sabit dış IP, allowlist, host güvenliği, disk/backup şifrelemesi, Telegram session
  hırsızlığı, manuel hesap teyidi, 30 günlük pin yenilemesi ve mutable image tag'leri
  operasyonel risklerdir. Compose file secrets bir şifreli kasa değildir.

Kaynak paketi `.artifacts/okx-live-source-20261022.zip`; makinece okunabilir
sonuçlar `.artifacts/live-deployment-verification.json` içindedir. Paket gerçek
secret, session, DB veya backup içermez. Paket oluşturma ve içerik adları kontrolü
tamamlandı; dosya hash'i sonuç JSON'una kaydedildi.

```powershell
.venv\Scripts\python.exe scripts/export_live_source.py --output .artifacts/okx-live-source-20261022.zip
Get-FileHash .artifacts/okx-live-source-20261022.zip -Algorithm SHA256
```
