The DATA FILES example from the leaderboard prompt ("Payments Sandbox — Build Prompts.md",
Phase 4), transcribed as JSON. Where the prompt writes `...`, a concrete value of the same shape
is filled in. `test_export.py` checks that the runner's schemas accept these files unchanged, so
the schemas describe the format the leaderboard expects, not only what the runner writes.
