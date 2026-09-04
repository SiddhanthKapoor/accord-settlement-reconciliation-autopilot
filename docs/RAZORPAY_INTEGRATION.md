# Razorpay integration boundary

Short version: the integration is real and works, and the account still
returns no settlements. Both halves of that are verified rather than
assumed, and the reason is a property of how settlements come into
existence, not of this code.

## What was actually probed

Every call below was run against this project's own test-mode account
with the credentials in `backend/.env`. This is the output, not a
description of it.

| Call | Result |
|---|---|
| `client.settlement.all({"count": 5})` | `entity=collection, count=0, items=0` |
| `client.payment.all({"count": 5})` | `entity=collection, count=0, items=0` |
| `client.order.all({"count": 5})` | `entity=collection, count=0, items=0` |
| `client.refund.all({"count": 5})` | `entity=collection, count=0, items=0` |
| `client.settlement.report({"year": 2026, "month": 9})` | `entity=collection, count=0, items=0` |
| `client.settlement.report({"year": 2025, "month": 6})` | `entity=collection, count=0, items=0` |
| `client.order.create({...})` | **succeeded** — `order_TXwOqE2JuvpyeF`, amount 51900, status `created` |
| `client.order.fetch("order_TXwOqE2JuvpyeF")` | **succeeded** — returned the full order |
| `client.order.payments("order_TXwOqE2JuvpyeF")` | `count=0` |

Two things worth separating, because they are usually collapsed into
"the sandbox is empty":

1. **The credentials and the client are fine.** `order.create` wrote a
   real object and `order.fetch` read it back by id. Authentication,
   signing, and request/response parsing all work.
2. **Collection endpoints return nothing regardless** — including
   immediately after a successful write, and including
   `settlement.report`, which is Razorpay's own reconciliation report
   endpoint and would have been the ideal source here.

## Why no test-mode workflow fixes this

A settlement is downstream of a captured payment. The chain is:

```
order created (server API)  ->  payment captured (checkout, browser)
    ->  payment settled in a bank settlement cycle (T+2/T+3)
        ->  appears in settlement.all() / settlement.report()
```

The first step is reachable from server-side API calls and was confirmed
above. The second is not: capturing a payment requires the client-side
checkout flow with a test card, which is a browser interaction, not an
API call. The third is a scheduled banking process that a test account
does not run at all.

So there is no supported test-mode API sequence that produces
representative settlement, fee, tax, or adjustment records. Even driving
checkout by hand would only produce a captured payment, not a settled
one — and settlement fees and timing are precisely what this system
reconciles.

## What the code does about it

`app/integrations/settlement_source.py` defines the boundary. Both
sources implement the same interface and everything downstream consumes
the same `RazorpaySettlementRecord` type, so the engine cannot tell them
apart — which is why the distinction is carried explicitly, as a
`provenance` field, rather than left implicit:

| | `LIVE_RAZORPAY` | `SYNTHETIC` |
|---|---|---|
| Source | `client.settlement.all()` | `data/datasets/razorpay_pool.jsonl` |
| Real Razorpay data | yes | no |
| Currently returns | 0 records | 4,800 records |
| Used for evaluation | no | yes |

`GET /data-sources` reports the live status of both, and the console
renders it as a banner. A viewer never has to infer whether a number on
screen came from Razorpay or from a generator.

The live path is not a stub: `app/integrations/razorpay_settlements.py`
makes the real call and parses the real response shape, and
`tests/test_razorpay_integration.py` exercises it against
realistically-shaped payloads plus the not-configured and empty-account
paths.

## What this does and does not prove

It proves the integration is wired correctly and would consume a real
merchant's settlement history — an account with genuine payment volume
needs a credential change, not a code change.

It does not prove the engine reconciles real Razorpay data correctly.
Every accuracy number in this repository comes from synthetic data, and
real settlement text, reference conventions, and fee structures will
differ from the generator's. That gap is real and is stated the same way
in the README's limitations section.

## Reproducing the probe

```bash
cd backend
python -c "
import os; from dotenv import load_dotenv; load_dotenv('.env')
import razorpay
c = razorpay.Client(auth=(os.environ['RAZORPAY_KEY_ID'], os.environ['RAZORPAY_KEY_SECRET']))
print(c.settlement.all({'count': 5}))
print(c.settlement.report({'year': 2026, 'month': 9}))
"
```

Requires `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` in `backend/.env`.
Without them the source reports itself unavailable instead of failing.
