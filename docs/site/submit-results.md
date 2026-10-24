# Submitting results

A result is one model's run of a world's **whole public task set**: one `sandbox run` directory.

## 1. Run

```bash
sandbox run --world payments --tasks payments-v0.1 --model <litellm-id> \
    --model-name "<name for the leaderboard>" --provider <provider> [--open-weight] \
    --runs 3 --out runs/<model>/
```

Use the settings as they are (temperature 0, the default prompt); a submission records them all.

## 2. Check it yourself

```bash
sandbox submission check runs/<model>/ --min-runs 3
```

It refuses a run directory unless:

- the manifest and every record are valid `sandbox-run-v1`;
- it ran the whole public set of the world's current version (not a sample, a stressed or hidden
  set, or a task directory), with the current system prompt, every task with every run, all
  completed;
- no record is of a hidden task, and no file holds a hidden-set canary;
- `sandbox verify` reproduces every run: same responses, same state changes, same grades;
- the leaderboard files built from it are valid against the `results-v0.1` schemas.

## 3. Publish the run directory and open a pull request

Run directories are never committed. Pack yours, put it somewhere anyone can download it over
https (a GitHub release asset of your fork works), and note its sha256:

```bash
tar czf <model>.tar.gz -C runs <model>
sha256sum <model>.tar.gz
```

Then open a pull request that adds one file, `submissions/payments-v0.1/<model-slug>.json`, using
the [results submission template](https://github.com/prarabdha-soni/agent-sandbox/compare/main...main?template=results_submission.md):

```json
{
  "format": "submission-v1",
  "task_set": "payments-v0.1",
  "model": "openai/gpt-4.1-mini",
  "model_name": "GPT-4.1 mini",
  "provider": "openai",
  "open_weight": false,
  "archive_url": "https://github.com/<you>/agent-sandbox/releases/download/<tag>/<model>.tar.gz",
  "archive_sha256": "<64 hex characters>",
  "contact": "<how to reach you>"
}
```

`model`, `model_name` and `provider` must be what the run directory recorded.

## 4. What CI and the maintainers do

- **CI, on the pull request** (`.github/workflows/results-submission.yml`, no secrets): fails if
  the pull request changes anything but `submissions/**.json`; then for each submission file,
  `sandbox submission fetch` downloads the archive (500 MiB at most), checks its sha256, unpacks it
  safely (no path may leave the folder) and checks it holds exactly one run directory that matches
  the file; then `sandbox submission check` runs every check above.
- **A maintainer, by hand** (the same workflow's *spot-check* job, with the project's model keys):
  reruns a seeded sample of ten tasks with the same model and settings and compares the pass rate
  on those tasks:

  ```bash
  sandbox submission spot-check <run-dir> --sample 10 --seed <the workflow's run id>
  ```

  The seed is chosen when the job runs, so a submitter cannot know the sample in advance. It fails
  if the rerun scores more than 30 points below the submission on the same tasks. The tolerance is
  a judgement call, not a measured figure: at temperature 0 a model's ten-task pass rate can still
  move, so only a large gap is treated as a sign the actions were not the model's. `--tolerance`
  changes it.
- Accepted results are exported with `sandbox export` into the leaderboard's data, and the run
  directory joins the release's archive so anyone can verify it.

Hidden-set scores are added by maintainers only, from runs on the private set, and published only
as an aggregate.
