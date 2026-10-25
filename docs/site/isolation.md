# Running episodes in containers

An agent never runs code of its own here, but its tool arguments drive world code: the tool
handlers, the services and the grader. A recorded trace does the same when `sandbox verify`
replays it, and submitted traces come from outside. `--isolation container` runs that code in a
locked-down Docker container per episode; the model calls, the provider keys and the agent loop
stay outside it.

```bash
docker build -t agent-sandbox:0.1.0 .           # once; containers never pull an image

uv run sandbox run --world payments --tasks payments-v0.1 --model fake:golden \
    --out runs/contained --isolation container
uv run sandbox verify --run runs/contained --isolation container
uv run sandbox submission check <run dir> --isolation container
```

`--image` names another image (the default is `agent-sandbox:<version>`). The run directory
comes out byte for byte the same as an in-process run, so a directory can be resumed or
verified either way, and every score stays the same.

## What an episode container gets

| Setting | Value |
| --- | --- |
| Network | none (`--network none`): only a loopback interface |
| Filesystem | read-only root (`--read-only`), no tmpfs, no `/dev/shm` (`--ipc none`) |
| Writable | only `runs/<run_id>/episode/`, removed when the run ends; verify gets nothing writable |
| User | the runner's own uid and gid, never root; a runner started as root uses `65534:65534` |
| Privileges | every capability dropped, `no-new-privileges`, Docker's default seccomp profile |
| Limits | 1 CPU, 512 MiB memory and no swap, 128 processes, 120 CPU-seconds |
| Time | 60 s for each answer over MCP; 60 s to grade and exit after the run; 30 min for a whole `verify` |
| Logs | no logging driver: the MCP traffic carries task content |
| Image | `--pull never` |

The memory and CPU numbers are measured: in the container, playing every golden and wrong run of
a world in one process peaks at 145 MiB (payments) and 159 MiB (security) and takes 18 and 27
CPU-seconds; the imports alone are 68 MiB and 1.2 CPU-seconds.

## How the runner talks to it

The boundary is MCP over stdio, the protocol the agent already speaks in process:

1. the runner writes the task as JSON to a private temporary file, mounted read-only;
2. `docker run -i ... sandbox episode serve` builds the episode through the world registry and
   serves its tools on stdin and stdout;
3. when the run ends, the worker grades it and writes `episode.json` into its scratch directory;
4. the runner reads that file defensively (a regular file, size-capped, validated, the same task
   and version), puts the model's messages back into the trace where they were said, and writes
   `trace.jsonl` and `result.json`.

A container that is killed (out of memory, CPU time), times out, exits with an error or writes a
bad file makes that run an `error` run with the reason in `result.json`: it is not graded, and
running the same command again retries it, as with a provider failure. Before the first run the
runner checks that Docker is installed and running and that the image exists.

## Not covered yet

- `--guard` cannot be combined with `--isolation container`: the guard reads the live episode
  state, which stays inside the container.
- Multi-agent tasks cannot run with `--isolation container` yet: the runner plays the counterparts
  from the episode's turn state, which stays inside the container. `sandbox verify --isolation
  container` checks them.
- A scratch directory's disk use is not capped.
- Starting containers needs access to the Docker daemon, which is root-equivalent on the host.
- Containers that a killed runner leaves behind carry the label `agent-sandbox.episode`:
  `docker container prune --filter label=agent-sandbox.episode`.
