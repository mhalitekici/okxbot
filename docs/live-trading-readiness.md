# Live hazırlığı — bu sürümde kapalı

Güncel [Live deployment rehberi](live-deployment-guide.md), ayrı Compose/DB ve
salt okunur sürekli gözlemciyi kapsar. Gözlemci hazırdır; Live executor yoktur ve
gerçek emir kapıları açılmamıştır. Aşağıdaki önceki aktivasyon şartları devam eder.

Bağımsız [salt okunur Live bağlantı aracı](live-read-only-connection.md) eklendi.
Bu aracın ayrı yapılandırması Live GET erişimine izin verir; aşağıdaki Demo trading
servisinin kilitleri değişmez. [Önceki Live Docker planı](live-docker-deployment-plan.md)
tarihsel taslaktır; güncel kaynaklar deployment rehberindedir. Bu geliştirmede
deployment veya gerçek bağlantı testi çalıştırılmadı.

`OKX_DEMO=1` ve `LIVE_TRADING_ENABLED=false`, Pydantic literal'leri ve HTTP istemcisindeki ikinci kapıyla zorunludur. `/resume` yalnızca Demo akışını açabilir. Telegram üzerinden live etkinleştirme komutu yoktur. Model doğrulaması atlanarak oluşturulmuş ayarlarda da HTTP katmanı live isteği göndermez. İzinli HTTPS REST hostları allowlist ile sınırlıdır; redirect/proxy devralma kapalıdır.

Mevcut `OKX_API_KEY/SECRET/PASSPHRASE` isimleri Demo uyumluluğu için korunmuştur. İsteğe bağlı üç `OKX_DEMO_API_*` alanı birlikte tanımlanırsa yalnızca Demo credential kaynağını değiştirir. Live credential alanları bilerek kabul edilmez. Mevcut `.env`, anahtarlar ve Telegram session bu geliştirmede değiştirilmemiştir. Her veritabanı Demo ortamına ve ilk doğrulanan OKX UID'nin hash'ine bağlanır; farklı hesaba geçiş kilitlenir. Gerçek UID veya credential raporlara yazılmaz.

Gelecekte live için ayrı bir sürüm ve açık yetkilendirme gerekir. Ayrı live credential deposu, ayrı PostgreSQL veritabanı, ayrı Compose project/volumes ve yalnızca bota ayrılmış OKX trading sub-account kullanılmalıdır. Ortak net pozisyonda client order ID tek başına izolasyon kanıtı değildir. `DEDICATED_TRADING_ACCOUNT_CONFIRMED=true` bir operatör teyididir; borsanın diğer kullanıcı erişimlerini teknik olarak engellemez ve live kapısını açmaz.

Operatörün ayrı aşamada tamamlayacağı kontroller:

1. Ayrı sub-account üzerinde yalnızca Read + Trade izinli, Withdrawal izni olmayan API erişimi ve sabit çıkış IP allowlist'i.
2. Bölgeye uygun REST hostu, Futures hesap modu (`acctLv=2`), 10x isolated ve pozisyon modu doğrulaması. Bot hesap/pozisyon modunu kendiliğinden değiştirmez.
3. Ayrı Demo hesabında açık izinle gerçek IOC, kısmi fill, attached TP/SL, fill sonrası amend, child order ID, fee/funding ve net/reduceOnly davranışı testi.
4. Uygulama kapalıyken koruyucu emirlerin OKX'te kaldığını ve tetiklenebildiğini bağımsız borsa ekranından/API'den doğrulama.
5. Ayrı laptopta Docker/Windows restart, internet/DB kesintisi, backup ve restore provası.
6. Gelecek live sürümü için ayrıca kimlik, hesap sahipliği, API izinleri ve snapshot'a bağlı iki aşamalı aktivasyon tasarımı ve incelemesi. Bu sürümün hard gate'ini `.env` değiştirerek aşmaya çalışmayın.

Demo performansı live getirisi veya strateji kârlılığı kanıtı değildir. Spread, kayma, stop boşlukları, fee/funding gecikmeleri ve manuel hesap müdahaleleri ölçülmelidir. Bu geliştirme sırasında Demo veya gerçek para emri gönderilmemiştir; dış hesap üstünde emir testi için ayrıca açık kullanıcı izni gerekir.

API sözleşmeleri için [OKX V5 resmi belgeleri](https://www.okx.com/docs-v5/en/) esas alındı. Uygulama REST polling kullanır. İleride WebSocket eklenirse global Demo `wss://wspap.okx.com/ws/v5/private`, live `wss://ws.okx.com/ws/v5/private` adreslerinde 443 kullanılmalıdır; bölgesel hostlar ayrıca doğrulanmalıdır. [OKX'in 8443 kapatma duyurusu](https://www.okx.com/en-eu/help/okx-websocket-port-8443-discontinuation-announcement) genel internet için 31 Ekim 2026 tarihini bildiriyor; mevcut REST akışı bundan etkilenmez.
