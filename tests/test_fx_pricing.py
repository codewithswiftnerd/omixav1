"""Live USD->NGN pricing (utils/fx.py) and the Paystack plan amount that follows it. Offline: the rate API is faked."""
import json

import pytest

from accounts import paystack
from config import Config
from routes import billing as billing_mod
from utils import fx, redis_client
from tests.fakes import FakeRedis


@pytest.fixture(autouse=True)
def live_mode(monkeypatch):
    monkeypatch.setattr(Config, "FX_MODE", "auto")
    monkeypatch.setattr(Config, "FX_URL", "https://open.er-api.com/v6/latest/USD")
    monkeypatch.setattr(Config, "PRICE_USD_MONTHLY", 5.0)
    monkeypatch.setattr(Config, "PRICE_USD_ANNUAL", 50.0)
    monkeypatch.setattr(Config, "PRICE_NGN_MONTHLY", 0)
    monkeypatch.setattr(Config, "PRICE_NGN_ANNUAL", 0)
    monkeypatch.setattr(Config, "FX_FALLBACK_USD_NGN", 0.0)
    monkeypatch.setattr(Config, "FX_BUFFER_PERCENT", 0.0)
    monkeypatch.setattr(Config, "FX_ROUND_TO_NGN", 100)
    fx.reset_for_tests()
    paystack.reset_plan_cache_for_tests()
    yield
    fx.reset_for_tests()
    paystack.reset_plan_cache_for_tests()


def feed(monkeypatch, rates):
    """Fake provider: returns each rate in turn (an Exception instance is raised); records the call count."""
    calls = []
    seq = list(rates)

    def fake():
        calls.append(1)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item
    monkeypatch.setattr(fx, "_fetch_live", fake)
    return calls


# ------------------------------------------------------------------------------------------ parsing and maths

def test_parse_accepts_common_shapes_and_rejects_nonsense():
    assert fx._parse({"result": "success", "rates": {"NGN": 1523.4}}) == 1523.4
    assert fx._parse({"conversion_rates": {"NGN": "1500"}}) == 1500.0
    for bad in ({"result": "error"}, {"rates": {}}, {"rates": {"NGN": 5}}, {"rates": {"NGN": 10**9}},
                {"rates": {"NGN": float("nan")}}, {"rates": {"NGN": True}}, [], "x"):
        with pytest.raises(ValueError):
            fx._parse(bad)


def test_rounding_and_buffer(monkeypatch):
    assert fx.ngn_for_usd(5, 1537) == 7700        # 7685 -> nearest 100
    assert fx.ngn_for_usd(50, 1537) == 76900      # 76850 -> half up
    monkeypatch.setattr(Config, "FX_BUFFER_PERCENT", 10.0)
    assert fx.ngn_for_usd(5, 1500) == 8300        # 8250 -> half up
    monkeypatch.setattr(Config, "FX_ROUND_TO_NGN", 1)
    assert fx.ngn_for_usd(5, 1500) == 8250
    assert fx.ngn_for_usd(0, 1500) == 0


# ------------------------------------------------------------------------------------------ the live rate

def test_naira_price_follows_the_dollar_without_any_config_change(monkeypatch):
    feed(monkeypatch, [1500.0, 1700.0])
    assert fx.ngn_prices(now=0)["monthly"] == 7500
    assert fx.ngn_prices(now=0)["annual"] == 75000
    later = Config.FX_CACHE_SECONDS + 1                       # dollar went up, cache expired
    p = fx.ngn_prices(now=later)
    assert (p["monthly"], p["annual"], p["rate"], p["source"]) == (8500, 85000, 1700.0, "live")


def test_rate_is_cached_between_refreshes(monkeypatch):
    calls = feed(monkeypatch, [1500.0])
    for t in (0, 60, 3600, Config.FX_CACHE_SECONDS - 1):
        fx.ngn_prices(now=t)
    assert len(calls) == 1
    fx.ngn_prices(now=Config.FX_CACHE_SECONDS + 1)
    assert len(calls) == 2


def test_outage_keeps_the_last_good_rate_and_backs_off(monkeypatch):
    calls = feed(monkeypatch, [1500.0, OSError("down")])
    fx.ngn_prices(now=0)
    t = Config.FX_CACHE_SECONDS + 1
    assert fx.ngn_prices(now=t)["monthly"] == 7500            # provider down: last good rate still served
    for dt in (10, 100, 500):
        assert fx.ngn_prices(now=t + dt)["monthly"] == 7500
    assert len(calls) == 2                                    # no hammering during the 10-minute back-off
    fx.ngn_prices(now=t + fx.RETRY_AFTER_FAILURE_SECONDS + 1)
    assert len(calls) == 3


def test_no_rate_ever_falls_back_then_hides(monkeypatch):
    feed(monkeypatch, [OSError("down")])
    assert fx.ngn_prices(now=0)["monthly"] == 0               # nothing known: naira line hidden, as before
    fx.reset_for_tests()
    monkeypatch.setattr(Config, "PRICE_NGN_MONTHLY", 7000)
    monkeypatch.setattr(Config, "PRICE_NGN_ANNUAL", 70000)
    p = fx.ngn_prices(now=0)
    assert (p["monthly"], p["source"]) == (7000, "configured")
    fx.reset_for_tests()
    monkeypatch.setattr(Config, "FX_FALLBACK_USD_NGN", 1600.0)
    p = fx.ngn_prices(now=0)
    assert (p["monthly"], p["source"]) == (8000, "fallback")


def test_fixed_mode_never_calls_the_provider(monkeypatch):
    calls = feed(monkeypatch, [1500.0])
    monkeypatch.setattr(Config, "FX_MODE", "fixed")
    monkeypatch.setattr(Config, "PRICE_NGN_MONTHLY", 7777)
    assert fx.ngn_prices(now=0)["monthly"] == 7777 and calls == []


def test_rate_is_shared_through_redis(monkeypatch):
    r = FakeRedis()
    redis_client.set_redis_for_tests(r)
    try:
        calls = feed(monkeypatch, [1500.0])
        fx.ngn_prices(now=1_000_000)
        assert json.loads(r.get(fx.REDIS_KEY))["rate"] == 1500.0
        fx.reset_for_tests()                                  # "another gunicorn worker" with an empty memory
        import time
        monkeypatch.setattr(fx, "_fetch_live", lambda: pytest.fail("should read the shared rate"))
        p = fx.ngn_prices(now=1_000_000 + 60)
        assert (p["rate"], p["source"]) == (1500.0, "cached") and len(calls) == 1
    finally:
        redis_client.set_redis_for_tests(None)


# ------------------------------------------------------------------------------------------ pages and API

def test_pricing_page_and_plans_api_show_live_naira(client, monkeypatch):
    feed(monkeypatch, [1500.0])
    html = client.get("/pricing").get_data(as_text=True)
    assert "₦7,500/month at today's exchange rate" in html and "₦75,000/year" in html
    assert "Rates by Exchange Rate API" in html and "approximate" in html
    plans = client.get("/api/billing/plans").get_json()
    assert plans["pro_monthly"] == {"usd": 5.0, "ngn": 7500} and plans["pro_annual"]["ngn"] == 75000 and plans["ngn_rate"] == 1500.0
    feed(monkeypatch, [1800.0])
    fx.reset_for_tests()
    assert "₦9,000/month" in client.get("/pricing").get_data(as_text=True)


def test_pricing_page_hides_naira_when_no_rate_is_known(client, monkeypatch):
    feed(monkeypatch, [OSError("down")])
    html = client.get("/pricing").get_data(as_text=True)
    assert "₦" not in html and client.get("/pricing").status_code == 200


# ------------------------------------------------------------------------------------------ Paystack plan follows the rate

class FakeHttp:
    def __init__(self, plan_amount_kobo):
        self.amount, self.requests = plan_amount_kobo, []

    def request(self, method, url, headers=None, timeout=None, json=None):
        self.requests.append((method, url, json))
        body = {"status": True, "data": {"amount": self.amount}}
        if method == "PUT":
            self.amount = json["amount"]
        return type("R", (), {"status_code": 200, "json": lambda s: body})()


@pytest.fixture
def paystack_cfg(monkeypatch):
    monkeypatch.setattr(Config, "PAYSTACK_SECRET_KEY", "sk_test")
    monkeypatch.setattr(Config, "PAYSTACK_PLAN_MONTHLY", "PLN_m")
    monkeypatch.setattr(Config, "PAYSTACK_PLAN_ANNUAL", "PLN_a")
    monkeypatch.setattr(Config, "PAYSTACK_AUTO_PRICE", True)
    monkeypatch.setattr(Config, "PAYSTACK_AUTO_PRICE_TOLERANCE_PERCENT", 2.0)


def test_plan_is_repriced_for_new_subscribers_only(monkeypatch, paystack_cfg):
    feed(monkeypatch, [1700.0])
    http = FakeHttp(750000)                                    # plan still at the old ₦7,500
    assert paystack.ensure_plan_price(paystack.PaystackClient(http), "pro_monthly") == "updated"
    method, url, body = http.requests[-1]
    assert (method, url.endswith("/plan/PLN_m")) == ("PUT", True)
    assert body == {"amount": 850000, "update_existing_subscriptions": False}


def test_small_drift_does_not_touch_the_plan(monkeypatch, paystack_cfg):
    feed(monkeypatch, [1510.0])                                # target ₦7,600 vs plan ₦7,500: 1.3%
    http = FakeHttp(750000)
    assert paystack.ensure_plan_price(paystack.PaystackClient(http), "pro_monthly") == "unchanged"
    assert [m for m, _, _ in http.requests] == ["GET"]


def test_plan_check_is_cached_and_annual_uses_its_own_price(monkeypatch, paystack_cfg):
    feed(monkeypatch, [1500.0])
    http = FakeHttp(7500000)
    client = paystack.PaystackClient(http)
    for _ in range(3):
        paystack.ensure_plan_price(client, "pro_annual")
    assert [m for m, _, _ in http.requests] == ["GET"]


def test_auto_price_off_or_no_live_rate_changes_nothing(monkeypatch, paystack_cfg):
    http = FakeHttp(1)
    feed(monkeypatch, [1700.0])
    monkeypatch.setattr(Config, "PAYSTACK_AUTO_PRICE", False)
    assert paystack.ensure_plan_price(paystack.PaystackClient(http), "pro_monthly").startswith("skipped")
    monkeypatch.setattr(Config, "PAYSTACK_AUTO_PRICE", True)
    monkeypatch.setattr(Config, "FX_FALLBACK_USD_NGN", 1700.0)
    feed(monkeypatch, [OSError("down")])
    fx.reset_for_tests()
    assert paystack.ensure_plan_price(paystack.PaystackClient(http), "pro_monthly").startswith("skipped")
    assert http.requests == []


def test_checkout_still_works_when_the_plan_sync_fails(client, monkeypatch, paystack_cfg):
    from accounts import auth as auth_mod, store as store_mod
    feed(monkeypatch, [1700.0])

    class Exploding(paystack.PaystackClient):
        def __init__(self):
            self.inits = []

        def fetch_plan(self, code):
            raise RuntimeError("paystack down")

        def initialize(self, **kw):
            self.inits.append(kw)
            return {"authorization_url": "https://checkout.paystack.test/x", "reference": kw["reference"]}

    fake = Exploding()
    billing_mod.set_client_for_tests(fake)
    store_mod.set_store_for_tests(store_mod.MemoryStore())
    auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": "a@example.com", "name": "A"})
    try:
        assert client.post("/api/auth/session", json={"idToken": "u1"}).status_code == 200
        r = client.post("/api/billing/checkout", json={"plan": "pro_monthly"})
        assert r.status_code == 200 and r.get_json()["authorization_url"].startswith("https://checkout")
        assert fake.inits
    finally:
        billing_mod.set_client_for_tests(paystack.PaystackClient())
