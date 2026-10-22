# Reconciliation kilidi: inceleme ve düzeltme

20 Ekim 2026 tarihinde çalışan Docker hizmeti üzerinde yapılan inceleme salt okunurdu.
Emir gönderilmedi, pozisyon kapatılmadı, `/resume` çağrılmadı ve gerçek veritabanının
güvenlik durumu değiştirilmedi. `OKX_DEMO=1`, `LIVE_TRADING_ENABLED=false` korunuyor.

## Bulunan neden

Docker başlangıç loglarında hata `account_config()` aşamasında:

```text
RiskRejected: Use a dedicated OKX demo account in Futures mode (acctLv=2)
```

İlk emergency kaydı `2026-10-20 13:25:50 UTC` tarihinde oluşmuş
(`16:25:50`, UTC+03:00). Eski `reconcile()` bütün exception'ları
`Reconciliation failed; new orders disabled` metnine indirgediği için veritabanında
asıl aşama ve hata ayrıntısı yoktu. Eski JSON formatter da yalnız exception türünü
yazıyordu. Başlangıç traceback'i hesap modu doğrulamasının başarısız olduğunu gösteriyor;
o zamanki gerçek `acctLv` değeri saklanmadığından bu değer tahmin edilmiyor.

İnceleme sırasındaki durum:

| Kontrol | Sonuç |
| --- | --- |
| PostgreSQL erişimi / migration | Başarılı / `0001` |
| OKX özel hesap API'si | Başarılı, `acctLv=2`, `posMode=net_mode` |
| Açık SWAP pozisyonları | 0 |
| Bekleyen normal emirler | 0 |
| Bekleyen conditional/OCO, trigger, trailing, iceberg, TWAP, smart iceberg, chase | 0 |
| Veritabanındaki emir / pozisyon / gerçekleşme | 0 / 0 / 0 |
| Günlük zarar kilidi | `loss_latched=false` |
| Kalıcı runtime kilidi | `paused=true`, `emergency=true` |

Sonraki başarılı başlangıç ve periyodik eşitleme `engine.healthy` değerini bellekte
`true` yapmış. `paused` ve `emergency` ise PostgreSQL'de kalıcıdır ve başarı yolunda
değiştirilmez. Bu nedenle Docker healthy, `exchange_healthy=true` ve emergency'nin
birlikte görülmesi mümkün. Bağlantının düzelmesi otomatik işlem yetkisi vermez.

Bu ilk nedenin bugünkü kontrollerde devam ettiğine dair bulgu yok. Geçmiş kilit
yerinde tutuldu; kullanıcı adına işlem yetkisi açılmadı.

## Düzeltmeler

- Eşitleme hatası artık aşama, exception türü, OKX kodu, endpoint, HTTP durumu,
  gerektiğinde PostgreSQL SQLSTATE ve güvenli stack konumu ile kaydedilir.
  Ham exception metni, SQL/parametreler, API yanıtı, token veya oturum loglanmaz.
- İlk emergency nedeni korunur. Son eşitleme hatası ve toparlanma zamanı ayrı
  saklanır. Tekrarlanan aynı hata bildirim kuyruğunu her turda doldurmaz.
- PostgreSQL kesintisi hata kaydının yazılmasını engellerse süreç bellekte kilitlenir;
  bağlantı döndüğünde bu hata kalıcı kilide dönüştürülür. Başarılı eşitleme bunu açmaz.
- Eşitleme döngüsü API, veritabanı ve bozuk snapshot hatalarından sonra kilitli
  biçimde kontrol etmeye devam eder. Fatal görev/tek çalışan kilidi kontrolleri korunur.
- Gerçek günlük zarar sınırı ile eksik/bozuk risk verisi ayrıldı. Bozuk günlük veri
  artık yanlışlıkla yalnız zarar sınırıymış gibi yutulmaz.
- Bekleyen algo emirleri de kontrol edilir. Takipsiz emir veya pozisyonlar kilidi korur.
- Yönetici pause'u geçmiş emergency nedenini ezmez.
- `/status` ve sağlık yanıtında servis sağlığı ile işlem yetkisi ayrı gösterilir.
- Onay kodları 120 saniyelik, tek kullanımlık ve yönetici/kanala bağlıdır. Kodun
  yalnız hash'i bellekte tutulur; restart geçersiz kılar. Kodlar audit kayıtlarına yazılmaz.
- Onay öncesinde ve sonrasında hesap modu, pozisyonlar, normal/algo emirler,
  TP/SL koruması, DB tutarlılığı ve günlük risk yeniden doğrulanır. Son commit'te
  veritabanı güvenlik revizyonu tekrar karşılaştırılır.
- Onay kontrolleri borsada yalnız GET kullanır; sorunlu pozisyonu otomatik kapatarak
  onayı geçerli hale getirmez. Normal koruyucu eşitleme döngüsü ayrı olarak devam eder.
- `0002` migration'ı yalnız tanı/revizyon alanları ekler; geçmiş kayıtları ve kilitleri silmez.

## Docker üzerinde güncelleme

Mevcut PostgreSQL ve Telegram volume'leri korunarak:

```powershell
docker compose build trading-bot
docker compose up -d --no-deps trading-bot
docker compose logs --since 5m trading-bot
docker compose exec -T trading-bot python scripts/diagnose.py
```

Başlangıç script'i `alembic upgrade head` çalıştırır. Tanı çıktısında migration
`0002`, demo açık ve canlı işlem kapalı görünmeli. Normal olarak `paused` ve
`emergency` bu aşamada hâlâ `true` kalacaktır; bu bir migration hatası değildir.

Sağlık adresini mevcut Compose port eşlemenizden öğrenin:

```powershell
docker compose port trading-bot 8000
```

İncelenen kurulumda adres `http://127.0.0.1:8010/health/ready` idi. Sağlık yanıtının
`ready` olması işlem yetkisini açmaz; `trading_enabled` ve `/status` da kontrol edilmelidir.

**Yeni sürüm yüklendikten sonra**, yalnız işlem yetkisini geri vermek isteyen yönetici
Telegram'dan `/resume` gönderir. Özet uygun görünüyorsa botun verdiği
`/resume CONFIRM <onay-kodu>` komutunu 120 saniye içinde kendisi gönderir.
Başarısız kontrolde mevcut kilit korunur. Bu inceleme sırasında bu adımlar uygulanmadı.

SQL ile bayrak değiştirmek, durum tablolarını temizlemek veya volume silmek bu akışın
parçası değildir. Manuel pause ve günlük zarar kontrolü de atlanmaz.

Ek yerel yapılandırma bulgusu: `.env` içindeki `DATABASE_URL` değerinin başında ikinci
bir `DATABASE_URL=` bulunuyor. Compose bu adresi container ortamında yeniden tanımladığı
için çalışan Docker'ın PostgreSQL erişimi etkilenmiyor. Yerel CLI/migration kullanımı
için satırın tek `DATABASE_URL=postgresql+asyncpg://...` ataması olması gerekir.
Bu incelemede `.env` veya gizli bilgiler değiştirilmedi.

## Otomatik doğrulama

```powershell
uv run pytest -q
uv run python scripts/check_postgres.py
uv run ruff check .
uv run ruff format --check .
docker compose build trading-bot
```

PostgreSQL script'i yalnız rastgele adla kendi oluşturduğu geçici test container'ında
çalışır. Legacy `0001 → 0002` kilidinin korunması, kayıt sayılarının değişmemesi,
başarılı eşitlemenin otomatik kilit açmaması, açık yönetici onayı, restart, tek
çalışan kilidi ve migration rollback/upgrade bu kapsamda doğrulanır. Gerçek OKX
hesabında emirli entegrasyon testi yapılmaz.

Doğrulama sonucu: **133 mock test + 5 PostgreSQL testi geçti**. Ruff, format ve
lockfile kontrolleri ile Compose doğrulaması başarılı. `telegram-okx-demo:reconciliation-fix`
imajı derlendi; ağ erişimi kapalı container'da import, demo kilidi, ayrı kullanıcı,
gizli dosya dışlama ve tanı istemcisinin POST engeli doğrulandı. Çalışan container
yeniden oluşturulmadı; gerçek veritabanı `0001` ve güvenlik kilitleri korundu.

Resmi API başvurusu: [OKX hesap yapılandırması ve algo emir listesi](https://www.okx.com/docs-v5/en/).
