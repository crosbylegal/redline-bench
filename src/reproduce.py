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
import json
import shutil
import subprocess
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
    """`anthropic/claude-opus-4-8` → `claude-opus-4-8`.

    OpenRouter/OpenCode model strings can look like
    `openrouter/~z-ai/glm-5.2`; strip the routing provider while keeping
    the assembled run layout to one path segment.
    """
    return model.split("/", 1)[-1].lstrip("~").replace("/", "-")


def _traj_dir_for(model_id: str) -> str:
    return _MODEL_TO_TRAJ_DIR.get(model_id, model_id)


def run_harbor(
    tasks_path: Path,
    *,
    agent: str,
    model: str,
    n_concurrent: int,
    env: str | None,
    env_file: Path | None,
    agent_env: list[str],
    jobs_dir: Path,
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
    if env:
        cmd += ["--env", env]
    if env_file:
        cmd += ["--env-file", str(env_file)]
    for item in agent_env:
        cmd += ["--ae", item]
    print(f"+ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    after = [p for p in jobs_dir.iterdir() if p.is_dir() and p.name not in before]
    if not after:
        raise RuntimeError(f"no new job directory created under {jobs_dir}")
    return max(after, key=lambda p: p.stat().st_mtime)


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
    traj_dir = _traj_dir_for(model_id)
    n = 0
    for trial in sorted(job_dir.iterdir()):
        if not trial.is_dir() or "__" not in trial.name:
            continue
        task = trial.name.rsplit("__", 1)[0]
        grade = trial / "verifier" / "grade.json"
        docx = trial / "artifacts" / "contract.docx"
        if not grade.exists():
            print(f"  skip {trial.name}: no verifier/grade.json")
            continue

        dest_traj = runs_dir / "trajectories" / traj_dir / task
        dest_traj.mkdir(parents=True, exist_ok=True)
        shutil.copy2(grade, dest_traj / "grade.json")
        if docx.exists():
            shutil.copy2(docx, dest_traj / "redline.docx")

        judges_src = trial / _VERIFIER_JUDGES_SUBDIR
        if judges_src.is_dir():
            for jf in judges_src.glob("*.json"):
                dest = runs_dir / "panel" / "judges" / jf.stem / model_id
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copy2(jf, dest / f"{task}.json")
        n += 1
    return n


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
    ap.add_argument("--n-concurrent", type=int, default=8)
    ap.add_argument("--env", default=None, help="Harbor environment, e.g. modal.")
    ap.add_argument("--env-file", default=".env",
                    help="Environment file to pass to Harbor; set to '' to disable.")
    ap.add_argument("--agent-env", action="append", default=[],
                    metavar="KEY=VALUE",
                    help="Environment variable to pass to the Harbor agent; "
                         "repeat for multiple values.")
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
    tasks_path = tasks_root / args.task if args.task else tasks_root
    if not tasks_path.exists():
        print(f"ERROR: tasks path not found: {tasks_path}")
        return 1

    workdir = Path(args.workdir)
    jobs_dir = workdir / "jobs"
    model_id = _strip_provider(args.model)
    env_file = Path(args.env_file) if args.env_file else None
    if env_file and not env_file.exists():
        env_file = None

    job_dir = run_harbor(
        tasks_path, agent=args.agent, model=args.model,
        n_concurrent=args.n_concurrent, env=args.env,
        env_file=env_file, agent_env=args.agent_env, jobs_dir=jobs_dir,
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
