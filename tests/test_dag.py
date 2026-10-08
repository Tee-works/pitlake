"""The Airflow DAG: structure, and a real end-to-end run with `airflow dags test`.

Skipped when Airflow is not installed (it lives in its own environment; see README).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

# Never touch a developer's real ~/airflow: point Airflow at a throwaway home before import.
os.environ["AIRFLOW_HOME"] = tempfile.mkdtemp(prefix="pitlake-airflow-")
pytest.importorskip("airflow")

from pitlake.lake import Lake

DAGS = Path(__file__).resolve().parents[1] / "dags"


@pytest.fixture
def airflow_env(tmp_path):
    env = {
        **os.environ,
        "AIRFLOW_HOME": str(tmp_path / "airflow"),
        "AIRFLOW__CORE__DAGS_FOLDER": str(DAGS),
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "PITLAKE_DATA_DIR": str(tmp_path / "data"),
    }
    airflow = str(Path(sys.executable).with_name("airflow"))
    subprocess.run([airflow, "db", "migrate"], env=env, check=True, capture_output=True)
    return airflow, env


def test_dag_structure():
    spec = importlib.util.spec_from_file_location("pitlake_daily", DAGS / "pitlake_daily.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # raises on any import error
    dag = module.dag
    assert dag.dag_id == "pitlake_daily"
    assert dag.max_active_runs == 1
    ids = [t.task_id for t in dag.tasks]
    assert ids == [
        "ingest_ai_infrastructure",
        "ingest_platforms",
        "ingest_health_tech",
        "collect_developer_activity",
        "report",
    ]
    # Groups run serially, and a failed group must not block the next.
    assert dag.get_task("ingest_platforms").upstream_task_ids == {"ingest_ai_infrastructure"}
    assert dag.get_task("ingest_health_tech").upstream_task_ids == {"ingest_platforms"}
    assert dag.get_task("collect_developer_activity").upstream_task_ids == {"ingest_health_tech"}
    assert all(t.trigger_rule == "all_done" for t in dag.tasks)


def test_dag_runs_end_to_end_on_sample_data(airflow_env, tmp_path):
    airflow, env = airflow_env
    result = subprocess.run(
        [airflow, "dags", "test", "pitlake_daily", "-c", '{"source": "sample"}'],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    log = result.stdout + result.stderr
    assert result.returncode == 0, log[-3000:]
    assert "state=success" in log

    lake = Lake(tmp_path / "data" / "lake")
    runs = lake.read("pipeline_runs").to_pylist()
    assert [r["status"] for r in runs] == ["succeeded"] * 4  # three groups + developer activity
    assert sum(r["restatements"] for r in runs) > 0
    taxonomies = set(lake.read("facts_history").column("taxonomy").to_pylist())
    assert taxonomies == {"us-gaap", "ifrs-full", "github"}
