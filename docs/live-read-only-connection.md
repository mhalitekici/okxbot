# Salt okunur OKX Live kontrolü

Bu araç 3 slotlu Demo servisinden bağımsızdır. FastAPI, Telegram, emir motoru,
reconciliation, PostgreSQL, migration veya Docker başlatmaz. Live emir yeteneği
eklenmemiştir; servisin `OKX_DEMO=1` ve `LIVE_TRADING_ENABLED=false` kilitleri korunur.

## Kullanıcı terminalinde çalıştırma

Proje dizininde Python 3.12+ ve proje bağımlılıkları kurulu ortamı etkinleştirin:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/check_live_connection.py --env-file "$env:LOCALAPPDATA\OKXTrading\secrets\live.env"
```

Activation policy izin vermiyorsa aynı aracın doğrudan venv komutu:

```powershell
.\.venv\Scripts\python.exe scripts/check_live_connection.py --env-file "$env:LOCALAPPDATA\OKXTrading\secrets\live.env"
```

Bu geliştirmede bu dosyaya erişilmedi ve gerçek bağlantı komutu çalıştırılmadı.
Dosya yalnızca kullanıcının çalıştırdığı süreçte, salt okunur açılır. Araç dosya
oluşturmaz/değiştirmez, değerleri environment'a kopyalamaz, rapor dosyası yazmaz.

## Yapılandırma yükleme sırası

1. CLI, bu proje dizininin `src` kodunu başka kurulumların önüne alır. Farklı
   `PYTHONPATH`/kurulu paket kopyası tanı kodunu değiştirmemelidir.
2. Yalnız `--env-file` ile açıkça belirtilen dosya bir kez açılır. Proje `.env`
   dosyası aranmaz; `load_dotenv`, uygulamanın `Settings` sınıfı ve süreçteki OKX
   environment değişkenleri kullanılmaz. Dosya dışında override/fallback yoktur.
3. Dosyadaki ortam işaretleri ve credential grupları doğrulanır. `OKX_DEMO=0` ve
   `LIVE_TRADING_ENABLED=false` salt okunur Live kontrolü için uyumludur. Yalnız
   gizli olmayan işaretlerde baş/son boşluklar temizlenir; credential değerleri
   değiştirilmez. Tırnaklı `" 0 "`/`" false "` işaretleri de kabul edilir.
4. Doğrulama tamamlandıktan sonra yalnız GET yetenekli Live istemcisi oluşturulur.
   Bu işlem trading engine'i veya gerçek emir kapısını açmaz.

`ENVIRONMENT` hatası artık hangi sabit koşulun uyuşmadığını belirtir:
`OKX_DEMO`, `OKX_ENVIRONMENT`, seçilen dosyadaki dolu `OKX_DEMO_API_*` alanları veya
eksik Live ortam beyanı. Yalnız alan isimleri gösterilir; değerler ve dosya yolu
gösterilmez. Demo `.env` dosyasını veya emir kilitlerini değiştirmeyin.

## Kabul edilen ayrı Live yapılandırması

Aşağıdaki şema **temsili alan isimlerini** gösterir; gerçek dosyanız okunmamıştır.
Anahtarları terminale, kaynak koduna veya sohbet içine yapıştırmayın.

```dotenv
OKX_LIVE_API_KEY=<Live anahtarı>
OKX_LIVE_API_SECRET=<Live secret>
OKX_LIVE_API_PASSPHRASE=<Live passphrase>
OKX_BASE_URL=https://eea.okx.com
OKX_DEMO=0
LIVE_TRADING_ENABLED=false
# İsteğe bağlı, yalnız 2 desteklenir:
OKX_EXPECTED_ACCOUNT_MODE=2
# İsteğe bağlı: net_mode veya long_short_mode
OKX_EXPECTED_POSITION_MODE=net_mode
```

`OKX_LIVE_BASE_URL`, `OKX_BASE_URL` için eşdeğer isimdir. Aynı anda verilen eşdeğer
alanlar aynı olmalıdır. `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`
isimleri yalnız bu açıkça seçilen dosyada ve `OKX_DEMO=0/false` veya
`OKX_ENVIRONMENT=live/production` ile kullanılabilir. `OKX_LIVE_API_*` isimleri
kullanılırsa üçü birlikte verilmelidir. İki isim kümesi çelişemez. Demo anahtarları
asla yedek kaynak değildir; dolu `OKX_DEMO_API_*` alanları reddedilir.

Her credential isim kümesi kendi içinde tam olmalıdır. Eksik generic alanlar
`OKX_LIVE_API_*` grubuyla tamamlanmaz; iki küme birlikte tam verilirse değerleri
aynı olmalıdır. Eksiklik veya çakışma `CREDENTIALS` ve sabit güvenli açıklamayla
reddedilir. Süreç environment'ındaki eksik tamamlayıcı değerler dikkate alınmaz.

`LIVE_TRADING_ENABLED` verilmezse `false` kabul edilir; `true` reddedilir.
Diğer desteklenen alanlar yukarıdakilerle sınırlıdır. Bilinmeyen veya yinelenen
alanlar, eksik değerler ve bozuk dotenv satırları güvenli hatayla reddedilir.
UTF-8/BOM, yorumlar ve tırnaklı değerler desteklenir. Değişken genişletme yapılmaz;
`${...}` credential yerine kullanılamaz. Projedeki `.env` ve süreç environment'ı
credential/yapılandırma kaynağı olarak okunmaz.

## Bölgesel adres ve güvenlik sınırı

Adres dosyada **açıkça** belirtilmelidir; bölge tahmin edilmez veya otomatik denenmez:

| Hesap kayıt bölgesi | İzin verilen REST adresi |
| --- | --- |
| Global | `https://openapi.okx.com` veya `https://www.okx.com` |
| EEA / my.okx.com | `https://eea.okx.com` |
| US ve AU / app.okx.com | `https://us.okx.com` |

Hesabın kayıt bölgesini esas alın; Windows konumu veya dilini değil. Global güncel
adres ve bölgesel gereklilikler [OKX API genel bakışında](https://www.okx.com/docs-v5/)
belirtilir; `www.okx.com` uyumluluğu [OKX'in site tablosunda](https://github.com/okx/agent-trade-kit/blob/github-main/docs/site-compatibility.md)
yer alır. URL'ye port, path, query, kullanıcı bilgisi veya son `/` eklenmez.

TLS doğrulaması açıktır. Redirect ve environment proxy devralma kapalıdır. Live
isteklerinde `x-simulated-trading` header'ı hiç gönderilmez. Demo header'ı, başka
host, gövde veya aşağıdaki liste dışındaki method/path/parametre HTTP sınırında
da reddedilir. Tüm non-GET methodlar engellenir. Hatalar otomatik yeniden denenmez;
başka ortama veya domaine geçilmez. İstekler sırayla ve hız sınırlı gönderilir.

| GET ucu | Kullanım |
| --- | --- |
| `/api/v5/account/config` | Kimlik doğrulama, hesap ve pozisyon modu |
| `/api/v5/account/balance` | Trading hesabı özkaynağı ve USDT kullanılabilir bakiye |
| `/api/v5/account/positions` | Sıfır olmayan pozisyonların sayısı, tüm ürün türleri |
| `/api/v5/trade/orders-pending` | Tüm ürün türlerinde bekleyen emir sayısı |
| `/api/v5/trade/orders-algo-pending` | Ayrı `conditional` ve `oco` sorguları |

Pending listeleri `limit=100`, gerekiyorsa `after` ile sayfalanır. Yinelenen/geçersiz
ID veya 100 sayfa sınırı eksik sayıyı başarı olarak raporlamaz. Kimlikler yalnız
bellekte sayfalama için kullanılır, çıktıya yazılmaz.

Toplam özkaynak `totalEq` alanından **USD**, kullanılabilir bakiye USDT detayındaki
`availBal` alanından **USDT** olarak gösterilir; farklı para birimleri toplanmaz.
USDT satırı yoksa kullanılabilir USDT sıfırdır. `totalEq=0`, boş `details` ve boş
pozisyon/emir listeleri normaldir. Eksik/bozuk cevaplar sıfırmış gibi kabul edilmez.
Bot Futures modu (`acctLv=2`) bekler; uyuşmazlık ayar değiştirilmeden `FAILED`
sonucuna dönüşür. Net ve long/short desteklenir; isteğe bağlı beklenen pozisyon
modu uyuşmazlığı da reddedilir.

TP/SL sayısı bekleyen **conditional + OCO algo kayıtlarıdır**; OCO'nun iki bacağı
tek algo sayılır. Trailing stop, trigger, iceberg/TWAP ve henüz aktifleşmemiş parent
emrine bağlı TP/SL tanımları bu sayıya dahil değildir. Kontrol bir koruma yeterlilik
denetimi veya atomik hesap snapshot'ı değildir. Alanlar farklı GET anlarını yansıtır.

Başarı yalnızca seçilen anahtarın Live GET erişimini ve beklenen modları doğrular.
Bu salt okunur uçlar `TelegramBot1` adını bağımsız doğrulamaz; anahtarın bu alt hesaba
ait olduğunu OKX ekranında kullanıcı doğrulamalıdır. Trade/Withdrawal yetkileri
denenmez; bu kontrol için Read izni yeterlidir.

## Çıktı ve hata davranışı

Başarıda yalnız dokuz özet satırı, hatada ayrıca HTTP kodu ve sabit güvenli açıklama
gösterilir. Tamamlanmamış alanlar `N/A` kalır; işlem kapısı daima `DISABLED` yazar.
Çıkış kodu başarıda `0`, başarısızlıkta `1` olur. HTTP yanıtı alınmadıysa `HTTP: N/A`
gösterilir. Başarısız kontrolde önceden doğrulanan alanlar gösterilebilir.

`50101` ortam, `50110` IP izni, `50111` anahtar, `50119` bölge/anahtar, `50102`
saat uyumsuzluğunu güvenli açıklamayla bildirir. [OKX API SSS](https://www.okx.com/en-us/help/api-faq)
ortam ayrımını, [bölgesel SSS](https://www.okx.com/en-us/help/api-faq-eea) `50119`
durumunu açıklar. Ham `msg`, response body, credential, imza, UID/mainUid, dosya
yolu veya traceback basılmaz. Bilinmeyen hata kodları da ham olarak yansıtılmaz.

Sürekli salt okunur gözlemci için [Live deployment rehberi](live-deployment-guide.md)
hazırdır; [önceki Docker planı](live-docker-deployment-plan.md) geçmiş tasarımı kaydeder.

Yapılandırma izolasyonu düzeltmesi ve güncel test sonuçları:
[Live environment düzeltme raporu](live-env-isolation-fix-report.md).
