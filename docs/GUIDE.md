# Guide

This guide covers the practical path through RedlineBench: install the tooling,
run a small task, reproduce a larger run, and understand the files the pipeline
writes.

For the benchmark structure, see [Benchmark Design](DESIGN.md). For
scoring details, see [Evaluation](EVALUATION.md).

## Setup

RedlineBench runs tasks through [Harbor](https://harborframework.com), so Docker
must be available locally unless you run in a cloud Harbor environment.

```bash
pip install -e .
uv tool install harbor
```

Copy `.env.template` to `.env` and add the provider keys you need. The verifier
uses a judge panel across model providers, so reproduction runs need keys for
the supported judge providers.

```bash
cp .env.template .env
```

Do not commit `.env` or a local `benchmark/` directory.

## Dataset Resolution

The benchmark tasks are not committed to this repository. `dataset.py` resolves
the benchmark root in this order:

1. `./benchmark/`, if it exists.
2. `$REDLINEBENCH_BENCHMARK_DIR`, if set.
3. A cached Hugging Face download of `crosbylegal/RedlineBench`.

The resolved benchmark root contains a `tasks/` directory with runnable Harbor
tasks.

## Run One Task

Use a single task as a smoke test before launching a larger run:

```bash
redlinebench-reproduce \
  --agent claude-code \
  --model anthropic/claude-opus-4-8 \
  --task redline-s1-t1-g01a
```

This runs the agent, collects the edited `contract.docx`, grades the output, and
writes the intermediate run files under the work directory.

## Reproduce A Full Run

Omit `--task` to run the benchmark task set:

```bash
redlinebench-reproduce \
  --agent claude-code \
  --model anthropic/claude-opus-4-8 \
  --n-concurrent 8
```

For cloud parallelism through Harbor, pass the Modal environment:

```bash
redlinebench-reproduce \
  --agent claude-code \
  --model anthropic/claude-opus-4-8 \
  --env modal \
  --n-concurrent 8
```

To compare against an earlier metrics summary, pass:

```bash
redlinebench-reproduce \
  --agent claude-code \
  --model anthropic/claude-opus-4-8 \
  --baseline path/to/metrics_summary.json
```

Run-to-run differences are expected. Agents sample, judge calls can vary, and
the comparison output should be treated as a reproducibility check rather than a
bit-exact test.

## Useful Flags

- `--task redline-s1-t1-g01a`: run one task instead of the full benchmark.
- `--tasks NAME` (repeatable): run a specific subset of tasks in one Harbor job.
- `--env modal`: run through Harbor's Modal environment.
- `--env-file PATH`: `.env` file passed to Harbor (defaults to `./.env` if present).
- `--n-concurrent N`: set the number of parallel trials.
- `--effort LEVEL`: reasoning effort, forwarded as `--agent-kwarg reasoning_effort=LEVEL`.
- `--agent-kwarg KEY=VALUE` (repeatable): any extra Harbor agent kwarg.
- `--agent-env KEY=VALUE` (repeatable): an environment variable for the agent process inside the sandbox,
  forwarded as `harbor run --agent-env`. Pass secrets as a literal `${VAR}` template (single-quoted) so Harbor
  resolves them from `.env` at runtime and never writes the value to job files. Values of secret-looking keys are
  masked as `****` in the command and summaries these tools print.
- `--agent-timeout-multiplier X`: scale the per-task agent timeout for slow models.
- `--workdir DIR`: choose where `jobs/` and `runs/` are written.
- `--out PATH`: choose where the metrics summary JSON is written.
- `--baseline PATH`: print a comparison against an existing metrics summary.

## Rerun Incomplete Tasks

Long runs can leave some trials without a grade (agent timeouts, sandbox
failures, provider errors). `redlinebench-rerun` finds those tasks in an
existing job, reruns only them in a new Harbor job, and merges both jobs into a
single runs directory before rebuilding the metrics summary:

```bash
# Preview which tasks would be rerun
redlinebench-rerun --from-job reproduce_out/jobs/<job> --dry-run

# Rerun ungraded tasks plus trials that errored, with a longer agent timeout
redlinebench-rerun --from-job reproduce_out/jobs/<job> \
  --include-errors --agent-timeout-multiplier 3
```

Agent, model, environment, agent kwargs (such as `reasoning_effort`), agent env,
and the timeout multiplier are inherited from the original job; any of them can be
overridden on the command line. Harbor saves secret-looking agent env values (any
key containing `KEY`, `TOKEN`, `SECRET`, ...) masked unless they were passed as a
`${VAR}` template, so the rerun stops and asks you to pass those again with
`--agent-env`. Pass `--tasks NAME` or `--tasks-file PATH` to
choose the tasks explicitly instead of auto-detecting them. Merged output is
written to `reproduce_out/runs/rerun/` (see `--runs-id`).

## Output Layout

By default, `redlinebench-reproduce` writes Harbor jobs and assembled runs under
`reproduce_out/`, and writes the metrics summary to top-level
`metrics_summary.json`.

```text
metrics_summary.json
reproduce_out/
  jobs/
    <harbor-job>/
      ...
  runs/
    <run-id>/
      trajectories/
      panel/
```

The reproduction driver adapts Harbor output into the layout expected by the
metrics pipeline:

- `verifier/grade.json` becomes a per-task grade record.
- `artifacts/contract.docx` becomes the model redline artifact.
- `verifier/judges/*.json` becomes the judge-panel record used for scoring.

## Command Reference

RedlineBench exposes these console scripts:

- `redlinebench-reproduce`: end-to-end run, grading, and metrics summary.
- `redlinebench-rerun`: rerun incomplete or errored tasks from a prior job and merge the results.
- `redlinebench-aggregate`: summarize a Harbor job tree into per-task and summary files.
- `redlinebench-rejudge`: grade saved outputs with a different judge model.
- `redlinebench-panel`: assemble judge verdicts into a panel vote and leaderboard files.

The source modules live directly under `src/`:

- `dataset.py`: resolves local or downloaded benchmark data.
- `reproduce.py`: coordinates Harbor runs and metrics summary generation.
- `rerun.py`: reruns incomplete tasks from an existing Harbor job and merges them into one runs layout.
- `metrics_summary.py`: computes leaderboard fields, breakdowns, diagnostics, and aggregate summary data.
- `aggregate.py`: aggregates per-task scores into benchmark summaries.
- `panel.py`: shared scoring and panel-vote helpers.
- `panel_reader.py`: reads panel-majority judge records.
- `runs_reader.py`: reads single-judge grade records.
- `docx_metrics.py`: reads `.docx` outputs for document-level diagnostics.
- `judging.py`: builds judge prompts and calls judge models.
- `rejudge.py`: reruns judging over saved model outputs.
