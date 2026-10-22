# Sabit sermaye döngüsü ve slotlar

Bu sürüm yalnızca Demo içindir. `allocation_base_equity`, OKX Futures modundaki USDT `eq` değeridir; serbest işlem teminatı olan `availBal` ile karıştırılmaz. Diğer para birimlerinin USD karşılığı bu USDT stratejisinin sermayesine eklenmez.

Pozisyonsuz ve doğrulanmış hesapta oluşturulan döngü için:

```
slot_target_margin = allocation_base_equity / allocation_slots
hedef nominal = slot_target_margin × 10
kontrat = floor_to_lot(hedef nominal / (ctVal × muhafazakâr giriş fiyatı))
```

Varsayılan `allocation_slots=3`: 1500 → 500; 900 → 300; 3000 → 1000 USDT/slot. Kontrat adımı nedeniyle gerçek teminat hedefin biraz altında kalabilir. Lot adımını aşarak yukarı yuvarlama veya yetersiz bakiyede sessiz küçültme yapılmaz.

1500 USDT örneğinde ilk işlemden sonra serbest teminat 1000 olsa bile ikinci işlemin hedefi 500'dür. Üçüncü işlem de 500 hedefler. Borsa teminatı, giriş/çıkış ücret rezervi, bakım marjini tamponu, yerel bekleyen rezervasyonlar ve minimum serbest rezerv yeterli değilse üçüncü işlem reddedilir. Tam üç işlem açılacağı garanti edilmez.

`capital_cycles` başlangıç özkaynağını ve sabit hedefi; `capital_slots` dolu slotları; `trade_orders` döngü, slot ve gerçek teminat geçmişini saklar. PostgreSQL'de `runtime_state` satır kilidi altında emir niyeti ve slot birlikte kaydedilir. İşlem limiti, enstrüman çakışması, toplam risk ve bekleyen rezervler aynı transaction içinde tekrar kontrol edilir; bakiye bu kilit altında yeniden okunur. Process advisory lock aynı veritabanına ikinci çalışan servisi engeller.

Bir işlem kesin çıkış fill'leriyle kapandığında slot bırakılır. Diğer işlemler açıkken hedef değişmez; yeni sinyal boşalan slotu aynı hedefle kullanır. Belirsiz submission, kısmi kapanış ve eksik çıkış kanıtı slotu tutar. Terminal sıfır fill veya kesin emir reddinde slot serbest bırakılır.

Tüm bot işlemleri ve hesap emirleri bittiğinde, reconciliation doğrulandığında `auto_rebalance=true` yeni döngü oluşturur. Yeni özkaynak 1650 ise 550; 1200 ise 400 USDT/slot olur. `auto_rebalance=false` döngüyü korur. `/rebalance`, iki aşamalı onay ve tamamen pozisyonsuz hesap gerektirir.

`/setslots 2` veya `4` de aynı kontrolleri gerektirir. Slot sayısı ile ayrı `max_open_positions` limitinin küçüğü uygulanır; dört slot seçmek maksimum pozisyon limitini otomatik yükseltmez. Hiçbir ayar mevcut pozisyon teminatını veya TP/SL'sini değiştirmez.

0003'ten kalan açık işlemler korunur; geriye dönük slot veya risk bilgisi uydurulmaz. Risk bilgisi eksik eski işlem varken yeni emir engellenir. İlk yeni döngü için pozisyonsuz, emirsiz doğrulanmış durum gerekir. Restart döngüyü silmez; uygulama başlangıcında yeni girişler yönetici `/resume` onayına kadar kapalıdır.
