# Security

This project is a simulator. It contains no real credentials, no real customer data and no
connection to real payment, network or security systems, and it must stay that way.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
<https://github.com/prarabdha-soni/agent-sandbox/security/advisories/new>.

**Pending, needs the repository owner:** private vulnerability reporting must be turned on in
the repository's settings before that link accepts reports, and no security email address has
been named yet. Until one of them is in place, open an issue that says only that you have a
security report and how to reach you, with no details, and a maintainer will contact you.

Please do not put anything exploitable in a public issue. We aim to acknowledge within a week
while the project is pre-release. Only the latest `main` is supported.

## What counts as a security issue here

- A path by which a sandbox episode can reach the network, the host filesystem outside its run
  directory, or another episode's state.
- Real data of any kind in the repository: credentials, personal data, customer or patient data,
  real indicators of compromise, or anything from a private source.
- Hidden task content, canary strings, or anything that would let a model's training set absorb the
  held-out set.
- A dependency with a known vulnerability that we ship.
- A release artifact whose signature does not verify against this repository's release workflow.

## What does not

- An agent behaving badly inside a world. That is the point of the benchmark; file it as a task
  proposal or a grader bug.
- Offensive-security requests. The security world is defensive only, and we do not accept attack
  tooling, exploit code or malware samples in any form.

## Hardening status

**Episodes in locked-down containers: in place, opt-in.** `sandbox run --isolation container`
runs each episode's world (tool handlers, services, grader) in its own container, and
`sandbox verify --isolation container` and `sandbox submission check --isolation container`
replay recorded traces the same way. Every container gets: no network (`--network none`), a
read-only root filesystem and no tmpfs (`--read-only`, `--ipc none`), every capability dropped,
no new privileges, a non-root user, 1 CPU, 512 MiB of memory with no swap, 128 processes, 120
CPU-seconds, no logging driver, and the image is never pulled. An episode's only writable path
is its own scratch directory; verify gets none. Model calls, provider keys and the agent loop
stay outside. Tests try the network, writes outside the scratch directory, privilege changes
and every limit (`runner/tests/test_isolation_docker.py`, the `container` job in CI). Details:
`docs/site/isolation.md`. The default is still in process: **run untrusted agents with
`--isolation container`.** The results-submission check replays submitted traces this way.

Not covered yet:

- `--guard` with `--isolation container` is refused (the guard reads the live episode state).
- The OpenEnv Space runs its episodes in process, inside its own Space container.
- A scratch directory's disk use is not capped (the container's run time and CPU are).
- Starting containers needs access to the Docker daemon, which is root-equivalent on the host.
- Docker's default seccomp and AppArmor profiles are used, not custom ones; the image's
  operating-system packages are not scanned.

**Dependency scanning: in place.** `.github/workflows/dependency-scan.yml` runs `uv audit` over
`uv.lock` and `npm audit` over the TypeScript SDK on dependency changes, on `main` and weekly
(`scripts/dependency_scan.py`). Advisories we cannot fix yet are accepted one by one, each with
its reason, in `.github/dependency-scan-ignores.toml`; anything else fails, and so does an entry
that is no longer reported.

**Signed releases: set up, pending the first release.** On a version tag, and only on the
owner's repository, `release.yml` signs every archive the build writes to `dist/` and the image with Sigstore through
GitHub's OIDC identity for that workflow (SLSA build provenance, `actions/attest`), and PyPI
receives PEP 740 attestations. No release has been signed yet: that needs the owner to push the
first tag. Verifying a release: `docs/RELEASING.md`.
