# Results submissions

One file per model and task set: `submissions/<task-set>/<model-slug>.json` (format
`submission-v1`, defined by `runner.submission.Submission`). It names a `.tar.gz` of the model's
`sandbox run` directory and that archive's sha256. Run directories themselves are never committed
here.

How to submit, and what CI and the maintainers check: `docs/site/submit-results.md`, or
<https://prarabdha-soni.github.io/agent-sandbox/submit-results/>.
