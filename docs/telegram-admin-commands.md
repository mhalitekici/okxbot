# Telegram yönetimi

Yönetim botu: @Okxrizikbot. Yalnızca `TELEGRAM_ADMIN_ID` ile eşleşen gerçek kullanıcıdan, aynı ID'nin özel sohbetinden gelen komutlar kabul edilir. Mevcut tek yönetici allowlist'i korunmuştur. Sinyal kaynağı `-1003769687656`, topic `23576`; servis mesajları, başka topic'ler ve kimliği doğrulanmamış topic'ler emir akışına alınmaz.

| Komut | Davranış |
|---|---|
| `/status` | Demo/live kapısı, Telegram, OKX, PostgreSQL, kilitler, son güvenli hata, belirsiz bildirim sayısı |
| `/balance` | USDT özkaynak, serbest teminat, mevcutsa kullanılan/dondurulan teminat ve unrealized PnL |
| `/allocation` | Döngü, başlangıç özkaynağı, sabit hedef, dolu/boş slot ve gerçek teminat |
| `/positions` | Yalnızca kayıtlı bot işlemleri; giriş/mark, TP/SL, teminat, üç risk yüzdesi, PnL |
| `/pnl` | Bugün, son 7/30 İstanbul takvim günü, tüm zamanlar net bot PnL |
| `/daily` | İstanbul günü performansı; ayrıca açıkça etiketli güncel hesap görüntüsü |
| `/history` | Son 20 kapanan bot işlemi ve kanıtlanmış kapanış nedeni |
| `/trades`, `/stats` | Geriye uyumlu işlem listesi ve yalnızca bot fill istatistikleri |
| `/limits` | Kalıcı risk ve sermaye ayarları |
| `/pause` | Yeni girişleri durdurur; takip devam eder |
| `/resume` | Okuma işlemleriyle güvenlik kontrolü ve süreli önizleme |
| `/resume CONFIRM <kod>` | Aynı yönetici, aynı güvenlik revizyonu ve yeniden doğrulanan hesapla devam |
| `/emergency` | Yeni girişleri kilitler; mevcut pozisyonu kapatmaz |
| `/close BTC-USDT-SWAP` | Sahipliği doğrulanmış bot pozisyonuna ayrı kontrollü çıkış yetkisi |
| `/setslots 3`, `/rebalance` | Yalnızca hesap tamamen pozisyonsuz ve emirsizken yeni döngü |
| `/setdailycap 3` | Günlük gerçekleşmiş zarar yüzdesi |
| `/setriskcap 40` | Portföy toplam tahmini SL risk yüzdesi |
| `/setreserve 10` | Asgari serbest rezerv, USDT |
| `/setmaxpositions 3` | Slot sayısından bağımsız maksimum pozisyon |
| `/setspread 0.5`, `/setdeviation 0.5`, `/setslippage 0.5` | Fiyat güvenlik sınırları, yüzde |
| `/setliqbuffer 0.25`, `/setstopwarning 5` | Likidasyon tamponu ve geniş SL uyarısı, yüzde |
| `/setautorebalance true` | Tamamen pozisyonsuzken otomatik döngü yenileme |
| `/confirm <kod>` | Ayar önizlemesini 120 saniye içinde uygular |

Ondalık ayar değerlerinde nokta kullanın. Tüm ayar değişiklikleri iki aşamalıdır; token hash'i bellekte tutulur, token audit'e yazılmaz. Başka kullanıcı, süre aşımı, servis restart'ı, güvenlik/ayar değişimi veya hesap değişimi onayı geçersiz kılar. Ayarlar PostgreSQL'de, önceki/yeni değer ve aktör audit olayında saklanır. Açık işlemin TP/SL'si veya teminatı değiştirilmez.

OKX çevrimdışıyken devam, rebalance veya ayar değişikliği uygulanmaz. Acil kilit/pause bağlantısızken de uygulanabilir. PostgreSQL kesintisinde yönetim botu hafızadaki offset ile okumaya devam etmeyi dener; `/status` güvenli kapalı durum gösterir, kalıcı ayar değişikliği yapılamaz. Yeniden başlatılmış uygulama `/resume` onayı ister.

Kapanış nedeni yalnızca algo-child emir ilişkisi ve gerçekleşen fill'lerle TP/SL olarak etiketlenir. Yönetici çıkışı, borsa order kategorisinin kanıtladığı liquidation ve bilinmeyen neden ayrıdır. Net PnL = fillPnl + işaretli USDT fee + atfedilmiş işaretli funding. Eksik veri `Hesaplanamadı` gösterilir.

Günlük rapor 00:05 Europe/Istanbul'dan itibaren bir önceki gün için kalıcı, tekil outbox kaydı oluşturur. Yeniden başlatmada eksik günler 31 günlük gruplarla tamamlanır. Açık pozisyon PnL'si ve hesap bakiyesi rapor oluşturma anına aittir; geçmiş gün sonu bakiyesi diye sunulmaz.

Telegram Bot API idempotency anahtarı sağlamaz. Başarılı gönderi ve message ID kaydedilir. Kesin bağlantı kurulamadığında retry yapılır; read-timeout veya kayıp ACK durumunda kayıt `uncertain`/`sending` kalır ve otomatik tekrar gönderilmez. `/status` sayıyı gösterir. Yönetici sohbeti ve kayıt ID'sini incelemeden bu kaydı yeniden kuyruğa almamalıdır. Olay üretimi tekildir; ağ kesintisinde hem kesin bir kez teslim hem kayıpsız teslim garantisi verilemez.
