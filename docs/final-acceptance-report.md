# Son kabul raporu — 21 Ekim 2026

Kod mevcut repository'ye uygulandı. Sabit sermaye slotları, emir güvenliği, Telegram yönetimi, bot muhasebesi ve kesinti davranışı mock/SQLite ve ayrı PostgreSQL ortamında doğrulandı. **Live kapalıdır. Gerçek Demo emir yaşam döngüsü ve Docker restart provası henüz kabul edilmedi.**

Mevcut .env, API anahtarları, PostgreSQL verileri/volume'ları ve Telegram session değiştirilmedi. Üretim container'ı başlatılmadı, durdurulmadı veya güncellenmedi. Gerçek veya Demo borsa emri gönderilmedi. Mevcut veritabanına migration uygulanmadı.

## 1. Değişen dosyalar

Git metadata bulunmadığından karşılaştırma başlangıç kaynak arşiviyle yapıldı: 13 yeni, 29 güncellenen dosya. Tam liste: .artifacts/change-manifest.json. Silinen kaynak dosyası yok.

Yeni dosyalar:

- src/okx_bot/allocation.py: kalıcı döngü ve atomik slot rezervasyonu.
- src/okx_bot/limits.py: güvenli sınırlarla doğrulanan kalıcı limit modeli.
- src/okx_bot/accounting.py: emir/fill sahipliği, fee ve funding muhasebesi.
- src/okx_bot/reporting.py: İstanbul günü PnL ve günlük rapor kuyruğu.
- src/okx_bot/management.py: iki aşamalı ayar onayı, bot pozisyonları ve geçmiş.
- migrations/versions/0004_capital_accounting.py.
- scripts/check_local_postgres.py: mevcut hizmetlerden bağımsız PostgreSQL testi.
- tests/test_production_readiness.py.
- docs/capital-allocation.md, docs/risk-management.md, docs/telegram-admin-commands.md, docs/live-trading-readiness.md, docs/docker-deployment.md.

Güncellenen dosyalar:

- .env.example, Dockerfile, README.md, pyproject.toml, uv.lock.
- src/okx_bot/app.py, config.py, models.py, engine.py, risk.py.
- src/okx_bot/exchange/client.py, gateway.py.
- src/okx_bot/telegram/admin.py, formatting.py.
- scripts/check_postgres.py.
- tests/conftest.py, fakes.py, docker_restart_harness.py, test_engine.py, test_gateway_diagnostics.py, test_mz_risk.py, test_notifications.py, test_postgres.py, test_reconciliation.py, test_risk.py, test_topics.py, test_wire_flow.py.
- docs/user-guide.md ve bu kabul raporu.

docker-compose.yml project/volume isimleri ve 0001–0003 migration'ları korundu. Yeni tzdata bağımlılığı Windows üzerinde Europe/Istanbul dönüşümünü sağlar. Gerçek .env dosyasına yeni değer yazılmadı.

## 2. Bakiye nasıl üçe bölünüyor?

Doğrulanmış, pozisyonsuz hesapta allocation_base_equity = USDT Futures equity; slot_target_margin = allocation_base_equity / 3. PostgreSQL bu değeri kalıcı tutar. Serbest teminat availBal ayrıca kontrol edilir; yeniden bölme tabanı değildir.

Kontrat miktarı hedef teminat ×10 nominalden, ctVal, muhafazakâr giriş fiyatı ve lotSz ile aşağı yuvarlanır. Gerekli margin, fee, bakım tamponu ve minimum serbest rezerv kontrol edilir. Yetersiz bakiye durumunda slot küçültülmez veya risk limiti aşılmaz; sinyal reddedilir.

## 3. 1500 USDT için üç işlem

| Durum | Yeni işlemin hedef teminatı |
|---|---|
| Döngü başlangıcı | 500 USDT |
| İlk işlemden sonra ikinci sinyal | 500 USDT |
| İkinci işlemden sonra üçüncü sinyal | 500 USDT |

Her işlem yaklaşık 5000 nominal hedefler. Gerçek teminat lot/fiyat nedeniyle değişebilir. Üçüncü işlem yeterli margin, rezerv ve tüm güvenlik koşulları varsa açılır. Maksimum üç slot ve ayrı maksimum pozisyon limiti uygulanır.

Rezervasyon ve emir niyeti tek transaction'dadır. PostgreSQL satır kilidi altında bakiye tekrar okunur; açık/bekleyen risk, aynı enstrüman, slot doluluğu ve limit revizyonu kontrol edilir. Aynı DB'de ikinci çalışan süreç advisory lock ile reddedilir.

## 4. Kapanış, slot ve yeni döngü

Kesin çıkış fill'leriyle tamamen kapanan işlem slotu bırakır. Diğer işlemler açıkken hedef değişmez; yeni sinyal boşalan slotu aynı bütçeyle kullanır. Belirsiz submission veya eksik çıkış kanıtı slotu tutar.

Tüm işlemler ve bekleyen emirler bittiğinde, hesap doğrulanırsa auto_rebalance=true yeni döngü oluşturur. 1650 →550; 1200 →400 USDT/slot. /setautorebalance false otomatik yenilemeyi kapatır. /rebalance ve /setslots pozisyonsuz hesap ve iki aşamalı onay ister. Restart döngüyü kaybetmez.

## 5. TP/SL formülü

Gerçek giriş F ve sinyal aralığı R için d=R/100×0.6. LONG: TP=F×(1+d), SL=F×(1−d); SHORT'ta işaretler tersidir. Enstrüman tick adımı uygulanır. Genel parser'ın açık mutlak TP/SL seviyeleri korunur.

%5,78 aralık →%3,468 mesafe; 5000 nominal için teorik brüt ±173,40 USDT. Ücret, funding ve kayma ayrıca ölçülür. Eski işlem başı %3–4 sınırı veya MAX_ACCOUNT_RISK_PERCENT yeni stratejiyi daraltmaz. Portföy sınırı aşılırsa işlem küçültülmeden reddedilir.

Gerçek fill sonrası koruma amend edilir ve borsadan okunarak doğrulanır. Tam fill ve aktif doğru koruma doğrulanmadan “işlem açıldı” bildirimi oluşturulmaz.

## 6. Risk yüzdeleri

Tahmini SL nakit riski fiyat mesafesi ve muhafazakâr giriş/çıkış fee rezervini içerir. Telegram ayrı ayrı gösterir:

- Tahmini SL zararı / gerçek kullanılan teminat.
- Tahmini SL zararı / hesap özkaynağı.
- Tüm açık bot işlemlerinin tahmini SL zararı / hesap özkaynağı.

Açılış bildirimi açılış snapshot'ını, /positions güncel özkaynağı kullanır. Varsayılan toplam risk limiti %40, günlük gerçekleşmiş zarar limiti %3, minimum serbest rezerv 10 USDT. İlk devam onayından önce /limits incelenmelidir. Tek kanal SL'si günlük limitten büyük olabilir; sonrasında yeni girişler engellenir.

## 7. İşlem sahipliği

Internal trade/signal ID, OKX entry/client/algo/child order ID, pozisyon ID, enstrüman, yön, miktar, fill, fee ve funding bağlantıları saklanır. Bot dışı fill'ler atanmamış kayıtlardır; bot istatistiklerine alınmaz.

DB Demo ortamına ve doğrulanmış hesap UID hash'ine bağlanır. Farklı hesap kimliği veya bilinmeyen inventory kilit oluşturur. Envanter sorguları diğer işlem türlerindeki yabancı pozisyon/emirleri de görebilmek için hesap genelini okur. Net modda miktar/pozisyon kimliği uyuşmazlığı ve yabancı emir/algolar otomatik kapatmayı engeller. Hesap geneli cancel ve close-position autoCxl yolu kaldırıldı; doğrulanmış bot miktarıyla reduce-only çıkış kullanılır.

Kesin izolasyon için ayrı trading sub-account gerekir. DEDICATED_TRADING_ACCOUNT_CONFIRMED=false varsayılandır; bu teyit olmadan otomatik flatten yapılmaz, kritik müdahale alarmı verilir. Teyit diğer kullanıcıların borsadan işlem yapmasını teknik olarak engellemez. Live kapısı ayrıca kapalıdır.

## 8. Telegram komutları

/status, /balance, /allocation, /setslots, /rebalance, /positions, /pnl, /daily, /history, /limits, /setdailycap, /setriskcap, /setreserve, /pause, /resume, /emergency, /confirm; eski /trades, /stats, /close korunmuştur.

Ek limit komutları: /setmaxpositions, /setspread, /setdeviation, /setslippage, /setliqbuffer, /setstopwarning, /setautorebalance.

Yalnızca izinli admin ID'nin özel sohbeti kabul edilir. Ayar değişiklikleri iki aşamalı, 120 saniyelik, kullanıcıya ve güvenlik revizyonuna bağlıdır. Restart onayı geçersiz kılar. Ayarlar ve önceki/yeni değer audit kaydı kalıcıdır. Kimlik bilgileri Telegram'dan yönetilmez. Tam kullanım: [komut kılavuzu](telegram-admin-commands.md).

## 9. Günlük PnL ve hesap bakiyesi

Brüt bot PnL OKX fillPnl'den; fee fill kayıtlarından; funding atfedilebilir bills kayıtlarından alınır. Net = brüt + işaretli fee + işaretli funding. Yatırma/çekme ve diğer hesap hareketleri bot getirisi sayılmaz. USDT dışı ücret dönüşümü veya kesin funding atfı yoksa net uydurulmaz.

Raporlar açılan/kapanan işlem, kazanan/kaybeden, win rate, ortalama kâr/zarar, net/başlangıç stop riskinden gerçekleşen R, brüt/fee/funding/net, ortalama margin ve döngü özkaynağını gösterir. Açılış günü gerçek entry fill zamanından hesaplanır. Eksik metrik Hesaplanamadı olur. Bot unrealized PnL ve hesap bakiyesi güncel snapshot olarak etiketlenir; geçmiş gün sonu bakiyesi diye sunulmaz.

Europe/Istanbul günü UTC sınırlarına çevrilir. Önceki gün raporu 00:05 sonrası tekil DB/outbox kaydıyla oluşturulur; restart eksik günleri tamamlar. Kısmi kapanışta tam kapanış bildirimi yoktur. TP/SL yalnızca algo-child order ve fill kanıtıyla sınıflandırılır; bilinmeyen neden tahmin edilmez.

## 10. Test sonuçları

Başlangıç sürümü 322 testle geçti. Eski risk boyutlandırma ve fill olmadan kapanış varsayımları yeni stratejiye göre güncellendi. Son tam paket sonucu: **367 geçti, 7 PostgreSQL testi varsayılan paketten ayrı tutuldu; başarısız 0.** PostgreSQL paketinde **7 geçti**. Son envanter sorgusu değişikliği ayrıca 10 gateway/wire testiyle doğrulandı.

| Kontrol | Sonuç |
|---|---|
| Mock/SQLite/Python otomatik paket | 367 geçti |
| Ayrı PostgreSQL 16.14 entegrasyon paketi | 7 geçti |
| upgrade head, alembic check | Geçti; schema drift yok |
| Ayrı DB'de downgrade/upgrade | Geçti |
| Eski emergency ve kayıtların korunması | Geçti |
| Aynı anda 4 rezervasyon, 3 tekil slot | Geçti |
| Ruff ve Python compile | Geçti |
| Gerçek Docker build/restart | Çalıştırılmadı; Docker motoru kapalı |
| Gerçek OKX Demo emir/fill/TP/SL | Çalıştırılmadı; açık emir testi onayı yok |
| Live emir | Gönderilmedi; etkinleştirme kapısı kapalı |

Kanıtlar: .artifacts/final-verification.xml, .artifacts/postgres-validation.log. Komutlar: python -m pytest -q; python -m ruff check src tests scripts migrations; python scripts/check_local_postgres.py --bin 'C:/Program Files/PostgreSQL/16/bin'. Bağımlılık kilidi çevrimdışı kontrol edildi; format ve compile kontrolleri geçti.

Kapsama: 1500/900/3000 dağılımı, sabit hedef, reuse/rebalance, yetersiz margin, lot rounding, LONG/SHORT, fill bazlı TP/SL, spread/sapma/likidasyon, portföy/günlük risk, kısmi fill, timeout, duplicate engeli; sahiplik/net çakışması; admin onayı/persistence; fee/funding/PnL; offline kapanış ve bildirim tekilliği; 401 başlangıcında yönetimin açık kalması; DB/Telegram reconnect mock'ları; Demo/live gate ve endpoint allowlist.

## 11. Gerçek Demo ile doğrulanmayanlar

Hesaba erişilebilir enstrümanların gerçek yanıtları, bölge host/credential eşleşmesi, gerçek bounded IOC kısmi fill, attached OCO oluşumu/alanları, fill sonrası amend, reduce-only çıkış ve gerçek liquidation/fee/funding davranışı henüz denenmedi.

Laptop kapalıyken koruyucu emirlerin OKX'te varlığı ve tetiklenmesi gerçek hesap üzerinde doğrulanmadı. Offline kapanış recovery'si mock ve PostgreSQL'de test edildi. Gerçek Telegram hizmetine yeni komut/mesaj gönderilmedi. Mock HTTP testleri borsa kabulü yerine geçmez.

## 12. Live için eksikler

Önce ayrı Demo hesapta açık izinli emir ve kesinti kabul provası, ayrılmış sub-account doğrulaması, ayrı laptop/container provası ve restore denemesi gerekir. Gelecek live sürümü ayrı credential deposu, ayrı DB/volumes, hesap sahipliği ve API izin doğrulaması, ayrıca açık iki aşamalı aktivasyon tasarımı gerektirir.

Bu sürüm live credential alanlarını ve live etkinleştirmeyi kabul etmez. .env değiştirerek live'a geçilemez. Ayrıntı: [live hazırlığı](live-trading-readiness.md).

## 13. OKX izinleri ve API ayarları

İleride kullanıcı tarafından oluşturulacak, yalnızca doğru ortam/sub-account'a ait Read + Trade anahtarı; Withdrawal yetkisi olmaması; mümkünse sabit egress IP allowlist'i. Futures mode acctLv=2, isolated, 10x ve desteklenen net/long-short modu kontrol edilmelidir. Bu görevde anahtar oluşturulmadı/değiştirilmedi ve gerçek anahtar istenmedi.

[OKX V5 resmi belgeleri](https://www.okx.com/docs-v5/en/) üzerinden sözleşmeler kontrol edildi. eq, availBal ve currency upl ayrı anlam taşır; isoUpl toplam UPL'ye ikinci kez eklenmez. İzinli global/bölgesel REST HTTPS hostları allowlist'tedir. Uygulama REST kullanır. Gelecek WebSocket için 443 kullanılmalıdır; [OKX duyurusu](https://www.okx.com/en-eu/help/okx-websocket-port-8443-discontinuation-announcement) internet üzerindeki 8443 desteğinin 31 Ekim 2026'da kaldırılacağını bildirir.

## 14. Docker dağıtımı için manuel adımlar

1. /pause, borsa koruması/sahiplik incelemesi, tercihen tamamen pozisyonsuz strateji geçişi.
2. DB custom dump, session volume ve kaynak/imaj yedeği; ayrı restore provası.
3. Mevcut .env korunarak Demo, kaynak grup -1003769687656/topic 23576, admin ve hesap ayrılığı kontrolü. Yeni limitlerin incelenmesi.
4. Kullanıcı tarafından onaylanan aşamada docker compose config -q, docker compose build trading-bot, docker compose up -d. Başlangıç 0004 migration'ını uygular.
5. /status, /allocation, /limits, health/ready ve gerçek Demo koruma testleri; ardından yalnızca Demo devamı için /resume ve süreli onay.

/health/live container canlılığını, /health/ready bağımlılıkları, yetkili /health/subsystems ayrıntıları ayırır. 401 yönetimi düşürmez; iyileşme yeni girişleri otomatik açmaz. Her process başlangıcında yeni girişler yönetici onayına kadar kapalıdır. Windows/Docker açılışı, uyku ayarları ve backup görevi operatör aşamasıdır. Tam komutlar: [Docker kılavuzu](docker-deployment.md).

## 15. Geri dönüş

Kaynak başlangıç arşivi .artifacts/source-before-slot-upgrade.zip; credential, session ve DB dump içermez. Yeni girişler kapalıyken önceki imaj/kaynak yüklenebilir. Üretim DB'sine otomatik downgrade veya eski dump overwrite yapılmaz; yeni muhasebe/slot geçmişi korunur.

Eski sürümün risk/kapatma davranışı farklı olduğundan eski /resume veya /close kullanılmadan önce inceleme gerekir. Downgrade testi yalnızca ayrı test DB'sinde yapıldı. Kurtarma dump'ı yedekten sonraki borsa işlemlerini kapsamaz; trading kapalıyken reconciliation gerekir.

## 16. Açık riskler ve kabul sınırları

- Ortak net hesap ve eşzamanlı manuel müdahale kesin kaynak izolasyonu sağlamaz. Ayrılmış hesap ve operatör disiplini gerekir. Aynı hesapta farklı DB/laptop çalıştırmak advisory lock ile engellenmez.
- IOC kısmi fill, koruma oluşumu/amend arasındaki zaman, piyasa boşluğu ve borsa gecikmesi gerçek Demo ile doğrulanmalıdır. Stop ve estimated liquidation kesin gerçekleşme garantisi değildir.
- Belirsiz submission, eksik fill veya sahiplik uyuşmazlığı slotu kilitli tutabilir; kör retry yoktur, manuel inceleme gerekir.
- Fill/bills geçmişi sınırlıdır; 89 günden eski eksik geçmiş güvenle yeniden üretilemez. Eski 0003 kapanışlarının eksik timestamp/exit/funding ilişkileri uydurulmaz; tam tarihsel performans için doğrulanmış backfill gerekebilir.
- Eski kayıtlarda gerçek fill veya kapanış zamanı yoksa işlem sayısı ve ilgili performans metrikleri sıfır yerine Hesaplanamadı gösterilir. Funding kaydındaki açık pozisyon ID çelişkisi, ayrılmış hesap teyidi olsa da atıf yapılmasını engeller.
- Fee/funding sonradan gelebilir. İki günlük settlement penceresinde yeniden sorgulanır; önceden gönderilmiş rapor snapshot'ı geriye dönük kesin nihai muhasebe garantisi değildir.
- Telegram Bot API exactly-once garantisi vermez. Kayıp ACK'de belirsiz teslim kaydı ve yönetici incelemesi kullanılır; bildirim eksik kalabilir. Eski sürümün ACK kaydı olmayan outbox kayıtları ayrıca incelenmelidir.
- PostgreSQL startup/lease kaybında servis yeniden başlamak zorunda kalabilir. Yeni süreç yönetici onayı ister. Koruyucu emirlerin borsada kalması gerçek Demo kabul şartıdır.
- Docker motoru kapalı olduğundan gerçek Compose/Windows restart, PostgreSQL 17 container, gerçek session taşıma ve gerçek dump restore kabulü yapılmadı. PostgreSQL kilit/migration testleri ayrı kurulu 16.14 üzerinde geçti.

Kod ve yerel doğrulama teslim edildi. Sonraki aşama kullanıcı tarafından yürütülecek/onaylanacak Demo kabul ve dağıtım adımlarıdır. Bu görev sonunda trading başlatılmadı.

Geçici kaynak temizliği: otomatik güvenlik denetimi yalnızca görev için oluşturulan, kapatılmış PostgreSQL test klasörlerini silme komutunu “blocked by policy” gerekçesiyle reddetti. Bu altı klasör ve logları .artifacts/postgres-test-* altında yerinde bırakıldı. Mevcut PostgreSQL verilerine veya volume'larına dokunulmadı.
