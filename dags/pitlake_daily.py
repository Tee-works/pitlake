"""Airflow DAG: daily point-in-time fundamentals refresh.

The pipeline logic lives in the `pitlake` package and is orchestrator-agnostic; this DAG only
decides *when* and *in what order*:

* one task per ticker group of SEC filings, then one for GitHub developer activity
  (alternative data), run one after another (they commit to the same Delta tables);
* a failed group does not stop the groups after it (trigger_rule="all_done");
* a failed data-quality gate fails its task and rolls back that group's writes;
* a final task alerts people about failures, revisions and changes awaiting review.

Run it locally without a scheduler (uses the offline SEC sample):

    airflow dags test pitlake_daily -c '{"source": "sample"}'
"""

from __future__ import annotations

from datetime import datetime, timedelta

from airflow.sdk import Param, dag, get_current_context, task

# Largest holdings of BIT Global Technology Leaders (see src/pitlake/conf/universe.toml),
# grouped so one failing group does not hold up the others.
TICKER_GROUPS = {
    "ai_infrastructure": ["NVDA", "MU", "TSM", "IREN", "SIMO"],
    "platforms": ["AMZN", "META", "MSFT"],
    "health_tech": ["HNGE"],
}


def _alert_on_failure(context) -> None:
    from pitlake import notify

    ti = context["task_instance"]
    notify.send(f"pitlake: task {ti.task_id} failed in run {context['run_id']}. Check the logs.")


@dag(
    dag_id="pitlake_daily",
    schedule="0 6 * * 1-5",  # weekdays; SEC filings from the previous US day are in by then
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,  # runs write to the same Delta tables; never overlap them
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "on_failure_callback": _alert_on_failure,
    },
    params={
        "source": Param("sec", enum=["sec", "sample"], description="sample = offline SEC data"),
    },
    tags=["fundamentals", "sec", "point-in-time"],
    doc_md=__doc__,
)
def pitlake_daily():
    @task(trigger_rule="all_done")
    def ingest(group: str) -> dict | None:
        from airflow.sdk.exceptions import AirflowSkipException

        from pitlake.config import sample_dir
        from pitlake.extract import sample_files
        from pitlake.pipeline import run

        tickers = TICKER_GROUPS[group]
        if get_current_context()["params"]["source"] == "sample":
            paths = sample_files(sample_dir() / "sec_companyfacts", tickers)
            if not paths:
                raise AirflowSkipException(f"no sample data for group {group}")
            rec = run("files", paths=paths)
        else:
            rec = run("sec", tickers)
        return _summary(group, rec)

    @task(trigger_rule="all_done")
    def collect_developer_activity() -> dict | None:
        """Alternative data. Collected daily: each day's snapshot is a new vintage, and the
        history we collect ourselves is the only history a backtest can trust."""
        from pitlake.github import SOURCE
        from pitlake.pipeline import run

        if get_current_context()["params"]["source"] == "sample":
            from pitlake.config import sample_dir
            from pitlake.extract import sample_files

            rec = run("files", paths=sample_files(sample_dir() / SOURCE))
        else:
            rec = run("github")
        return _summary("developer_activity", rec)

    def _summary(group: str, rec) -> dict:
        return {
            "group": group,
            "run_id": rec.run_id,
            "message": rec.message,
            "restatements": rec.restatements,
            "quarantined": rec.quarantined,
            "dq_warnings": rec.dq_warnings,
        }

    @task(trigger_rule="all_done")
    def report(results: list[dict | None]) -> str | None:
        """Tell people what needs their attention. Returns the message (for tests/logs)."""
        from pitlake import notify

        results = [r for r in results if r]  # failed or skipped groups return nothing
        message = notify.run_summary(results)
        if message:
            notify.send(message)
        return message

    results = []
    previous = None
    for group in TICKER_GROUPS:
        t = ingest.override(task_id=f"ingest_{group}")(group)
        if previous is not None:
            previous >> t
        previous = t
        results.append(t)
    alt = collect_developer_activity()
    previous >> alt
    results.append(alt)
    report(results)


dag = pitlake_daily()
