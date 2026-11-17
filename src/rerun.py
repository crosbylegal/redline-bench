"""Rerun incomplete Harbor trials and merge results into a runs layout.

`redlinebench-rerun` finds tasks in a prior job that never produced a
grade, reruns them in a single Harbor job, then merges the base job with
the rerun job for metrics.

Agent, model, environment, agent kwargs (e.g. ``reasoning_effort``), agent
env and the agent timeout multiplier are inherited from the original job
unless overridden on the command line. Harbor saves secret-looking agent env
values masked, so those must be passed again with ``--agent-env``.

Example:
    # rerun every task that never produced a grade
    redlinebench-rerun --from-job reproduce_out/jobs/<job>
    # also rerun trials that errored, with a longer agent timeout
    redlinebench-rerun --from-job reproduce_out/jobs/<job> \\
        --include-errors --agent-timeout-multiplier 3
    # preview the selection without running Harbor
    redlinebench-rerun --from-job reproduce_out/jobs/<job> --dry-run
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import metrics_summary
from dataset import get_benchmark_dir
from reproduce import (
    _strip_provider,
    errored_tasks,
    incomplete_tasks,
    job_tasks_root,
    merge_jobs_into_runs,
    read_job_harbor_config,
    resolve_env_file,
    run_harbor,
)


def _load_tasks_file(path: Path) -> list[str]:
    tasks = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tasks.append(line)
    if not tasks:
        raise ValueError(f"no tasks found in {path}")
    return tasks


def _resolve_tasks(
    *,
    from_job: Path,
    tasks: list[str],
    tasks_file: Path | None,
    include_errors: bool,
) -> list[str]:
    if tasks_file is not None:
        explicit = _load_tasks_file(tasks_file)
    else:
        explicit = list(tasks)

    if explicit:
        return sorted(set(explicit))

    selected = set(incomplete_tasks(from_job))
    if include_errors:
        selected.update(errored_tasks(from_job))
    if not selected:
        raise ValueError(f"no tasks to rerun under {from_job}")
    return sorted(selected)


def merge_agent_kwargs(inherited: list[str], overrides: list[str]) -> list[str]:
    """Combine ``KEY=VALUE`` lists; a key in ``overrides`` replaces ``inherited``."""
    merged: dict[str, str] = {}
    for kw in [*inherited, *overrides]:
        key, sep, _ = kw.partition("=")
        if not sep:
            raise ValueError(f"agent kwarg must be KEY=VALUE, got {kw!r}")
        merged[key] = kw
    return list(merged.values())


def masked_agent_env(env: list[str]) -> list[str]:
    """Keys whose ``KEY=VALUE`` value is Harbor's redaction of a secret-looking env var.

    Harbor saves sensitive agent env (keys containing TOKEN, KEY, SECRET, ...) as
    ``****`` or ``abcd****xyz`` unless the value is a ``${VAR}`` template, so
    reusing it verbatim would hand the agent the mask instead of the value.
    """
    return [
        key
        for key, _, value in (kv.partition("=") for kv in env)
        if re.fullmatch(r"(.{4})?\*{4}(.{3})?", value)
    ]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--from-job",
        type=Path,
        required=True,
        help="Harbor job directory with completed + incomplete trials.",
    )
    ap.add_argument(
        "--tasks",
        action="append",
        default=[],
        metavar="NAME",
        help="Task to rerun (repeatable). Default: auto-detect from --from-job.",
    )
    ap.add_argument(
        "--tasks-file",
        type=Path,
        help="Newline-separated task names to rerun (lines starting with # ignored).",
    )
    ap.add_argument(
        "--include-errors",
        action="store_true",
        help="When auto-detecting, also rerun trials that ended with agent errors "
             "(even if a grade exists).",
    )
    ap.add_argument("--agent", help="Harbor agent (default: read from --from-job).")
    ap.add_argument("--model", help="LiteLLM model string (default: read from --from-job).")
    ap.add_argument("--env", help="Harbor environment, e.g. modal (default: from job).")
    ap.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Path to a .env file for Harbor (default: ./.env if present).",
    )
    ap.add_argument(
        "--n-concurrent",
        type=int,
        default=None,
        help="Concurrent trials for the rerun job (default: min(len(tasks), 8)).",
    )
    ap.add_argument(
        "--effort",
        default=None,
        help="Override reasoning effort (passed as reasoning_effort=<value>).",
    )
    ap.add_argument(
        "--agent-kwarg",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra agent kwarg forwarded to `harbor run --agent-kwarg` "
             "(repeatable). Overrides the same key from --from-job.",
    )
    ap.add_argument(
        "--agent-env",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Agent environment variable forwarded to `harbor run --agent-env` "
             "(repeatable). Overrides the same key from --from-job.",
    )
    ap.add_argument(
        "--agent-timeout-multiplier",
        type=float,
        default=None,
        help="Multiply the per-task agent timeout (default: from job).",
    )
    ap.add_argument(
        "--workdir",
        default="reproduce_out",
        help="Where jobs/ and runs/ are written.",
    )
    ap.add_argument(
        "--runs-id",
        default="rerun",
        help="Subdirectory under workdir/runs/ for merged output.",
    )
    ap.add_argument(
        "--out",
        default="metrics_summary.json",
        help="Metrics summary JSON path.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print selected tasks and exit without running Harbor.",
    )
    ap.add_argument(
        "--skip-metrics",
        action="store_true",
        help="Merge runs only; do not rebuild metrics_summary.json.",
    )
    args = ap.parse_args()

    from_job = args.from_job.expanduser().resolve()
    if not from_job.is_dir():
        print(f"ERROR: job directory not found: {from_job}")
        return 1
    if args.runs_id in ("", ".", "..") or Path(args.runs_id).name != args.runs_id:
        print(f"ERROR: --runs-id must be a single directory name, got {args.runs_id!r}")
        return 1

    try:
        task_names = _resolve_tasks(
            from_job=from_job,
            tasks=args.tasks,
            tasks_file=args.tasks_file,
            include_errors=args.include_errors,
        )
        job_cfg = read_job_harbor_config(from_job)
        overrides = list(args.agent_kwarg)
        if args.effort:
            overrides.append(f"reasoning_effort={args.effort}")
        agent_kwargs = merge_agent_kwargs(job_cfg["agent_kwargs"], overrides)
        agent_env = merge_agent_kwargs(job_cfg["agent_env"], list(args.agent_env))
        masked = masked_agent_env(agent_env)
        if masked:
            raise ValueError(
                f"agent env {', '.join(masked)} was saved masked by Harbor; "
                "pass it again with --agent-env KEY=VALUE"
            )
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1

    agent = args.agent or job_cfg["agent"]
    model = args.model or job_cfg["model"]
    for flag, value, original in (
        ("--agent", agent, job_cfg["agent"]),
        ("--model", model, job_cfg["model"]),
    ):
        if value != original:
            print(f"WARNING: {flag} {value} differs from the base job ({original}); "
                  "merged results will mix the two.")
    env = args.env if args.env is not None else job_cfg["env"]
    model_id = _strip_provider(model)
    timeout_multiplier = (
        args.agent_timeout_multiplier
        if args.agent_timeout_multiplier is not None
        else job_cfg["agent_timeout_multiplier"]
    )

    print(f"from-job: {from_job}")
    print(f"agent: {agent}  model: {model}  env: {env}")
    if agent_kwargs:
        print(f"agent kwargs: {' '.join(agent_kwargs)}")
    if agent_env:
        print(f"agent env: {' '.join(agent_env)}")
    if timeout_multiplier is not None:
        print(f"agent timeout multiplier: {timeout_multiplier}")
    print(f"tasks to rerun ({len(task_names)}):")
    for name in task_names:
        print(f"  {name}")

    if args.dry_run:
        return 0

    tasks_root = job_tasks_root(from_job)
    if tasks_root is None:
        tasks_root = get_benchmark_dir() / "tasks"
        print(f"WARNING: tasks dir recorded in {from_job.name} is gone; "
              f"falling back to {tasks_root}, which may differ from the base job.")
    benchmark = tasks_root.parent
    workdir = Path(args.workdir)
    jobs_dir = workdir / "jobs"
    n_concurrent = args.n_concurrent
    if n_concurrent is None:
        n_concurrent = min(len(task_names), 8)

    rerun_job = run_harbor(
        tasks_root,
        agent=agent,
        model=model,
        n_concurrent=n_concurrent,
        env=env,
        env_file=resolve_env_file(args.env_file),
        jobs_dir=jobs_dir,
        include_tasks=task_names,
        agent_timeout_multiplier=timeout_multiplier,
        agent_kwargs=agent_kwargs,
        agent_env=agent_env,
    )
    print(f"rerun job: {rerun_job}")

    runs_dir = workdir / "runs" / args.runs_id
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    n = merge_jobs_into_runs(
        from_job,
        rerun_job,
        runs_dir,
        model_id=model_id,
        override_tasks=set(task_names),
    )
    print(f"merged {n} trial(s) into {runs_dir}")
    if n == 0:
        print("ERROR: no trials assembled — cannot build metrics summary.")
        return 1

    if args.skip_metrics:
        return 0

    return metrics_summary.run(
        runs=runs_dir,
        out=args.out,
        benchmark_dir=benchmark,
        judge_method="panel",
    )


if __name__ == "__main__":
    raise SystemExit(main())
