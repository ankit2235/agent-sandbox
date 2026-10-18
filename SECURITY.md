# Security

This project is a simulator. It contains no real credentials, no real customer data and no
connection to real payment, network or security systems, and it must stay that way.

## Reporting a vulnerability

Email the maintainers at `security@example.invalid` (replace before launch). Please do not open a
public issue for anything exploitable. We aim to acknowledge within a week while the project is
pre-release.

## What counts as a security issue here

- A path by which a sandbox episode can reach the network, the host filesystem outside its run
  directory, or another episode's state.
- Real data of any kind in the repository: credentials, personal data, customer or patient data,
  real indicators of compromise, or anything from a private source.
- Hidden task content, canary strings, or anything that would let a model's training set absorb the
  held-out set.
- A dependency with a known vulnerability that we ship.

## What does not

- An agent behaving badly inside a world. That is the point of the benchmark; file it as a task
  proposal or a grader bug.
- Offensive-security requests. The security world is defensive only, and we do not accept attack
  tooling, exploit code or malware samples in any form.

## Hardening status

Episode sandboxing (no network, read-only filesystem, resource limits per container), dependency
scanning and signed releases are Phase 9 items and are **not** in place yet. Until then, run
untrusted agent code at your own risk.
