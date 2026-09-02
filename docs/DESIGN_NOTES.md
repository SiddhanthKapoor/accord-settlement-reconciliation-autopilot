# Design notes

Short record of decisions made during the product-experience refinement pass,
so the reasoning isn't lost.

## Terminology: "execution boundary," not "settlement boundary"

Interlock runs immediately before Razorpay is called to create a payment —
i.e. before **payment execution**. "Settlement" is a distinct, later process
(funds actually clearing to a merchant's bank account, which Razorpay's own
Settlements API models separately from payment creation). Using "settlement"
to describe where Interlock sits would be technically imprecise to a
Razorpay engineer. All copy (README, docs, code comments, UI) was corrected
to "payment execution boundary."

## Razorpay Blade: evaluated, not adopted

[Blade](https://github.com/razorpay/blade) (`@razorpay/blade`) is real,
actively maintained (MIT-licensed, React + React Native), and white-labelable
rather than hard-locked to Razorpay's own brand colors. It was seriously
considered for this refinement pass.

Decision: **not adopted**, for three reasons specific to this project's
stage, not a knock on Blade itself:

1. It's a full component library with its own theming/provider architecture.
   Wrapping the app in it this late, without the ability to visually verify
   the result myself in this environment (see below), is a real regression
   risk with no fast way to catch a mismatch.
2. This app's actual design requirement — a small number of bespoke,
   data-driven visualizations (the live pipeline, the mutation comparison,
   the concurrent-race view) — isn't well served by a general component
   library; those are custom visual logic, not buttons/inputs/cards.
3. The existing CSS was already rebuilt directly from Razorpay's own
   production stylesheet values (colors, type, radius, shadow conventions —
   see below), which delivers the "belongs beside their ecosystem" goal
   without the dependency and integration surface.

If a future pass adds more conventional form/table UI (settings, data entry),
Blade is worth revisiting for exactly that — it's the right tool for
standard UI surfaces, just not for this app's core visualizations.

## Palette and type: pulled from real data, not guessed

Colors (`#305eff`, `#080d29`, `#192839`, `#40566d`, etc.), type pairing
(Inter Tight for body/UI, a distinctive display face for headlines), border
radius conventions (pill buttons, 16px cards), and the "borders instead of
drop-shadows on resting elements" pattern were extracted directly from
razorpay.com's production CSS rather than approximated. Razorpay's actual
proprietary display typeface isn't reusable here, so Space Grotesk (SIL Open
Font License, freely available) fills that role in the same spirit — a
distinctive display face paired with a neutral workhorse body face — without
copying a proprietary asset.

## A note on visual QA in this environment

Screenshot-based verification of the running app was not available in this
session (Playwright's browser dependency required a system install this
environment couldn't complete, and screen-capture tooling here returns the
desktop background rather than window contents). All visual changes in this
pass were reasoned from first principles and real extracted design tokens,
then verified functionally (production build, full test/scenario suite) —
but the actual pixels have not been confirmed by a screenshot on this
pass. Treat visual details as a first draft to sanity-check against the
running app.
