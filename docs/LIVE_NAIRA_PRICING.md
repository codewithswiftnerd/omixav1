# Live naira pricing

Permanent feature, controlled only by Railway variables.

## Live USD→NGN pricing

Prices stay in dollars (`OMIXA_PRICE_USD_MONTHLY=5`, `OMIXA_PRICE_USD_ANNUAL=50`). The naira amount is
`dollar price × today's rate`, rounded to ₦100, and updates by itself.

* Rate source: `open.er-api.com` (free, no key, updates daily; the pricing page shows the credit its terms ask for).
  Fetched on the server, cached 6 h, shared between workers via Redis, refreshed by the first visit after expiry.
* If the rate service is down: the last good rate keeps being used (retry every 10 min). If none was ever fetched:
  `OMIXA_FX_FALLBACK_USD_NGN`, then `OMIXA_PRICE_NGN_*`, then the naira line is hidden. A page never breaks.
* A rate outside ₦100–₦20,000 per dollar is rejected as garbage.
* **What Paystack charges:** a Paystack plan has one fixed naira amount. At checkout Omixa compares the plan with today's price and, if they differ by more than 2 %,
  updates the plan amount through Paystack's API (`update_existing_subscriptions=false`: **new subscribers only**; people already subscribed keep
  their price). Any failure is logged and checkout carries on. Switch off with `OMIXA_PAYSTACK_AUTO_PRICE=false`.

Variables (all optional): `OMIXA_FX_MODE` (`auto`|`fixed`), `OMIXA_FX_URL`, `OMIXA_FX_CACHE_SECONDS`, `OMIXA_FX_FALLBACK_USD_NGN`,
`OMIXA_FX_BUFFER_PERCENT` (e.g. `3` to cover rate drift and fees), `OMIXA_FX_ROUND_TO_NGN`, `OMIXA_FX_MIN_RATE`, `OMIXA_FX_MAX_RATE`,
`OMIXA_PAYSTACK_AUTO_PRICE`, `OMIXA_PAYSTACK_AUTO_PRICE_TOLERANCE_PERCENT`.
**Action for you:** clear `OMIXA_PRICE_NGN_MONTHLY` and `OMIXA_PRICE_NGN_ANNUAL` in Railway (or leave them as fallbacks), and
make sure the Paystack secret key and both plan codes are set. Test one checkout with Paystack **test keys** before launch.
Note: Paystack's "plan" API behaviour was taken from its public docs and verified here only against a fake; the test checkout is the real proof.

## Tests
`pytest tests/test_fx_pricing.py`
