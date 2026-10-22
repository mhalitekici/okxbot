# Live yapılandırma izolasyonu düzeltmesi

22 Ekim 2026. Bu çalışma yalnız kaynak kodu ve mock cevaplarla doğrulandı;
kullanıcının gerçek Live secret dosyası açılmadı, okunmadı veya değiştirilmedi.

## Bulgular ve düzeltme

Mevcut `load_live_config` zaten yalnız belirtilen dosyayı açıyordu. Proje kökündeki
Demo `.env` veya süreç environment'ından değer devraldığı doğrulanmadı. Yeni testte
Demo `.env`, Demo süreç değişkenleri ve süreçte `LIVE_TRADING_ENABLED=true` birlikte
bulunsa bile seçilen dosyadaki Live ayarlarıyla kontrolün geçtiği gösterildi.
Dosya erişimi testte sınırlandırıldı: yalnız açıkça seçilen mock Live dosyası bir
kez açıldı, başka bir yapılandırma dosyası okunmadı veya değiştirilmedi.

Gerçek dosya incelenmediği için kullanıcıdaki hatanın tam tetikleyicisi belirlenmiş
sayılmaz. Kaynak kodunda iki ek sorun mock testlerle yeniden üretildi ve düzeltildi:

- `scripts/check_live_connection.py` modülü kurulu paket/PYTHONPATH sırasına göre
  seçebiliyordu. CLI artık bu checkout'un `src` dizinini önceliklendiriyor. Başka
  kurulumdaki sahte eski modülle yapılan subprocess testi düzeltmeden önce yanlış
  modüle gidiyordu; düzeltmeden sonra yerel CLI'ye ulaşıyor.
- Tırnak içinde baş/son boşluk içeren `0` ve `false` işaretleri reddediliyordu.
  Yalnız gizli olmayan `OKX_DEMO`, `OKX_ENVIRONMENT`, `LIVE_TRADING_ENABLED`
  işaretleri normalize ediliyor. Credential değerlerine dokunulmuyor.

`ENVIRONMENT` tek bir genel mesaj yerine seçilen dosyadaki sabit nedeni de
açıklıyor: Demo modu işareti, ortam etiketi, dolu Demo credential alanları veya
eksik Live ortam beyanı. Yalnız alan isimleri gösteriliyor; değerler, dosya yolu,
imza ve kullanıcı kimlikleri gösterilmiyor.

Credential seçimi ayrı bir yardımcıya ayrıldı. `OKX_LIVE_API_*` ve `OKX_API_*`
grupları kendi içinde tam olmalı; kısmi gruplar birleştirilmiyor. İki tam grup
birlikte verilirse değerleri aynı olmalı. Eksiklik ve çakışma `CREDENTIALS` ile
güvenli ve ayrıntılı reddediliyor. Süreç environment'ı eksikleri tamamlamıyor.

## Korunan sınırlar

`OKX_DEMO=0` ve `LIVE_TRADING_ENABLED=false`, **yalnız bu bağımsız salt okunur
araçta** uyumludur. Gerçek emir kapısı açılmaz. Demo alanları veya çelişkili ortam
işaretleri bulunan açıkça seçilmiş dosya yine reddedilir; dış `.env`/environment
değerlerini yok saymak, dosyanın kendi çelişkilerini yok saymak değildir.

GET method/path/parametre allowlist'i, HTTP sınırındaki ikinci kontrol, Demo
header'ını reddetme, TLS, proxy/redirect sınırları ve `50101` ortam hatasında
durma değişmedi. Trading engine, Demo `Settings`/istemci, üç slot yönetimi,
Docker/Compose, PostgreSQL ve migration kodları değiştirilmedi. Hiçbir servis
başlatılmadı. Gerçek Demo veya Live API çağrısı/emir/transfer yapılmadı.

## Son doğrulama

| Kontrol | Sonuç |
| --- | --- |
| Live mock testleri | **139 passed**, 3.92 saniye |
| Tüm çevrimdışı regresyon | **506 passed, 7 deselected**, 96.22 saniye |
| Ruff lint | All checks passed |
| Ruff format | 64 files already formatted |
| compileall | Exit 0 |
| Offline dependency lock | Exit 0, 45 paket |
| Yerel CLI `--help` | Exit 0, credential dosyası açılmaz |

14 yeni senaryo eklendi. Önceki 125 Live testi de geçti. Yeni senaryoların ilk
hedefli çalıştırmasında modül seçimi ve boşluklu işaretlerden kaynaklanan üç
başarısızlık yeniden üretildi; düzeltmeden sonra hepsi geçti. Nihai lint öncesi
iki uzun açıklama satırı yalnız biçim açısından düzeltildi. PostgreSQL işaretli
yedi test, bu isteğin çevrimdışı/mock kapsamı nedeniyle çalıştırılmadı.

Çalıştırılan son doğrulama komutları:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_live_connection.py -q --tb=short --show-capture=no --junitxml=.artifacts/live-env-isolation-unit-tests.xml
.venv\Scripts\python.exe -m pytest -q --tb=short --show-capture=no --junitxml=.artifacts/live-env-isolation-regression-tests.xml
.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
.venv\Scripts\python.exe -m compileall -q src scripts
uv lock --check --offline
.venv\Scripts\python.exe scripts/check_live_connection.py --help
```

Kanıtlar: `.artifacts/live-env-isolation-unit-tests.xml`,
`.artifacts/live-env-isolation-regression-tests.xml` ve
`.artifacts/live-env-isolation-verification.json`.

## Kullanıcının çalıştıracağı komut

Proje ortamı etkinleştirildiğinde:

```powershell
python scripts/check_live_connection.py --env-file "$env:LOCALAPPDATA\OKXTrading\secrets\live.env"
```

Doğrudan bu projenin Python ortamını seçmek için:

```powershell
.\.venv\Scripts\python.exe scripts/check_live_connection.py --env-file "$env:LOCALAPPDATA\OKXTrading\secrets\live.env"
```

Bu iki gerçek bağlantı komutu geliştirme sırasında çalıştırılmadı. Uyumsuzluk
sürerse araç artık hangi güvenli yapılandırma koşulunun reddedildiğini açıklar;
`LIVE_TRADING_ENABLED` açılmamalı, Demo `.env` dosyası değiştirilmemelidir.
