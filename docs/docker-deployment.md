# Windows laptop ve Docker dağıtımı

Bu geliştirmede mevcut Docker servisleri, PostgreSQL volume'u ve Telegram session volume'u değiştirilmedi. Docker motoru kontrol sırasında kapalıydı; imaj build ve gerçek container restart doğrulaması yapılmış sayılmaz. Komutlar aşağıdaki manuel dağıtım aşaması içindir.

Mevcut Compose project adı `telegram-okx-demo`; `postgres-data` ve `telegram-session` volume adları korunur. Project adını veya Compose dizinini değiştirerek boş volume ile başlamayın. Tek Uvicorn worker çalışır; ikinci servis aynı DB'nin advisory lock'unu alamaz. PostgreSQL ve bot `restart: unless-stopped` kullanır. Docker Desktop'ın Windows oturum açılışında başlaması ayrıca ayarlanmalıdır; kapalı/uyuyan laptop üzerinde Docker çalışmaz. Uyku/hibernation ve disk doluluğu operatör tarafından yönetilir.

## Dağıtımdan önce

1. Telegram `/pause`; borsa üzerindeki kendi pozisyonları ve korumayı inceleyin. İlk strateji geçişini tamamen pozisyonsuzken yapın.
2. Mevcut imajı/image ID'sini ve kaynak sürümünü kaydedin. Kaynak geri dönüş arşivi `.artifacts/source-before-slot-upgrade.zip`; `.env` veya session içermez.
3. PostgreSQL custom-format dump ve Telegram session volume yedeği alın; yedekleri şifreli diskte tutun. Dump/session içeriğini sohbet veya loglara yazmayın.
4. Mevcut `.env` dosyasını koruyun. Kaynak grup `-1003769687656`, topic `23576`, Demo anahtar kaynağı ve admin ID'yi yerel olarak kontrol edin. `OKX_DEMO=1`, `LIVE_TRADING_ENABLED=false` kalmalıdır. Ayrılmış hesabı doğrulamadan `DEDICATED_TRADING_ACCOUNT_CONFIRMED` açmayın.

## Veritabanı yedeği

PowerShell'de binary dump'ı `>` ile yönlendirmek yerine önce container içinde dosya oluşturun:

```powershell
New-Item -ItemType Directory -Force .\backups | Out-Null
docker compose exec -T postgres pg_dump -U okx -d okx -Fc -f /tmp/okx-backup.dump
$dbContainer = docker compose ps -q postgres
docker cp "${dbContainer}:/tmp/okx-backup.dump" .\backups\okx-backup.dump
```

Her yedek için ayrı tarihli dosya adı seçin; önceki yedeği ezmeyin. Telegram session volume'unu ayrıca kapalı bir yedek ortamına arşivleyin. API/.env dosyası ve session kimlik bilgisi niteliğindedir; güvenli transfer gerekir. Düzenli backup Windows Task Scheduler ile bu komutların tarihli varyantı olarak kurulabilir. Görevi kurma ve retention silme işlemi bu geliştirmede yapılmadı.

## Onaylı dağıtım

```powershell
docker compose config -q
docker compose build trading-bot
docker compose up -d
docker compose logs --tail 100 trading-bot
```

`config -q` kullanın; normal `config` çözülmüş secret değerlerini gösterebilir. Başlangıç script'i Alembic `upgrade head` çalıştırır. Yeni `0004` migration'ı tablo/nullable alan ekler; eski kayıtları veya acil kilitleri silmez. Çalışan üretim veritabanına burada migration uygulanmadı.

`/health/live` uygulama/supervisor canlılığını kontrol eder ve Docker healthcheck bunu kullanır. `/health/ready` OKX, Telegram, DB ve reconciliation güncelliğini denetler; bağımlılık kesintisinde 503 döner. Bearer korumalı `/health/subsystems` ayrıntılı alt sistem durumunu sağlar. Docker healthcheck tek başına unhealthy container'ı restart etmez. API yalnızca `127.0.0.1:8010` üzerinde yayınlanır.

OKX 401 veya geçici API hatası başlangıcı düşürmez: yönetim açık kalır, reconciliation tekrar dener, yeni girişler kilitlenir. Düzelince kilit otomatik temizlenmez. Her process başlangıcında ayrıca `/resume` iki aşamalı onayı gerekir. DB lease kaybolursa servis çıkar; yeniden bağlantıyla advisory lock'u varmış gibi davranmaz. Docker yeniden başlatınca veritabanındaki slot, durum ve outbox korunur.

## Restore ve taşıma provası

Önce ayrı isimli DB/container/volume üzerinde `pg_restore --list` ve tam restore provası yapın. Yeni, boş prova DB'sine dump'ı `docker cp` ile aktarın, `pg_restore -U <test-user> -d <test-db> /tmp/okx-backup.dump` çalıştırın. Session yedeğini ayrı kalıcı volume'a geri koyun. Üretim DB'sini resetlemeyin; `docker compose down -v` kullanmayın. İki laptopun aynı anda aynı hesapta bot çalıştırmasına izin vermeyin: farklı DB'lerde advisory lock ortak değildir.

Gerçek veritabanı kaybında restore, yedekten sonraki borsa işlemlerini içermez. Yeni girişler kapalıyken emir/fill reconciliation ve sahiplik incelemesi gerekir. Güncel session'ı rastgele eski session ile değiştirmeyin.

## Geri dönüş

Yeni girişleri durdurun; belirsiz/açık işlemleri borsadan inceleyin. Önceki imaj veya kaynak arşiviyle rollback yapılabilir; ek kolonlar eski uygulama için yerinde bırakılabilir. Mevcut DB üzerinde otomatik `downgrade` veya eski dump ile overwrite yapmayın: yeni slot/muhasebe geçmişi kaybolur. Eski sürüm farklı boyutlandırma ve kapatma davranışı içerir; `/resume` ve `/close` kullanmadan önce inceleme gerekir. Schema downgrade yalnızca ayrı test DB'sinde doğrulandı.

Yerel doğrulama: `python -m pytest`, `python -m ruff check src tests scripts migrations`; Docker kapalıyken kurulu PostgreSQL için `python scripts/check_local_postgres.py --bin 'C:\Program Files\PostgreSQL\16\bin'`. Bu helper yalnızca `.artifacts/postgres-test-*` altında kendi yeni cluster'ını açıp kapatır; mevcut hizmet veya volume'a bağlanmaz.
