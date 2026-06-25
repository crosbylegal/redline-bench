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
the supported judge providers. OpenRouter-backed `opencode` runs also need
`OPENROUTER_API_KEY`.

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

To route an `opencode` run through OpenRouter, pass the OpenRouter API key.
This example runs GLM 5.2 with 70 concurrent Modal trials:

```bash
redlinebench-reproduce \
  --agent opencode \
  --model openrouter/z-ai/glm-5.2 \
  --env modal \
  --n-concurrent 70 \
  --workdir reproduce_out_openrouter_glm52 \
  --out metrics_summary_openrouter_glm52.json \
  --agent-env "OPENROUTER_API_KEY=$OPENROUTER_API_KEY"
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
- `--env modal`: run through Harbor's Modal environment.
- `--env-file PATH`: pass environment variables to Harbor; defaults to `.env`
  when present.
- `--agent-env KEY=VALUE`: pass an environment variable to the Harbor agent.
- `--n-concurrent N`: set the number of parallel trials.
- `--workdir DIR`: choose where `jobs/` and `runs/` are written.
- `--out PATH`: choose where the metrics summary JSON is written.
- `--baseline PATH`: print a comparison against an existing metrics summary.

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
- `redlinebench-aggregate`: summarize a Harbor job tree into per-task and summary files.
- `redlinebench-rejudge`: grade saved outputs with a different judge model.
- `redlinebench-panel`: assemble judge verdicts into a panel vote and leaderboard files.

The source modules live directly under `src/`:

- `dataset.py`: resolves local or downloaded benchmark data.
- `reproduce.py`: coordinates Harbor runs and metrics summary generation.
- `metrics_summary.py`: computes leaderboard fields, breakdowns, diagnostics, and aggregate summary data.
- `aggregate.py`: aggregates per-task scores into benchmark summaries.
- `panel.py`: shared scoring and panel-vote helpers.
- `panel_reader.py`: reads panel-majority judge records.
- `runs_reader.py`: reads single-judge grade records.
- `docx_metrics.py`: reads `.docx` outputs for document-level diagnostics.
- `judging.py`: builds judge prompts and calls judge models.
- `rejudge.py`: reruns judging over saved model outputs.
