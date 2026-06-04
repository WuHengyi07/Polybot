"""Small shared helpers: logging, time, math, and safe parsing.

Pure standard library so every core module + the tests run with no third-party
packages installed.
"""
from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

_LOG_CONFIGURED = False


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, idempotently."""
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (handles a trailing 'Z'). Returns None on failure."""
    if not value:
        return None
    try:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def hours_until(when: Optional[datetime], *, now: Optional[datetime] = None) -> Optional[float]:
    """Hours from ``now`` until ``when`` (negative if in the past)."""
    if when is None:
        return None
    now = now or utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - now).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# Math helpers
# --------------------------------------------------------------------------- #
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def celsius_to_fahrenheit(c: float) -> float:
    """Convert degrees Celsius to Fahrenheit (the bot's internal unit)."""
    return c * 9.0 / 5.0 + 32.0


def normal_cdf(x: float, mean: float = 0.0, std: float = 1.0) -> float:
    """Standard normal CDF via the error function (no numpy/scipy needed)."""
    if std <= 0:
        # Degenerate distribution: a step function at the mean.
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def safe_mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def safe_std(values: Sequence[float]) -> float:
    """Sample standard deviation; 0.0 for fewer than two points."""
    return statistics.stdev(values) if len(values) > 1 else 0.0


def fraction(values: Iterable[float], predicate) -> float:
    vals = list(values)
    if not vals:
        return 0.0
    return sum(1 for v in vals if predicate(v)) / len(vals)


def to_float(value, default: float = 0.0) -> float:
    """Parse a number that may arrive as a string like '0.1000'."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"
