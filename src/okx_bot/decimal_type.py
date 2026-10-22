"""Exact NUMERIC in PostgreSQL and decimal text in the offline-only SQLite test backend."""

from decimal import Decimal

from sqlalchemy import Numeric, String
from sqlalchemy.types import TypeDecorator


class PreciseDecimal(TypeDecorator):
    impl = Numeric(38, 18)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        # SQLite NUMERIC affinity converts decimal strings to binary floats.
        return dialect.type_descriptor(String(80) if dialect.name == "sqlite" else self.impl)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("Non-finite numeric data")
        return str(number) if dialect.name == "sqlite" else number

    def process_result_value(self, value, dialect):
        return Decimal(str(value)) if value is not None else None
