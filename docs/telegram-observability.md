# Telegram dinleyicisi ve logların görünürlüğü

Forum topic filtresiyle eklenen alanlar ve güncel salt okunur mesaj karşılaştırması:
[Telegram topic filtreleme raporu](telegram-topic-filter.md).

## İnceleme sonucu

Çalışan Docker hizmetinde kanal ID'sinin `.env` ve Pydantic ayarlarında aynı sayısal
değer olduğu doğrulandı. Telegram oturumu yetkili, yapılandırılan kanal hesabın
dialog listesinde ve kanalın mesajları okunabiliyor. Aynı kanalın kalıcı cursor'u
ilerlemiş; son üç mesajın `telegram_signals` tablosunda `needs_review` kaydı var.
Bu kayıtlar, mevcut listener → engine → parser yolunun çalıştığını gösteriyor.

Son mesaj ayrıca yalnız okuyup parser'a verilerek kontrol edildi. Sonuç
`needs_review` idi. Bu kontrol `engine.ingest()` çağırmadı, durum/cursor yazmadı,
mesaj göndermedi ve OKX'e erişmedi. Bir sinyalin parser tarafından incelenmesi,
işleme uygun bulunduğu anlamına gelmez; eksik yön veya çelişkili alanlar mevcut
kurallara göre incelemeye ayrılır.

Logların sessiz olmasının nedeni dinleyicinin başlatılmaması değildi. Başarılı
Telegram bağlantısı, kanal aboneliği ve sinyal işleme yollarında uygulama INFO
logu yoktu. Telethon'un kendi logları da token/oturum içeriğinin sızmaması için
CRITICAL seviyesinde tutuluyordu. Bu kısıt korunarak uygulama logları eklendi.

## Başlangıç akışı

```text
FastAPI lifespan
  → OKX başlangıç eşitlemesi
  → asyncio.create_task(guarded("telegram_listener", listener.run()))
  → Telethon connect + is_user_authorized
  → Kanalın marked sayısal ID ile dialog'lardan bulunması
  → Kalıcı cursor / ilk açılış sınırının yüklenmesi
  → NewMessage ve MessageEdited handler'larının eklenmesi
  → Periyodik scan + ready
```

Kanal bulunmazsa veya oturum yetkisizse hata aşaması loglanır; client kapatılır ve
mevcut background task güvenlik kilidi devreye girer. İlk açılıştaki eski mesajları
atlama, duplikasyon, sinyal yaşı ve pause/emergency kontrolleri korunur.

## Beklenen loglar

`LOG_LEVEL=INFO` ile başlangıçta:

```text
Trading bot starting
Background task started                 context.task=telegram_listener
Telegram listener starting
Telegram connected                      context.authorized=true
Telegram cursor restored                veya Telegram baseline initialized
Channel subscribed                      context.handler_count=2
Telegram listener ready
```

Yeni bir mesaj için:

```json
{"message":"Signal received","context":{"message_id":100,"coin":"BTCUSDT","analysis_result":"pending","trade_status":"received"}}
{"message":"Signal parser started","context":{"message_id":100,"coin":"BTCUSDT"}}
{"message":"Signal processed","context":{"message_id":100,"coin":"BTCUSDT","analysis_result":"needs_review","trade_status":"needs_review","reason_code":"MISSING_DIRECTION"}}
```

Örnekteki ID ve coin test verisidir. Gerçek kayıtlar timestamp, logger ve kanal ID
alanlarını da içerir. Geçerli parse için `Signal parsed`, risk/pause nedeniyle
ret için `trade_status=rejected`, `reason_code=TRADING_DISABLED` veya ilgili risk
kodu görülür. İşlem yaşam döngüsünde durum gerçekten değiştiğinde
`Signal trade status changed` yazılır. Bu logların eklenmesi emir açma yetkisini değiştirmez.

Ham mesaj metni, kanal başlığı, Telegram telefon numarası, bot token'ı, API hash'i,
oturum string'i ve ham exception metni loglanmaz. Coin yalnız açık USDT token'ından
çıkarılır; yön tahmin edilmez. Geçersiz bir düzenlemede önceki parse sonucu artık
sonuç loguna veya yeni mesajın kaydına yanlışlıkla taşınmaz.

## Durum ve emir göndermeyen tanı

Yönetici bearer token'ıyla korunan `GET /admin/telegram` şu alanları döndürür:

```text
channel_id, connected, authorized, subscribed, handler_count, ready,
topic_id, topic_verified, topic,
last_scan_at, last_message_at, last_message_id, last_error
```

`GET /admin/status` yanıtında da aynı bilgiler `telegram` alanındadır. Bağlantı
kopması ve yeniden bağlanma uygulama loglarına yazılır; handler'lar ikinci kez
eklenmez. Normal periyodik scan ve tekrar mesajlar INFO seviyesinde gürültü üretmez.

```powershell
docker compose exec -T trading-bot python scripts/telegram_diagnose.py
```

Tanı aracı:

- Çalışan sürecin listener durumunu yerel `/admin/telegram` üzerinden okur.
- Oturumun **ayrı bir bağlantıda** yetkili olduğunu ve kanal erişimini kontrol eder.
- Bu bağlantıda `receive_updates=False` kullanır; canlı listener handler'larını çalıştırmaz.
- Mesajın forum topic'ini çözümler; yalnız izinli topic'te parser'ı çağırır.
  `--message-ids 23576 32877` ile belirli mesajları ve topic metadata'sını karşılaştırır.
  Ham metin yerine sonuç metadata'sını gösterir.
- `engine_ingest_called=false`, `trading_attempted=false` döndürür.
- Veritabanına yazmaz, cursor değiştirmez, OKX istemcisi oluşturmaz ve emir göndermez.

Bağımsız probe'un `connected=true` sonucu ile çalışan sürecin durumu ayrı alanlardır;
listener'ın gerçekten çalışması için `running_listener.ready/subscribed` de incelenmelidir.

## Docker güncellemesi

```powershell
docker compose build trading-bot
docker compose up -d --no-deps trading-bot
docker compose logs --since 5m -f trading-bot
```

Ayrı bir terminalde tanı komutunu çalıştırabilirsiniz. Bu değişiklik yeni migration
gerektirmez. PostgreSQL/Telegram volume'leri, pause/emergency durumu ve mevcut
`OKX_DEMO=1`, `LIVE_TRADING_ENABLED=false` ayarları korunur. İnceleme sırasında çalışan
container yeniden oluşturulmadı; yeni loglar güncel imaj başlatılınca görünür.

## Testler

```powershell
uv run pytest tests/test_telegram_observability.py -q
uv run pytest -q
uv run python scripts/check_postgres.py
uv run ruff check .
```

Yeni testler Telethon sınırında mock kullanır. Gerçek event builder'larıyla yeni
mesaj/düzenleme callback'leri, parser çağrı sayısı, güvenli INFO metadata'sı,
duplikasyon, ilk açılışta eski mesajı atlama, oturum/kanal hataları, yeniden bağlanma
ve yetkili durum uç noktası doğrulanır. Bu testlerde strateji paused tutulur;
emir yürütmesine girilirse test doğrudan başarısız olur. Bağımsız tanı testinde
engine veya exchange nesnesi bulunmaz.

Doğrulama sonucu: **145 mock test + 5 PostgreSQL entegrasyon testi geçti**.
Ruff, format, lockfile ve Compose kontrolleri başarılı. `telegram-okx-demo:telegram-observability`
imajı derlendi; ağ erişimi kapalı container'da listener/durum uç noktası, parser,
metadata formatter'ı ve tanı aracı importları doğrulandı. Testler gerçek veya demo
borsa emri göndermedi. Çalışan servisin güvenlik durumu ve `.env` değiştirilmedi.

Telethon'un ilgili davranışları:
[TelegramClient ve receive_updates](https://docs.telethon.dev/en/stable/modules/client.html).
