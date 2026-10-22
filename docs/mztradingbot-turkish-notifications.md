# MZTRADINGBOT parser ve Türkçe bildirimler

Bu belge ilk LONG parser teslimatının kaydıdır. Güncel sürüm SHORT direnç
başlığını da destekler, doğru topic zorunludur ve TP/SL gerçek fill üzerinden
yenilenir. Güncel davranış ve sonuçlar [kabul raporunda](final-acceptance-report.md)
ve [kullanım kılavuzunda](user-guide.md). Bu son MVP çalışmasında `.env` değiştirilmedi.

## Kanal ve yorumlama

`MZTRADINGBOT_CHANNEL_ID`, sadece MZTRADINGBOT olarak izin verilen sayısal kanal
kimliğidir. Mevcut `TELEGRAM_CHANNEL_ID` ile eşleşmek zorundadır. `0` özel parser'ı
kapatır. Bu geliştirmede `.env` dosyasına mevcut izinli kanalın kimliğiyle yalnız
bu yeni ayar eklendi; mevcut değerler, API bilgileri ve demo kilitleri korundu.
`.env.example` içinde varsayılan değer `0` olarak bırakıldı.

MZ biçimi algılandığında `MZTradingBotParser`, diğer açık sinyallerde mevcut
`RegexSignalParser` kullanılır. `CTUSDT`, `CT/USDT`, `CT-USDT` aynı `CTUSDT`
sembolünü üretir. `Anlık Fiyat` dolar işaretini, nokta/virgül ondalık ayracını;
`Ortalama Aralık` yüzde işaretinin iki tarafta yazılmasını destekler.

Tam olarak tanımlanan başlık, açık LONG/SHORT yoksa LONG adayıdır:

```text
CTUSDT destekten dönüş için pozitif durumda, kontrol etmenizi öneririm.

Anlık Fiyat: 0.3604000000$
Ortalama Aralık: %17.33
Hedef Fiyat: Ortalama aralığın 0.3 - 0.9 katı
Stop Fiyat: Ortalama aralığın 0.3 - 0.9 katı
```

`kontrol etmenizi öneririm` eki olmadan aynı başlık da kabul edilir. Başlıktaki
bilinmeyen veya olumsuz ifadeler yön tahminine yol açmaz. Açık LONG/SHORT varsa
yön o alandan alınır ve başlıkla uyumu zorunludur. Çelişkili yönler inceleme gerektirir. MZ formatında
fiyat ve aralık zorunludur; katsayı sabit 0.6'dır. Hedef/stop açıklamalarındaki
0.3–0.9 bandı işlem fiyatı olarak yorumlanmaz. Sayısal TP/SL eklenerek katsayı
değiştirilemez; bu durumda sinyal incelemeye ayrılır.

Hesaplar `Decimal` ile yapılır:

```text
Mesafe = 0.3604 × 17.33 / 100 × 0.6 = 0.037474392
LONG TP = 0.397874392; SL = 0.322925608
SHORT TP = 0.322925608; SL = 0.397874392
```

`parsed` JSON alanında `parser_name=mztradingbot_v1`, `level_coefficient=0.6` ve
çıkarım yapıldıysa `interpretation_rule=mztradingbot_support_positive_long_v1`
saklanır. Ham metin normalleştirilmeden `original_text` alanına kaydedilir.
Mevcut düzenleme akışı, yeni mesaj metnini de olduğu gibi saklar; bildirim için
Türkçeleştirme ham metni değiştirmez. Olaylarda ham mesaj metni bulunmaz.

## Risk ve demo güvenceleri

Parser sonucu emir onayı değildir. Çalışma anında mevcut OKX adaptörü
`GET /api/v5/account/instruments` ile sözleşmenin o demo hesabına açık olduğunu,
USDT uzlaşmalı, doğrusal, normal ve aktif bir SWAP olduğunu doğrular. CT'nin
gerçekte listeli olduğu bu geliştirmedeki sahte verilerden çıkarılamaz.

Kaldıraç 10x, marjin isolated olarak korunur. Risk yüzdesi, açık pozisyon limiti,
günlük zarar kilidi, fiyat sapması, mesaj yaşı, kontrat sınırları ve fiyat adımı
kontrolleri aynı sırada çalışır. Stop seviyesi tahmini likidasyondan sonra
kalıyorsa giriş emri hazırlanmaz. Gerçekleşmeden sonra gerçek pozisyon,
likidasyon ve OCO parametreleri yeniden doğrulanır.

Örnekteki %17.33 aralık, %10.398 stop mesafesi verir. Testlerde kullanılan
borsa ücret/marjin verileriyle bu stop hem LONG hem SHORT için likidasyon
kontrolünde reddedilir. Daha küçük, %5 aralık aynı risk kontrollerinden geçer.
Risk eşiği bu örneği işleme almak için gevşetilmedi.

`OKX_DEMO=1`, `LIVE_TRADING_ENABLED=false` ve zorunlu demo HTTP başlığı korundu.
Referans: [OKX API V5](https://www.okx.com/docs-v5/en/).
Bu geliştirmede gerçek OKX/Telegram emir veya mesaj gönderimi yapılmadı;
harici servisler testlerde taklit edildi. Telethon medya açıklaması varsa
yalnız o metni okur. Metinsiz görselde OCR veya içerik tahmini yapılmaz.

## Olay ve bildirim akışı

Mevcut `BotEvent` kuyruğu kullanılır; yeni tablo veya migration eklenmedi.
`signal_received` ve `signal_parsed` INFO loglarında olay adı bağlamda bulunur.
`risk_rejected`, `order_opened`, `tpsl_placed` ayrıca bu adlarla loglanır.
Önceki dinleyici logları ve `/admin/telegram` tanı uç noktası korunur.
`telegram_diagnose.py` aynı kanal parser'ını salt okunur kullanır.

Kullanıcı bir sinyal özeti ve sonrasında sonucu alır. Teknik `signal_received`
olayı ayrıca gönderilmez; incelemeye ayrılan mesaj için tek inceleme bildirimi
üretilir. TP/SL olayı denetim için ayrı saklanır, doğrulanmış emir açılma
bildiriminde birleştirilir. Botun pause/resume yanıtları ayrıca olay bildirimiyle
tekrarlanmaz; API üzerinden pause/resume olayları bildirim üretir. Aynı bağlantı
kesintisi boyunca Telegram uyarısı tekrarlanmaz.

Aynı kanal/mesajın yeniden teslimi yeni emir veya bildirim üretmez. Başarıyla
gönderilen olayın `delivered_at` alanı kalıcıdır; yeni bot nesnesi de bunu yeniden
göndermez. Bildirim hatasında olay kuyrukta kalır ve yeniden denenir; emir
durumuna veya emergency kilidine bildirim göndericisi müdahale etmez.
Telegram gönderimi ile veritabanı kaydı tek atomik işlem olmadığından, Telegram
kabulünden hemen sonraki süreç/veritabanı çökmesinde tekrar teslim mümkün olur;
Telegram Bot API bu akış için tek seferlik teslim anahtarı sağlamaz.

Emir açılma olayı; pozisyon, emir durumu ve koruma olaylarıyla aynı veritabanı
işleminde kaydedilir. Bildirimdeki giriş fiyatı OKX'in doğrulanan pozisyon
ortalamasıdır. Büyüklük gerçek kontrat miktarıdır; USDT veya coin miktarı diye
etiketlenmez. TP/SL, borsaya gönderilip doğrulanmış, fiyat adımına yuvarlanmış
seviyelerdir. Kısmi gerçekleşme, gerçekleşmeme, emir reddi, koruma eksikliği veya
güvensiz gerçek likidasyon durumunda “işlem açıldı” bildirimi oluşmaz.

Tüm metinler `telegram/formatting.py` içinde merkezidir. Teknik neden kodları
Türkçeye çevrilir; bilinmeyen ham sağlayıcı hataları kullanıcıya aktarılmaz.
Eski kuyruk olayları için de Türkçe şablonlar vardır. Eski olayda bulunmayan
gerçekleşme verisi “Bilgi yok” olarak gösterilir; bir fiyat uydurulmaz.

## Bildirim örnekleri

Aşağıdaki ilk çıktı doğrudan örnek sinyalin parser değerlerini gösterir:

```text
📥 YENİ SİNYAL ALINDI

🪙 Coin: CT/USDT
Sinyal: LONG adayı
Referans fiyat: 0.3604 USDT
Ortalama aralık: %17,33
Kaldıraç: 10x (İzole marjin / Isolated)
Kâr alma (TP): 0.397874392 USDT
Zarar durdurma (SL): 0.322925608 USDT
Yön, MZTRADINGBOT kanalına özel destekten dönüş kuralıyla yorumlandı.
Durum: Sinyal analiz edildi; risk kontrolüne alındı.
```

Örnek risk reddi (bildirim ayrıca sinyalin yön/fiyat/TP/SL alanlarını da içerir):

```text
⚠️ SİNYAL REDDEDİLDİ

🪙 Coin: CT/USDT
Neden: Stop-loss seviyesi tahmini likidasyon fiyatının ötesinde kalıyor veya likidasyon verisi yok.
🚫 İşlem açılmadı.
```

Emir açılması için yalnızca sahte borsalı testteki doğrulanmış BTC örneği:

```text
✅ DEMO İŞLEM AÇILDI

🪙 Coin: BTC/USDT
Yön: LONG
Kaldıraç: 10x (İzole marjin / Isolated)
Gerçekleşen giriş fiyatı: 100.2 USDT
Kâr alma (TP): 106 USDT
Zarar durdurma (SL): 94 USDT
Pozisyon büyüklüğü: 1 kontrat
Durum: Koruyucu emirler doğrulandı.
```

Yönü bilinmeyen mesaj:

```text
🔎 SİNYAL İNCELEME GEREKTİRİYOR

🪙 Coin: CT/USDT
Neden: İşlem yönü kesin olarak belirlenemedi.
İşlem açılmadı.
```

`/status`, `/balance`, `/positions`, `/trades`, `/stats` JSON yerine Türkçe
başlık ve alanlar döndürür. `/status` gerçek dinleyici hazır bilgisini ve
kayıtlı açık pozisyon sayısını gösterir. `/start`, `/pause`, `/resume`, `/close`
yardım ve sonuçları da merkezîdir. Yönetici kimliği, özel sohbet kontrolü,
60 saniyelik komut yaşı ve `/resume CONFIRM <kod>` onay akışı korunur.

## Kontroller ve güncelleme

20 Ekim 2026 son doğrulama sonuçları:

| Kontrol | Sonuç |
| --- | --- |
| Tüm çevrimdışı/mock testler | 222 başarılı; 5 PostgreSQL testi bu koşuda ayrı tutuldu |
| Geçici PostgreSQL testleri | 5 başarılı |
| Migration ve şema uyumu | upgrade, downgrade, yeniden upgrade ve drift kontrolü başarılı |
| Ruff lint | Başarılı |
| Ruff biçim kontrolü | 41 dosya uygun |
| Docker Compose build | Başarılı; `telegram-okx-demo-trading-bot:latest` |
| Ağsız imaj kontrolü | Parser, Türkçe sinyal özeti ve demo kilidi başarılı |
| İmaj/kaynak eşleşmesi | Kurulu 20 Python kaynak dosyasının SHA-256 değerleri eşleşti |

İmaj kontrolü yeni, geçici bir konteynerde `--network none`, ayrı Python giriş
noktası ve hiçbir volume/ortam dosyası bağlanmadan yapıldı. Servis başlangıcı
çalıştırılmadı. Mevcut 145 mock test de 222 testlik son koşunun içindedir.

Yerel doğrulama komutları (PowerShell):

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
$env:PATH = (Resolve-Path '.\.venv\Scripts').Path + ';' + $env:PATH
.\.venv\Scripts\python.exe scripts/check_postgres.py
docker compose build trading-bot
```

PostgreSQL aracı rastgele isimli ayrı bir konteyner/veritabanı açar, migration,
şema uyumu ve kalıcılık kontrollerini orada yapıp yalnız onu kaldırır. Çalışan
veritabanı, Telegram oturumu ve volume'ler bu testlerin hedefi değildir.
Compose, `DATABASE_URL` değerini kendi servis ortamında tanımlar. Yerel `.env`
dosyasındaki mevcut URL, desteklenen sürücü biçiminde değildi ve değiştirilmedi;
bu nedenle doğrudan yerel servis başlangıcı ayrıca uygun `DATABASE_URL` ister.
Demo anahtarları ve MZ kanal bağı, Compose gibi açık bir veritabanı URL'si
verilerek ayrıca doğrulandı; bu kontrol bir bağlantı açmadı.

Bu geliştirmede mevcut konteyner yeniden oluşturulmadı veya başlatılmadı.
Çalışmanın başında bot konteyneri zaten `Restarting (3)` durumundaydı; bu
operasyonel durum ayrıca incelenmelidir. Yeni imajın derlenmesi çalışan
konteynerin kullandığı imaj kimliğini değiştirmez.

Kullanıcının güncellemeyi uygulamak için çalıştırabileceği komutlar:

```powershell
docker compose build trading-bot
docker compose up -d --no-deps trading-bot
docker compose ps
docker compose logs --tail 100 trading-bot
```

İkinci komut yalnız bot konteynerini yeniden oluşturur; PostgreSQL servisini
yeniden başlatmaz. Başlangıçtaki mevcut güvenlik ve otomatik işlem durumu
veritabanından geri gelir. Kilit varsa kendiliğinden açılmaz: Telegram'da
`/status`, ardından gerekirse `/resume` ve verilen süreli `/resume CONFIRM <kod>`
kullanılır. Konteyneri değiştiren güncelleme komutları geliştirme sırasında
çalıştırılmadı; yalnız imaj derleme komutu çalıştırıldı.

## Değişen dosyalar

- `.env`, `.env.example`: yalnız MZ kanal kuralı için ek kimlik ayarı.
- `src/okx_bot/config.py`, `domain.py`, `parser.py`: kanal bağı ve yorumlama metaverisi.
- `src/okx_bot/engine.py`: zengin olaylar, doğrulanmış açılış verisi, bildirim birleştirmesi.
- `src/okx_bot/risk.py`, `exchange/gateway.py`: Türkçe açıklamaya bağlanan neden kodları.
- `src/okx_bot/app.py`: yönetim botuna mevcut dinleyicinin durumunu iletme.
- `src/okx_bot/telegram/formatting.py`: yeni merkezi Türkçe şablonlar.
- `src/okx_bot/telegram/admin.py`: Türkçe komutlar ve olay kuyruğu sunumu.
- `src/okx_bot/telegram/listener.py`: aynı kesintide tekrar bildirimi önleme.
- `src/okx_bot/telegram/diagnostics.py`: tanı aracında aynı parser seçimi.
- `tests/test_mz_parser.py`, `test_mz_risk.py`, `test_notifications.py`: yeni testler.
- `tests/test_controls.py`, `test_listener.py`, `test_telegram_observability.py`: genişletilen kontroller.
- `README.md`, bu belge: davranış, doğrulama ve güncelleme bilgileri.
