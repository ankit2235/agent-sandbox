# World: security

**Status:** not built yet. Phase 3S of the build plan. **Version:** `security-v0.1` (planned).
**Domain reviewer:** to be confirmed (SOC analyst or incident responder). No reviewer, no merge of
a grader.

## What it will simulate

A small company's security operations: hosts, users and identities with behaviour baselines;
synthetic logs (authentication, endpoint process, network flow, DNS, cloud audit) behind a query
API; a SIEM-style alert queue where most alerts are noise; a containment API (isolate host, disable
account, reset credentials, block indicator, restore, reimage); and incident tickets with
escalation SLAs on the simulated clock.

The agent is a SOC analyst: triage alerts, investigate across logs, decide true versus false
positive, contain confirmed compromises without disrupting healthy assets, escalate on time and
file a correct report. Incidents are pre-scripted synthetic stories with ground truth, including
benign look-alikes (maintenance windows, backup jobs, travel logins) so false positives are common.

## What it does **not** test — and hard limits

- **Defensive only.** No attack tooling, no exploit code, no malware, and no instructions for
  breaking into systems. The agent only ever investigates and defends.
- No real logs, IP addresses, domains, hashes or indicators. Addresses come from
  [RFC 5737](https://datatracker.ietf.org/doc/html/rfc5737) and domains from
  [RFC 2606](https://datatracker.ietf.org/doc/html/rfc2606).
- Not a measure of offensive skill, not a red-team environment, and not a substitute for a real
  detection stack.

## Failure types (planned)

`security.missed_compromise`, `security.false_positive_escalated`, `security.false_containment`,
`security.over_containment`, `security.evidence_destroyed`, `security.late_escalation`,
`security.wrong_root_cause`, `security.incomplete_report`.
