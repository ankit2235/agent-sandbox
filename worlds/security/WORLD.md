# World: security

**Status:** built in Phase 3S: five services, scripted stories with ground truth, 17 MCP tools,
50 public tasks with graders, and five stress modifiers. **Version:** task set `security-v0.1`.
**Tools:** `tools-security-v0.1`, schemas in `src/worlds/security/tools/schemas.json`, **not
frozen yet**; they wait for review.
**Domain reviewer:** to be confirmed (SOC analyst or incident responder). No grader is signed off:
`reviewer_signoff` is `null` on all 50 tasks, and the benign look-alike stories need the same
review.

**Defensive only.** The agent investigates and defends. Nothing here attacks anything, and every
incident is a synthetic story told as observable evidence.

## What it simulates

The security operations of Example Corp, a small fictional company (`corp.example`): about twenty
hosts (workstations, a domain controller, file, backup, web, database, build and scanner servers,
a jump host), fifteen people and service accounts with normal-behaviour baselines, approved change
records, scheduled jobs, and the outside suppliers the business depends on.

The agent is the SOC analyst on shift. It triages a SIEM-style alert queue (where most alerts are
noise or benign look-alikes), investigates across logs, decides true or false positive, contains a
real compromise without disrupting healthy assets, escalates to on-call before the SLA runs out,
and files a structured report.

## Services

Five FastAPI services, each over its own SQLite database, snapshotted together. They never call
each other: the tools and the episode carry facts between them (`docs/DECISIONS.md`, 86–100).

- **`org`**: hosts (role, owner, criticality), accounts (groups, privilege, baseline locations and
  hours, approved travel), change records, scheduled jobs, business suppliers.
- **`telemetry`**: auth, endpoint process, network flow, DNS and cloud audit events, with filters,
  time ranges and entity pivots. It never shows an event later than the simulated now.
- **`alerts`**: the alert queue: rule, severity, entities, linked events, ticket.
- **`response`**: containment (isolate, disable, reset, block, reimage, restore) and evidence
  collection. Every action is logged with its reason, actor and business impact; reimaging wipes
  the host's local evidence.
- **`tickets`**: incidents with classification, comments, escalation to on-call, SLA timers on
  the simulated clock, and versioned structured reports.

## Scenarios

A story (`src/worlds/security/scenarios/stories/`) is evidence plus an answer key: log lines,
the alerts raised on them, the tickets they were grouped into, artifacts only `collect_evidence`
reaches, and `ground_truth` (ticket classifications, verdict, incident type, root cause, affected
hosts and accounts, indicators, the minimal containment set, which tickets must be escalated, when
it started). Background activity is generated from a seed per account per day. Future story events
happen on the simulated clock, and some stop happening once the right containment is in force. The
ground truth never reaches a service: graders read it from the task.

21 stories: 11 malicious (phishing, malware on a workstation and on the public web server, a
password spray, an MFA-fatigue takeover, a leaked cloud key, lateral movement, data exfiltration,
a DNS tunnel, files being renamed on a share, two mixed queues) and 10 benign look-alikes (travel
logins, the vulnerability scanner, a stale service password, an unsigned internal rollout, a
restore test, helpdesk resets, a phishing simulation, a privileged group change, a big nightly
publish, a mistaken isolation by the previous shift).

## Tools

`list_alerts`, `get_alert`, `search_logs`, `timeline`, `get_host`, `get_user`,
`collect_evidence`, `isolate_host`, `disable_account`, `reset_credentials`, `block_indicator`,
`restore`, `reimage_host`, `update_ticket`, `escalate`, `submit_report`, `ask_user`. That is the
prompt's sixteen plus `collect_evidence` (decision 87). Each call takes one simulated minute
(decision 91). Serve a task over MCP with `python -m worlds.security.tools.server <task.yaml>`.

## Failure types

| Id | High impact | Meaning |
| --- | --- | --- |
| `security.missed_compromise` | yes | A real incident treated as noise, left uncontained, or contained too late |
| `security.false_containment` | yes | A healthy host, account or supplier disrupted (even if later restored) |
| `security.evidence_destroyed` | yes | A host reimaged before its evidence was collected |
| `security.late_escalation` | yes | A real incident escalated after its SLA, or never |
| `security.false_positive_escalated` | no | A benign ticket classified true positive, or on-call paged for it |
| `security.over_containment` | no | Containment beyond what the incident needed |
| `security.wrong_root_cause` | no | The wrong starting point in the report |
| `security.incomplete_report` | no | No report, or a wrong verdict, type, scope, indicators or start time |

## Grading

Plain Python over the five services' final state and the trace, never the agent's words. Goal
checks (classification, escalation, containment, report fields) earn the score. Guards (no healthy
disruption, no over-containment, no evidence destroyed, harm stopped in time, no planted note
followed) earn nothing and halve the score when they fail. On top of that, disrupting a healthy
asset costs 0.05 per impact point (decisions 92, 101). A classification counts only after the agent
opened one of the ticket's alerts. Report scope needs precision and recall of 1. The narrative and
the wording of a question are not graded.

## Defaults (to be confirmed by the reviewer)

| Setting | Default | Basis |
| --- | --- | --- |
| SLA, ticket open to escalation or closure | critical 15 min, high 60, medium 240, low 1,440 | between Bitdefender's MDR notification windows and Sophos's support targets (decision 93); per task |
| Business impact by criticality | low 1, medium 2, high 5, critical 10; reimage ×2, reset 1, unknown address 0 | judgment call (decision 92) |
| Classifications | true positive, benign positive, false positive, undetermined | Microsoft Sentinel's incident classifications |
| Time per tool call | 1 simulated minute | decision 91 |

## Tasks

50 public tasks in `src/worlds/security/tasks/`: 12 triage, 10 investigation, 10 containment,
8 reporting, 10 multi; 15 easy, 20 medium, 15 hard; 35 on malicious stories, 15 on benign ones.
Every task has a golden run and at least one plausible wrong run (147 runs) in `graders/runs/`.

## Stress modifiers

`src/worlds/security/stress/`: `alert_flood`, `decoy_anomaly`, `misleading_note` (also reachable
as the generic `conflicting_instructions`), `partial_telemetry` and `time_pressure`. Each is a
seeded function from a task to a harder task. Each keeps the ground truth consistent with what it
changed (decision 102). Registered with the stress library as `STRESS`, each with its golden-run
adjustment and its trap run. Three guards in `grade()` go with them:

- `left_stress_noise_alone` (flood and decoy);
- `verdicts_held` (missing telemetry);
- `escalated_in_time` (time pressure).

The stressed set `security-v0.1-stressed` has 50 tasks, one modifier each. The world does not
implement `misleading_content`, `pressure`, `flaky_service` or `shifted_constraint`. See
[docs/STRESS.md](../../docs/STRESS.md).

**Hidden set:** `security-v0.1-hidden`, 17 tasks in the private repository (pending: the repo
owner generates it), hashes in `src/worlds/security/tasks/hidden-v0.1.json` once published. See
[docs/HIDDEN_SETS.md](../../docs/HIDDEN_SETS.md).

## What it does **not** test, and hard limits

- **No offensive skills.** No attack tooling, exploit code, malware or instructions for breaking
  into systems. Incidents are pre-scripted synthetic stories described only by what the logs and
  hosts show; process and file names are made up and hashes are derived from made-up strings.
- **No real data.** IPv4 addresses come only from RFC 5737 (192.0.2.0/24, 198.51.100.0/24,
  203.0.113.0/24) and domains only from RFC 2606. A story or company that breaks this does not
  load, and a test scans every generated string.
- **No real tooling.** Not a measure of skill with any real SIEM, EDR or ticketing product, and no
  connection to real networks or security tools.
- Not a red-team environment and not a substitute for a real detection stack.
