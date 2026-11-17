"""Tests for Harbor job rerun helpers."""

from __future__ import annotations

import json
from pathlib import Path

import reproduce
import rerun


def _trial(
    job: Path,
    task: str,
    *,
    graded: bool = True,
    error: str | None = None,
    agent_kwargs: dict | None = None,
    agent_env: dict | None = None,
    agent_timeout_multiplier: float | None = None,
    suffix: str = "abc123",
    docx: bool = False,
) -> None:
    trial = job / f"{task}__{suffix}"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps(
            {
                "agent": {
                    "name": "claude-code",
                    "model_name": "anthropic/claude-opus-4-8",
                    "kwargs": agent_kwargs or {},
                    **({"env": agent_env} if agent_env else {}),
                },
                "environment": {"type": "modal"},
                "agent_timeout_multiplier": agent_timeout_multiplier,
            }
        )
    )
    if graded:
        grade = trial / "verifier" / "grade.json"
        grade.parent.mkdir(parents=True)
        grade.write_text('{"score": {"weighted": 0.5}}')
        judges = trial / "verifier" / "judges"
        judges.mkdir(parents=True)
        (judges / "judge-a.json").write_text("{}")
    if docx:
        (trial / "artifacts").mkdir()
        (trial / "artifacts" / "contract.docx").write_text("docx")
    if error is not None or graded:
        result = {
            "exception_info": {"exception_type": error} if error else None,
            "verifier_result": {"rewards": {"reward": 0.5}} if graded else None,
        }
        (trial / "result.json").write_text(json.dumps(result))


def test_incomplete_tasks(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(job, "redline-s1-t1-g01a")
    _trial(job, "redline-s1-t1-g01b", graded=False)
    assert reproduce.incomplete_tasks(job) == ["redline-s1-t1-g01b"]


def test_incomplete_tasks_ignores_task_with_any_graded_attempt(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(job, "redline-s1-t1-g01a", graded=False, suffix="first")
    _trial(job, "redline-s1-t1-g01a", suffix="second")
    assert reproduce.incomplete_tasks(job) == []


def test_incomplete_tasks_includes_never_started(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    for name in ("redline-s1-t1-g01a", "redline-s1-t1-g01b", "redline-s2-t1-g01a"):
        (tasks_root / name).mkdir(parents=True)
        (tasks_root / name / "task.toml").write_text("")
    job = tmp_path / "job"
    _trial(job, "redline-s1-t1-g01a")
    (job / "config.json").write_text(json.dumps({
        "tasks": [],
        "datasets": [{
            "path": str(tasks_root),
            "task_names": ["redline-s1-*"],
            "exclude_task_names": None,
            "n_tasks": None,
        }],
    }))
    assert reproduce.incomplete_tasks(job) == ["redline-s1-t1-g01b"]


def test_errored_tasks(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(job, "redline-s1-t1-g01a")
    _trial(job, "redline-s1-t1-g01b", error="AgentTimeoutError")
    assert reproduce.errored_tasks(job) == ["redline-s1-t1-g01b"]


def test_errored_tasks_ignores_task_with_clean_attempt(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(job, "redline-s1-t1-g01a", error="AgentTimeoutError", suffix="first")
    _trial(job, "redline-s1-t1-g01a", suffix="second")
    assert reproduce.errored_tasks(job) == []


def test_read_job_harbor_config(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(
        job,
        "redline-s1-t1-g01a",
        agent_kwargs={"reasoning_effort": "high", "max_turns": 50},
        agent_timeout_multiplier=3.0,
    )
    cfg = reproduce.read_job_harbor_config(job)
    assert cfg == {
        "agent": "claude-code",
        "model": "anthropic/claude-opus-4-8",
        "env": "modal",
        "agent_kwargs": ["reasoning_effort=high", "max_turns=50"],
        "agent_env": [],
        "agent_timeout_multiplier": 3.0,
    }


def test_read_job_harbor_config_agent_env(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial(
        job,
        "redline-s1-t1-g01a",
        agent_env={"OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX": "128000"},
    )
    cfg = reproduce.read_job_harbor_config(job)
    assert cfg["agent_env"] == ["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=128000"]


def test_merge_agent_kwargs_overrides_by_key() -> None:
    merged = rerun.merge_agent_kwargs(
        ["reasoning_effort=high", "max_turns=50"],
        ["reasoning_effort=xhigh"],
    )
    assert merged == ["reasoning_effort=xhigh", "max_turns=50"]


def test_masked_agent_env_flags_harbor_redactions_only() -> None:
    env = [
        "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=****",
        "GOOGLE_VERTEX_API_KEY=AQ.A****xyz",
        "TEMPLATED_API_KEY=${TEMPLATED_API_KEY}",
        "GOOGLE_CLOUD_PROJECT=crosby-prod",
    ]
    assert rerun.masked_agent_env(env) == [
        "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX",
        "GOOGLE_VERTEX_API_KEY",
    ]


def test_masked_agent_env_cleared_by_override() -> None:
    merged = rerun.merge_agent_kwargs(
        ["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=****"],
        ["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=128000"],
    )
    assert rerun.masked_agent_env(merged) == []


def test_redact_agent_env_masks_secret_literals_only() -> None:
    assert reproduce.redact_agent_env("CUSTOM_API_KEY=abc123") == "CUSTOM_API_KEY=****"
    assert reproduce.redact_agent_env("SERVICE_TOKEN=xyz") == "SERVICE_TOKEN=****"
    assert reproduce.redact_agent_env("CUSTOM_API_KEY=${CUSTOM_API_KEY}") == "CUSTOM_API_KEY=${CUSTOM_API_KEY}"
    assert reproduce.redact_agent_env("GOOGLE_CLOUD_PROJECT=demo") == "GOOGLE_CLOUD_PROJECT=demo"


def test_run_harbor_prints_redacted_command_but_passes_real_values(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    jobs_dir = tmp_path / "jobs"
    seen: list[list[str]] = []

    def fake_run(cmd, check):
        seen.append(cmd)
        (jobs_dir / "job-1").mkdir()

    monkeypatch.setattr(reproduce.shutil, "which", lambda _: "/usr/bin/harbor")
    monkeypatch.setattr(reproduce.subprocess, "run", fake_run)
    reproduce.run_harbor(
        tmp_path / "tasks", agent="opencode", model="google/x", n_concurrent=1,
        env=None, env_file=None, jobs_dir=jobs_dir,
        agent_env=["CUSTOM_API_KEY=abc123", "GOOGLE_CLOUD_PROJECT=demo"],
    )
    printed = capsys.readouterr().out
    assert "abc123" not in printed
    assert "--agent-env CUSTOM_API_KEY=****" in printed
    assert "--agent-env GOOGLE_CLOUD_PROJECT=demo" in printed
    assert "CUSTOM_API_KEY=abc123" in seen[0]


def test_merge_jobs_into_runs(tmp_path: Path) -> None:
    base = tmp_path / "base"
    rerun_job = tmp_path / "rerun"
    runs = tmp_path / "runs"
    _trial(base, "redline-s1-t1-g01a")
    _trial(base, "redline-s1-t1-g01b", graded=False)
    _trial(rerun_job, "redline-s1-t1-g01b")

    n = reproduce.merge_jobs_into_runs(
        base,
        rerun_job,
        runs,
        model_id="claude-opus-4-8",
        override_tasks={"redline-s1-t1-g01b"},
    )
    assert n == 2
    traj = runs / "trajectories" / "opus48"
    assert (traj / "redline-s1-t1-g01a" / "grade.json").is_file()
    assert (traj / "redline-s1-t1-g01b" / "grade.json").is_file()
    assert (
        runs / "panel" / "judges" / "judge-a" / "claude-opus-4-8" / "redline-s1-t1-g01b.json"
    ).is_file()


def test_merge_replaces_stale_artifacts(tmp_path: Path) -> None:
    base = tmp_path / "base"
    rerun_job = tmp_path / "rerun"
    runs = tmp_path / "runs"
    _trial(base, "redline-s1-t1-g01a", error="AgentTimeoutError", docx=True)
    (base / "redline-s1-t1-g01a__abc123" / "verifier" / "judges" / "judge-b.json").write_text("{}")
    _trial(rerun_job, "redline-s1-t1-g01a")

    reproduce.merge_jobs_into_runs(
        base,
        rerun_job,
        runs,
        model_id="claude-opus-4-8",
        override_tasks={"redline-s1-t1-g01a"},
    )
    assert not (runs / "trajectories" / "opus48" / "redline-s1-t1-g01a" / "redline.docx").exists()
    judges = runs / "panel" / "judges"
    assert (judges / "judge-a" / "claude-opus-4-8" / "redline-s1-t1-g01a.json").is_file()
    assert not (judges / "judge-b" / "claude-opus-4-8" / "redline-s1-t1-g01a.json").exists()
