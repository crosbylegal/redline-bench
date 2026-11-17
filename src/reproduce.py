"""Run RedlineBench and write aggregate metrics.

`redlinebench-reproduce` runs the full pipeline against the benchmark
hosted on HuggingFace (`crosbylegal/RedlineBench`):

    1. Resolve / download the benchmark (the `tasks/` tree).
    2. Run an agent over the tasks with Harbor  → a `jobs/<job>/` tree.
    3. Assemble that job output into the `runs/<id>/` layout the metrics
       pipeline expects (trajectories + panel verdicts). The Harbor
       verifier already emits the 3-judge panel per trial, so no
       separate re-judging step is needed.
    4. Build `metrics_summary.json`.
    5. If `--baseline` is given, print a delta table vs. that summary.

A full re-run is non-deterministic (agent sampling + LLM judges), so the
comparison is informational — it is NOT an exact-match gate. Requires
the relevant API keys and a Harbor environment (local Docker or Modal).

Example:
    redlinebench-reproduce --agent claude-code \\
        --model anthropic/claude-opus-4-8 --n-concurrent 8
    # one-task smoke test:
    redlinebench-reproduce --agent claude-code \\
        --model anthropic/claude-opus-4-8 --task redline-s1-t1-g01a
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import metrics_summary
from dataset import get_benchmark_dir

# Judge verdict files the Harbor verifier writes per trial, under
# `<trial>/verifier/judges/`. The file stem becomes the judge label
# (the directory name under `runs/<id>/panel/judges/`).
_VERIFIER_JUDGES_SUBDIR = "verifier/judges"

# Short trajectory-directory names, mirroring panel_reader's map so the
# leaderboard labels stay consistent across runs.
_MODEL_TO_TRAJ_DIR = {
    "gpt-5.5": "gpt55",
    "claude-opus-4-8": "opus48",
    "gemini-3.5-flash": "gemini35",
    "claude-fable-5": "archival-fable5",
}


def _strip_provider(model: str) -> str:
    """Drop the provider prefix; `openrouter/google/x` → `google-x`."""
    return model.split("/", 1)[-1].replace("/", "-")


def _traj_dir_for(model_id: str) -> str:
    return _MODEL_TO_TRAJ_DIR.get(model_id, model_id)


# Agent env keys whose values are treated as secrets when printed.
_SENSITIVE_ENV_KEY = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH", re.IGNORECASE)
# Only a bare reference is safe to print; `${VAR:-fallback}` can carry a literal secret.
_ENV_TEMPLATE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def redact_agent_env(kv: str) -> str:
    """``KEY=VALUE`` for display, with the value masked when the key looks secret.

    A bare ``${VAR}`` reference is shown as-is since it carries no secret;
    anything else under a secret-looking key, defaults included, is masked.
    Only the printed form changes, never what is passed to Harbor.
    """
    key, sep, value = kv.partition("=")
    if sep and value and _SENSITIVE_ENV_KEY.search(key) and not _ENV_TEMPLATE.fullmatch(value):
        return f"{key}=****"
    return kv


def _display_cmd(cmd: Sequence[str]) -> str:
    """The command as printed, with ``--agent-env`` secrets redacted."""
    shown = list(cmd)
    for i in range(1, len(shown)):
        if shown[i - 1] == "--agent-env":
            shown[i] = redact_agent_env(shown[i])
    return " ".join(shown)


def task_name_from_trial_dir(trial_dir: Path) -> str | None:
    if not trial_dir.is_dir() or "__" not in trial_dir.name:
        return None
    return trial_dir.name.rsplit("__", 1)[0]


def expected_tasks(job_dir: Path) -> set[str]:
    """Return the task names a Harbor job was configured to run.

    Read from the job-level ``config.json`` (explicit ``tasks`` entries plus
    ``datasets`` directories filtered by ``task_names`` /
    ``exclude_task_names``). Dataset directories that no longer exist
    locally, or that were sampled with ``n_tasks``, are skipped.
    """
    cfg_path = job_dir / "config.json"
    if not cfg_path.is_file():
        return set()
    cfg = json.loads(cfg_path.read_text())
    names: set[str] = set()
    for task in cfg.get("tasks") or []:
        if task.get("path"):
            names.add(Path(task["path"]).name)
    for ds in cfg.get("datasets") or []:
        root = Path(ds["path"]) if ds.get("path") else None
        if root is None or not root.is_dir() or ds.get("n_tasks"):
            continue
        candidates = [p.name for p in root.iterdir() if (p / "task.toml").is_file()]
        include = ds.get("task_names")
        exclude = ds.get("exclude_task_names") or []
        for name in candidates:
            if include and not any(fnmatch.fnmatch(name, pat) for pat in include):
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in exclude):
                continue
            names.add(name)
    return names


def job_tasks_root(job_dir: Path) -> Path | None:
    """Return the existing ``tasks/`` directory a Harbor job ran against, if any."""
    cfg_path = job_dir / "config.json"
    if not cfg_path.is_file():
        return None
    cfg = json.loads(cfg_path.read_text())
    roots = [Path(ds["path"]) for ds in cfg.get("datasets") or [] if ds.get("path")]
    roots += [Path(t["path"]).parent for t in cfg.get("tasks") or [] if t.get("path")]
    return next((r for r in roots if r.is_dir()), None)


def incomplete_tasks(job_dir: Path) -> list[str]:
    """Return task names in ``job_dir`` with no graded attempt.

    Covers tasks whose every trial lacks ``verifier/grade.json`` as well as
    tasks the job was configured to run but never started (e.g. an
    interrupted job).
    """
    seen: set[str] = set()
    graded: set[str] = set()
    for trial in job_dir.iterdir():
        task = task_name_from_trial_dir(trial)
        if task is None:
            continue
        seen.add(task)
        if (trial / "verifier" / "grade.json").exists():
            graded.add(task)
    return sorted((seen | expected_tasks(job_dir)) - graded)


def errored_tasks(job_dir: Path) -> list[str]:
    """Return task names where every finished attempt recorded an exception."""
    failed: set[str] = set()
    clean: set[str] = set()
    for trial in job_dir.iterdir():
        task = task_name_from_trial_dir(trial)
        if task is None:
            continue
        result = trial / "result.json"
        if not result.exists():
            continue
        data = json.loads(result.read_text())
        (failed if data.get("exception_info") else clean).add(task)
    return sorted(failed - clean)


def read_job_harbor_config(job_dir: Path) -> dict:
    """Read the Harbor run settings from the first trial ``config.json`` in a job.

    Returns ``agent``, ``model``, ``env``, ``agent_kwargs`` (as ``KEY=VALUE``
    strings for ``--agent-kwarg``), ``agent_env`` (as ``KEY=VALUE`` strings for
    ``--agent-env``) and ``agent_timeout_multiplier``.
    """
    for trial in sorted(job_dir.iterdir()):
        cfg_path = trial / "config.json"
        if not cfg_path.is_file():
            continue
        cfg = json.loads(cfg_path.read_text())
        agent = cfg.get("agent") or {}
        environment = cfg.get("environment") or {}
        model = agent.get("model_name")
        if not agent.get("name") or not model:
            continue
        kwargs = agent.get("kwargs") or {}
        return {
            "agent": agent["name"],
            "model": model,
            "env": environment.get("type"),
            "agent_kwargs": [
                f"{k}={v if isinstance(v, str) else json.dumps(v)}"
                for k, v in kwargs.items()
            ],
            "agent_env": [f"{k}={v}" for k, v in (agent.get("env") or {}).items()],
            "agent_timeout_multiplier": cfg.get("agent_timeout_multiplier"),
        }
    raise RuntimeError(f"no trial config.json found under {job_dir}")


def resolve_env_file(env_file: Path | None) -> Path | None:
    """Return ``env_file``, else ``./.env`` if it exists, else None."""
    if env_file is not None:
        return env_file
    default_env = Path(".env")
    return default_env if default_env.is_file() else None


def run_harbor(
    tasks_path: Path,
    *,
    agent: str,
    model: str,
    n_concurrent: int,
    env: str | None,
    env_file: Path | None,
    jobs_dir: Path,
    include_tasks: Sequence[str] | None = None,
    agent_timeout_multiplier: float | None = None,
    agent_kwargs: Sequence[str] | None = None,
    agent_env: Sequence[str] | None = None,
) -> Path:
    """Invoke `harbor run` and return the created job directory."""
    if shutil.which("harbor") is None:
        raise RuntimeError(
            "`harbor` CLI not found on PATH. Install it with "
            "`uv tool install harbor` and ensure Docker (or Modal) is "
            "available. See https://harborframework.com"
        )
    jobs_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in jobs_dir.iterdir() if p.is_dir()}

    cmd = [
        "harbor", "run",
        "-p", str(tasks_path),
        "-a", agent,
        "-m", model,
        "--n-concurrent", str(n_concurrent),
        "--jobs-dir", str(jobs_dir),
        "--yes",
    ]
    if include_tasks:
        for task in include_tasks:
            cmd += ["-i", task]
    if agent_kwargs:
        for kw in agent_kwargs:
            cmd += ["--agent-kwarg", kw]
    if agent_env:
        for kv in agent_env:
            cmd += ["--agent-env", kv]
    if agent_timeout_multiplier is not None:
        cmd += ["--agent-timeout-multiplier", str(agent_timeout_multiplier)]
    if env:
        cmd += ["--env", env]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    print(f"+ {_display_cmd(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        # CalledProcessError carries the raw argv, secrets included; report the
        # redacted command and drop the original from the traceback.
        raise RuntimeError(
            f"harbor run failed (exit {exc.returncode}): {_display_cmd(cmd)}"
        ) from None

    after = [p for p in jobs_dir.iterdir() if p.is_dir() and p.name not in before]
    if not after:
        raise RuntimeError(f"no new job directory created under {jobs_dir}")
    return max(after, key=lambda p: p.stat().st_mtime)


def copy_trial_to_runs(
    trial_dir: Path,
    runs_dir: Path,
    *,
    model_id: str,
    task: str,
) -> bool:
    """Copy one graded trial into the ``runs/`` layout. Returns True on success.

    Any artifacts already present for ``task`` are removed first, so the
    result never mixes files from different attempts.
    """
    grade = trial_dir / "verifier" / "grade.json"
    if not grade.exists():
        print(f"  skip {trial_dir.name}: no verifier/grade.json")
        return False

    traj_dir = _traj_dir_for(model_id)
    dest_traj = runs_dir / "trajectories" / traj_dir / task
    if dest_traj.exists():
        shutil.rmtree(dest_traj)
    for stale in (runs_dir / "panel" / "judges").glob(f"*/{model_id}/{task}.json"):
        stale.unlink()
    dest_traj.mkdir(parents=True)
    shutil.copy2(grade, dest_traj / "grade.json")
    docx = trial_dir / "artifacts" / "contract.docx"
    if docx.exists():
        shutil.copy2(docx, dest_traj / "redline.docx")

    judges_src = trial_dir / _VERIFIER_JUDGES_SUBDIR
    if judges_src.is_dir():
        for jf in judges_src.glob("*.json"):
            dest = runs_dir / "panel" / "judges" / jf.stem / model_id
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jf, dest / f"{task}.json")
    return True


def assemble_runs(job_dir: Path, runs_dir: Path, *, model_id: str) -> int:
    """Convert a Harbor `jobs/<job>/` tree into the `runs/<id>/` layout.

    Produces, for each completed trial:
      runs/<id>/trajectories/<traj_dir>/<task>/grade.json   (← verifier/grade.json)
      runs/<id>/trajectories/<traj_dir>/<task>/redline.docx (← artifacts/contract.docx)
      runs/<id>/panel/judges/<judge>/<model_id>/<task>.json (← verifier/judges/<judge>.json)

    `<traj_dir>` is the short model dir; the panel `<model_id>` matches
    panel_reader's `panel_model` key. Returns the number of trials
    assembled.
    """
    n = 0
    for trial in sorted(job_dir.iterdir()):
        task = task_name_from_trial_dir(trial)
        if task is None:
            continue
        if copy_trial_to_runs(trial, runs_dir, model_id=model_id, task=task):
            n += 1
    return n


def _best_trial_for_task(job_dir: Path, task: str) -> Path | None:
    """Pick the trial dir for ``task`` that has a grade, else the newest dir."""
    candidates = [
        p for p in job_dir.iterdir()
        if p.is_dir() and p.name.startswith(f"{task}__")
    ]
    if not candidates:
        return None
    graded = [p for p in candidates if (p / "verifier" / "grade.json").exists()]
    pool = graded or candidates
    return max(pool, key=lambda p: p.stat().st_mtime)


def merge_jobs_into_runs(
    base_job: Path,
    rerun_job: Path,
    runs_dir: Path,
    *,
    model_id: str,
    override_tasks: set[str] | None = None,
) -> int:
    """Assemble ``base_job``, then overlay selected tasks from ``rerun_job``."""
    assemble_runs(base_job, runs_dir, model_id=model_id)
    tasks = override_tasks if override_tasks is not None else set(incomplete_tasks(base_job))
    for task in sorted(tasks):
        trial = _best_trial_for_task(rerun_job, task)
        if trial is None:
            print(f"  skip rerun merge for {task}: no trial dir in {rerun_job.name}")
            continue
        if copy_trial_to_runs(trial, runs_dir, model_id=model_id, task=task):
            print(f"  merged rerun: {task}")

    traj_root = runs_dir / "trajectories" / _traj_dir_for(model_id)
    if not traj_root.is_dir():
        return 0
    return sum(1 for p in traj_root.iterdir() if (p / "grade.json").is_file())


def _delta_table(regen_path: Path, baseline_path: Path) -> None:
    if not baseline_path.exists():
        print(f"(no baseline at {baseline_path}; skipping comparison)")
        return
    regen = {r["model"]: r for r in json.loads(regen_path.read_text())["leaderboard"]}
    base = {r["model"]: r for r in json.loads(baseline_path.read_text())["leaderboard"]}
    print()
    print("Comparison vs baseline (overall_turn_weighted):")
    print(f"  {'model':<20} {'reproduced':>12} {'published':>12} {'delta':>10}")
    for model in sorted(set(regen) | set(base)):
        r = regen.get(model, {}).get("overall_turn_weighted")
        b = base.get(model, {}).get("overall_turn_weighted")
        if r is None:
            print(f"  {model:<20} {'—':>12} {b:>12.4f} {'(not run)':>10}")
        elif b is None:
            print(f"  {model:<20} {r:>12.4f} {'—':>12} {'(new)':>10}")
        else:
            print(f"  {model:<20} {r:>12.4f} {b:>12.4f} {r - b:>+10.4f}")
    print("\n(Full re-runs vary run-to-run; treat deltas as informational.)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agent", required=True, help="Harbor agent, e.g. claude-code")
    ap.add_argument("--model", required=True,
                    help="LiteLLM model string, e.g. anthropic/claude-opus-4-8")
    ap.add_argument("--task", default=None,
                    help="Run a single task (e.g. redline-s1-t1-g01a) instead of all 140.")
    ap.add_argument(
        "--tasks",
        action="append",
        default=[],
        metavar="NAME",
        help="Run specific tasks in one Harbor job (repeatable). "
             "Mutually exclusive with --task.",
    )
    ap.add_argument("--n-concurrent", type=int, default=8)
    ap.add_argument("--effort", default=None,
                    help="Reasoning effort, passed to the agent as "
                         "reasoning_effort=<value> (e.g. minimal, low, medium, "
                         "high, xhigh).")
    ap.add_argument("--agent-kwarg", action="append", default=[], metavar="KEY=VALUE",
                    help="Extra agent kwarg forwarded to `harbor run --agent-kwarg` "
                         "(repeatable).")
    ap.add_argument("--agent-env", action="append", default=[], metavar="KEY=VALUE",
                    help="Environment variable for the agent process, forwarded to "
                         "`harbor run --agent-env` (repeatable), e.g. "
                         "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=128000.")
    ap.add_argument("--agent-timeout-multiplier", type=float, default=None,
                    help="Multiply the per-task agent timeout (e.g. 3 for slow "
                         "high-effort models that time out at the default 1.0).")
    ap.add_argument("--env", default=None, help="Harbor environment, e.g. modal.")
    ap.add_argument("--env-file", default=None, type=Path,
                    help="Path to a .env file for Harbor (default: ./.env if present).")
    ap.add_argument("--workdir", default="reproduce_out",
                    help="Where jobs/ and runs/ are written.")
    ap.add_argument("--out", default="metrics_summary.json",
                    help="Regenerated metrics summary JSON path.")
    ap.add_argument("--baseline", default=None,
                    help="Optional metrics summary JSON to diff the regenerated "
                         "numbers against; omit to skip the comparison.")
    args = ap.parse_args()

    benchmark = get_benchmark_dir()
    tasks_root = benchmark / "tasks"
    if args.task and args.tasks:
        print("ERROR: pass only one of --task or --tasks")
        return 1
    tasks_path = tasks_root / args.task if args.task else tasks_root
    include_tasks: list[str] | None = None
    if args.tasks:
        include_tasks = sorted(set(args.tasks))
        missing = [t for t in include_tasks if not (tasks_root / t).is_dir()]
        if missing:
            print(f"ERROR: task(s) not found under {tasks_root}: {', '.join(missing)}")
            return 1
    if not tasks_path.exists():
        print(f"ERROR: tasks path not found: {tasks_path}")
        return 1

    workdir = Path(args.workdir)
    jobs_dir = workdir / "jobs"
    model_id = _strip_provider(args.model)

    agent_kwargs = list(args.agent_kwarg)
    if args.effort:
        agent_kwargs.append(f"reasoning_effort={args.effort}")

    job_dir = run_harbor(
        tasks_path, agent=args.agent, model=args.model,
        n_concurrent=args.n_concurrent, env=args.env,
        env_file=resolve_env_file(args.env_file),
        jobs_dir=jobs_dir, include_tasks=include_tasks,
        agent_timeout_multiplier=args.agent_timeout_multiplier,
        agent_kwargs=agent_kwargs,
        agent_env=args.agent_env,
    )
    print(f"job: {job_dir}")

    runs_dir = workdir / "runs" / "reproduce"
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    n = assemble_runs(job_dir, runs_dir, model_id=model_id)
    print(f"assembled {n} trial(s) into {runs_dir}")
    if n == 0:
        print("ERROR: no trials assembled — cannot build metrics summary.")
        return 1

    rc = metrics_summary.run(
        runs=runs_dir, out=args.out, benchmark_dir=benchmark,
        judge_method="panel",
    )
    if rc != 0:
        return rc

    if args.baseline:
        _delta_table(Path(args.out), Path(args.baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
