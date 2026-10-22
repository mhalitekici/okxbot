# Live Docker hazırlık planı — uygulanmadı

Bu belge önceki planı kaydeder. Güncel ayrı Compose ve salt okunur gözlemci
[deployment rehberinde](live-deployment-guide.md) uygulanmıştır; container
başlatma ve gerçek Live trading bu geliştirmede yapılmamıştır.

Bu belge gelecekteki ayrı dağıtım çalışması içindir. Mevcut kodun emir istemcisi
Demo'ya kilitlidir; Live env dosyasını mevcut `trading-bot` servisine vermek uygun
değildir. `LIVE_TRADING_ENABLED=false` korunur. Bu çalışmada imaj build, container
başlatma/recreate, migration, para transferi veya Demo/Live emir testi yapılmaz.

## Ayrı yapılandırma ve depolar

| Bileşen | Mevcut Demo | Gelecekte Live |
| --- | --- | --- |
| Compose project | `telegram-okx-demo` korunur | Ayrı `telegram-okx-live` |
| Credential | Mevcut `.env` korunur | Kullanıcıya ait harici Live secret dosyası |
| OKX | Demo header + Demo anahtarları | Açık bölgesel Live host, Demo header yok |
| Hesap | Demo hesabı | Yalnız bot için TelegramBot1 alt hesabı |
| PostgreSQL | Mevcut DB ve volume korunur | Yeni Live DB, kullanıcı, parola ve volume |
| Telegram session | Mevcut Demo volume korunur | Ayrı erişim kontrollü Live session volume |
| Yönetim portu | `127.0.0.1:8010` | Ayrı localhost portu ve ayrı admin token |
| Başlangıç | Mevcut başlangıç pause kapısı | Her başlangıçta paused + emir kapısı kapalı |

Live veritabanına Demo runtime state/slot/işlem geçmişi kopyalanmaz. Üç slot ve
sermaye döngüsü algoritması aynı kalır; yeni Live DB, gerçek trading hesabı ve
sahiplik doğrulandıktan sonra kendi slot döngüsünü oluşturur. Migration 0004
gelecekte yalnız yeni, ayrı Live DB'de ve yedek/inceleme sonrası uygulanır.
Mevcut Demo DB/volume, Compose project adı ve migration geçmişine dokunulmaz.

## Secret aktarımı

`%LOCALAPPDATA%\OKXTrading\secrets\live.env` build context dışında kalır. Dockerfile'a
`COPY`, `ARG`, `ENV` ile credential eklenmez. `.dockerignore` iç içe `*.env`,
`*.env.*`, `secrets` dizinlerini de dışlar. Build cache ve image layer'ları secret
taşımaz. Kaynak arşivinde bu dosya bulunmaz.

İlk olası container kullanımı yalnız tanı aracıdır: ayrı, one-shot servis;
uygulama CMD'sini geçersiz kılan `python scripts/check_live_connection.py
--env-file /run/secrets/okx_live_env` komutu; `restart: "no"`, `read_only: true`,
`cap_drop: [ALL]`, `no-new-privileges`; PostgreSQL/Telegram bağımlılığı veya portu
yok. Bu servis şu an oluşturulmamış ve çalıştırılmamıştır.

Harici dosya Compose `secrets` file source veya `read_only` bind mount ile yalnız
`/run/secrets/okx_live_env` altına bağlanmalı, `env_file` ile tüm container
environment'ına açılmamalıdır. Salt okunur root filesystem tek başına mount'u
salt okunur yapmaz; mount ayrıca `read_only` olmalıdır. Windows ACL yalnız ilgili
kullanıcıya erişim vermeli, non-root UID 10001'in Docker Desktop mount okuması
ayrıca doğrulanmalıdır. File tabanlı Compose secrets otomatik şifreli kasa değildir;
host diski ve yedekler şifreli tutulmalıdır. Ayrıntılar:
[Docker Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

Loglar, support arşivleri, normal `docker compose config`, `inspect` ve shell
transcript çıktıları credential içerebilir; kullanıcı paylaşımına açılmaz.
Yapılandırma doğrulaması için gelecekte `config -q` tercih edilir.

## Paused, emir kapısı ve tek instance

Gelecek Live sürümünde env anahtarı tek başına emir yetkisi açmamalı. En alt HTTP
mutation sınırında aşağıdakiler birlikte zorunlu olmalı: Live için ayrı incelenmiş
kod, doğru credential/ortam, doğrulanmış hesap kimliği, beklenen mod, yalnız bir
aktif worker lease'i, güncel reconciliation, emergency yokluğu ve hesap/snapshot'a
bağlı süreli iki aşamalı operatör aktivasyonu. Process restart, lease kaybı veya
şüpheli durumda aktivasyon iptal edilmeli; DB/env'den otomatik devam etmemeli.
Mevcut sürümde bu kapı daimi kapalı kalır; bu tasarım uygulanmış kabul edilmez.

**Paused yalnız yeni girişleri durdurabilir.** Reconciliation, açık emir yönetimi,
TP/SL düzeltmesi, iptal veya flatten gibi mutation'lar için ayrıca merkezi kapı
gerekir. Salt okunur tanı bu yüzden `scripts/start.py` veya uygulama lifespan'ını
kullanmaz. İleride koruyucu emir yönetimini açık tutan işlem modu ile hiçbir
mutation göndermeyen bakım modu açıkça ayrılmalıdır.

Compose'ta tek bot servisi, tek replika ve tek Uvicorn worker çalışır. Aynı Live
DB'deki PostgreSQL advisory lock ikinci işçiyi reddeder; lease kaybında süreç
durmalıdır. **Ayrı DB kullanan iki bilgisayarı bu lock engellemez.** Otomatik
çok-host failover kurulmaz: ikinci hosta geçmeden eski worker durdurulur,
yeniden başlaması kapatılır ve eski Live API anahtarı iptal edilerek yeni hosta
ayrı anahtar/IP bağı verilir. Bu geçiş doğrulanmadan yeni host başlatılmaz.

## Windows ve ikinci bilgisayara taşıma

1. Live bot için başlangıç tercihi `restart: "no"`; Docker Desktop açılsa bile bot
   otomatik çalışmaz. Windows Task Scheduler kullanılacaksa yalnız sağlık/uyarı
   görevi kurulur; trading/resume/kapı açma komutu eklenmez. İleride gözetimsiz
   yeniden başlatma istenirse açılışta paused/kapalı kapı şartı ayrıca test edilir.
2. Uyku/hibernation, elektrik, disk doluluğu, saat senkronu ve sabit çıkış IP'si
   izlenir. Healthcheck'in unhealthy container'ı kendiliğinden restart etmediği
   hesaba katılır. DB kesintisi otomatik lease geri kazanımı sayılmaz.
3. Taşımada operatör yeni girişleri durdurur, açık pozisyon ve borsa korumasını
   inceler; eski worker ve Windows başlatma görevlerini kapatır. Kapalı gate ile
   tutarlı DB dump ve session yedeği alınır. Eski host yeniden başlayamamalıdır.
4. Kaynak kodu ve `uv.lock` aktarılır; `.venv`, gerçek secret ve session kaynak
   arşivine konmaz. Yedek/session ayrı şifreli kanalla aktarılır; Live anahtarı
   hedefte yeniden üretilir. Hedef host IP allowlist'i ve bölgesel adres yerelde
   doğrulanır. Demo verileri kendi DB/volume'unda korunur.
5. Restore önce ayrı prova DB'sinde, workersız doğrulanır. Sonra yeni hedef Live
   DB'ye geri yüklenir. Hedefte ilk kontrol yalnız bu Live GET aracıdır. Gelecekteki
   Live worker ayrı aşamada paused başlar; bakiye/emir/slot/sahiplik uzlaşması ve
   operatör aktivasyonu olmadan gerçek emir kapısı açılmaz.

## PostgreSQL yedek ve restore

Plan: günlük custom-format `pg_dump`, sürüm değişikliği öncesi ek dump, şifreli
yerel ve ayrı cihaz kopyası; tarihli dosyalar ve retention. Binary dump'ı Windows
PowerShell `>` ile taşımak yerine container içinde üretip `docker cp` ile alma
örneği [mevcut Docker rehberinde](docker-deployment.md) bulunur. Bu komutlar bu
çalışmada çalıştırılmadı. PostgreSQL portu dışarı açılmaz; parola komuta/loga yazılmaz.

Hedef PostgreSQL sürümü/dump araçları uyumluluğu doğrulanır. `pg_restore --list`
sonrası yalnız yeni boş prova DB'sine `--exit-on-error --no-owner` ile restore
yapılır; migration sürümü, slot/cycle, trade/fill, PnL, outbox, paused/emergency ve
hesap bağlama kayıtları kontrol edilir. Üretim DB'sine `--clean`, downgrade veya
eski dump ile overwrite yapılmaz; `docker compose down -v` kullanılmaz.
DB dump secret dosyasını veya Telegram session'ını içermez; bunlar ayrı korunur.
Restore sonrası son yedekten sonraki borsa hareketleri ayrıca uzlaştırılır.
Bkz. [PostgreSQL pg_dump](https://www.postgresql.org/docs/current/app-pgdump.html),
[pg_restore](https://www.postgresql.org/docs/current/app-pgrestore.html).

## Telegram, IP ve acil durum

Telethon session bir hesap erişim credential'ıdır: private volume, şifreli yedek,
en az dosya izni, log/arşiv dışı tutma gerekir. Aynı session'ın iki hostta eşzamanlı
kullanımı engellenir. Demo session volume'u silinmez veya Live ile üzerine yazılmaz;
yeni Live session oluşturma kullanıcı tarafından ayrı aşamada yapılır. Session
sızıntısında Telegram Devices ekranından oturumlar iptal edilir, yerel kopyalar
yenilenir. Admin ID/özel sohbet denetimi ve ayrı admin API token korunur.

OKX anahtarı yalnız TelegramBot1'e ait olmalı, Withdrawal izni olmamalı; bu aşamada
Read yeterlidir. Gelecekte Trade izni yalnız ayrı aktivasyon aşamasında kullanılır.
IP allowlist sabit çıkış IP'siyle sınırlandırılır; `50110` halinde allowlist'i
kaldırarak hata aşılmaz. VPN, ISP/CGNAT ve host değişimi sonrasında GET kontrolü
yenilenir. Bkz. [OKX API anahtar güvenliği](https://www.okx.com/docs-v5/).

Acil durumda operatör yeni girişleri durdurur ve emergency kilidini kalıcı tutar;
gelecekteki merkezi mutation kapısını kapatır, gerektiğinde worker'ı durdurur veya
OKX anahtarını iptal eder. Kapı kapalıyken borsa emirleri otomatik iptal edilmez ve
pozisyonlar otomatik kapanmaz; açık pozisyon/TP-SL OKX ekranından incelenir ve manuel
karar verilir. İletişim veya DB sorunu emergency'yi otomatik temizlememeli.
Yeniden açılış, hesap ve açık risk incelemesinden sonra ayrı operatör kararıdır.

## Gelecek dağıtım kabul sırası

Secret mount/log izolasyonu → izole restore → salt okunur Live GET doğrulaması →
yanlış ortam/hesap/IP ve ikinci instance ret testleri → Windows/restart/lease kaybı
provası → ayrı Live trading kod incelemesi ve açık aktivasyon. Gerçek emir, transfer,
kaldıraç veya hesap ayarı testi bu planın mevcut aşamasında yetkilendirilmemiştir.
