"""Offline tests for the SQL guard helpers in the BigQuery agent's tool layer.

These are the only tests in the suite that need neither ADC nor a network call:
every helper here is a pure function. Keep it that way — validation logic that
needs a live client belongs behind `run_sql_query`, not in these helpers.
"""

import inspect

import pytest

from talk_to_database_agent.sub_agents.bigquery_agent.tools import (
    check_sql_read_only,
    format_bytes,
    run_sql_query,
    sanitize_rows,
    sanitize_sql,
    sanitize_value,
)

PM10_QUERY = (
    "SELECT local_site_name, AVG(arithmetic_mean) AS avg_pm10 "
    "FROM `bigquery-public-data.epa_historical_air_quality.pm10_daily_summary` "
    "WHERE date_local BETWEEN '2026-05-01' AND '2026-05-31' "
    "GROUP BY 1 ORDER BY avg_pm10 DESC LIMIT 10"
)


class TestSanitizeSql:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("```sql\nSELECT 1\n```", "SELECT 1"),
            ("```SQL\nSELECT 1\n```", "SELECT 1"),
            ("```googlesql\nSELECT 1\n```", "SELECT 1"),
            ("```\nSELECT 1\n```", "SELECT 1"),
            # Fence opened and never closed: strip only the opening one.
            ("```sql\nSELECT 1", "SELECT 1"),
            ("  SELECT 1  ", "SELECT 1"),
            ("SELECT 1;", "SELECT 1"),
            ("SELECT 1 ;\n", "SELECT 1"),
        ],
    )
    def test_normalizes_model_output(self, raw, expected):
        assert sanitize_sql(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "\n", ";", None, 123])
    def test_rejects_empty(self, raw):
        with pytest.raises(ValueError):
            sanitize_sql(raw)

    def test_rejects_multiple_statements(self):
        with pytest.raises(ValueError):
            sanitize_sql("SELECT 1; DROP TABLE t")

    @pytest.mark.parametrize(
        "raw",
        [
            "SELECT * FROM t WHERE note = 'a;b'",
            'SELECT * FROM t WHERE note = "a;b"',
            "SELECT * FROM t WHERE note = '''a;b'''",
            "SELECT 1 -- trailing ; comment",
            "SELECT 1 /* inline ; comment */",
            "SELECT `weird;column` FROM t",
            r"SELECT * FROM t WHERE note = 'it\'s; fine'",
        ],
    )
    def test_semicolon_inside_literal_or_comment_is_not_a_statement_break(self, raw):
        assert sanitize_sql(raw)


class TestCheckSqlReadOnly:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            PM10_QUERY,
            "WITH m AS (SELECT 1 AS a) SELECT a FROM m",
            "(SELECT 1) UNION ALL (SELECT 2)",
            "-- leading comment\nSELECT 1",
            "/* leading block */ SELECT 1",
        ],
    )
    def test_allows_reads(self, sql):
        assert check_sql_read_only(sanitize_sql(sql)) is True

    @pytest.mark.parametrize(
        "sql",
        [
            # A denylist over the raw text rejected all of these: the keyword is
            # a substring of a column name, a literal, or a comment.
            "SELECT date_local, updated_at FROM t",
            "SELECT created_date FROM t",
            "SELECT alteration FROM t",
            "SELECT * FROM t WHERE city_name = 'Update Springs'",
            "SELECT 1 -- create a summary",
            "SELECT * FROM t /* DROP everything */",
            "SELECT delete_flag, insert_ts FROM t",
        ],
    )
    def test_does_not_reject_reads_that_merely_mention_a_keyword(self, sql):
        assert check_sql_read_only(sanitize_sql(sql)) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DELETE FROM t WHERE 1 = 1",
            "CREATE TABLE x AS SELECT 1",
            "CREATE OR REPLACE VIEW v AS SELECT 1",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN c INT64",
            "TRUNCATE TABLE t",
            "MERGE t USING s ON 1=1 WHEN MATCHED THEN UPDATE SET a = 1",
            "GRANT `roles/bigquery.dataViewer` ON SCHEMA d TO 'user:x'",
            "REVOKE `roles/bigquery.dataViewer` ON SCHEMA d FROM 'user:x'",
            "CALL my_dataset.my_procedure()",
            "DECLARE x INT64 DEFAULT 1",
            # Writes data out to GCS. Read-only as far as BigQuery tables go,
            # which is exactly why a keyword denylist let it through.
            "EXPORT DATA OPTIONS(uri='gs://bucket/x*.csv', format='CSV') AS SELECT 1",
            "LOAD DATA INTO t FROM FILES (uris=['gs://bucket/x.csv'])",
            # A CTE does not launder the statement that follows it.
            "WITH a AS (SELECT 1) DELETE FROM t",
        ],
    )
    def test_blocks_writes(self, sql):
        assert check_sql_read_only(sanitize_sql(sql)) is False


class TestSanitizeValue:
    def test_passes_through_json_native_scalars(self):
        for value in (None, True, False, 0, -3, 42, "text"):
            assert sanitize_value(value) == value

    def test_non_finite_floats_become_none(self):
        assert sanitize_value(float("nan")) is None
        assert sanitize_value(float("inf")) is None
        assert sanitize_value(float("-inf")) is None
        assert sanitize_value(1.5) == 1.5

    def test_bigquery_types_become_json_safe(self):
        import datetime
        import decimal

        assert sanitize_value(decimal.Decimal("2.50")) == 2.5
        assert sanitize_value(datetime.date(2026, 5, 31)) == "2026-05-31"
        assert sanitize_value(datetime.time(7, 0)) == "07:00:00"
        assert sanitize_value(datetime.timedelta(minutes=2)) == 120.0
        assert sanitize_value(b"ab") == "YWI="

    def test_recurses_into_containers(self):
        import decimal

        value = {"a": [decimal.Decimal("1.5"), float("nan")], "b": (1, 2)}
        assert sanitize_value(value) == {"a": [1.5, None], "b": [1, 2]}

    def test_unknown_types_fall_back_to_str(self):
        class Opaque:
            def __str__(self):
                return "opaque"

        assert sanitize_value(Opaque()) == "opaque"

    def test_sanitize_rows(self):
        import decimal

        rows = [[decimal.Decimal("1.5"), "x"], [float("inf"), None]]
        assert sanitize_rows(rows) == [[1.5, "x"], [None, None]]


class TestCostControls:
    @pytest.mark.parametrize(
        "num_bytes, expected",
        [
            (0, "0 B"),
            (512, "512 B"),
            (1024, "1.00 KB"),
            (162_740_000, "155.20 MB"),
            (20 * 1024**3, "20.00 GB"),
            (118_160_000_000, "110.05 GB"),
            (5 * 1024**4, "5.00 TB"),
        ],
    )
    def test_format_bytes(self, num_bytes, expected):
        assert format_bytes(num_bytes) == expected

    def test_run_sql_query_is_async(self):
        """The BigQuery client is blocking; a sync tool would stall the event loop.

        ADK awaits a coroutine tool directly (`FunctionTool._invoke_callable`),
        so this signature is what keeps one slow query from freezing every other
        request the server is handling.
        """
        assert inspect.iscoroutinefunction(run_sql_query)

    async def test_write_is_rejected_before_reaching_bigquery(self):
        """No credentials needed: the guard must short-circuit before any client call."""
        result = await run_sql_query("DROP TABLE t", tool_context=None)
        assert result["status"] == "error"
        assert "SELECT" in result["error"]

    async def test_empty_sql_is_rejected_before_reaching_bigquery(self):
        result = await run_sql_query("   ", tool_context=None)
        assert result["status"] == "error"

    async def test_multi_statement_is_rejected_before_reaching_bigquery(self):
        result = await run_sql_query("SELECT 1; DROP TABLE t", tool_context=None)
        assert result["status"] == "error"
