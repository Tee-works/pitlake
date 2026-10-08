-- Semantic layer over the lake. Plain SQL (DuckDB dialect, close to Databricks SQL).
-- Every analytical question is answered "as of" a date: only values that were public on
-- that date are visible, which is what keeps backtests free of look-ahead bias.

-- Facts as they were publicly known on `as_of`.
CREATE OR REPLACE MACRO facts_as_of(as_of) AS TABLE
SELECT *
FROM facts_history
WHERE known_from <= as_of
  AND (known_to IS NULL OR known_to > as_of);

-- Every version of every business metric (all as-of dates at once). Maps XBRL concepts to
-- metrics and classifies the period. Used by metrics_as_of and by research queries that need
-- "the value as known on the day X happened" for many different days.
CREATE OR REPLACE VIEW metric_history AS
SELECT
    f.ticker,
    f.entity_name,
    mc.metric,
    mc.label,
    mc.kind,
    f.unit,
    f.taxonomy,
    f.concept,
    mc.priority,
    f.period_start,
    f.period_end,
    CASE
        WHEN f.period_start IS NULL THEN 'instant'
        WHEN date_diff('day', f.period_start, f.period_end) + 1 BETWEEN 80 AND 100 THEN 'quarter'
        WHEN date_diff('day', f.period_start, f.period_end) + 1 BETWEEN 350 AND 380 THEN 'annual'
        ELSE 'ytd'
    END AS period_type,
    f.value,
    f.known_from,
    f.known_to,
    f.source_accn,
    f.source_form,
    f.change_type
FROM facts_history AS f
JOIN metric_concepts AS mc
  ON mc.taxonomy = f.taxonomy AND mc.concept = f.concept
 AND regexp_full_match(f.unit, mc.unit_pattern);

-- Business metrics as of a date: the highest-priority tag when a company reported several
-- for the same period (e.g. after switching from IFRS to US GAAP, or renaming a tag).
CREATE OR REPLACE MACRO metrics_as_of(as_of) AS TABLE
SELECT
    ticker, entity_name, metric, label, kind, unit, concept, period_start, period_end,
    period_type, value, known_from, source_accn, source_form, change_type
FROM metric_history
WHERE known_from <= as_of
  AND (known_to IS NULL OR known_to > as_of)
QUALIFY row_number() OVER (
    PARTITION BY ticker, metric, period_start, period_end
    ORDER BY priority
) = 1;

-- Quarterly series as of a date. Companies rarely file a standalone Q4 (it is inside the
-- 10-K), so for additive ("flow") metrics Q4 is derived as FY minus the three reported
-- quarters. Per-share and share-count metrics are NOT additive and are never derived.
CREATE OR REPLACE MACRO quarterly_as_of(as_of) AS TABLE
WITH m AS (
    SELECT * FROM metrics_as_of(as_of)
),
reported AS (
    SELECT ticker, entity_name, metric, label, kind, unit, period_start, period_end, value,
           known_from, source_form, FALSE AS derived
    FROM m
    WHERE period_type = 'quarter'
),
fiscal_year AS (
    SELECT * FROM m WHERE period_type = 'annual' AND kind = 'flow'
),
derived_q4 AS (
    SELECT
        fy.ticker, fy.entity_name, fy.metric, fy.label, fy.kind, fy.unit,
        CAST(max(q.period_end) + INTERVAL 1 DAY AS DATE) AS period_start,
        fy.period_end,
        fy.value - sum(q.value) AS value,
        greatest(fy.known_from, max(q.known_from)) AS known_from,
        'derived: FY - Q1..Q3' AS source_form,
        TRUE AS derived
    FROM fiscal_year AS fy
    JOIN reported AS q
      ON q.ticker = fy.ticker
     AND q.metric = fy.metric
     AND q.period_start >= fy.period_start
     AND q.period_end < fy.period_end
    WHERE NOT EXISTS (
        SELECT 1 FROM reported AS r
        WHERE r.ticker = fy.ticker AND r.metric = fy.metric AND r.period_end = fy.period_end
    )
    GROUP BY fy.ticker, fy.entity_name, fy.metric, fy.label, fy.kind, fy.unit,
             fy.period_end, fy.value, fy.known_from
    HAVING count(*) = 3
)
SELECT * FROM reported
UNION ALL
SELECT * FROM derived_q4;

-- Developer activity (alternative data) as of a date: weekly commits per repository.
CREATE OR REPLACE MACRO developer_activity_as_of(as_of) AS TABLE
SELECT ticker, concept AS repo, period_start AS week_start, period_end AS week_end,
       value AS commits, known_from
FROM facts_as_of(as_of)
WHERE taxonomy = 'github';

-- Currently published value of every fact.
CREATE OR REPLACE VIEW facts_current AS
SELECT * FROM facts_history WHERE known_to IS NULL;

-- The latest accepted source value per fact (the reference for reconciliation).
CREATE OR REPLACE VIEW observations_latest AS
SELECT fact_key, arg_max(value, (filed, accn)) AS value
FROM observations
GROUP BY fact_key;

-- Helper views for the post-publish data-quality gate (checked with trueset).
CREATE OR REPLACE VIEW dq_open_versions_per_fact AS
SELECT fact_key, count(*) FILTER (WHERE known_to IS NULL) AS open_versions
FROM facts_history
GROUP BY fact_key;

CREATE OR REPLACE VIEW dq_invalid_intervals AS
SELECT version_id, known_from, known_to
FROM facts_history
WHERE known_to IS NOT NULL AND known_to <= known_from;

CREATE OR REPLACE VIEW dq_overlapping_versions AS
SELECT a.fact_key, a.version_id AS version_a, b.version_id AS version_b
FROM facts_history AS a
JOIN facts_history AS b
  ON a.fact_key = b.fact_key
 AND a.version_id < b.version_id
 AND a.known_from < coalesce(b.known_to, DATE '9999-12-31')
 AND b.known_from < coalesce(a.known_to, DATE '9999-12-31');

-- Monitoring: companies file at least quarterly, so a long silence means a source or
-- pipeline problem (e.g. a ticker change or a broken extract), not a quiet company.
CREATE OR REPLACE VIEW dq_days_since_last_filing AS
SELECT ticker, taxonomy, date_diff('day', max(filed), current_date) AS days_since_last_filing
FROM observations
WHERE taxonomy IN ('us-gaap', 'ifrs-full') AND form <> 'MANUAL'
GROUP BY ticker, taxonomy
-- A company that switched standards (IREN: IFRS -> US GAAP) is only judged on its latest.
QUALIFY days_since_last_filing = min(days_since_last_filing) OVER (PARTITION BY ticker);

-- Alternative data is collected daily, so it goes stale much faster.
CREATE OR REPLACE VIEW dq_days_since_last_collection AS
SELECT concept AS repo, date_diff('day', max(filed), current_date) AS days_since_last_collection
FROM observations
WHERE taxonomy = 'github'
GROUP BY concept;
