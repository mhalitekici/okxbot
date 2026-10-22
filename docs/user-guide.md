# Demo MVP kullanım ve güncelleme kılavuzu

Bu dosya önceki MVP akışının tarihsel kılavuzudur. Güncel sabit slot stratejisi,
sahiplik koşulları, onaylar ve dağıtım için [sermaye dağılımı](capital-allocation.md),
[Telegram komutları](telegram-admin-commands.md) ve [Docker kılavuzu](docker-deployment.md)
esas alınmalıdır. Uygulama artık her restart sonrasında `/resume` onayı ister.

Bu sürüm OKX Demo içindir. Gerçek borsada emir/fill/OCO yaşam döngüsü henüz
çalıştırılmadı. Güncel kanıtlar [kabul raporunda](final-acceptance-report.md).
Mevcut `.env`, Telegram oturumu ve pause/emergency durumu geliştirme sırasında korunur.

## Günlük kullanım

@Okxrizikbot ile özel sohbetten `/status` gönderin. `/start` komutları gösterir;
`/balance`, `/positions`, `/trades`, `/stats` tanı için kullanılabilir.
Yalnız yapılandırılmış sayısal yönetici kabul edilir.
`/pause` yeni işlemleri durdurur; mevcut pozisyonların eşitlemesi devam eder.
`/close BTC-USDT-SWAP` ilgili pozisyonu kapatır ve borsada işlem yapabilir.
Emergency, koruma/hesap/eşitleme hatalarında otomatik kilitlenir; `/pause` mevcut
pozisyonları kapatmaz. Acil müdahalede pozisyonları ayrıca kontrol edin.

İşlemleri etkinleştirmek ayrı bir kullanıcı kararıdır. `/resume` önce güncel hesap,
pozisyon ve koruma durumunu tekrar denetleyen önizleme döndürür. Önizlemedeki
`/resume CONFIRM <kod>` aynı yönetici tarafından süresi dolmadan gönderilmelidir.
Kod tek kullanımlıktır; durum değişirse veya uygulama yeniden başlarsa yeni önizleme
gerekir. Başarılı yeniden bağlantı emergency kilidini kendiliğinden açmaz.

Bu teslimatta çalışan botun kilitleri açılmadı. Test amacıyla eski mesajı tekrar
göndermek veya geçmiş sinyal zamanını yenilemek uygun bir doğrulama yöntemi değildir.

## Kabul edilen kaynak ve sinyal

Kaynak grup `-1003769687656`, topic `23576` — **AL SAT ÖNCÜ SİNYAL ODASI**.
Başlangıçta Telegram metadata doğrulaması zorunludur. Yanlış/eksik topic, General
belirsizliği ve service mesajları sinyal/bildirim/emir üretmez. Topic ID metinden
veya bağlantıdan tahmin edilmez. `TELEGRAM_TOPIC_ID=0` yeni sinyalleri engeller.

İzinli MZ kanalı için otomasyon politikası
`mztradingbot_verified_topic_patterns_v2` olarak sinyal kaydına yazılır:

| Başlık | Sonuç |
| --- | --- |
| `CTUSDT destekten dönüş için pozitif durumda, kontrol etmenizi öneririm.` | LONG adayı |
| `BATUSDT dirençten dönüş için pozitif durumda, kontrol etmenizi öneririm.` | SHORT adayı |
| Ayrı satırda açık `LONG` veya `SHORT` | Açık yön |
| Olumsuzlama, belirsizlik, çelişen başlık/yön veya birden fazla hedef | `needs_review` |

MZ formatında `Anlık Fiyat` ve `Ortalama Aralık` gerekir. `CTUSDT`, `CT/USDT`,
`CT-USDT` aynı sözleşmeye normalleştirilir. URL'ler ve açıkça etiketli hukuki uyarı
satırları analizden çıkarılır; ham mesaj denetim kaydında korunur. Bilinmeyen işlem
ifadeleri için otomasyon onayı verilmez. Genel format README'de açıklanmıştır.

Emir girişimi olmayan inceleme/reddedilmiş/manuel sinyal düzenlenince yeniden
doğrulanabilir; orijinal mesaj zamanı korunur. Emir girişiminden sonra aynı mesaj
ikinci emir oluşturamaz. Açık işlemin mesajı değişirse değişiklik özeti bir kez
uyarılır; mevcut pozisyon ve koruma otomatik değiştirilmez.

## Fiyat, risk ve koruma politikası

Önce hesaba uygun Demo SWAP listesi okunur. Listede olmayan coin `manual_required`
olur: Türkçe bildirim referansı, hesaplanan TP/SL'yi ve zamanı gösterir. Bu seviyeler
manuel değerlendirme içindir; bakiye/likidasyon/risk onayı verilmiş sayılmaz. Canlı
piyasada listelendiği iddia edilmez. Durdurulmuş sözleşme, geçersiz sembol ve API
hatası farklı durumlar olarak işlenir. Pause/emergency işlemleri önceliklidir;
kilitli sinyaller için işlem ön kontrolü yürütülmez.

Yaş sınırı 60 saniye, fiyat sapması en fazla %0,5, spread varsayılan en fazla %0,5.
Bid, ask ve mark ayrı okunur. LONG için ask, SHORT için bid esas alınır. Giriş
referansa göre sınırlandırılmış IOC limit emridir: LONG üst fiyatı, SHORT alt fiyatı
tick adımına güvenli yönde yuvarlanır. Uygun fiyat bulunamazsa fırsat kovalanmaz.
`MAX_BID_ASK_SPREAD_PERCENT` yeni ve isteğe bağlıdır; `.env` içinde yoksa %0,5 kullanılır.

10x isolated, %1 hesap riski, en fazla 3 pozisyon ve %3 günlük zarar kilidi korunur.
Lot/contract değeri, komisyon, kayma rezervi, stop mesafesi ve tahmini likidasyon
önceden denetlenir. Gerçek pozisyonun likidasyonu da sonradan doğrulanır.

İlk emir referans seviyelerine bağlı koruma içerir. Tam gerçekleşme doğrulanınca
gerçek ortalama fill fiyatı üzerinden `fiyat × aralık% × 0.6` mesafesiyle TP/SL
yenilenir. Açık mutlak TP/SL verilen genel sinyallerde mutlak fiyatlar korunur ve
gerçek girişe göre yeniden doğrulanır. Risk bütçesi aşılamaz. Mevcut OCO,
`cxlOnFail=false` ile değiştirilir; eski koruma önce iptal edilmez. Başarı ACK'si
yeterli değildir: GET yanıtında kimlik, yön, miktar, isolated, reduce-only ve mark
tetikleme fiyatları doğrulanmadan **işlem açıldı** bildirimi oluşmaz.

Kısmi gerçekleşme büyütülmez; mevcut kontrollü kapatma ve emergency politikası
uygulanır. Sıfır fill veya reddedilmiş emir açık pozisyon sayılmaz. Koruma hatasında
kontrollü kapatma denenir; teyit edilemiyorsa manuel müdahale uyarısı ve kilit kalır.
OCO'nun TP/SL çıkışı ve kalan koruma periyodik eşitlemede izlenir.

Bildirimler kalıcı olaylardan üretilir; teslim edilmiş olaylar tekrar gönderilmez.
Telegram gönderimi başarılı olup teslim kaydı DB'ye yazılmadan süreç kesilirse bir
bildirim tekrarı mümkün olabilir; Telegram `sendMessage` için uçtan uca tek teslim
garantisi yoktur. Bu durum emir tekilliğini etkilemez.

## Emir göndermeden kontroller

```powershell
docker compose config --quiet
docker compose ps
docker compose run --rm --no-deps -T --entrypoint python trading-bot scripts/acceptance_check.py
```

Son komut ayrı süreçte sadece GET API ve salt okunur SQL kullanır. Servis, migration,
ingest, reconciliation veya bildirim göndericisi başlatmaz. Ham mesajı/anahtarı
yazdırmaz. `/status` yanıtını yerel olarak üretir; Telegram'a teslim etmez. Gerçek
Telegram yanıt teslimini sınamak için yönetici kendisi `/status` gönderebilir.

Yerel otomatik doğrulama:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts migrations
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts migrations
.\.venv\Scripts\python.exe scripts/check_postgres.py --docker-restart-image telegram-okx-demo-trading-bot:latest
```

Son test rastgele adlı kendi PostgreSQL'ini ve sahte borsa işçisini oluşturur;
gerçek `docker restart` yalnız bu geçici işçiye uygulanır. Üretimdeki Compose servisi
ve kalıcı volume'ler kullanılmaz. Anahtar veya gerçek emir gerekmez.

## Kullanıcının uygulayacağı güvenli güncelleme

Aşağıdaki durdurma/yeniden oluşturma komutları bu geliştirmede çalıştırılmadı.
Önce `/status` ve `/positions` ile durumu inceleyin. Açık/belirsiz işlem varsa
güncellemeyi erteleyin; kapanışı ve korumayı önce doğrulayın. Pause varsa koruyun.

1. Çalışan imajı geri dönüş etiketiyle saklayın; mevcut kaynakların da kopyasını alın:

   ```powershell
   $botContainer = docker compose ps -q trading-bot
   $oldImage = docker inspect --format '{{.Image}}' $botContainer
   docker image tag $oldImage telegram-okx-demo-trading-bot:before-mvp
   docker compose build trading-bot
   docker compose config --quiet
   ```

2. Başarılı derleme sonrası bakım penceresinde yalnız uygulamayı durdurun. Veri
   yedeğini DB konteynerinin içinde üretip `docker cp` ile aktarın; PowerShell'de
   ikili dump'ı `>` ile yönlendirmeyin:

   ```powershell
   docker compose stop trading-bot
   New-Item -ItemType Directory -Force backups | Out-Null
   docker compose exec -T postgres pg_dump -U okx -d okx -Fc -f /tmp/okx-before-mvp.dump
   $pgContainer = docker compose ps -q postgres
   docker cp "${pgContainer}:/tmp/okx-before-mvp.dump" .\backups\okx-before-mvp.dump
   docker cp "${botContainer}:/app/data/telegram.session-string" .\backups\telegram.session-string
   docker compose exec -T postgres pg_restore --list /tmp/okx-before-mvp.dump
   ```

   Yedek adları her bakımda benzersiz olmalı. Oturum dosyası yerine environment
   oturumu kullanılıyorsa mevcut `.env` dosyasını güvenli ve şifreli yedekleyin.
   Yedekler Telegram erişimi ve işlem geçmişi içerir; Windows erişimini yalnız
   hesabınızla sınırlandırın, paylaşmayın. `pg_restore --list` okunabilirlik kontrolüdür;
   geri yükleme provası ayrıca izole veritabanında yapılmalıdır.

3. Uygulamayı yeni imajla oluşturun; başlangıç `0003_execution_context` migration'ını
   uygular. Sadece nullable JSON sütunu eklenir, mevcut kayıt/kilitler silinmez:

   ```powershell
   docker compose up -d --no-deps trading-bot
   docker compose ps
   docker compose logs --tail 100 trading-bot
   docker compose run --rm --no-deps -T --entrypoint python trading-bot scripts/acceptance_check.py
   ```

   Healthy, migration `0003`, topic doğrulaması, hesap modu ve önceki kilitleri
   kontrol edin. Yeni imaj otomatik `/resume` vermez. Güncelleme öncesinde bot
   zaten çalışır durumdaysa mevcut otomasyon durumu devam edebilir; planlı bakım
   için kullanıcı önce `/pause` uygulamalıdır. Bu belgede verilen komutlar onay
   yerine geçmez; gerçek/demo emir testi ayrı açık onay gerektirir.

## Geri dönüş ve veri kurtarma

Yeni sürüm sorunluysa önce uygulamayı durdurun. Yeni giriş/pozisyon varsa eski
imaja dönmeden durumun elle incelenmesi gerekir. İmaj geri dönüşü veritabanını
geçmiş zamana döndürmez; mevcut borsa durumunu eski DB yedeğiyle karıştırmayın.

```powershell
docker compose stop trading-bot
docker image tag telegram-okx-demo-trading-bot:before-mvp telegram-okx-demo-trading-bot:latest
docker compose up -d --no-deps --no-build trading-bot
docker compose ps
```

`0003` eklemeli olduğundan önceki kod nullable ek sütunu görmezden gelir; normal
imaj geri dönüşünde migration downgrade veya DB restore gerekmez. Bu sürümün
fill bağlamını kullanan işlemleri varsa önce koruma/fiyat uyumunu inceleyin.
Eski kaynakları yeniden build etmek için kaynak yedeğini ayrıca geri yüklemek gerekir.

DB kurtarmada önce uygulama durur. Dump'ı **yeni ve ayrı** bir veritabanına
`pg_restore --exit-on-error --no-owner` ile açın; migration, kilit, sinyal tekilliği,
pozisyon ve OCO kimliklerini kontrol edin. Ardından OKX'in güncel durumu ile
eşleştirmeden bu DB'yi işlem için kullanmayın. Güncel borsa pozisyonlarını eski
yedeğe göre otomatik tekrar açmayın. Ayrıntılar [VPS rehberinde](vps-deployment.md).

Kalıcı volume silme, veritabanı kayıtlarını temizleme veya emergency'yi SQL ile
sıfırlama bu prosedürün parçası değildir.
