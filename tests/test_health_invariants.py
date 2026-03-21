import json
import os
import pathlib
import time

import supervisor.state as supervisor_state
from ouroboros.context import _build_health_invariants


class _EnvStub:
    def __init__(self, repo_root: pathlib.Path, drive_root: pathlib.Path):
        self._repo_root = repo_root
        self._drive_root = drive_root

    def repo_path(self, rel: str) -> pathlib.Path:
        return self._repo_root / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self._drive_root / rel


def _make_env(tmp_path: pathlib.Path, drift_pct: float, identity_age_hours: float) -> _EnvStub:
    repo_root = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    (repo_root).mkdir(parents=True, exist_ok=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory").mkdir(parents=True, exist_ok=True)

    # Version sync files
    (repo_root / "VERSION").write_text("1.0.0", encoding="utf-8")
    (repo_root / "pyproject.toml").write_text(
        "[project]\nname='x'\nversion='1.0.0'\n",
        encoding="utf-8",
    )

    # Budget drift state
    (drive_root / "state" / "state.json").write_text(
        json.dumps(
            {
                "budget_drift_pct": drift_pct,
                "spent_usd": 17.38,
                "openrouter_total_usd": 21.86,
            }
        ),
        encoding="utf-8",
    )

    # Identity freshness
    identity_path = drive_root / "memory" / "identity.md"
    identity_path.write_text("identity", encoding="utf-8")
    mtime = time.time() - (identity_age_hours * 3600)
    os.utime(identity_path, (mtime, mtime))

    return _EnvStub(repo_root=repo_root, drive_root=drive_root)


def test_health_invariants_budget_drift_warning_over_20(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor_state, "per_task_cost_summary", lambda n=5: [])
    env = _make_env(tmp_path, drift_pct=340.8, identity_age_hours=1.0)

    text = _build_health_invariants(env)

    assert "WARNING: BUDGET DRIFT 340.8%" in text
    assert "tracked=$17.38 vs OpenRouter=$21.86" in text


def test_health_invariants_budget_drift_ok_under_or_equal_20(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor_state, "per_task_cost_summary", lambda n=5: [])
    env = _make_env(tmp_path, drift_pct=20.0, identity_age_hours=1.0)

    text = _build_health_invariants(env)

    assert "OK: budget drift within tolerance" in text
    assert "WARNING: BUDGET DRIFT" not in text




def test_agent_version_sync_recognizes_russian_readme_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor_state, "per_task_cost_summary", lambda n=5: [])
    env = _make_env(tmp_path, drift_pct=0.0, identity_age_hours=1.0)
    env.repo_dir = tmp_path / "repo"
    (tmp_path / "repo" / "README.md").write_text("**Версия:** 1.0.0\n", encoding="utf-8")

    class _Proc:
        returncode = 0
        stdout = "v1.0.0\n"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())

    from ouroboros.agent import OuroborosAgent

    agent = OuroborosAgent.__new__(OuroborosAgent)
    agent.env = env
    result, issues = agent._check_version_sync()

    assert result["readme_version"] == "1.0.0"
    assert result["version_file"] == "1.0.0"
    assert result["latest_tag"] == "1.0.0"
    assert issues == 0


def test_health_invariants_identity_stale_after_4_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor_state, "per_task_cost_summary", lambda n=5: [])
    env = _make_env(tmp_path, drift_pct=0.0, identity_age_hours=5.1)

    text = _build_health_invariants(env)

    assert "WARNING: STALE IDENTITY" in text
