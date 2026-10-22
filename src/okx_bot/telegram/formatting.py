"""Turkish presentation only. Never expose raw provider errors or JSON to Telegram."""

from decimal import Decimal, InvalidOperation

REASONS = {
    "CLOSURE_UNCONFIRMED": (
        "Çıkış fill kayıtları henüz tamamlanmadı; kapanış onaylanmadı ve slot kilitli."
    ),
    "ACCOUNT_MISMATCH": "Veritabanı ile OKX hesap kimliği uyuşmuyor; işlem kapısı kilitlendi.",
    "UNKNOWN_OWNERSHIP": (
        "Pozisyon veya emir sahipliği kesin doğrulanamadı; otomatik müdahale durduruldu."
    ),
    "PORTFOLIO_RISK_LIMIT": "Toplam tahmini açık stop riski portföy limitini aşıyor.",
    "ALLOCATION_NOT_FLAT": (
        "Bu değişiklik için tüm pozisyon ve bekleyen emirlerin kapanması gerekiyor."
    ),
    "ENVIRONMENT_MISMATCH": "Veritabanının Demo/Live ortamı uyuşmuyor.",
    "INVALID_SETTING": "Ayar değeri izin verilen güvenli sınırlar dışında.",
    "SETTINGS_CHANGED": "Ayarlar veya hesap durumu değişti; yeni bir onay isteyin.",
    "CONFIRMATION_REQUIRED": "Geçerli, süreli yönetici onayı gerekiyor.",
    "SLIPPAGE_LIMIT": "Gerçek emir kayması izin verilen sınırı aşıyor.",
    "ACCOUNTING_INCOMPLETE": "Gerçekleşmiş işlem muhasebesi henüz doğrulanamadı.",
    "NO_TEXT": (
        "Mesajda okunabilir metin veya medya açıklaması bulunmuyor; görsel içeriği analiz edilmedi."
    ),
    "MISSING_SYMBOL": "USDT işlem çifti belirlenemedi.",
    "AMBIGUOUS_SYMBOL": "Mesajda birden fazla USDT işlem çifti var.",
    "MISSING_DIRECTION": "İşlem yönü kesin olarak belirlenemedi.",
    "AMBIGUOUS_DIRECTION": "Mesajda birbiriyle çelişen LONG ve SHORT yönleri var.",
    "INVALID_ENTRY_OR_RANGE": "Referans fiyat veya ortalama aralık geçersiz.",
    "MISSING_PRICE_OR_RANGE": "Anlık fiyat veya ortalama aralık yüzdesi eksik.",
    "MISSING_LEVELS": "Ortalama aralık veya TP/SL seviyeleri eksik.",
    "UNSUPPORTED_FIELD": "Fiyat, aralık veya hedef bilgisi güvenli biçimde çözümlenemedi.",
    "UNSUPPORTED_TARGETS": "Birden fazla veya numaralı hedef için yönetici incelemesi gerekiyor.",
    "CONFLICTING_LEVELS": "TP/SL seviyeleri işlem yönü veya giriş fiyatıyla uyuşmuyor.",
    "INVALID_SIGNAL_TIME": "Sinyalin tarih ve saat bilgisi geçersiz.",
    "CHANNEL_NOT_ALLOWED": "Bu kanal için özel sinyal yorumlama izni bulunmuyor.",
    "STALE_OR_FUTURE_SIGNAL": "Sinyalin süresi dolmuş veya zamanı gelecekte görünüyor.",
    "TRADING_DISABLED": "Yeni işlemler durdurulmuş veya güvenlik kontrolleri tamamlanmamış.",
    "STOP_BEYOND_LIQUIDATION": (
        "Stop-loss seviyesi tahmini likidasyon fiyatının ötesinde kalıyor veya likidasyon "
        "verisi yok."
    ),
    "DAILY_LOSS_LIMIT": (
        "Günlük zarar limitine ulaşıldı; yeni işlemler bir sonraki Türkiye gününe kadar kapalı."
    ),
    "ENTRY_DEVIATION": "Piyasa veya işaret fiyatı izin verilen fiyat sapmasını aşıyor.",
    "INSUFFICIENT_BALANCE": "Kullanılabilir USDT bakiyesi veya hesap özkaynağı yetersiz.",
    "MAX_OPEN_POSITIONS": "İzin verilen açık pozisyon sayısına ulaşıldı.",
    "INSTRUMENT_UNAVAILABLE": (
        "Coin, bu OKX Demo hesabında USDT sürekli vadeli işlemler için uygun değil."
    ),
    "EXISTING_EXPOSURE": "Bu coin için zaten açık bir pozisyon veya bekleyen emir var.",
    "UNSAFE_LEVELS": "Yuvarlanan TP/SL seviyeleri veya emir fiyatı piyasa fiyatıyla uyuşmuyor.",
    "CONTRACT_LIMIT": "Hesaplanan kontrat miktarı piyasanın işlem sınırlarına uymuyor.",
    "LEVERAGE_UNAVAILABLE": "OKX, 10x izole marjin ayarını doğrulamadı.",
    "UNSAFE_MARGIN_DATA": "Güvenli likidasyon hesabı için gerekli marjin verileri uygun değil.",
    "INVALID_EXCHANGE_DATA": "OKX verileri eksik veya geçersiz; güvenlik kontrolü tamamlanamadı.",
    "UNSUPPORTED_ACCOUNT_MODE": "OKX Demo hesabı, vadeli işlemler hesap modunda olmalı.",
    "UNSUPPORTED_POSITION_MODE": "OKX hesabının pozisyon modu desteklenmiyor.",
    "STALE_MARKET_DATA": "OKX fiyat verileri güncel değil veya saatler uyuşmuyor.",
    "51008": "OKX, emir için yeterli kullanılabilir bakiye olmadığını bildirdi.",
    "TRANSPORT_OR_RESPONSE": "OKX bağlantısı veya yanıtı doğrulanamadı.",
    "RETRIES_EXHAUSTED": "OKX bağlantısı yeniden denemelerden sonra kurulamadı.",
    "DEMO_INSTRUMENT_MISSING": (
        "Coin, bu OKX Demo hesabının erişilebilir Futures listesinde bulunamadı."
    ),
    "INSTRUMENT_SUSPENDED": "Sözleşme mevcut ancak bu Demo hesabında şu anda işlem yapılamıyor.",
    "INVALID_SYMBOL": "Sembol, geçerli bir USDT sürekli vadeli sözleşmeye eşleştirilemedi.",
    "INVALID_QUOTE": "OKX alış/satış fiyatları geçersiz veya birbiriyle çelişiyor.",
    "SPREAD_LIMIT": "Alış ve satış fiyatı arasındaki fark izin verilen sınırı aşıyor.",
    "FILL_RISK_EXCEEDED": "Gerçekleşen fiyatla hesaplanan stop riski hesap limitini aşıyor.",
    "FILL_PRICE_MISMATCH": "Emir gerçekleşme fiyatı ile pozisyon ortalaması uyuşmuyor.",
    "PROTECTION_UNCONFIRMED": "Gerçekleşen fiyata göre güncellenen TP/SL doğrulanamadı.",
    "HTTP_401": "OKX Demo API yetkilendirmesi başarısız; API izinlerini kontrol edin.",
}

# Backward compatibility for already queued events and unchanged safety exceptions.
LEGACY_REASONS = {
    "Exactly one USDT symbol and explicit LONG/SHORT are required": (
        "İşlem çifti veya işlem yönü kesin olarak belirlenemedi."
    ),
    "Stop is beyond estimated liquidation, or liquidation data is absent": REASONS[
        "STOP_BEYOND_LIQUIDATION"
    ],
    "Trading paused or reconciliation unavailable": REASONS["TRADING_DISABLED"],
    "Signal expired or future-dated": REASONS["STALE_OR_FUTURE_SIGNAL"],
    "Unknown contract": REASONS["INSTRUMENT_UNAVAILABLE"],
    "No active bot-owned order for this instrument": (
        "Bu coin için botun yönettiği aktif bir işlem bulunmuyor."
    ),
    "Explicit administrator confirmation is required; request /resume first": (
        "Yönetici onayı gerekiyor; önce /resume komutunu gönderin."
    ),
    "Confirmation expired or safety state changed; request /resume again": (
        "Onayın süresi doldu veya güvenlik durumu değişti; yeniden /resume gönderin."
    ),
    "Account snapshot changed; request /resume again": (
        "Hesap durumu değişti; yeniden /resume gönderin."
    ),
    "Safety state changed during validation; request /resume again": (
        "Kontrol sırasında güvenlik durumu değişti; yeniden /resume gönderin."
    ),
    "Safety state changed or confirmation expired during validation; request /resume again": (
        "Kontrol sırasında onay geçersiz oldu; yeniden /resume gönderin."
    ),
    "A successful recent reconciliation is required before resuming": (
        "Devam etmeden önce güncel hesap ve emir kontrolleri başarıyla tamamlanmalı."
    ),
    "Required background task stopped; restart the service": (
        "Gerekli bir sistem görevi durdu; servisin yeniden başlatılması gerekiyor."
    ),
    "An unresolved order requires manual intervention": (
        "Durumu doğrulanamayan bir emir için yönetici müdahalesi gerekiyor."
    ),
    "Unmanaged exchange exposure remains": (
        "Botun yönetmediği açık pozisyon var; önce hesabı inceleyin."
    ),
    "Pending exchange orders remain": "Hesapta bekleyen emirler var; önce bunları inceleyin.",
    "Unmanaged pending algo orders remain": "Botun yönetmediği bekleyen koşullu emirler var.",
    "Positive USDT equity and available balance are required": REASONS["INSUFFICIENT_BALANCE"],
    "LIVE_TRADING_DISABLED": "Yalnızca OKX Demo işlemlerine izin veriliyor.",
}


def reason_tr(code=None, reason=None) -> str:
    return (
        REASONS.get(code)
        or LEGACY_REASONS.get(reason)
        or ("Güvenlik kontrolü tamamlanamadı. Yönetici tanı kayıtlarını incelemeli.")
    )


def number(value, *, comma=False) -> str:
    if value is None:
        return "Hesaplanamadı"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        result = format(amount, "f")
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return result.replace(".", ",") if comma else result
    except (InvalidOperation, ValueError):
        return "Bilgi yok"


def coin(value) -> str:
    if not value:
        return "Belirlenemedi"
    return str(value).replace("-USDT-SWAP", "/USDT").replace("USDT", "/USDT").replace("//", "/")


def direction(value) -> str:
    return {"long": "LONG", "short": "SHORT", "net": "Net"}.get(value, "Belirlenemedi")


def signal_lines(details) -> list[str]:
    lines = [
        f"🪙 Coin: {coin(details.get('symbol') or details.get('coin') or details.get('inst_id'))}"
    ]
    if details.get("direction"):
        candidate = " adayı" if details.get("interpretation_rule") else ""
        lines.append(f"Sinyal: {direction(details['direction'])}{candidate}")
    if details.get("entry") is not None:
        lines.append(f"Referans fiyat: {number(details['entry'])} USDT")
        if details.get("range_percent") is not None:
            lines.append(f"Ortalama aralık: %{number(details['range_percent'], comma=True)}")
        lines += [
            "Kaldıraç: 10x (İzole marjin / Isolated)",
            f"Kâr alma (TP): {number(details.get('take_profit'))} USDT",
            f"Zarar durdurma (SL): {number(details.get('stop_loss'))} USDT",
        ]
        if details.get("interpretation_rule"):
            source = (
                "dirençten dönüş"
                if "resistance" in details["interpretation_rule"]
                else "destekten dönüş"
            )
            lines.append(f"Yön, MZTRADINGBOT kanalına özel {source} kuralıyla yorumlandı.")
    elif details.get("take_profit") is not None and details.get("stop_loss") is not None:
        lines += [
            f"Kâr alma (TP): {number(details['take_profit'])} USDT",
            f"Zarar durdurma (SL): {number(details['stop_loss'])} USDT",
        ]
    return lines


def format_notification(kind: str, details: dict) -> str:
    lines = signal_lines(details)
    why = reason_tr(details.get("reason_code") or details.get("code"), details.get("reason"))
    if kind in {"rejected", "risk_rejected"} and details.get("reason_code") == "ENTRY_DEVIATION":
        return "⚠️ İŞLEM AÇILMADI\n\n" + "\n".join(
            [
                f"🪙 Coin: {coin(details.get('coin') or details.get('symbol'))}",
                f"Sinyal fiyatı: {number(details.get('reference_price'))} USDT",
                f"Güncel giriş fiyatı: {number(details.get('market_price'))} USDT",
                f"İşaret fiyatı: {number(details.get('mark_price'))} USDT",
                f"Fiyat farkı: %{number(details.get('price_deviation_percent'), comma=True)}",
                "İzin verilen maksimum sapma: %"
                + number(details.get("max_deviation_percent"), comma=True),
                f"Neden: {why}",
                "Otomatik işlem açılmadı.",
            ]
        )
    match kind:
        case "manual_required":
            calculated = [
                line.replace("Kâr alma (TP):", "Hesaplanan TP:").replace(
                    "Zarar durdurma (SL):", "Hesaplanan SL:"
                )
                for line in lines
            ]
            return "⚠️ MANUEL İŞLEM GEREKLİ\n\n" + "\n".join(
                calculated
                + [
                    f"Sinyal zamanı: {details.get('timestamp', 'Bilgi yok')}",
                    f"Durum: {why}",
                    "Otomatik işlem açılmadı. Seviyeler manuel değerlendirme içindir; "
                    "risk uygunluğu doğrulanmadı.",
                ]
            )
        case "order_submitted":
            return "📤 DEMO EMİR GÖNDERİLDİ\n\n" + "\n".join(
                lines + ["OKX emri aldı. Gerçekleşme ve koruyucu emir doğrulaması bekleniyor."]
            )
        case "entry_filled":
            return "📊 DEMO EMİR GERÇEKLEŞTİ\n\n" + "\n".join(
                lines
                + [
                    f"Gerçekleşen fiyat: {number(details.get('fill_price'))} USDT",
                    "Koruyucu emir doğrulaması bekleniyor.",
                ]
            )
        case "entry_partial":
            return "⚠️ EMİR KISMEN GERÇEKLEŞTİ\n\n" + "\n".join(
                lines
                + [
                    f"Gerçekleşen miktar: {number(details.get('contracts'))} kontrat",
                    "İşlem açık kabul edilmedi. Kalan emir iptali ve kontrollü kapatma "
                    "doğrulanıyor.",
                ]
            )
        case "signal_edit_review":
            return "🔎 AÇIK İŞLEMİN SİNYALİ DEĞİŞTİRİLDİ\n\n" + "\n".join(
                lines
                + [
                    "Yeni emir açılmadı. Mevcut pozisyon ve TP/SL otomatik değiştirilmedi.",
                    "Kaynak mesajı ve mevcut pozisyonu manuel inceleyin.",
                ]
            )
        case "signal_received":
            return "📥 YENİ SİNYAL ALINDI\n\n" + "\n".join(
                lines + ["Telegram mesajı analiz ediliyor."]
            )
        case "signal_parsed":
            return "📥 YENİ SİNYAL ALINDI\n\n" + "\n".join(
                lines + ["Durum: Sinyal analiz edildi; risk kontrolüne alındı."]
            )
        case "needs_review":
            return "🔎 SİNYAL İNCELEME GEREKTİRİYOR\n\n" + "\n".join(
                lines + [f"Neden: {why}", "İşlem açılmadı."]
            )
        case "rejected" | "risk_rejected":
            return "⚠️ SİNYAL REDDEDİLDİ\n\n" + "\n".join(
                lines + [f"Neden: {why}", "🚫 İşlem açılmadı."]
            )
        case "failed" | "order_rejected" | "entry_canceled":
            message = "Emir gerçekleşmeden iptal edildi." if kind == "entry_canceled" else why
            return "⚠️ DEMO İŞLEM AÇILAMADI\n\n" + "\n".join(
                lines + [f"Neden: {message}", "İşlem açılmadı."]
            )
        case "order_opened" | "position_opened":
            return "✅ DEMO İŞLEM AÇILDI\n\n" + "\n".join(
                [
                    f"🪙 Coin: {coin(details.get('inst_id'))}",
                    f"Yön: {direction(details.get('direction'))}",
                    "Kaldıraç: 10x (İzole marjin / Isolated)",
                    f"Referans fiyat: {number(details.get('reference_price'))} USDT",
                    f"Gerçekleşen giriş fiyatı: {number(details.get('fill_price'))} USDT",
                    f"Fiyat sapması: %{number(details.get('price_deviation_percent'), comma=True)}",
                    f"Kâr alma (TP): {number(details.get('take_profit'))} USDT",
                    f"Zarar durdurma (SL): {number(details.get('stop_loss'))} USDT",
                    f"Pozisyon büyüklüğü: {number(details.get('contracts'))} kontrat",
                    f"Kullanılan teminat: {number(details.get('actual_margin'))} USDT",
                    f"Nominal pozisyon: {number(details.get('notional'))} USDT",
                    "Tahmini SL zararı (ücret rezervi dahil): "
                    f"{number(details.get('estimated_sl_loss'))} USDT",
                    "SL zararı / teminat: "
                    f"%{number(details.get('risk_margin_percent'), comma=True)}",
                    "SL zararı / özkaynak: "
                    f"%{number(details.get('risk_equity_percent'), comma=True)}",
                    "Toplam açık risk / özkaynak: "
                    f"%{number(details.get('portfolio_risk_percent'), comma=True)}",
                    *(
                        ["⚠️ Olağandışı geniş SL mesafesi; kanal seviyeleri korundu."]
                        if details.get("unusual_stop")
                        else []
                    ),
                    "Durum: Koruyucu emirler doğrulandı.",
                    f"İşlem zamanı: {details.get('trade_time', 'Bilgi yok')}",
                ]
            )
        case "tpsl_placed":
            return "🛡️ TP/SL EMİRLERİ DOĞRULANDI\n\n" + "\n".join(lines)
        case "position_closed" | "tp_executed" | "sl_executed":
            cause = details.get("cause")
            title = (
                "🎯 KÂR ALMA (TP) GERÇEKLEŞTİ"
                if cause == "tp" or kind == "tp_executed"
                else "🔴 ZARAR DURDURMA (SL) GERÇEKLEŞTİ"
                if cause == "sl" or kind == "sl_executed"
                else "✅ POZİSYON KAPANDI"
            )
            return (
                title
                + "\n\n"
                + "\n".join(
                    lines
                    + [
                        "Kaldıraç: 10x Isolated",
                        f"Kapanış nedeni: {close_cause(cause)}",
                        f"Kullanılan teminat: {number(details.get('actual_margin'))} USDT",
                        f"Nominal pozisyon: {number(details.get('notional'))} USDT",
                        f"Giriş fiyatı: {number(details.get('entry_price'))}",
                        f"Çıkış fiyatı: {number(details.get('exit_price'))}",
                        f"Brüt PnL: {number(details.get('gross_pnl'))} USDT",
                        f"Komisyon (işaretli): {number(details.get('fees'))} USDT",
                        f"Fonlama (işaretli): {number(details.get('funding'))} USDT",
                        f"Net PnL: {number(details.get('net_pnl'))} USDT",
                        f"Bugünkü net bot PnL: {number(details.get('daily_net'))} USDT",
                        f"OKX toplam özkaynak: {number(details.get('equity'))} USDT",
                        "Pozisyonun kapandığı doğrulandı.",
                    ]
                )
            )
        case "daily_report":
            return format_daily(details)
        case "okx_connection_error":
            return (
                "⚠️ OKX BAĞLANTI HATASI\n\n"
                "OKX bağlantısı doğrulanamadı. Hesap ve emir durumunu kontrol edin."
            )
        case "telegram_disconnected" | "telegram_connection_error":
            return (
                "⚠️ TELEGRAM BAĞLANTI HATASI\n\n"
                "Kanal bağlantısı kesildi. Bağlantı yeniden deneniyor."
            )
        case "daily_risk_block":
            return "🛑 GÜNLÜK ZARAR LİMİTİ\n\n" + REASONS["DAILY_LOSS_LIMIT"]
        case "emergency" | "manual_intervention_required":
            return (
                "🚨 ACİL DURUM KİLİDİ AKTİF\n\nYeni işlemler durduruldu. "
                "Açık emir ve pozisyonları inceleyin.\n" + f"Neden: {why}\n"
                "Devam etmek için güvenlik kontrolleri ve /resume CONFIRM yönetici onayı gerekiyor."
            )
        case "paused":
            return (
                "⏸ BOT DURDURULDU\n\nYeni demo işlemler durduruldu. "
                "Açık pozisyonların güvenlik takibi sürüyor."
            )
        case "resumed":
            return (
                "▶️ BOT YENİDEN BAŞLATILDI\n\nGüvenlik kontrolleri geçti. "
                "Yönetici onayıyla yeni demo işlemler etkin."
            )
        case "reconciliation_failed":
            return (
                "🚨 HESAP KONTROLÜ BAŞARISIZ\n\nHesap, emir veya bağlantı kontrolü tamamlanamadı. "
                "Yeni işlemler kilitlendi; tanı kayıtlarını inceleyin."
            )
        case "reconciliation_recovered":
            return (
                "✅ HESAP BAĞLANTISI DÜZELDİ\n\nHesap ve emir kontrolleri geçti. "
                "İşlemler kilitliyse /resume ile yönetici onayı gerekiyor."
            )
        case "telegram_initial_baseline":
            return (
                "📡 TELEGRAM KANALI HAZIR\n\n"
                "Geçmiş mesajlardan işlem açılmayacak. Yeni mesajlar bekleniyor."
            )
        case "resume_requested":
            return "🔐 DEVAM ONAYI BEKLENİYOR\n\n/resume yanıtındaki süreli onay komutunu kullanın."
        case _:
            return (
                "ℹ️ DEMO BOT BİLDİRİMİ\n\nSistem durumu güncellendi. "
                "Ayrıntılar için /status komutunu kullanın."
            )


COMMAND_TEXT = {
    "expired": "Komutun süresi doldu; yeniden gönderin.",
    "pause": "Yeni işlemler durduruldu.",
    "already_running": "Demo işlemler zaten etkin.",
    "resume_usage": "Kullanım: /resume; ardından /resume CONFIRM <onay-kodu>",
    "resumed": "Kontroller tekrar geçti; yönetici onayıyla demo işlemler etkin.",
    "close_usage": "Kullanım: /close BTC-USDT-SWAP",
    "closed": "Pozisyonun kapandığı doğrulandı.",
    "unknown": "Komut bulunamadı. /start",
    "error": "İşlem uygulanamadı; güvenlik kilidi korunuyor. Tanı kayıtlarını inceleyin.",
}

TRADE_STATUS = {
    "received": "Alındı",
    "needs_review": "İnceleme gerekiyor",
    "rejected": "Reddedildi",
    "prepared": "Hazırlandı",
    "submitting": "Emir sonucu bekleniyor",
    "open": "Açık",
    "closing": "Kapatılıyor",
    "closed": "Kapalı",
    "canceled": "İptal edildi",
    "failed": "Başarısız",
    "emergency": "Acil durum incelemesi gerekiyor",
    "manual_required": "Manuel değerlendirme gerekiyor",
}


def format_command(command: str, value=None) -> str:
    if command in COMMAND_TEXT:
        return COMMAND_TEXT[command]
    match command:
        case "/start":
            return (
                "🤖 OKX DEMO İŞLEM BOTU\n\n"
                "/status — Bot ve bağlantı durumu\n/balance — USDT bakiyesi\n"
                "/positions — Açık pozisyonlar\n/trades — Son işlemler\n"
                "/stats — İşlem istatistikleri\n/pause — Yeni işlemleri durdur\n"
                "/resume — Güvenlik kontrolü ve iki aşamalı devam onayı\n"
                "/close BTC-USDT-SWAP — Botun pozisyonunu kapat\n"
                "/allocation — Sabit sermaye slotları\n/setslots 3 — Bölme sayısı\n"
                "/rebalance — Pozisyonsuzken yeni sermaye döngüsü\n"
                "/pnl — Bugün / 7 gün / 30 gün / tüm zamanlar\n/daily — Günlük rapor\n"
                "/history — Kapanan işlemler\n/limits — Güvenlik limitleri\n"
                "/setdailycap <yüzde> /setriskcap <yüzde> /setreserve <USDT>\n"
                "/setmaxpositions /setspread /setdeviation /setslippage\n"
                "/setliqbuffer /setstopwarning /setautorebalance true|false\n"
                "/confirm <kod> — Ayar onayı\n/emergency — Acil durum kilidi"
            )
        case "/status":
            active = value.get("trading_enabled", False)
            telegram = value.get("telegram", {}).get("ready")
            telegram_state = (
                "Aktif" if telegram else "Bağlı değil" if telegram is False else "Bilgi yok"
            )
            exchange_state = "Sağlıklı" if value.get("exchange_healthy") else "Doğrulanamadı"
            return "\n".join(
                [
                    "🤖 OKX İŞLEM BOTU DURUMU",
                    "",
                    f"Bot: {'Aktif' if active else 'Yeni işlemler kapalı'}",
                    "İşlem modu: Demo",
                    "Canlı işlemler: Kapalı",
                    f"Telegram kanal bağlantısı: {telegram_state}",
                    f"OKX bağlantısı: {exchange_state}",
                    "PostgreSQL: "
                    + (
                        "Bağlı"
                        if value.get("database_healthy", True)
                        else "Erişilemiyor; işlemler kilitli"
                    ),
                    "Son OKX hata nedeni: "
                    + reason_tr(
                        (value.get("reconciliation", {}).get("last_error") or {}).get("code")
                    )
                    if value.get("reconciliation", {}).get("last_error")
                    else "Son OKX hata nedeni: Yok",
                    "Kaldıraç: 10x (İzole marjin / Isolated)",
                    f"Açık pozisyon: {value.get('open_positions', 'Bilgi yok')}",
                    f"Acil durum kilidi: {'Aktif' if value.get('emergency') else 'Kapalı'}",
                    f"Teslimatı belirsiz bildirim: {value.get('uncertain_notifications', 0)}",
                    f"Otomatik işlemler: {'Aktif' if active else 'Kapalı'}",
                ]
            )
        case "/balance":
            return "💰 DEMO HESAP BAKİYESİ\n\n" + "\n".join(
                [
                    f"Özkaynak: {number(value['equity'])} USDT",
                    f"Kullanılabilir bakiye: {number(value['available'])} USDT",
                    f"Kullanılan teminat: {number(value.get('used_margin'))} USDT",
                    f"Bekleyen emir teminatı: {number(value.get('order_frozen'))} USDT",
                    f"Gerçekleşmemiş kâr/zarar: {number(value['unrealized'])} USDT",
                ]
            )
        case "/positions":
            if not value:
                return "📊 AÇIK POZİSYONLAR\n\nAçık pozisyon bulunmuyor."
            blocks = []
            for p in value:
                side = p.get("posSide")
                if side == "net":
                    side = "short" if Decimal(str(p["pos"])) < 0 else "long"
                blocks.append(
                    "\n".join(
                        [
                            f"Coin: {coin(p.get('instId'))} | Yön: {direction(side)}",
                            f"Miktar: {number(abs(Decimal(str(p['pos']))))} kontrat",
                            f"Giriş fiyatı: {number(p.get('avgPx'))} USDT",
                            f"Güncel işaret fiyatı: {number(p.get('markPx'))} USDT",
                            f"TP: {number(p.get('take_profit'))}"
                            f" | SL: {number(p.get('stop_loss'))}",
                            f"Teminat: {number(p.get('actual_margin'))} USDT",
                            f"SL riski / teminat: %{number(p.get('risk_margin_percent'))}",
                            f"SL riski / özkaynak: %{number(p.get('risk_equity_percent'))}",
                            f"Toplam risk / özkaynak: %{number(p.get('portfolio_risk_percent'))}",
                            f"Likidasyon fiyatı: {number(p.get('liqPx'))} USDT",
                            f"Gerçekleşmemiş kâr/zarar: {number(p.get('upl'))} USDT",
                        ]
                    )
                )
            return "📊 AÇIK POZİSYONLAR\n\n" + "\n\n".join(blocks)
        case "/trades":
            if not value:
                return "📋 SON DEMO İŞLEMLER\n\nKayıtlı işlem bulunmuyor."
            return "📋 SON DEMO İŞLEMLER\n\n" + "\n\n".join(
                f"Coin: {coin(t['inst_id'])} | Yön: {direction(t['direction'])}\n"
                f"Gerçekleşen: {number(t['filled'])} / {number(t['contracts'])} kontrat\n"
                f"Durum: {TRADE_STATUS.get(t['status'], 'İnceleme gerekiyor')}"
                for t in value
            )
        case "/stats":
            return (
                f"📈 DEMO İŞLEM İSTATİSTİKLERİ\n\nGerçekleşen kayıt sayısı: {value['executions']}\n"
                f"Gerçekleşen kâr/zarar: {number(value['realized_pnl'])} USDT\n"
                f"USDT ücret toplamı (işaretli): {number(value['usdt_fees'])} USDT"
            )
        case "resume_preview":
            summary = value["summary"]
            return (
                "Güvenlik kontrolleri geçti. Yeni demo işlemlerini açıkça onaylamak "
                "için 120 saniye içinde aşağıdaki komutu gönderin:\n"
                f"/resume CONFIRM {value['confirmation']}\n\n"
                f"Doğrulanan pozisyon: {len(summary['positions'])}\n"
                f"Bekleyen emir: {summary['pending_orders']}\n"
                f"Bekleyen koşullu emir: {summary['pending_algos']}\n"
                f"Günlük zarar hesabı: {number(summary['gross_daily_loss'])} USDT"
            )
        case "rejected":
            return "İşlem uygulanamadı: " + reason_tr(value.code, str(value))
        case "change_preview":
            return (
                f"Ayar: {LIMIT_LABELS.get(value['field'], 'Sermaye döngüsü')}\n"
                f"Önceki: {value.get('old')} → Yeni: {value.get('new')}\n"
                "120 saniye içinde onaylayın:\n/confirm " + value["confirmation"]
            )
        case "/limits":
            return "🛡️ GÜVENLİK LİMİTLERİ\n\n" + "\n".join(
                f"{LIMIT_LABELS[k]}: "
                f"{number(v) if type(v) is not bool else ('Açık' if v else 'Kapalı')}"
                for k, v in value.items()
            )
        case "/allocation":
            if value.get("cycle_id") is None:
                return (
                    "Sermaye döngüsü henüz oluşturulmadı; doğrulanmış pozisyonsuz hesap bekleniyor."
                )
            return "💰 SERMAYE DAĞILIMI\n\n" + "\n".join(
                [
                    f"Döngü: {value['cycle_id']}",
                    f"Başlangıç özkaynağı: {number(value['allocation_base_equity'])} USDT",
                    f"Sabit slot hedefi: {number(value['slot_target_margin'])} USDT",
                    f"Dolu / boş: {value['occupied']} / {value['free']}",
                    *[
                        f"Slot {s['number']}: {coin(s['inst_id']) if s['order_id'] else 'Boş'}"
                        f" | Teminat: {number(s['actual_margin'])}"
                        for s in value["slots"]
                    ],
                ]
            )
        case "/pnl":
            labels = {
                "today": "Bugün",
                "7_days": "Son 7 gün",
                "30_days": "Son 30 gün",
                "all_time": "Tüm zamanlar",
            }
            return "📈 BOT NET PNL (Europe/Istanbul)\n\n" + "\n".join(
                f"{labels[k]}: {number(v['net_pnl'])} USDT" for k, v in value.items()
            )
        case "/daily":
            return format_daily(value)
        case "/history":
            return "📋 KAPANAN BOT İŞLEMLERİ\n\n" + (
                "\n\n".join(
                    f"#{t['id']} {coin(t['inst_id'])} {direction(t['direction'])}\n"
                    f"{close_cause(t['cause'])} | Net: {number(t['net_pnl'])} USDT\n"
                    f"{t['closed_at']}"
                    for t in value
                )
                or "Kayıtlı işlem bulunmuyor."
            )
        case _:
            return COMMAND_TEXT["unknown"]


LIMIT_LABELS = {
    "allocation_slots": "Sermaye slot sayısı",
    "auto_rebalance": "Pozisyonsuzken otomatik döngü",
    "max_open_positions": "Maksimum açık pozisyon",
    "max_portfolio_risk_percent": "Toplam SL riski / özkaynak (%)",
    "max_daily_loss_percent": "Günlük gerçekleşmiş zarar (%)",
    "minimum_free_margin": "Minimum serbest rezerv (USDT)",
    "max_bid_ask_spread_percent": "Maksimum spread (%)",
    "max_entry_price_deviation_percent": "Maksimum giriş sapması (%)",
    "max_order_slippage_percent": "Maksimum kayma (%)",
    "unusual_stop_distance_percent": "Geniş SL uyarısı (%)",
    "liquidation_buffer_percent": "Likidasyon tamponu (%)",
}


def close_cause(cause):
    return {
        "tp": "Take Profit",
        "sl": "Stop Loss",
        "manual": "Yönetici kapatması",
        "liquidation": "Likidasyon",
    }.get(cause, "Bilinmiyor; neden tahmin edilmedi")


def format_daily(value):
    labels = {
        "opened": "Açılan",
        "closed": "Kapanan",
        "wins": "Kazanan",
        "losses": "Kaybeden",
        "win_rate": "Kazanma oranı (%)",
        "average_win": "Ortalama kâr",
        "average_loss": "Ortalama zarar",
        "average_realized_r": "Ortalama gerçekleşen R (net / başlangıç stop riski)",
        "gross_pnl": "Brüt PnL",
        "fees": "Komisyonlar (işaretli)",
        "funding": "Fonlama (işaretli)",
        "net_pnl": "Net gerçekleşmiş bot PnL",
        "average_margin": "Ortalama işlem teminatı",
        "allocation_base_equity": "Güncel sermaye döngüsü başlangıç özkaynağı",
    }
    lines = [f"📊 GÜNLÜK BOT RAPORU — {value.get('day', '')} (Europe/Istanbul)", ""]
    lines += [f"{label}: {number(value.get(key))}" for key, label in labels.items()]
    balance = value.get("current_balance") or {}
    lines += [
        "",
        "Hesap anlık görüntüsü (rapor anı; geçmiş gün kapanış bakiyesi değildir):",
        f"OKX toplam özkaynak: {number(balance.get('equity'))} USDT",
        f"Kullanılabilir teminat: {number(balance.get('available'))} USDT",
        f"Bot açık pozisyon PnL: {number(value.get('bot_unrealized'))} USDT",
        "Yatırma/çekme ve bot dışı hareketler bot PnL'sine dahil edilmez.",
    ]
    return "\n".join(lines)
