# Risk yönetimi

10x isolated sabittir. Kanal parser'ının destek/direnç ve LONG/SHORT kuralları korunmuştur. `range_percent × 0.6` hem TP hem SL fiyat mesafesidir. Gerçek giriş `F`, yüzde aralık `R` için `d=R/100×0.6`:

| Yön | TP | SL |
|---|---|---|
| LONG | F×(1+d) | F×(1−d) |
| SHORT | F×(1−d) | F×(1+d) |

Fiyatlar enstrümanın `tickSz` adımına yönüne uygun yuvarlanır. Açıkça verilmiş mutlak TP/SL korunur. Sinyal değişikliği açık pozisyonun stratejisini değiştirmez. Gerçek fill sonrası aralık seviyeleri yeniden hesaplanır, borsadaki koruma güncellenir ve GET ile doğrulanır.

500 teminat, 5000 nominal ve %5,78 aralık için mesafe %3,468; teorik brüt TP/SL ±173,40 USDT'dir. Bu bir performans tahmini değildir. Ücret, fonlama ve gerçekleşme kayması ayrıca ölçülür. Eski `MAX_ACCOUNT_RISK_PERCENT` yalnızca eski `.env` uyumluluğu için kabul edilir; yeni boyutlandırmada kullanılmaz. İşlem başı %3–4 stop sınırı uygulanmaz.

Tahmini nakit risk, kontrat miktarı × ctVal × fiyat mesafesi ve muhafazakâr giriş/çıkış ücret rezervidir. Telegram ayrı ayrı riski teminata, hesap özkaynağına ve toplam açık riski özkaynağa böler. `/positions` güncel özkaynağı, açılış bildirimi açılıştaki özkaynağı kullanır. Geniş SL uyarısı seviyesi değiştirmez.

Varsayılan bağımsız sınırlar:

| Sınır | Değer |
|---|---|
| Slot / maksimum pozisyon | 3 / 3 |
| Toplam tahmini stop riski / özkaynak | %40 |
| Günlük gerçekleşmiş net zarar / gün başlangıç özkaynağı | %3 |
| Minimum serbest USDT rezervi | 10 |
| Spread / referans sapması / emir kayması | %0,5 / %0,5 / %0,5 |
| Likidasyondan fiyat tamponu | %0,25 |
| Geniş SL uyarısı | %5 fiyat mesafesi |

Bu değerler kârlılık veya üç işlemin açılabilirliği varsayımı taşımaz. Günlük limit tek bir normal kanal SL'sinden küçük olabilir; gerçekleşen zarar yeni girişleri durdurur. Limitler ilk çalışmada PostgreSQL'e alınır; daha sonra `.env` düzenlemek kayıtlı limitleri sessizce ezmez. Sıfır/negatif limit, %75 üstü portföy riski ve %25 üstü günlük limit reddedilir. Her değişiklik yönetici onayı ve audit kaydı gerektirir.

IOC limit fiyatı hem sinyal referans bandı hem güncel karşı fiyatın kayma bandıyla sınırlanır. `Decimal` kullanılır. Borsa tier/MMR, taker fee, ctVal, lot, minimum/maksimum kontrat ve gerçek likidasyon fiyatı kontrol edilir. Stopun likidasyondan önce gelmediği işlem reddedilir. Borsa tarafındaki tahmini formül gerçek fill sonrasında yeniden doğrulanır; ani fiyat boşlukları stop fiyatında gerçekleşmeyi garanti etmez.

Günlük risk günü Europe/Istanbul'dur; UTC kaydedilen bot fill'leri ve atfedilmiş funding kullanılır. Yatırma/çekme ve diğer işlemlerin hesap özkaynağı değişimi bot zararı sayılmaz. Günlük limit gün sonuna kadar latch edilir; limit artırmak aynı gün latch'i temizlemez. Bilinmeyen ücret para birimi veya bot işlemine ait olabilecek belirsiz funding yeni girişleri engeller.

Koruma reddi/timeout prosedürü: acil kilit, kritik Telegram alarmı, yeni girişlerin durması. Yalnızca ayrılmış hesap açıkça teyit edilmişse ve giriş, pozisyon kimliği, miktar, emirler ve fill sahipliği doğrulanıyorsa bot kendi miktarı için kontrollü çıkış gönderebilir. Net modda `reduceOnly` kullanılır. Belirsiz sahiplikte otomatik kapatma yapılmaz; yönetici borsadan müdahale eder. Kısmi çıkışta kalan miktar korunur ve kilit uygulanır; tam kapanış bildirimi gönderilmez.

Hiçbir hesap geneli iptal veya `autoCxl` kullanılmaz. `/emergency` yalnızca yeni girişleri kilitler. Kontrol edilemeyen dış işlem eşzamanlılığı nedeniyle ortak net hesapta kesin pozisyon izolasyonu iddia edilmez. Üretim için ayrı trading sub-account gereklidir; bu sürümün live kapısı her durumda kapalıdır.
