# Billing provider: fees, merchant of record, API version, and card-less sponsorship

Status: **Proposed** · drafted 2026-09-21 · bead `agent-forge-harness-yje.1.4` (comparison half only)

> **Partial by design.** This record drafts only the **comparison half** of
> `yje.1.4`: fees, merchant of record, the pinned API version, and the trial API
> for card-less sponsorship. The local tables, projection, webhook inbox and
> reconciliation contracts are **left for `yje.1.3`** — see §7, which is a heading
> and nothing else on purpose.
>
> `yje.1.4` is still blocked on `yje.1.3` and `yje.1.2` and has **not** been
> claimed. It carries a `design:` comment pointing here.
>
> The owner signs this record after the design agent finishes.

---

## 1. Why this exists

The billing plan's working recommendation is Stripe, and it says so explicitly so
that the choice is "reviewed rather than smuggled into implementation"
(`docs/forge/plans/subscription-billing-coupons-profitability.md:137-141`). This
record re-checks that recommendation's numbers against the vendors' own current
documentation, because `yje.1.4`'s "Verified inputs" were gathered in a plan review
on **2026-09-16** and vendor fees and API versions change.

**Every figure below was read on 2026-09-21 and carries a link.**

## 2. Fees, verified today

From [stripe.com/pricing](https://stripe.com/pricing), read 2026-09-21:

| Line | Published rate (verbatim) |
|---|---|
| Domestic card | "2.9% + 30¢ per successful transaction for domestic cards" |
| International card | "+ 1.5% for international cards" |
| Currency conversion | "+ 1% if currency conversion is required" |
| Billing (pay-as-you-go) | "0.7% of Billing volume" |
| Tax, no-code | "0.5% per transaction, where you're registered to collect taxes" |
| Tax, API | "$0.50 per transaction, where you're registered to collect taxes" |
| Managed Payments | "3.5% per successful Managed Payments transaction in addition to Payments fees" |

From [paddle.com/pricing](https://www.paddle.com/pricing), read 2026-09-21:
**5% + 50¢ per checkout transaction**, described as all-in-one with tax and
compliance, fraud and chargeback protection, and no monthly fee.

At the $15 launch hypothesis and the $3.00 per-account cost cap:

| Scenario | Rate | Fees | Contribution | Margin |
|---|---|---:|---:|---:|
| Stripe, self-managed tax (Payments + Billing + Tax no-code) | 4.1% + $0.30 | $0.92 | $11.08 | 73.9% |
| Stripe Managed Payments, merchant of record (Payments + 3.5% + Billing; Tax fee drops, Stripe is the seller) | 7.1% + $0.30 | $1.37 | $10.63 | 70.9% |
| Paddle, merchant of record | 5% + $0.50 | $1.25 | $10.75 | 71.7% |

**All three clear the 70% margin floor at $15 with $3 of COGS.** The spread between
the best and the worst is about **$0.45 a month per account** — at the plan's 46-user
break-even, roughly **$21 a month**. The fee difference is therefore *not* the
deciding factor; §3 and §4 are.

**One caveat the plan's price table needs.** Paddle's page directs products "under
$10" to contact sales for custom pricing. The plan models $10 and $12 price points
alongside $15 (`:369-379`). **If the owner prices below $10, Paddle's published
5% + $0.50 cannot be assumed** and must be quoted before it appears in any
projection.

## 3. Merchant of record: what the fee actually buys, and what it costs

### Stripe Managed Payments

Confirmed from [docs.stripe.com/managed-payments](https://docs.stripe.com/managed-payments)
and [how it works](https://docs.stripe.com/payments/managed-payments/how-it-works),
read 2026-09-21: Stripe is merchant of record, handling "indirect tax compliance
(sales tax, VAT, and GST) on transactions in more than 80 countries", plus fraud,
disputes and transaction-level customer support.

Eligibility, from [the eligibility page](https://docs.stripe.com/payments/managed-payments/eligibility):
business must be in a supported location (US, CA, the EU/EEA/UK set, AU, HK, JP,
SG); the product must be a digital product with an eligible tax code — and
`txcd_10105001` "Artificial Intelligence as a Service (AIaaS) - Cloud Based -
Personal Use" fits Aetheril directly. Customers can buy from 195+ countries except
a restricted list including China, Cuba, Iran, North Korea, Russia and Syria. The
product must be "a fully automated digital product"; a service involving "human
intervention (such as live 1-1 coaching)" does not qualify — Aetheril is
automated, so this is satisfied, but it would bite if professional-GM services
were ever sold through the same account.

**The constraints that matter more than the 3.5%, quoted verbatim:**

- Unsupported: "Creating a subscription outside of Checkout or Payment Links."
- Unsupported: "Third-party tax integrations", "Stripe Connect", "embeddable web
  components or other advanced integrations", "Attaching invoice items on a
  `Customer` object", "Generating a one-off invoice".
- "Custom domains aren't supported on Managed Payments checkouts."
- The customer "sees Link as the merchant of record, and sees purchases as *Sold
  through Link*", and the statement shows `LINK.COM* [Your statement descriptor]`.
- Support escalation: "If you don't respond within 48 hours, Stripe might issue a
  refund without your approval."
- Data deletion: a customer deletion request makes Stripe cancel their
  subscriptions and delete "their data from any data objects (including objects in
  your Stripe account)" — Customer, Subscription, Invoice, PaymentIntent, Charge.

Three of these have architectural consequences for this product:

1. **"Creating a subscription outside of Checkout or Payment Links" is
   unsupported.** Any operator-issued sponsored subscription created by API is out.
   This independently reinforces the plan's existing instruction to build sponsored
   card-less access as **a local time-boxed entitlement grant**, not a
   provider-side subscription (`:248-255`).
2. **Provider objects can vanish.** The reconciliation design reads customers and
   subscriptions back from the provider to repair missed events (`:466-470`). Under
   Managed Payments a customer's deletion request removes those objects from
   Aetheril's own Stripe account. The projection must treat "object no longer
   exists" as a valid, expected state — not as a reconciliation failure and not as
   a reason to revoke or grant access unexpectedly.
3. **Brand.** Customers transact with Link, not with Aetheril, and the checkout
   cannot use a custom domain. That is a product decision, not an engineering one.

### Paddle

Paddle is also merchant of record at 5% + $0.50 with tax and compliance included.
It was **not** investigated beyond its published rate, because adopting it would
replace the entire Checkout/Billing/Portal/Entitlements/webhook design the plan is
built on, and the fee difference (§2) does not justify that. Recorded as a real
option that has not been costed in engineering terms; if the owner wants it
seriously considered, that is a new research bead.

### Self-managed tax (Stripe + Stripe Tax)

Cheapest in fees and most expensive in workload: it hands tax registration,
filing and remittance to the owner and an accountant in every jurisdiction where a
threshold is crossed. The plan already flags this (`:143-148`). Note that Stripe
Tax remains usable **alongside** Managed Payments at no extra charge for
transactions in countries where Managed Payments does not handle compliance.

### The comparison the bead asks for

| | Self-managed (Stripe Tax) | Stripe Managed Payments | Paddle |
|---|---|---|---|
| Fees at $15 | $0.92 | $1.37 | $1.25 |
| Margin at $3 COGS | 73.9% | 70.9% | 71.7% |
| Tax registration / filing | **Owner + accountant, every jurisdiction** | Stripe, 80+ countries | Paddle |
| Disputes, fraud, txn support | Owner (Radar extra) | Stripe | Paddle |
| Keeps the planned architecture | Yes | Yes, minus the constraints above | **No — full redesign** |
| Customer-visible merchant | Aetheril | Link | Paddle |
| Custom checkout domain | Yes | **No** | Provider's |

**Working recommendation: Stripe with Managed Payments**, because a single operator
cannot realistically run multi-jurisdiction indirect-tax compliance, and 3 points
of margin is the cheapest possible price for not doing it. This is a
recommendation, not a decision: the merchant-of-record question is the owner's
(§8), and it is reversible only at the cost of taking that compliance work back.

## 4. Pinned API version — **`yje.1.4`'s recorded input is stale**

`yje.1.4` records: "Stripe API version `2025-09-30.clover` changed promotion codes
to reference coupons through `promotion[coupon]`; pin the API version."

Re-checked against [docs.stripe.com/changelog](https://docs.stripe.com/changelog),
read 2026-09-21:

- The current release train is **dahlia**, not clover. The newest listed stable
  version is **`2026-08-26.dahlia`**, with monthly releases back through
  `2026-07-29`, `2026-06-24`, `2026-05-27`, `2026-04-22` and `2026-03-25`.
  **`2025-09-30.clover` is roughly a year and two trains behind.**
- On [Coupons and promotion codes](https://docs.stripe.com/billing/subscriptions/coupons),
  read 2026-09-21, I **could not find the parameter shape `promotion[coupon]`.**
  What the current documentation shows is:
  - a promotion code is created with `-d "coupon={{COUPON_ID}}"`;
  - discounts are applied to a subscription through a **`discounts` array** —
    `discounts[0][coupon]` or `discounts[0][promotion_code]`;
  - the singular `coupon` / `promotion_code` parameters are described as
    **deprecated** ("you can't update the subscription with the deprecated `coupon`
    or `promotion_code` parameter" once more than one discount is set).

  So the real migration hazard is **singular parameter → `discounts[]` array**, not
  `promotion[coupon]`. Whether `promotion[coupon]` was ever accurate at
  `2025-09-30.clover` I could not confirm and do not assert.

**Recommendation: pin a current `dahlia` version — `2026-08-26.dahlia` unless a
newer one exists when implementation starts — and re-derive the coupon parameter
shape against the pinned version rather than against `yje.1.4`'s note.** Pin it in
one place, assert it in a test, and treat a version bump as a reviewed change.

**Confirmed and still true:** "You can apply up to 20 discounts to a subscription,
subscription item, or invoice", so Aetheril enforces its own no-stacking rule, as
the plan says (`:237`). Also confirmed: `max_redemptions`, `expires_at` on the
promotion code (and that it "can't be later than the coupon's" `redeem_by`),
`applies_to` on the coupon, `restrictions[first_time_transaction]`, and a
customer-restricted promotion code — every control the plan's coupon design relies
on (`:226-235`) exists today.

## 5. A trial API for card-less sponsorship — **works, with a new caveat**

`yje.1.4` records that a 100%-off repeating coupon on a card-less subscription does
not end cleanly. **Consistent with current documentation**: with
`missing_payment_method=create_invoice`, "If a payment method isn't provided when
the invoice finalizes, the subscription moves into `past_due`"
([Free trials](https://docs.stripe.com/billing/subscriptions/trials/free-trials),
read 2026-09-21). A plain 100%-off coupon has no `trial_settings` guard at all, so
it lands in exactly that path.

**The documented card-less mechanism is verified and works with Checkout today:**

```
POST /v1/checkout/sessions
  mode=subscription
  payment_method_collection=if_required
  subscription_data[trial_period_days]=30
  subscription_data[trial_settings][end_behavior][missing_payment_method]=cancel
```

The three allowed end behaviours are `cancel` (subscription "cancels immediately",
emitting `customer.subscription.deleted`), `pause` (emits
`customer.subscription.paused`, "can remain paused indefinitely"), and
`create_invoice` (the `past_due` path above). `customer.subscription.trial_will_end`
fires 3 days before the end.

**The new caveat, which `yje.1.4` predates.** The trials landing page now marks
`trial_end` / `trial_period_days` as **Legacy** ("Technology that's no longer
recommended") and steers new integrations to the **Trial Offer API**
([Trial offers](https://docs.stripe.com/billing/subscriptions/trials), read
2026-09-21). That newer API:

- requires the **preview** version `2026-03-25.preview`, not a stable release;
- requires `billing_mode=flexible`;
- and **does not support Checkout or Payment Links** — the page says of Checkout,
  "instead use legacy free trials with `trial_end`".

So the API Stripe recommends **cannot** do the card-less Checkout flow this product
needs, and the API that can is labelled legacy. Two honest consequences:

1. **For launch, use the legacy free-trial parameters.** They are documented,
   supported, and Checkout-compatible today.
2. **Prefer the plan's other option anyway.** The plan already offers "a local
   time-boxed entitlement grant" as the alternative to a provider trial
   (`:248-255`). Given §3's constraint that Managed Payments does not support
   creating subscriptions outside Checkout, and given that the legacy trial path
   may eventually be withdrawn, **the local grant is the more durable sponsorship
   mechanism and should be the default**, with the provider trial reserved for
   sponsorships that are genuinely meant to convert into a paid subscription.

Either way the plan's hard rule stands: sponsored card-less access and a
discounted subscription must never look like the same thing to the user
(`:244-262`).

## 6. What I could not verify

- **Whether promotion codes are supported on Managed Payments checkouts.**
  `yje.1.4` asks to "Confirm support for promotion codes, trials, and Customer
  Portal before choosing it." Trials: **confirmed** — Stripe sends trial-start and
  trial-ending emails from Link for Managed Payments subscriptions. Customer
  Portal: **confirmed** — "You can also offer additional subscription management
  from your own website using the Customer Portal or your own solution." Promotion
  codes: **neither confirmed nor denied on any Managed Payments page I read.**
  This must be tested in a Stripe test account before Managed Payments is signed
  off, and it is a real risk because Managed Payments enables **Adaptive Pricing**
  by default, which converts prices to local currency — an interaction with
  fixed-amount (rather than percentage) coupons that nothing I read describes.
- **Stripe Billing volume-tier pricing.** Only the pay-as-you-go 0.7% was read;
  any Starter/Scale tier structure was not. Irrelevant at this volume.
- **The dispute-received fee amount** under Managed Payments. The page points at
  stripe.com/pricing for it; not read. It is a negative-contribution event, not a
  recurring rate.
- **Paddle's engineering surface** — see §3.
- **Whether `promotion[coupon]` was ever the correct shape** at `2025-09-30.clover`.
  Not asserted either way.

## 7. Local tables, projection, webhook inbox and reconciliation

**Left for `yje.1.3`.** Nothing is recorded here.

## 8. What only the owner can decide

1. **Merchant of record or not.** §3 recommends Managed Payments; the trade is
   about 3 points of margin against running indirect-tax compliance alone, and it
   is the owner's risk appetite, not an engineering fact.
2. **Whether Link-as-merchant is acceptable**: customers see "Sold through Link",
   the statement reads `LINK.COM*`, and the checkout cannot use Aetheril's domain.
3. **Whether the 48-hour unilateral-refund term is acceptable.**
4. **Merchant country and legal entity**, which decide Managed Payments eligibility
   and the real fee schedule. The plan already says the economics model "must match
   the real account fee schedule before launch" (`:312-315`).
5. **The price point** — and if it is below $10, Paddle must be re-quoted (§2).
6. **Whether professional GMs, stores or conventions will ever be sold through the
   same account**, which would collide with the "fully automated digital product"
   requirement in §3.

## 9. Open questions

- Does account deletion in Aetheril delete the billing customer, and does a
  Managed Payments deletion request delete the Aetheril account? These are not
  symmetrical (§3) and the pairing belongs to `yje.1.3`'s state machine. See
  `docs/adr/identity-provider.md` §12.
- Does Adaptive Pricing change the contribution-margin model? Every figure in §2
  is USD; if a customer pays in another currency at a Stripe-set rate, the
  collected revenue per account is no longer exactly $15. `yje.1.2` should decide
  whether the margin floor is asserted per-currency or on the USD equivalent.
- Withheld tax appears in the `fee` column of Stripe's reports by default, with
  `withheld_tax` and `fee_net_of_withheld_tax` available as separate columns. The
  reporting in `yje.5.*` must use the split columns, or margin reporting will
  silently count tax as a Stripe fee.
