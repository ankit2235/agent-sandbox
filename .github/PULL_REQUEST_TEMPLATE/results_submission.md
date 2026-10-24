## Results submission

<!-- One model, one task set. This pull request adds exactly one file:
     submissions/<task-set>/<model-slug>.json. See docs/site/submit-results.md. -->

- **Task set:** <!-- e.g. payments-v0.1 -->
- **Model id (as run):** <!-- e.g. openai/gpt-4.1-mini -->
- **Name for the leaderboard:**
- **Provider:**
- **Open weights:** yes / no
- **Runs per task:** <!-- 3 -->
- **Settings changed from the defaults:** none / <!-- list them -->
- **Total cost (USD, as reported):**
- **Runner version (`sandbox --version`):**

## Checklist

- [ ] I ran the whole public set, unchanged, with `sandbox run` (no `--sample`, no task directory).
- [ ] `sandbox submission check <run-dir> --min-runs 3` passes on my machine.
- [ ] The archive at `archive_url` holds exactly that one run directory, and its sha256 is in the file.
- [ ] The run directory holds no hidden task and nothing from outside this sandbox.
- [ ] The model's provider terms allow publishing these outputs.
- [ ] My commit is signed off (`git commit -s`).

A maintainer will rerun a sample of ten tasks with the same model and settings before merging.
