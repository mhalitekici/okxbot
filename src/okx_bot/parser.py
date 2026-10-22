import re
import unicodedata
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from okx_bot.domain import Direction, Message, ReviewRequired, Signal

SYMBOL_TOKEN = r"#?[a-z0-9]{1,20}[ \t]*[/_-]?[ \t]*usdt"


def analysis_text(text: str) -> str:
    """Only strip URLs and explicitly labelled legal boilerplate; retain raw text elsewhere."""
    result = []
    for line in normalize(text).splitlines():
        line = re.sub(r"https?://\S+", "", line).strip()
        line = re.sub(r"^[^\w#]+", "", line)
        if re.match(
            r"(?:yasal uyari|hukuki uyari|yatirim tavsiyesi degildir|"
            r"burada yer alan .*yatirim|bu (?:mesaj|paylasim).*yatirim tavsiyesi)",
            line,
        ):
            continue
        result.append(line)
    return "\n".join(result)


def explicit_direction(line: str) -> str | None:
    match = re.fullmatch(
        rf"(?:(?:yon|islem yonu|sinyal)[ \t]*[:=][ \t]*)?"
        rf"(?:{SYMBOL_TOKEN}[ \t]+)?(long|short)(?:[ \t]+{SYMBOL_TOKEN})?[.!]?",
        line,
    )
    return match[1] if match else None


class SignalParser(Protocol):
    def parse(self, message: Message) -> Signal: ...


def normalize(text: str) -> str:
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.casefold().replace("ı", "i"))
        if not unicodedata.combining(c)
    )


def symbol_candidates(text: str) -> list[str]:
    """Only explicit USDT tokens, suitable for metadata; never infer a trading direction."""
    return sorted(
        {
            f"{base.upper()}USDT"
            for base in re.findall(
                r"(?<![a-z0-9])#?([a-z0-9]{1,20})\s*[/_-]?\s*usdt\b",
                normalize(text),
            )
            if any(c.isalpha() for c in base)
        }
    )


class RegexSignalParser:
    """One unambiguous symbol/entry/direction and at most one TP and SL.

    Explicit levels override computed levels individually, but must agree with
    the direction. Multiple targets and ambiguous numbers require human review.
    Message timestamps are authoritative; optional ISO times may only make them older.
    """

    number = r"([0-9]+(?:[.,][0-9]+)?)"
    price_suffix = r"(?:[ \t]+usdt)?"
    labels = {
        "entry": r"(?:fiyat|price|entry|giris)(?:\s+fiyati)?",
        "range": r"(?:ortalama\s+aralik|average\s+range|range)",
        "tp": r"(?:take\s*profit|tp|kar\s+al)",
        "sl": r"(?:stop\s*loss|sl|zarar\s+kes)",
    }

    def direction(self, text: str) -> tuple[Direction, str | None]:
        directions = set()
        for line in text.splitlines():
            value = explicit_direction(line.strip())
            if value:
                directions.add(value)
            elif re.search(r"\b(long|short)\b", line):
                raise ReviewRequired("Unclear or negated direction", code="MISSING_DIRECTION")
        if len(directions) != 1:
            raise ReviewRequired(
                "Exactly one USDT symbol and explicit LONG/SHORT are required",
                code="MISSING_DIRECTION" if not directions else "AMBIGUOUS_DIRECTION",
            )
        return Direction(next(iter(directions))), None

    def values(self, text: str) -> dict:
        values = {}
        for key, label in self.labels.items():
            prefix, suffix = (
                (r"%?[ \t]*", r"[ \t]*%?") if key == "range" else ("", self.price_suffix)
            )
            pattern = rf"(?m)^[ \t]*{label}[ \t]*[:=][ \t]*{prefix}{self.number}{suffix}[ \t]*$"
            found = re.findall(pattern, text)
            mentioned = re.findall(rf"\b{label}[ \t]*[:=]", text)
            if len(found) > 1 or len(mentioned) != len(found):
                raise ReviewRequired(f"Ambiguous or unsupported {key}", code="UNSUPPORTED_FIELD")
            values[key] = Decimal(found[0].replace(",", ".")) if found else None
        return values

    def parse(self, message: Message) -> Signal:
        text = analysis_text(message.text)
        if not text.strip():
            raise ReviewRequired("No text or media caption to parse", code="NO_TEXT")
        symbols = symbol_candidates(text)
        if len(symbols) != 1:
            raise ReviewRequired(
                "Exactly one USDT symbol and explicit LONG/SHORT are required",
                code="MISSING_SYMBOL" if not symbols else "AMBIGUOUS_SYMBOL",
            )
        direction, rule = self.direction(text)
        if re.search(r"\b(?:tp|sl|target|hedef)\s*[1-9]", text):
            raise ReviewRequired(
                "Multiple or numbered targets are unsupported", code="UNSUPPORTED_TARGETS"
            )
        values = self.values(text)
        entry, spread = values["entry"], values["range"]
        if entry is None or entry <= 0 or (spread is not None and not 0 < spread < 100):
            raise ReviewRequired("Invalid entry or range", code="INVALID_ENTRY_OR_RANGE")
        if spread is None and (values["tp"] is None or values["sl"] is None):
            raise ReviewRequired(
                "Range or both explicit TP and SL are required", code="MISSING_LEVELS"
            )
        sign = Decimal(1) if direction == Direction.LONG else Decimal(-1)
        offset = entry * (spread or Decimal(0)) / 100 * Decimal("0.6")
        tp = values["tp"] if values["tp"] is not None else entry + sign * offset
        sl = values["sl"] if values["sl"] is not None else entry - sign * offset
        if not (tp > entry > sl > 0 if direction == Direction.LONG else sl > entry > tp > 0):
            raise ReviewRequired("TP/SL contradict direction or entry", code="CONFLICTING_LEVELS")
        timestamp = message.date.astimezone(UTC)
        times = re.findall(r"(?:zaman|time|timestamp)\s*[:=]\s*([^\n]+)", text)
        if times:
            try:
                parsed = datetime.fromisoformat(times[0].strip().replace("z", "+00:00"))
                if len(times) != 1 or parsed.tzinfo is None:
                    raise ValueError
                timestamp = min(timestamp, parsed.astimezone(UTC))
            except ValueError as exc:
                raise ReviewRequired(
                    "Signal time must be a single ISO-8601 value with timezone",
                    code="INVALID_SIGNAL_TIME",
                ) from exc
        return Signal(
            symbol=symbols[0],
            direction=direction,
            entry=entry,
            range_percent=spread,
            take_profit=tp,
            stop_loss=sl,
            timestamp=timestamp,
            notes=message.text,
            interpretation_rule=rule,
            take_profit_source="explicit" if values["tp"] is not None else "range",
            stop_loss_source="explicit" if values["sl"] is not None else "range",
        )


class MZTradingBotParser(RegexSignalParser):
    """Fixed 0.6 range levels, bound to an explicitly allowlisted numeric channel ID."""

    labels = {**RegexSignalParser.labels, "entry": r"anlik\s+fiyat"}
    price_suffix = r"(?:[ \t]*(?:\$|usdt))?"
    support_rule = "mztradingbot_support_positive_long_v1"
    resistance_rule = "mztradingbot_resistance_positive_short_v1"
    headline = (
        r"#?[a-z0-9]{1,20}\s*[/_-]?\s*usdt\s+"
        r"(?P<origin>destekten|direncten)\s+donus\s+icin\s+pozitif\s+durumda"
        r"(?:\s*,\s*kontrol\s+etmenizi\s+oneririm)?\s*\.?"
    )

    def __init__(self, channel_id: int, topic_id: int = 0):
        if not channel_id:
            raise ValueError("An explicit MZTRADINGBOT channel ID is required")
        self.channel_id = channel_id
        self.topic_id = topic_id

    def direction(self, text: str) -> tuple[Direction, str | None]:
        hints, explicit = [], set()
        for line in text.splitlines():
            line = line.strip()
            match = re.fullmatch(self.headline, line)
            value = explicit_direction(line)
            if match:
                hints.append(Direction.LONG if match["origin"] == "destekten" else Direction.SHORT)
            elif value:
                explicit.add(Direction(value))
            elif re.search(r"\b(long|short|destekten|direncten|donus)\b", line) or (
                symbol_candidates(line)
                and not re.fullmatch(rf"(?:(?:coin|symbol)[ \t]*:[ \t]*)?{SYMBOL_TOKEN}", line)
            ):
                raise ReviewRequired(
                    "Unclear or negated channel interpretation", code="MISSING_DIRECTION"
                )
        directions = explicit | set(hints)
        if len(directions) != 1 or len(hints) > 1:
            raise ReviewRequired(
                "Conflicting or missing channel directions",
                code="AMBIGUOUS_DIRECTION" if directions else "MISSING_DIRECTION",
            )
        direction = next(iter(directions))
        rule = (
            None
            if explicit
            else self.support_rule
            if direction == Direction.LONG
            else self.resistance_rule
        )
        return direction, rule

    def values(self, text: str) -> dict:
        # These lines describe a range, not an explicit price or a choice of leverage.
        description = (
            r"(?:hedef|stop)\s+fiyat[ \t]*:[ \t]*ortalama\s+araligin\s+"
            r"0[.,]3\s*-\s*0[.,]9\s+kati[ \t]*"
        )
        cleaned = []
        for line in text.splitlines():
            if re.match(r"[ \t]*(?:hedef|stop)\s+fiyat\b", line):
                if not re.fullmatch(description, line.strip()):
                    raise ReviewRequired(
                        "Unsupported MZ target/stop format", code="UNSUPPORTED_FIELD"
                    )
            else:
                cleaned.append(line)
        values = super().values("\n".join(cleaned))
        if values["entry"] is None or values["range"] is None:
            raise ReviewRequired(
                "MZ price and average range are required", code="MISSING_PRICE_OR_RANGE"
            )
        if values["tp"] is not None or values["sl"] is not None:
            raise ReviewRequired(
                "MZ levels must use the fixed 0.6 coefficient", code="UNSUPPORTED_FIELD"
            )
        return values

    def parse(self, message: Message) -> Signal:
        if (
            message.channel_id != self.channel_id
            or not self.topic_id
            or message.topic_id != self.topic_id
        ):
            raise ReviewRequired("MZ channel is not allowlisted", code="CHANNEL_NOT_ALLOWED")
        signal = super().parse(message)
        signal.parser_name = "mztradingbot_v1"
        signal.level_coefficient = Decimal("0.6")
        signal.signal_kind = (
            "channel_interpreted_candidate" if signal.interpretation_rule else "explicit_signal"
        )
        signal.automation_policy = "mztradingbot_verified_topic_patterns_v2"
        return signal


class ChannelSignalParser:
    def __init__(self, mztradingbot_channel_id: int = 0, topic_id: int = 0):
        self.generic = RegexSignalParser()
        self.mz = (
            MZTradingBotParser(mztradingbot_channel_id, topic_id)
            if mztradingbot_channel_id
            else None
        )

    def parse(self, message: Message) -> Signal:
        text = analysis_text(message.text)
        if (
            self.mz
            and message.channel_id == self.mz.channel_id
            and (re.search(r"\banlik\s+fiyat\b|\b(?:destekten|direncten)\s+donus\b", text))
        ):
            return self.mz.parse(message)
        return self.generic.parse(message)
