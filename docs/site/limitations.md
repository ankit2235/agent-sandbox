# Limitations: what this does **not** test

Read this before reading a score.

## Every world

- **A simulation, not the real thing.** No world connects to a real payment system, network or
  security tool. Rules come from public documentation only, and every uncertain rule is a
  parameter recorded in `docs/DECISIONS.md`. A high score says an agent does well in these
  simulations, not that it is safe to give it a real account.
- **Fifty public tasks per world.** The confidence intervals are wide (see
  [Scoring](scoring.md)); small differences between models mean nothing.
- **Graders read state, not words.** Whether the agent explains itself well, or asks the user a
  good question, is not graded: only that it asked, after the step that revealed the problem.
- **One reference agent.** Scores are of a model inside this runner's tool-calling loop and its
  fixed system prompt (`runner/prompts/<world>/v1.md`). Another harness can score differently.
- **No domain reviewer has signed off yet.** Both worlds are `preview` until every grader is
  signed off.
- **The v0.1 hidden set is weak by construction.** Hidden tasks are seeded variants of public
  tasks under public modifiers: they catch copied answers and leaks, not transfer to unseen kinds of
  work.
- **A replay proves consistency, not authorship.** `sandbox verify` shows a run's grades follow from
  its recorded actions; it cannot show a model took them. Submissions are also spot-checked by
  rerunning a sample with the model.
- **Calibration is pending.** No real model has been run yet, so no stress modifier is yet known to
  be too easy or too hard.

## Payments

- Anything about real payment systems: no real VPAs, card numbers or customer data, and no
  internal or confidential rules.
- Fraud detection, credit decisioning, KYC, or anything that needs a real risk model.
- Arithmetic in general: money is integer paise throughout.
- Other currencies or exchange rates: UPI is rupee-only, so the rails are too.
- NPCI's own response or reason codes: the world uses descriptive codes of its own.
- The real dispute process between banks: a dispute here is decided once, by fixed rules over the
  world's state and the evidence.
- Returns, merchant-initiated cancellations and renewal retries. Pre-debit notices exist only for
  a seller agent's collections in the multi-agent set, not for subscription renewals.
- In the multi-agent set (`payments-v0.1-agents`, pre-release): a seller agent that contests a
  chargeback or answers the support inbox; opting out of one debit without pausing the mandate;
  and, with a model on both sides, the user's agent's score alone (it measures the pair).
- Several windows are invented or rest on secondary reports (the 15-minute payment window, the
  10-minute duplicate window, the merchant profiles' timings); they wait for the payments
  reviewer.

## Security (preview)

- **No offensive skills.** No attack tooling, exploit code, malware or instructions for breaking
  into systems. Incidents are pre-scripted synthetic stories.
- **No real data.** Addresses only from RFC 5737, domains only from RFC 2606.
- **No real tooling.** Not a measure of skill with any real SIEM, EDR or ticketing product.
- Not a red-team environment and not a substitute for a real detection stack.
