# Releasing a task set (payments-v0.1)

The release is gated by `sandbox release-check payments-v0.1`, which names every condition that
fails and what it waits on. Code cannot close the conditions that need people, real model runs or
outside accounts; this page lists those steps, in order. Every step here is the repo owner's.

## 0. The package name

The packages are built as `agent-sandbox-bench` (the name `agent-sandbox` on PyPI is someone
else's). To use another name, before anything is published:

```bash
python3 scripts/rename_distribution.py <new-name>
uv lock
uv run pytest runner/tests/test_packaging.py
```

## 1. Reviewer sign-off

The payments reviewer checks each of the 50 graders, its golden and wrong runs and the failure
types, and each task gets `reviewer_signoff: {name, role, date}`. Sign-off does not change a task's
hash, so runs made before it stay valid. If the reviewer asks for a task to change, change it, then
refreeze: `uv run sandbox freeze payments-v0.1`.

## 2. Real model runs

Results, for 5–6 models (each needs its provider's key; `uv sync --all-packages --extra litellm`):

```bash
uv run sandbox run --world payments --tasks payments-v0.1 --model <litellm-id> \
    --model-name "<leaderboard name>" --provider <provider> [--open-weight] \
    --runs 3 --concurrency 4 --max-cost 0.50 --out ~/sandbox-results/payments-v0.1/<model-slug>
```

The hidden set, for the same models (stays private; never archived publicly):

```bash
HIDDEN_TASKS_DIR=/home/ankit-kumar/Desktop/101/agent-sandbox-hidden \
uv run sandbox run --world payments --tasks payments-v0.1-hidden --model <litellm-id> \
    --model-name "<leaderboard name>" --provider <provider> \
    --runs 3 --out ~/sandbox-results/payments-v0.1/hidden/<model-slug>
```

The stress calibration, with 2–3 cheap models (`docs/STRESS.md`), then commit its report:

```bash
uv run sandbox stress calibrate --world payments --model <id-1> --model <id-2> \
    --out ~/sandbox-calibration/payments-v0.1
mkdir -p docs/calibration/payments-v0.1
cp ~/sandbox-calibration/payments-v0.1/report.{json,md} docs/calibration/payments-v0.1/
```

Fix or drop any modifier the report marks `too_easy` or `too_hard` (decision 141).

## 3. The leaderboard files

```bash
HIDDEN_TASKS_DIR=/home/ankit-kumar/Desktop/101/agent-sandbox-hidden \
uv run sandbox export ~/sandbox-results/payments-v0.1/*/ ~/sandbox-results/payments-v0.1/hidden/*/ \
    --out leaderboard/public/data --updated YYYY-MM-DD
```

Commit `leaderboard/public/data/` (public tasks and traces, hidden runs only as aggregates) and
load it in the leaderboard site.

## 4. The gate

```bash
HIDDEN_TASKS_DIR=/home/ankit-kumar/Desktop/101/agent-sandbox-hidden \
uv run sandbox release-check payments-v0.1 --results ~/sandbox-results/payments-v0.1
```

All nine conditions must pass. Run it on a clean checkout of the commit you will tag.

## 5. Accounts, once

- **PyPI**: a *pending trusted publisher* for each of the six projects
  (`agent-sandbox-bench`, `agent-sandbox-bench-kit`, `-runner`, `-stress`, `-world-payments`,
  `-world-security`): owner `prarabdha-soni`, repository `agent-sandbox`, workflow `release.yml`,
  environment `pypi`.
- **GitHub environments**: `pypi` (add required reviewers if you like) and `spot-check`, holding
  the provider keys the results spot-check may use (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY`, `OPENROUTER_API_KEY`; only those you have).
- **GitHub Pages**: Settings → Pages → Source: *GitHub Actions*. The site is published by
  `docs.yml` on the next push to main.
- **GHCR**: after the first image push, make the `agent-sandbox` package public.

## 6. Tags

```bash
git tag -a payments-v0.1 -m "payments-v0.1: 50 public tasks, frozen"
git tag -a v0.1.0 -m "agent-sandbox-bench 0.1.0"
git push origin payments-v0.1 v0.1.0
```

Pushing `v0.1.0` runs `release.yml`: it checks the tag matches the packages' version, builds the
six distributions, publishes them to PyPI and pushes `ghcr.io/prarabdha-soni/agent-sandbox:0.1.0`.

### Checking the signatures

The same run signs everything it releases with Sigstore, keylessly, through GitHub's OIDC
identity for `release.yml` on this repository (no key to keep): every archive in `dist/` and the
image get SLSA build provenance (`actions/attest@v4.2.2`; the image's is pushed to GHCR beside
it), and PyPI receives PEP 740 attestations from the publish action. The jobs run only on
`prarabdha-soni/agent-sandbox`, never on a fork. After the first release, check them:

```bash
gh attestation verify agent_sandbox_bench-0.1.0-py3-none-any.whl --repo prarabdha-soni/agent-sandbox
gh attestation verify oci://ghcr.io/prarabdha-soni/agent-sandbox:0.1.0 --repo prarabdha-soni/agent-sandbox
```

On PyPI each file's page shows its attestation ("Verified details"). No release has been signed
yet: the first one is the owner's tag.

## 7. The GitHub release

Create the release for `v0.1.0` with the `CHANGELOG.md` section and the calibration report, and
attach the **public** run directories as one archive, so anyone can verify the leaderboard. Check
each one first (the check includes the canary search), then pack them without `hidden/`:

```bash
cd ~/sandbox-results/payments-v0.1
public=$(ls -d */ | grep -v '^hidden/$')
for dir in $public; do uv run --project <repo> sandbox submission check "$dir" --min-runs 3; done
tar czf ../payments-v0.1-runs.tar.gz --transform 's,^,payments-v0.1-runs/,' $public
tar tzf ../payments-v0.1-runs.tar.gz | grep -c '/hidden/'   # must print 0
```

Never include the `hidden/` run directories: their traces hold hidden task content.

## 8. The OpenEnv / Hugging Face listing

After the packages are on PyPI, from the repository root (the openenv extra is installed with
`uv sync --all-packages --extra openenv`):

```bash
(cd worlds/payments/openenv && uv lock)
uv run openenv validate worlds/payments/openenv --level static --skip-build
uv run openenv push worlds/payments/openenv --repo-id <namespace>/agent-sandbox-payments
```

Commit the Space's `uv.lock`.
