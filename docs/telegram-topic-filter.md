# Telegram forum topic doğrulaması ve filtreleme

Bu rapor ilk topic filtre teslimatını belgeler. Son MVP'de 32876 numaralı
mesajın SHORT analizi de tamamlandı; güncel test sayıları ve gerçek salt okunur
kanıtlar [son kabul raporundadır](final-acceptance-report.md). Bu son çalışmada
`.env` değiştirilmedi.

## Salt okunur doğrulama

20 Ekim 2026'da önce mevcut `scripts/telegram_diagnose.py` aracı, ardından
Telethon ile belirli mesajlar ve `messages.GetForumTopicsByIDRequest` kullanıldı.
Grup `-1003769687656` için Telegram `forum=true` döndürdü. Güncelleme almayan
ayrı bir `StringSession` bağlantısı kullanıldı; oturum dosyası değiştirilmedi.
Mesaj gönderilmedi, okundu bildirimi gönderilmedi, cursor/veritabanı yazılmadı,
`engine.ingest()` veya OKX çağrılmadı.

| Mesaj | Tür | reply_to_top_id | reply_to_msg_id | forum_topic | Doğrulanan topic |
| --- | --- | --- | --- | --- | --- |
| 23576 | MessageService / MessageActionTopicCreate | null | null | null | 23576 — AL SAT ÖNCÜ SİNYAL ODASI |
| 32877 | Message | null | 6 | true | 6 — ŞükoNEWS |
| 32876 | Message | null | 23576 | true | 23576 — AL SAT ÖNCÜ SİNYAL ODASI |

23576 bir normal sinyal mesajı değil, topic'in oluşturulma mesajıdır. Oluşturma
eylemindeki eski başlık **Bot risk**, Telegram'ın güncel topic metadata'sındaki
başlık **AL SAT ÖNCÜ SİNYAL ODASI** idi. Aynı sayısal kimlik, yeniden adlandırılmış
topic'i doğruluyor. Linkin son parçasının her durumda topic kimliği olduğu
varsayılmadı; burada oluşturma eylemi ve metadata birlikte doğrulandı.

Topic 23576 için metadata `top_message=32876`, topic 6 için `top_message=32877`
döndürdü. Her ikisinde `closed=true`, `hidden=false` gözlendi. Bu alanlar salt
okunur bilgi olarak saklandı; topic'lerin durumu değiştirilmedi. Gizli General
topic'i de metadata sorgusunda ID 1 olarak görüldü; sinyal kaynağı olarak seçilmedi.

Bu gözlem, 32877'nin sinyal odasına ait olmadığını kesinleştirir. Diğer odada
yakalanan eski `needs_review` kaydı silinmez; yeni sürümde bu odadan gelen
mesajlar hiç sinyal kaydı veya bildirim oluşturmaz.

## Filtreleme davranışı

`.env` dosyasına doğrulanmış `TELEGRAM_TOPIC_ID=23576` eklendi. Diğer ortam
değerleri korundu. `.env.example` içinde bu ayar güvenli varsayılan `0` ile
belgelendi. `0` filtreyi devre dışı bırakmaz; yeni sinyalleri tamamen engeller.

Başlangıçta ve açık yeniden bağlantı akışında dinleyici, seçilen grupta forum
özelliği olduğunu ve istenen topic'in güncel metadata ile var olduğunu doğrular.
Topic ayarı eksikse, topic silinmişse veya metadata alınamıyorsa dinleyici
işlem almaya başlamaz. Mevcut arka plan görev güvenlik mekanizması korunur.

Normal mesajlarda:

1. Gerçek `chat_id` izinli grup ile eşleşmeli.
2. `MessageReplyHeader.forum_topic` açıkça `true` olmalı.
3. Varsa pozitif `reply_to_top_id` topic kimliğidir.
4. Üst kimlik yoksa pozitif `reply_to_msg_id` kullanılır; Telegram'ın doğrudan
   topic'e gönderilen 32876 ve 32877 mesajlarında verdiği biçim budur.
5. Topic kimliği yapılandırılmış 23576 ile eşleşmeli. Başka sohbete verilen
   yanıtlar, eksik/bozuk başlıklar, General için belirsiz başlıklar ve service
   mesajları işlem akışına alınmaz.

İç içe yanıtta, doğrudan yanıtlanan mesajın ID'si topic ID'si sanılmaz:
`reply_to_top_id=23576, reply_to_msg_id=32875` kabul edilen topic'e aittir.
`reply_to_top_id=6, reply_to_msg_id=23576` ise reddedilir; doğrudan parent alanı
üst topic'i geçersiz kılamaz. Yönlendirme bilgisi, metin veya linkten topic
tahmini yapılmaz.

Yeni mesaj ve düzenleme callback'leri aynı filtreyi kullanır. Polling tarafında
`iter_messages(..., reply_to=23576)` ile yalnız sinyal topic'i sorgulanır; dönen
mesajlara yine aynı filtre uygulanır. Böylece diğer odalardaki trafik sinyal
backlog'u sayılmaz. Kalıcı cursor ve yaş sınırı sıfırlanmaz; geçmişteki örnek
mesajlar yeniden işleme sokulmaz.

Ek olarak `TradingEngine.ingest()` içinde topic eşleşmesi zorunludur. Eksik veya
yanlış topic, parser/veritabanı sinyal kaydı/bildirim/emir aşamalarından önce
`ignored_topic` ile sonuçlanır. Bu nedenle dinleyici dışından bir çağrı da topic
bilgisi olmadan işlem başlatamaz. Doğru topic, yeni sinyallerin `parsed.topic_id`
metaverisine ve olay bağlamına kaydedilir; migration gerekmez.

10x isolated, 0.6 TP/SL katsayısı, OKX Demo kilitleri, fiyat sapması, mesaj yaşı,
hesap riski, günlük zarar ve likidasyon kontrolleri değiştirilmedi. Mevcut açık
pozisyonların izlenmesi veya yönetici onayları bu filtreyle devre dışı bırakılmaz.

## Tanı ve güncelleme

Yeni imajdaki salt okunur karşılaştırma komutu:

```powershell
docker compose exec -T trading-bot python scripts/telegram_diagnose.py --message-ids 23576 32877 32876
```

Araç `reply_to_top_id`, `reply_to_msg_id`, `forum_topic`, `action_type`, çözülen
`topic_id`, güncel topic metadata ve `eligible_topic` alanlarını raporlar.
`eligible_topic` yalnız kaynak odası eşleşmesidir; emir/risk onayı değildir.
Root service mesajı `service_message`, 32877 `ignored_topic` olarak görünür.
Örnek sinyallerin yaşı veya işleme uygunluğu ayrı kontrollerdir.

Mesaj ID listesi verilmezse, aracın önceki son-mesaj tanısı korunur. Son mesaj
başka odadaysa parser çağrılmaz; `ignored_topic` gösterilir.
`read_only=true`, `receive_updates=false`, `engine_ingest_called=false` ve
`trading_attempted=false` rapor alanları korunur.

Yeni imajdaki araç ayrıca geçici bir Compose süreç konteynerinde çalıştırıldı;
servis başlangıcı yerine doğrudan tanı betiği kullanıldı. 23576, 32877 ve 32876
aynı sonuçlarla karşılaştırıldı. Ham mesaj metni veya erişim bilgisi içermeyen
çıktı [.artifacts/telegram-topics-2026-10-20.json](../.artifacts/telegram-topics-2026-10-20.json)
dosyasındadır. Bu ayrı konteynerin `running_listener.available=false` alanı kendi
localhost adresinde bir web servisi çalışmamasını gösterir. Mevcut bot ayrı olarak
kontrol edildi ve sağlıklıydı; konteyner kimliği ve kullandığı imaj değişmedi.

32876 doğru topic'e aittir; bu mesaj için mevcut parser `MISSING_DIRECTION`
nedeniyle `needs_review` döndürdü. Topic filtresi parser'ın yön yorumunu değiştirmez.
Bu örneğe emir veya risk yürütmesi yapılmadı; geçmiş sinyal yeniden oynatılmadı.

Kullanıcının güncellemeyi uygulamak için çalıştıracağı komutlar:

```powershell
docker compose build trading-bot
docker compose up -d --no-deps trading-bot
docker compose ps
docker compose logs --tail 100 trading-bot
docker compose exec -T trading-bot python scripts/telegram_diagnose.py --message-ids 23576 32877
```

Çalışan bot bu geliştirme sırasında otomatik yeniden oluşturulmaz veya yeniden
başlatılmaz. Filtre, kullanıcının yeni imajı uygulamasıyla aktif olur. PostgreSQL,
Telegram oturumu, eski sinyal kayıtları ve güvenlik kilitleri korunur.

## Test sonuçları

| Kontrol | Sonuç |
| --- | --- |
| Tüm çevrimdışı/mock testler | 271 başarılı |
| Yeni topic testleri | 49 başarılı; yukarıdaki 271 teste dahil |
| Ayrı geçici PostgreSQL testleri | 5 başarılı |
| Migration ve şema uyumu | Upgrade, downgrade, yeniden upgrade ve drift kontrolleri başarılı |
| Ruff lint ve biçim | Başarılı; 43 Python dosyası uygun |
| Docker Compose build | Başarılı |
| Yeni imajdan gerçek Telegram tanısı | Salt okunur topic karşılaştırması başarılı |
| Ağsız imaj kontrolü | Topic çözümleyicisi ve demo kilitleri başarılı |
| Kaynak/imaj eşleşmesi | 21 paket dosyası ve tanı betiğinin SHA-256 değerleri eşleşti |

Testler doğru/yanlış/eksik topic, iç içe yanıt, forum bayrağı, çapraz sohbet
yanıtı, service mesajı, eksik topic ayarı, silinmiş topic, yeni/düzenlenmiş mesaj,
topic'e sınırlı polling, motorun ikinci kontrolü ve gereksiz bildirim oluşmamasını
kapsar. Önceki 10x isolated, demo HTTP başlığı, parser, risk, emergency ve iki
aşamalı yönetici onayı testleri de tüm test koşusunda yeniden geçti.

Yerel tekrar doğrulama:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests scripts
.\.venv\Scripts\python.exe -m ruff format --check src tests scripts
$env:PATH = (Resolve-Path '.\.venv\Scripts').Path + ';' + $env:PATH
.\.venv\Scripts\python.exe scripts/check_postgres.py
```

## Değişen dosyalar

- `.env`, `.env.example`: doğrulanan topic ayarı ve güvenli varsayılan.
- `src/okx_bot/config.py`, `domain.py`: topic ayarı ve taşınan mesajın topic kimliği.
- `src/okx_bot/telegram/topics.py`: ortak başlık çözümleyicisi ve salt okunur metadata sorgusu.
- `src/okx_bot/telegram/listener.py`: metadata doğrulama, mesaj/düzenleme filtresi ve topic taraması.
- `src/okx_bot/engine.py`: ikinci topic kontrolü ve kaynak metaverisi.
- `src/okx_bot/telegram/diagnostics.py`, `scripts/telegram_diagnose.py`: belirli mesajları karşılaştırma.
- `tests/test_topics.py`: filtre ve canlı örnek başlıkların regresyon testleri.
- Mevcut test yardımcıları ve giriş testleri: mesajları açık topic bilgisiyle oluşturma.
- `README.md`, bu belge: ayar, operasyon ve doğrulama açıklamaları.

Kaynaklar:
[Telegram forum API](https://core.telegram.org/api/forum),
[Telegram MessageReplyHeader](https://core.telegram.org/constructor/messageReplyHeader).
