"""
Live USD -> NGN conversion for the pricing page and for the naira amount Paystack charges.

Omixa prices are set in dollars. The naira figure is derived from the current exchange rate, so it follows the dollar
automatically: nobody edits code or a variable when the rate moves.

  * The rate comes from OMIXA_FX_URL (default: open.er-api.com, no key needed, updates daily). It is fetched on the
    server only, reused for OMIXA_FX_CACHE_SECONDS (default 6 h), shared across gunicorn workers through Redis when
    REDIS_URL is set, and refreshed lazily by the first request after it goes stale.
  * It can never break a page. A failed fetch keeps serving the last good rate (and backs off 10 minutes before trying
    again). With no rate ever obtained it falls back to OMIXA_FX_FALLBACK_USD_NGN, then to the old fixed
    OMIXA_PRICE_NGN_* values, and with none of those the naira line is simply hidden (the previous behaviour).
  * A rate outside OMIXA_FX_MIN_RATE..OMIXA_FX_MAX_RATE (default 100..20000), or one that is not a plain positive number,
    is rejected, so a garbled API answer cannot produce a nonsense price.
  * OMIXA_FX_BUFFER_PERCENT (default 0) adds a cushion on top of the rate; OMIXA_FX_ROUND_TO_NGN (default 100) rounds the
    result, so prices read ₦8,000 rather than ₦7,987.
  * OMIXA_FX_MODE=fixed switches the live rate off and uses OMIXA_PRICE_NGN_MONTHLY / _ANNUAL exactly as before.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass

import requests

from config import Config
from utils import redis_client

logger = logging.getLogger("omixa.fx")

REDIS_KEY = "omx:fx:usdngn"
REDIS_KEEP_SECONDS = 30 * 24 * 3600     # the last good rate outlives any API outage
RETRY_AFTER_FAILURE_SECONDS = 600

LIVE, CACHED, FALLBACK, CONFIGURED = "live", "cached", "fallback", "configured"


@dataclass(frozen=True)
class Rate:
    value: float          # NGN per 1 USD
    fetched_at: float     # unix time the provider answer was obtained
    source: str           # live | cached | fallback


_lock = threading.Lock()
_mem: Rate | None = None
_next_attempt = 0.0


def reset_for_tests() -> None:
    global _mem, _next_attempt
    with _lock:
        _mem = None
        _next_attempt = 0.0


# ----------------------------------------------------------------------------------------------------- fetching

def _valid(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
            and Config.FX_MIN_RATE <= value <= Config.FX_MAX_RATE)


def _parse(payload) -> float:
    """Accepts the common free-API shapes: {"rates": {"NGN": n}}, {"conversion_rates": {...}}, {"data": {"NGN": n}}."""
    if not isinstance(payload, dict):
        raise ValueError("unexpected response shape")
    if payload.get("result") not in (None, "success"):
        raise ValueError("provider reported an error")
    for key in ("rates", "conversion_rates", "data"):
        table = payload.get(key)
        if isinstance(table, dict) and "NGN" in table:
            value = table["NGN"]
            if isinstance(value, dict):   # some providers nest {"NGN": {"value": n}}
                value = value.get("value")
            if isinstance(value, str):
                value = float(value)
            if not _valid(value):
                raise ValueError("rate outside the accepted range")
            return float(value)
    raise ValueError("no NGN rate in response")


def _fetch_live() -> float:
    resp = requests.get(Config.FX_URL, timeout=Config.FX_TIMEOUT_SECONDS, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return _parse(resp.json())


def _redis_get() -> Rate | None:
    r = redis_client.get_redis()
    if r is None:
        return None
    try:
        raw = r.get(REDIS_KEY)
        if not raw:
            return None
        data = json.loads(raw)
        value, at = float(data["rate"]), float(data["at"])
        return Rate(value, at, CACHED) if _valid(value) else None
    except Exception as exc:   # a Redis or JSON problem must never reach the page
        redis_client.note_failure("fx_rate", exc)
        return None


def _redis_put(rate: Rate) -> None:
    r = redis_client.get_redis()
    if r is None:
        return
    try:
        r.set(REDIS_KEY, json.dumps({"rate": rate.value, "at": rate.fetched_at}), ex=REDIS_KEEP_SECONDS)
    except Exception as exc:
        redis_client.note_failure("fx_rate", exc)


def current_rate(now: float | None = None) -> Rate | None:
    """The USD->NGN rate to price with, or None (mode fixed, or nothing obtainable and no fallback configured)."""
    global _mem, _next_attempt
    if Config.FX_MODE == "fixed":
        return None
    now = time.time() if now is None else now
    fresh_for = Config.FX_CACHE_SECONDS

    mem = _mem
    if mem is not None and now - mem.fetched_at < fresh_for:
        return mem

    with _lock:
        mem = _mem
        if mem is not None and now - mem.fetched_at < fresh_for:
            return mem
        shared = _redis_get()
        if shared is not None and now - shared.fetched_at < fresh_for:
            _mem = shared
            return shared
        if now >= _next_attempt:
            try:
                rate = Rate(_fetch_live(), now, LIVE)
            except Exception as exc:   # network, HTTP, JSON, range: all end the same way (keep the last good rate)
                _next_attempt = now + RETRY_AFTER_FAILURE_SECONDS
                logger.warning("USD->NGN rate refresh failed (%s: %s); keeping the last known rate and retrying in %d min",
                               type(exc).__name__, str(exc)[:120], RETRY_AFTER_FAILURE_SECONDS // 60)
            else:
                _mem = rate
                _redis_put(rate)
                logger.info("USD->NGN rate refreshed: %.2f", rate.value)
                return rate
        stale = max((r for r in (mem, shared) if r is not None), key=lambda r: r.fetched_at, default=None)
        if stale is not None:
            _mem = stale
            return stale
    if _valid(Config.FX_FALLBACK_USD_NGN):
        return Rate(float(Config.FX_FALLBACK_USD_NGN), 0.0, FALLBACK)
    return None


# ----------------------------------------------------------------------------------------------------- pricing

def ngn_for_usd(usd: float, rate: float) -> int:
    """Naira for a dollar price: rate (+ optional buffer), rounded to the nearest OMIXA_FX_ROUND_TO_NGN, never below one step."""
    step = Config.FX_ROUND_TO_NGN
    raw = usd * rate * (1 + Config.FX_BUFFER_PERCENT / 100.0)
    return max(step, int(math.floor(raw / step + 0.5)) * step) if usd > 0 else 0


def ngn_prices(now: float | None = None) -> dict:
    """{"monthly": int, "annual": int, "rate": float|None, "source": str, "as_of": float|None}. 0 means "unknown, hide it"."""
    rate = current_rate(now)
    if rate is not None:
        return {"monthly": ngn_for_usd(Config.PRICE_USD_MONTHLY, rate.value),
                "annual": ngn_for_usd(Config.PRICE_USD_ANNUAL, rate.value),
                "rate": rate.value, "source": rate.source, "as_of": rate.fetched_at or None}
    return {"monthly": Config.PRICE_NGN_MONTHLY, "annual": Config.PRICE_NGN_ANNUAL, "rate": None,
            "source": CONFIGURED, "as_of": None}


def attribution_required() -> bool:
    """open.er-api.com's free terms ask for a visible credit wherever its rates are shown."""
    return "er-api.com" in Config.FX_URL and Config.FX_MODE != "fixed"
