# Gelecekte Ubuntu VPS'e taşıma

Bu bir hazırlık belgesidir; sunucu kiralanmadı, sunucuya bağlanılmadı ve deployment
yapılmadı. Gerçek Demo emir/fill/TP-SL kabul testi ayrı onayla yapılmalıdır.

## Sunucu ve Docker

Planlama önerisi: tek işçi için 2 vCPU, 4 GB RAM, en az 30 GB SSD, sabit IPv4,
NTP ve ayrı şifreli yedek alanı. Kaynak ihtiyacı log/DB büyümesine göre izlenmelidir;
bu kapasite için yük testi iddiası yoktur. Desteklenen Ubuntu LTS seçin.

Yeni sunucuda Docker'ın [resmî Ubuntu kurulum adımlarını](https://docs.docker.com/engine/install/ubuntu/)
izleyip APT deposu ve GPG anahtarını kurun. Ardından:

```bash
sudo apt update
sudo apt install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

Docker grubuna erişim sunucu üzerinde geniş yetki sağlar; yalnız güvenilen bakım
kullanıcısı kullanın. SSH anahtarı, sınırlı SSH kaynak IP'si ve güncel sistem paketleri
uygulayın. PostgreSQL portunu dışarı açmayın. Yönetim HTTP'si mevcut Compose'ta
`127.0.0.1:8010` ile sınırlıdır; SSH tüneli tercih edin. Dış erişim gerekirse TLS ve
ek erişim kontrolü gerekir. Docker'ın yayınlanan portlarının UFW kurallarını
atlayabildiğini [resmî ağ belgesi](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
açıklar; yalnız UFW'ye dayanarak DB/admin portunu yayınlamayın.

## Environment ve kalıcı veriler

Kaynakları `/opt/okx-bot` gibi bakım kullanıcısına ait bir dizine taşıyın. `.env`
dosyasını loga, shell geçmişine veya imaja koymayın; güvenli kanaldan aktarın,
`chmod 600 .env` uygulayın. Sadece Demo Read+Trade anahtarı kullanın, para çekme
yetkisi vermeyin. OKX API anahtarını VPS'in sabit çıkış IP'siyle sınırlandırın;
IP değişirse bağlantıyı tekrar salt okunur doğrulayın. Bölgesel `OKX_BASE_URL`
hesapla uyumlu olmalı. `OKX_DEMO=1`, `LIVE_TRADING_ENABLED=false` korunur.

Compose proje adı `telegram-okx-demo` sabit kalmalı. Kalıcı alanlar:

| Veri | Konum |
| --- | --- |
| PostgreSQL | `telegram-okx-demo_postgres-data` volume |
| Telethon StringSession dosyası | `telegram-okx-demo_telegram-session`, `/app/data/telegram.session-string` |
| İsteğe bağlı environment oturumu | `.env` içindeki gizli değer |
| Kaynak/migration/bağımlılıklar | proje kopyası ve `uv.lock` |
| Yedekler | hostta ayrı şifreli alan + sunucu dışı kopya |

Volume'ler konteyner yaşam döngüsünden bağımsızdır; taşınma/geri yükleme mantığı
[Docker volume belgesinde](https://docs.docker.com/engine/storage/volumes/).
Çalışan PostgreSQL veri dizinini doğrudan tar'lamak yerine tutarlı `pg_dump` kullanın.

## Geçiş sırası

1. Kaynak sistemde kullanıcı `/pause` uygular ve açık/belirsiz işlemleri inceler.
   Eski işçi durdurulur. Borsa korumaları kapanışa kadar takip edilir. Aynı hesap
   için iki sunucuda eşzamanlı işçi açılmaz; farklı DB'lerde advisory lock bunu önleyemez.
2. PostgreSQL dump, Telegram oturumu, `.env`, kaynaklar ve önceki imaj etiketi
   güvenli aktarılır. StringSession bir hesap erişim anahtarıdır; terminalde göstermeyin.
3. Hedefte imajı derleyip yalnız `postgres` servisini başlatın. DB dump'ını önce
   izole DB'de sınayın, sonra yeni boş hedef DB'ye yükleyin. Var olan hedef DB'yi silmeyin.
4. Session dosyası için bir geçici Compose konteyneri oluşturup dosyayı `/app/data`
   volume'üne aktarın. Örnek (dosya zaten güvenli host dizininde):

   ```bash
   docker compose build trading-bot
   docker compose up -d postgres
   docker compose run --name okx-session-import --no-deps -d --entrypoint sleep trading-bot 300
   docker cp ./secure/telegram.session-string okx-session-import:/app/data/telegram.session-string
   docker exec -u root okx-session-import chown 10001:10001 /app/data/telegram.session-string
   docker exec -u root okx-session-import chmod 600 /app/data/telegram.session-string
   docker rm -f okx-session-import
   ```

5. Yeni işçiyi başlatmadan `scripts/acceptance_check.py` ile salt okunur bağlantıyı
   doğrulayın. Worker alanının henüz unavailable olması beklenir. Sonra tek işçiyi
   başlatın; otomatik migration ve başlangıç reconciliation sonucunu inceleyin.
   Restore edilmiş pause/emergency kilitleri korunur. Devam kararı yalnız yönetici
   tarafından iki aşamalı `/resume` ile verilir.

## PostgreSQL backup ve restore provası

Linux shell örneği; yeni ve benzersiz yedek adı kullanın:

```bash
umask 077
mkdir -p backups
docker compose exec -T postgres pg_dump -U okx -d okx -Fc > backups/okx-YYYYMMDD-HHMM.dump
docker compose exec -T postgres pg_restore --list < backups/okx-YYYYMMDD-HHMM.dump > backups/manifest.txt
```

Dump işlem geçmişini ve kilitleri içerir, şifreleyip sunucu dışında saklayın.
Günlük yedek, güncelleme öncesi ek yedek ve düzenli restore provası planlayın.
Session dosyası ve `.env` ayrıca yedeklenir; DB dump bunları içermez.

Restore provası yalnız **önceden var olmadığı doğrulanmış** `okx_restore_check`
isimli ayrı DB'ye yapılır. Bu DB'de Telegram/OKX işçisi başlatmayın:

```bash
docker compose exec -T postgres createdb -U okx okx_restore_check
docker compose exec -T postgres pg_restore -U okx -d okx_restore_check --exit-on-error --no-owner < backups/okx-YYYYMMDD-HHMM.dump
docker compose exec -T postgres psql -U okx -d okx_restore_check -c 'SELECT version_num FROM alembic_version;'
docker compose exec -T postgres psql -U okx -d okx_restore_check -c 'SELECT paused, emergency FROM runtime_state WHERE id=1;'
```

Gerçek kurtarmada önce işçi durdurulur, mevcut DB ayrıca yedeklenir. Ayrı kurtarma
DB'sinin pozisyon/emir/algo kimlikleri güncel OKX durumuyla uzlaştırılır. Yalnız
bundan sonra hedef bağlantı kontrollü değiştirilir. Geçmiş DB'nin mevcut işlemleri
bilmediği bir anda otomasyonu açmayın; belirsizlik emergency ve manuel inceleme gerektirir.

## 7/24 izleme ve bakım

```bash
docker compose ps
curl --fail http://127.0.0.1:8010/health/ready
docker compose logs --tail 100 trading-bot
docker compose run --rm --no-deps -T --entrypoint python trading-bot scripts/acceptance_check.py
```

Compose `unless-stopped`, 45 saniye graceful shutdown, healthcheck ve dönen JSON
logları içerir. Docker unhealthy durumunda tek başına konteyneri yeniden başlatmaz;
dış izleme ve operatör uyarısı kurun. PostgreSQL advisory lock tek DB üzerinde ikinci
işçiyi engeller; tek Uvicorn worker ve tek replika kullanın. Ağ dönüşünde Telethon
yeniden bağlanır; borsa/DB hataları işlem kilidine dönüşebilir, kullanıcı onayı olmadan
emergency kaldırılmaz. DB kilidi kaybolursa süreç yeni işlem yapmayı durdurur.

Disk/RAM, yedek yaşı, readiness, son reconciliation ve Telegram topic doğrulamasını
izleyin. Loglara erişimi sınırlayın; gizli environment veya ham sağlayıcı cevaplarını
destek mesajlarına kopyalamayın. Güncelleme/geri dönüş [yerel kullanım rehberindeki](user-guide.md)
sırayla uygulanır. Şema downgrade ve eski DB yedeğine dönüş normal imaj rollback'inin
parçası değildir. Bu belge gerçek VPS üzerinde henüz uygulanmadı.
