"""Allowlisted diagnostics: no raw HTTP bodies, exception text, SQL, or credentials."""

import re
import traceback
from pathlib import Path

from okx_bot.domain import ExchangeError, RiskRejected


def identifier(value) -> str | None:
    value = str(value)
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else None


def failure_details(exc: Exception, stage: str) -> dict:
    result = {"stage": stage, "exception_type": type(exc).__name__}
    if isinstance(exc, ExchangeError):
        result.update(code=identifier(exc.code) or "INVALID_ERROR_CODE", ambiguous=exc.ambiguous)
        if exc.endpoint and re.fullmatch(r"/api/v5/[a-z0-9/_-]+", exc.endpoint):
            result["endpoint"] = exc.endpoint
        if exc.method in {"GET", "POST"}:
            result["method"] = exc.method
        if isinstance(exc.http_status, int):
            result["http_status"] = exc.http_status
    elif isinstance(exc, RiskRejected):
        result["code"] = identifier(exc.code) or "RISK_REJECTED"
        # These values are selected from account/config, never raw provider payloads.
        result.update(
            {
                k: identifier(v)
                for k, v in exc.context.items()
                if k in {"account_level", "expected_account_level", "position_mode"}
            }
        )
    else:
        result["code"] = "DATABASE_ERROR" if hasattr(exc, "orig") else "INTERNAL_ERROR"
        original = getattr(exc, "orig", exc)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        if sqlstate and re.fullmatch(r"[A-Z0-9]{5}", str(sqlstate)):
            result["sqlstate"] = str(sqlstate)
        if isinstance(exc, KeyError) and exc.args and identifier(exc.args[0]):
            result["missing_field"] = identifier(exc.args[0])
    frames = traceback.extract_tb(exc.__traceback__)
    result["frames"] = [
        {"file": Path(f.filename).name, "line": f.lineno, "function": f.name} for f in frames[-6:]
    ]
    return result
