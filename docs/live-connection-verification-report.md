# Live salt okunur kontrol geliştirme raporu

Tarih: 22 Ekim 2026. Gerçek Live bağlantı sonucu **henüz ölçülmedi**; bu rapor
kod ve çevrimdışı test sonuçlarıdır. Gerçek bağlantıyı kullanıcı kendi terminalinde
çalıştıracaktır. Bakiye sıfır olduğu için para yatırmak gerekmez.

## Değişiklikler

| Dosya | Sonuç |
| --- | --- |
| `scripts/check_live_connection.py` | İstenen bağımsız CLI giriş noktası |
| `src/okx_bot/live_connection.py` | Ayrı Live config, GET allowlist, imza, sayfalama, güvenli özet/hatalar |
| `tests/test_live_connection.py` | 125 mock test; gerçek HTTP transport test içinde engellenir |
| `pyproject.toml`, `uv.lock` | Kullanılan dotenv parser için açık bağımlılık; offline lock güncellemesi |
| `.dockerignore` | İç içe env dosyaları ve secrets dizinleri build context dışında |
| `docs/live-read-only-connection.md` | Komut, kabul edilen alanlar, bölge seçimi ve hata rehberi |
| `docs/live-docker-deployment-plan.md` | Ayrı DB/config, mount, pause/gate, tek worker, taşıma, backup/restore ve acil durum planı |
| `README.md`, `docs/live-trading-readiness.md` | Yeni tanı aracına bağlantılar; Demo trading kilidinin kapsamı |

Mevcut `Settings`, Demo `OKXClient`/gateway, emir motoru, üç slotlu sermaye yönetimi,
FastAPI lifespan ve migration 0004 değiştirilmedi. `docker-compose.yml` ve
Dockerfile değiştirilmedi. Gerçek `.env`, veritabanı/veriler ve Telegram session
değiştirilmedi. `LIVE_TRADING_ENABLED=false` ve Demo servisinin Live hard gate'i
korunur. Live tanı aracı bu bileşenleri import etmez veya çalıştırmaz.

## Çalıştırılan doğrulama komutları ve sonuçlar

Proje `.venv` Python 3.12 kullanıldı. Aşağıdaki komutlar proje dizininde çalıştı:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_live_connection.py -q --tb=short --show-capture=no --junitxml=.artifacts/live-connection-unit-tests.xml
.venv\Scripts\python.exe -m pytest -q --tb=short --show-capture=no --junitxml=.artifacts/live-connection-regression-tests.xml
.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
.venv\Scripts\python.exe -m compileall -q src scripts
uv lock --check --offline
.venv\Scripts\python.exe scripts/check_live_connection.py --help
```

| Kontrol | Sonuç |
| --- | --- |
| Yeni Live unit testleri | **125 passed**, 3.24 saniye |
| Tüm çevrimdışı regresyon | **492 passed, 7 deselected**, 101.98 saniye |
| Ruff lint | All checks passed |
| Ruff format kontrolü | 64 files already formatted |
| compileall | Başarılı, exit 0 |
| Offline lock kontrolü | Başarılı, 45 paket |
| CLI `--help` | Başarılı, secret dosyası açmadan kullanım bilgisi |

Hazırlıkta `uv lock --offline` çalıştırıldı. Yeni üç Python dosyası
`python -m ruff format` ile biçimlendirildi; ilk lint turundaki uzun satırlar
düzeltildi. İlk 109 senaryolu test turu da geçti; ek sınır testleriyle son sayı 125
oldu. Son doğrulamadan sonra uygulama kodu değiştirilmedi.

Makine tarafından okunabilir kanıtlar:

- `../.artifacts/live-connection-unit-tests.xml`
- `../.artifacts/live-connection-regression-tests.xml`
- `../.artifacts/live-connection-verification.json`

## Test kapsamı

Başarılı Live kimlik doğrulaması ve tam GET imzası; sıfır özkaynak, boş currency
detayları ve boş pozisyon/emir listeleri; iki desteklenen pozisyon modu; yanlış
anahtar, 401, IP allowlist, Demo/Live, passphrase, imza, saat, bölge ve izin
hataları; Futures hesap modu ve beklenen pozisyon modu uyuşmazlığı doğrulandı.

POST/PUT/PATCH/DELETE dahil GET dışındaki methodlar, emir/iptal/transfer/kaldıraç/
hesap ayarı uçları, GET gövdesi, query enjeksiyonu, başka hosta yönlendirme ve Demo
header'ı HTTP transport'a ulaşmadan engellendi. Testler model doğrulamasını atlayan
kopyaları ve istemcinin doğrudan HTTP sınırını da kapsar.

Sipariş ve conditional/OCO listeleri birden fazla sayfada sayıldı; tekrar eden ID,
iki algo türünde yinelenen ID, tamamlanmayan TP/SL sorgusu ve 100 sayfa sınırında
eksik sonuç başarı sayılmadı. Bozuk JSON/Decimal/veri satırları, debug child logları
ve beklenmeyen yerel istisnalardan secret/UID/traceback sızıntısı engellendi.

Proje `.env` keşfi, process environment'ından credential devralma, değişken
genişletme, bilinmeyen/çelişkili alanlar ve header enjeksiyonu ayrıca test edildi.
Bağımsız süreçte uygulama, config, DB, Telegram, exchange gateway ve Alembic
import'ları engellenerek CLI yardımının yine çalıştığı doğrulandı. TLS doğrulaması,
proxy devralmama ve redirect reddi kontrol edildi.

## Yapılmayan işlemler ve gerçek kullanım

Kullanıcının `%LOCALAPPDATA%\OKXTrading\secrets\live.env` dosyası **açılmadı,
okunmadı, kopyalanmadı veya loglanmadı**. Gerçek OKX Live/Demo API çağrısı,
emir/iptal/transfer/ayar isteği yapılmadı. İnternetten yalnız kamuya açık resmî
teknik belgeler okundu. Container başlatma/recreate/build, PostgreSQL migration,
backup/restore veya Live deployment çalıştırılmadı. PostgreSQL işaretli yedi test
bu isteğin mock kapsamı nedeniyle çalıştırılmadı.

Kullanıcı proje ortamını etkinleştirdikten sonra çalıştırabilir:

```powershell
python scripts/check_live_connection.py --env-file "$env:LOCALAPPDATA\OKXTrading\secrets\live.env"
```

Gerçek dosyanın içeriği bilinmediğinden alanlarının uyumlu olduğu iddia edilmez.
Eksik/uyumsuz yapılandırma veya açıkça belirtilmemiş bölgesel adres durumunda
istek gönderilmeden `FAILED` ve sabit güvenli açıklama gösterilir. Desteklenen
şema [kullanım kılavuzunda](live-read-only-connection.md) yer alır. Futures modu
uyuşmazlığı da `FAILED` olur; araç hesabın modunu değiştirmez. Anahtarın
TelegramBot1'e ait olduğu OKX ekranından kullanıcı tarafından doğrulanmalıdır.
