# Hidden task sets

A hidden set keeps scores trustworthy: tasks no model has seen, scored in the same format by the
same graders. They are reported only as an aggregate next to the public score. Hidden tasks never
live in this repository (hard rule 5). Only their hashes do.

## Where they live

A separate private repository, found through `HIDDEN_TASKS_DIR`. It is not a git submodule, and
nothing in this repository points at it except that environment variable.

```
$HIDDEN_TASKS_DIR/
  <world>/<version>/<category>/<id>.yaml   hidden tasks (TaskSpec, visibility hidden, one canary)
  <world>/<version>/set.json               the secret seed and what each task was made from
  <world>/runs-<version>/<id>.yaml         each task's golden run and trap runs (kit run format)
```

Ids are `hidden-001`, `hidden-002`, and so on: they say nothing about the task.

## Size

About a quarter of all tasks: 17 per world next to the 50 public ones (17 of 67, 25%).

## How a set is made (v0.1)

`sandbox hidden generate` builds each hidden task from a public task:

1. It takes the public tasks in an order seeded by the set's secret seed.
2. For each one it picks one or two of the stress modifiers that apply (seeded), each with its
   own seed derived from the secret one ([STRESS.md](STRESS.md)).
3. It keeps a candidate only if the stressed task's golden run passes and at least one trap run
   fails with the failure type it declares (CONTRIBUTING rule 4). Those runs are written next to
   the task.

Same format, same graders.

**The weakness, stated plainly.** In v0.1 a hidden task is a seeded variant of a public task under
public modifiers. A model that memorised the public tasks has not seen the variant's content
(which shop, which payee, which wording, which ticket), but it has seen the base task and could
learn the modifiers from this repository. Whoever learns the seed can rebuild the whole set from
the public code. So the v0.1 hidden set detects copying of public answers, and leaks through the
canaries; it does not measure transfer to unseen jobs. Later sets should add tasks written from
scratch, signed off by the world's reviewer; the tooling already loads, checks and hashes those
the same way.

## Canaries

Every hidden task carries one tag of the form `sandbox-canary:` followed by a UUID. It is drawn
from `SeededRNG(seed)` per task id, never from unseeded randomness. Tags are never shown to the
agent, and no trace, record or export holds them. So a canary in a model's output means the model
has seen the hidden task file itself: it was trained on it, or it was given it.

```bash
uv run sandbox hidden scan runs/some-run/ outputs/model-replies.txt
```

searches files, and every file under directories (run directories included), for any well-formed
canary. With `HIDDEN_TASKS_DIR` set, it also names the set a known canary came from. Canaries of a
retired set still count. Matches go to the terminal only.

## Published hashes

`worlds/<world>/src/worlds/<world>/tasks/hidden-<version>.json` (format `hidden-hashes-v1`) holds:

- the set name (`payments-v0.1-hidden`), the count and the algorithm;
- the sorted sha256 of every task, taken over the task as canonical JSON (sorted keys), so the
  hash does not depend on how the file is laid out;
- one `set_hash` over the sorted list.

No ids, no canaries, no content. Anyone can check later that a released set is the one whose
hashes were published before any model was scored on it. The runner checks it every time:
`--tasks <world>-<version>-hidden` refuses the set if a task is not marked hidden, if a canary is
missing, malformed or repeated, or if the hashes do not match the published list (or there is no
list).

## What protects the public repository

- `scripts/check_no_hidden.py`, as a pre-commit hook and in the `no-hidden-leak` CI job over
  every tracked file. It looks for a canary (the prefix followed by a UUID) and a task file marked
  hidden. Code that only names the prefix, or builds hidden tasks at test time, is fine.
- `sandbox hidden leaks <paths>` in the same CI job: no task file in the repository may hash to a
  published hidden task, whatever its name or layout.
- The runner loads hidden tasks only as the hidden set from `HIDDEN_TASKS_DIR`; a task marked
  hidden in any other directory is refused. `sandbox export` refuses to write if a hidden task id
  or run id would appear in a public file (decision 125). Hidden scores are aggregates only.
- `sandbox hidden generate` refuses to write inside this repository, and refuses to overwrite an
  existing set.
- Tests build their hidden tasks in code, in a temporary directory.

## Making the first set (the repo owner runs this)

Choose a secret seed and keep it out of every public place: no shell history you share, no issue,
no commit. Then, for each world:

```bash
export HIDDEN_TASKS_DIR=/home/ankit-kumar/Desktop/101/agent-sandbox-hidden
uv run sandbox hidden generate --world payments --count 17 --seed <SECRET_SEED>
uv run sandbox hidden generate --world security --count 17 --seed <SECRET_SEED>
uv run sandbox hidden hashes --world payments     # writes .../payments/tasks/hidden-v0.1.json
uv run sandbox hidden hashes --world security     # writes .../security/tasks/hidden-v0.1.json
uv run sandbox hidden check --world payments      # canaries, hashes, every run played
uv run sandbox hidden check --world security
cd "$HIDDEN_TASKS_DIR" && git add -A && git commit -s -m "hidden sets payments-v0.1, security-v0.1"
```

Commit the two `hidden-v0.1.json` files to this repository. Run real models on the set with
`--tasks payments-v0.1-hidden`, as with any set.

## Rotation policy

- **When.** A hidden set is retired at each major release of its world (`v0.1` → `v0.2`), when a
  canary is found in a model's output or in public data, or when the seed or the files may have
  leaked. Whichever comes first.
- **Retiring.** The retired set's task files and runs are published, in this repository or as a
  release asset, unchanged. Anyone can then hash them and compare with the hash list published
  earlier: the published content is the set models were scored on. The retired tasks may join the
  public set, keeping their ids under the old version.
- **Replacing.** A new set is generated with a new secret seed, for the new world version. New
  tasks are written from scratch where reviewers can sign them off. Its hash list is committed
  before any model is scored on it. Scores on the old and new sets are never mixed: each result
  carries its task-set version (hard rule 6).
- **A leak.** If a canary turns up in a model's output, that model's hidden score is withdrawn,
  the set is retired early as above, and the finding is noted in this file.

## Rotations so far

None yet. The first set, `v0.1`, is pending: the repo owner generates it (above).
