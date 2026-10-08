"""Core versioning rules. Pure functions: no I/O, so every rule is unit-testable.

For each incoming observation (a value reported in a filing) we decide:

====================================  ===========================================================
Situation                             Action
====================================  ===========================================================
Fact never seen                       Publish as the first version (``initial``).
Same filing seen before, same value   Nothing to do (re-runs are idempotent).
Same filing seen before, NEW value    Quarantine. A filing never changes after it is accepted, so
                                      this means the source or our parsing changed silently.
Later filing, same value              Confirmation: recorded, no new version.
Later filing, different value         Restatement: close the old version at the filing date,
                                      publish the new one, emit an explained change event.
Older filing than current version     Quarantine (``late_arrival``): rewriting history needs a
                                      human decision.
Two filings same day, different value Quarantine (``same_day_conflict``).
Filing's data swaps the values of    Quarantine (``suspected_wrong_period``): a tagging error in
two periods a year apart              the filing's XBRL data, not a restatement. Real case: the
                                      data filed with Amazon's 10-K of 30 Jan 2013 (its printed
                                      table is correct) swapped 2011 and 2012 quarterly figures.
====================================  ===========================================================

The rule of thumb: a published value only changes automatically when a newer, dated,
public document says so. Anything else waits for review.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from pitlake.explain import explain_change, explain_data_revision, pct_change
from pitlake.models import ChangeEvent, Correction, FactVersion, Observation, stable_id


def _distinctive(v: float) -> bool:
    """At least 4 significant figures, so an exact match cannot be a coincidence of rounding
    (13,185,000,000 is distinctive; 460,000,000 shares or $0.44 are not)."""
    digits = round(abs(v) * 100)
    while digits and digits % 10 == 0:
        digits //= 10
    return digits >= 1000


def _series(o: Observation) -> tuple:
    length = None if o.period_start is None else round((o.period_end - o.period_start).days / 7)
    return (o.ticker, o.taxonomy, o.concept, o.unit, length)


def _explain(o: Observation, old: FactVersion):
    if o.taxonomy == "github":
        return explain_data_revision(
            ticker=o.ticker,
            repo=o.concept,
            period_start=o.period_start,
            old_value=old.value,
            new_value=o.value,
            old_filed=old.known_from,
            filed=o.filed,
        )
    return explain_change(
        ticker=o.ticker,
        entity_name=o.entity_name,
        concept=o.concept,
        unit=o.unit,
        period_start=o.period_start,
        period_end=o.period_end,
        old_value=old.value,
        new_value=o.value,
        old_form=old.source_form,
        old_filed=old.known_from,
        form=o.form,
        filed=o.filed,
    )


@dataclass
class ReconcileResult:
    observations_to_record: list[Observation] = field(default_factory=list)
    new_versions: list[FactVersion] = field(default_factory=list)
    closed_versions: dict[str, object] = field(default_factory=dict)  # version_id -> known_to
    changes: list[ChangeEvent] = field(default_factory=list)
    corrections: list[Correction] = field(default_factory=list)
    stats: Counter = field(default_factory=Counter)


def reconcile(
    observations: list[Observation],
    *,
    seen: dict[tuple[str, str], float],
    current: dict[str, FactVersion],
    known_corrections: set[tuple[str, str, float]],
    run_id: str,
    recorded_at: datetime,
) -> ReconcileResult:
    """Decide what to publish, given the batch and the current state of the lake.

    seen:              (fact_key, accn) -> value already recorded in the observations table
    current:           fact_key -> currently open FactVersion (known_to is None)
    known_corrections: (fact_key, accn, proposed_value) already in the corrections table
    """
    res = ReconcileResult()
    known_corrections = set(known_corrections)
    created_here: dict[str, FactVersion] = {}

    def quarantine(o: Observation, reason: str, detail: str, current_value: float | None) -> None:
        sig = (o.fact_key, o.accn, o.value)
        if sig in known_corrections:
            res.stats["quarantine_already_recorded"] += 1
            return
        known_corrections.add(sig)
        res.stats["quarantined"] += 1
        res.corrections.append(
            Correction(
                correction_id=stable_id(*sig),
                fact_key=o.fact_key,
                ticker=o.ticker,
                entity_name=o.entity_name,
                taxonomy=o.taxonomy,
                concept=o.concept,
                unit=o.unit,
                period_start=o.period_start,
                period_end=o.period_end,
                current_value=current_value,
                proposed_value=o.value,
                accn=o.accn,
                form=o.form,
                filed=o.filed,
                reason_code=reason,
                detail=detail,
                status="pending",
                created_at=recorded_at,
                run_id=run_id,
            )
        )

    def new_version(o: Observation, change_type: str) -> FactVersion:
        v = FactVersion(
            version_id=stable_id(o.fact_key, o.accn, o.filed, change_type),
            fact_key=o.fact_key,
            ticker=o.ticker,
            entity_name=o.entity_name,
            taxonomy=o.taxonomy,
            concept=o.concept,
            unit=o.unit,
            period_start=o.period_start,
            period_end=o.period_end,
            value=o.value,
            known_from=o.filed,
            known_to=None,
            source_accn=o.accn,
            source_form=o.form,
            change_type=change_type,
            recorded_at=recorded_at,
            run_id=run_id,
        )
        res.new_versions.append(v)
        created_here[v.version_id] = v
        return v

    # Values by (company, concept, unit, period length) and period end, to recognise a filing
    # that swaps two periods' values (a tagging error, not a restatement).
    by_series: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))
    for o in observations:
        by_series[_series(o)][o.period_end].append((o.value, o.filed, o.accn))

    def wrong_period_twin(o: Observation, current_value: float | None):
        """The period a year apart that this filing mixed up with o's period, if any.

        * full swap: o takes the twin's earlier value AND the twin takes o's current value;
        * half swap: the twin takes o's current value, and that value is distinctive;
        * new fact: o arrives with the twin's earlier value while the twin moves away from it.
        """
        for end, seen in by_series[_series(o)].items():
            if not 358 <= abs((end - o.period_end).days) <= 372:
                continue
            took = any(v == o.value and f < o.filed for v, f, _ in seen)
            twin_now = {v for v, _, a in seen if a == o.accn}
            if current_value is None:
                if took and twin_now and o.value not in twin_now and _distinctive(o.value):
                    return end
            elif current_value in twin_now and (took or _distinctive(current_value)):
                return end
        return None

    # 1. Collapse the batch to one value per (fact, filing) and check against what we have seen.
    grouped: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in observations:
        grouped[(o.fact_key, o.accn)].append(o)

    by_fact: dict[str, list[Observation]] = defaultdict(list)
    for (fact_key, accn), group in grouped.items():
        values = sorted({o.value for o in group})
        if len(values) > 1:
            for o in {o.value: o for o in group}.values():
                quarantine(
                    o,
                    "conflict_within_filing",
                    f"Filing {accn} reports {len(values)} different values: {values}",
                    None,
                )
            continue
        o = group[0]
        prev = seen.get((fact_key, accn))
        if prev is not None:
            if prev == o.value:
                res.stats["already_seen"] += 1
            else:
                quarantine(
                    o,
                    "same_filing_value_changed",
                    f"Filing {accn} was previously recorded with value {prev}; the source "
                    f"now reports {o.value} for the same filing.",
                    prev,
                )
            continue
        by_fact[fact_key].append(o)

    # 2. Walk each fact's new filings in publication order and version them.
    for fact_key, obs in by_fact.items():
        obs.sort(key=lambda o: (o.filed, o.accn))
        cur = current.get(fact_key)
        for o in obs:
            if cur is None and (twin := wrong_period_twin(o, None)) is not None:
                quarantine(
                    o,
                    "suspected_wrong_period",
                    f"The data filed with the {o.form} of {o.filed} reports {o.value:,} for "
                    "the period ending "
                    f"{o.period_end}, the value previously reported for {twin}, while giving "
                    f"{twin} a different value. This is a tagging error in the filing's data.",
                    None,
                )
            elif cur is None:
                cur = new_version(o, "initial")
                res.stats["new_facts"] += 1
                res.observations_to_record.append(o)
            elif o.value == cur.value and o.filed >= cur.known_from:
                res.stats["confirmations"] += 1
                res.observations_to_record.append(o)
            elif o.filed < cur.known_from:
                if o.value == cur.value:
                    res.stats["confirmations"] += 1
                    res.observations_to_record.append(o)
                else:
                    quarantine(
                        o,
                        "late_arrival",
                        f"{o.form} filed {o.filed} arrived after the current value (known from "
                        f"{cur.known_from}) and disagrees with it. Rewriting history needs review.",
                        cur.value,
                    )
            elif (twin := wrong_period_twin(o, cur.value)) is not None:
                quarantine(
                    o,
                    "suspected_wrong_period",
                    f"The data filed with the {o.form} of {o.filed} swaps two periods: it "
                    f"reports {o.value:,} for "
                    f"the period ending {o.period_end} (previously reported for {twin}) and "
                    f"{cur.value:,} for {twin}. This is a tagging error in the filing's data, "
                    "not a restatement.",
                    cur.value,
                )
            elif o.filed == cur.known_from:
                quarantine(
                    o,
                    "same_day_conflict",
                    f"Two filings on {o.filed} disagree: {cur.source_accn} says {cur.value}, "
                    f"{o.accn} says {o.value}.",
                    cur.value,
                )
            else:
                if cur.version_id in created_here:
                    cur.known_to = o.filed
                else:
                    res.closed_versions[cur.version_id] = o.filed
                old = cur
                cur = new_version(o, "restatement")
                res.stats["restatements"] += 1
                res.observations_to_record.append(o)
                why = _explain(o, old)
                res.changes.append(
                    ChangeEvent(
                        change_id=stable_id(fact_key, old.source_accn, o.accn),
                        fact_key=fact_key,
                        ticker=o.ticker,
                        entity_name=o.entity_name,
                        concept=o.concept,
                        unit=o.unit,
                        period_start=o.period_start,
                        period_end=o.period_end,
                        old_value=old.value,
                        new_value=o.value,
                        pct_change=pct_change(old.value, o.value),
                        effective_date=o.filed,
                        old_accn=old.source_accn,
                        new_accn=o.accn,
                        new_form=o.form,
                        classification=why.classification,
                        headline=why.headline,
                        guidance=why.guidance,
                        explanation=why.text,
                        recorded_at=recorded_at,
                        run_id=run_id,
                    )
                )
    return res
