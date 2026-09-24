"""Account-level cost collection (M2C-E) with hermetic fake clients.

No network, no AWS, no credentials, no boto3. Exercises
``CostExplorerCollector`` against an injected in-memory client exposing
``get_cost_and_usage`` with canned Cost Explorer shaped responses.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from sws_agent.constants import (
    COST_GROUP_DIMENSION_SERVICE,
    COST_GROUP_TAG_OWNER,
    MAX_COST_GROUP_BY_KEYS,
    MAX_COST_WINDOW_DAYS,
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from sws_agent.cost_explorer import (
    CostExplorerCollector,
    normalize_group_by,
    normalize_window_days,
)
from sws_agent.models import CostEstimate
from sws_agent.trace import TraceRecorder

END = date(2026, 3, 31)


def _period(end_date: date, window_days: int) -> dict[str, str]:
    end = end_date
    start = end - timedelta(days=window_days - 1)
    return {"Start": start.isoformat(), "End": (end + timedelta(days=1)).isoformat()}


def _total_response(*amounts):
    """A Cost Explorer response with one ResultsByTime entry per amount."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": f"2026-01-0{i + 1}", "End": f"2026-01-0{i + 2}"},
                "Total": {
                    "UnblendedCost": {
                        "Amount": str(amount),
                        "Unit": "USD",
                    }
                },
            }
            for i, amount in enumerate(amounts)
        ]
    }


def _grouped_response(*groups):
    """A Cost Explorer response; groups are ``(key_value, amount)`` pairs."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {
                        "Keys": [value],
                        "Metrics": {
                            "UnblendedCost": {"Amount": str(amount), "Unit": "USD"}
                        },
                    }
                    for value, amount in groups
                ],
            }
        ]
    }


class FakeCostClient:
    """In-memory Cost Explorer client with call tracking and canned responses."""

    def __init__(self, script=None, fail_at=None, error=RuntimeError("boom")):
        self._script = list(script or [])
        self._fail_at = set(fail_at or ())
        self._error = error
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **params):
        self.calls.append(dict(params))
        index = len(self.calls) - 1
        if index in self._fail_at:
            raise self._error
        if index >= len(self._script):
            return {"ResultsByTime": []}
        return self._script[index]


def _failed_events(trace):
    return [
        e
        for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.FAILED
    ]


def _succeed_events(trace):
    return [
        e
        for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
        and e.status is TraceStatus.SUCCEEDED
    ]


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_collect_requires_explicit_end_date():
    client = FakeCostClient(script=[_total_response(1.0)])
    for end_date in (None, "2026-03-31", 31):
        with pytest.raises(ValueError, match="end_date"):
            CostExplorerCollector(client).collect(end_date=end_date)


def test_normalize_window_days_and_defaults():
    assert normalize_window_days(None) == MAX_COST_WINDOW_DAYS
    assert normalize_window_days(1) == 1
    assert normalize_window_days(MAX_COST_WINDOW_DAYS) == MAX_COST_WINDOW_DAYS
    for bad in (0, -1, MAX_COST_WINDOW_DAYS + 1, "30", True, 3.5):
        with pytest.raises(ValueError):
            normalize_window_days(bad)


def test_collect_uses_explicit_window_and_defaults_to_max():
    window_92 = _period(END, MAX_COST_WINDOW_DAYS)
    client = FakeCostClient(script=[_total_response(2.5)])
    CostExplorerCollector(client).collect(end_date=END)
    assert client.calls[0]["TimePeriod"] == window_92

    client = FakeCostClient(script=[_total_response(2.5)])
    CostExplorerCollector(client).collect(window_days=30, end_date=END)
    assert client.calls[0]["TimePeriod"] == _period(END, 30)
    assert client.calls[0]["Granularity"] == "DAILY"
    assert client.calls[0]["Metrics"] == ["UnblendedCost"]


def test_collect_rejects_out_of_bounds_window_days():
    client = FakeCostClient(script=[_total_response(1.0)])
    for window_days in (0, -1, MAX_COST_WINDOW_DAYS + 1, "30", True):
        with pytest.raises(ValueError):
            CostExplorerCollector(client).collect(
                window_days=window_days, end_date=END
            )


def test_min_and_max_windows_are_accepted():
    client = FakeCostClient(script=[_total_response(1.0)])
    result = CostExplorerCollector(client).collect(
        window_days=1, end_date=END, group_by=[]
    )
    assert len(result) == 1
    client = FakeCostClient(script=[_total_response(1.0)])
    result = CostExplorerCollector(client).collect(
        window_days=MAX_COST_WINDOW_DAYS, end_date=END, group_by=[]
    )
    assert len(result) == 1


def test_normalize_group_by_valid_and_empty():
    assert normalize_group_by(None) == []
    assert normalize_group_by([]) == []
    assert normalize_group_by([COST_GROUP_DIMENSION_SERVICE]) == [
        COST_GROUP_DIMENSION_SERVICE
    ]
    assert normalize_group_by(
        [COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER]
    ) == [COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER]


def test_collect_rejects_unknown_duplicate_and_over_cap_group_by():
    client = FakeCostClient(script=[_total_response(1.0)])
    with pytest.raises(ValueError, match="unsupported"):
        CostExplorerCollector(client).collect(
            group_by=["unknown_key"], end_date=END
        )
    with pytest.raises(ValueError, match="unsupported"):
        CostExplorerCollector(client).collect(
            group_by=[123], end_date=END
        )
    with pytest.raises(ValueError, match="duplicate"):
        CostExplorerCollector(client).collect(
            group_by=[COST_GROUP_DIMENSION_SERVICE, COST_GROUP_DIMENSION_SERVICE],
            end_date=END,
        )
    over_cap = [f"k{i}" for i in range(MAX_COST_GROUP_BY_KEYS + 1)]
    with pytest.raises(ValueError, match="too many"):
        normalize_group_by(over_cap)


# ---------------------------------------------------------------------------
# Happy path collection
# ---------------------------------------------------------------------------


def test_collect_happy_path_total_aggregates_daily_amounts():
    client = FakeCostClient(script=[_total_response(1.5, 2.5, 40.0)])
    estimate = CostExplorerCollector(client).collect(end_date=END)[0]
    assert isinstance(estimate, CostEstimate)
    assert estimate.line_item == "total"
    assert estimate.amount_usd == pytest.approx(44.0)
    assert estimate.projected is True
    assert estimate.basis == (
        "cost_explorer:unblended_cost:total:"
        + _period(END, MAX_COST_WINDOW_DAYS)["Start"]
        + ":"
        + _period(END, MAX_COST_WINDOW_DAYS)["End"]
    )
    assert estimate.assumptions == []


def test_collect_zero_cost_healthy_query_yields_zero_total():
    client = FakeCostClient(script=[_total_response()])
    estimate = CostExplorerCollector(client).collect(end_date=END)[0]
    assert estimate.line_item == "total"
    assert estimate.amount_usd == 0.0
    assert estimate.assumptions == []


def test_collect_grouped_by_service_aggregates_per_key():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 5.0), ("AWS Lambda", 5.0)),
    ]
    client = FakeCostClient(script=script)
    estimates = {e.line_item: e for e in CostExplorerCollector(client).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )}
    assert set(estimates) == {"total", "service:AmazonS3", "service:AWS Lambda"}
    assert estimates["service:AmazonS3"].amount_usd == pytest.approx(5.0)
    assert estimates["service:AWS Lambda"].amount_usd == pytest.approx(5.0)
    assert all(e.projected for e in estimates.values())
    # grouped row basis names the grouping key
    assert estimates["service:AmazonS3"].basis.startswith(
        "cost_explorer:unblended_cost:service:"
    )


def test_collect_grouped_by_owner_tag_aggregates_per_key_across_days():
    # two daily entries share one tag value and are summed
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {
                        "Keys": ["eng"],
                        "Metrics": {"UnblendedCost": {"Amount": "3.0", "Unit": "USD"}},
                    }
                ],
            },
            {
                "TimePeriod": {"Start": "2026-01-02", "End": "2026-01-03"},
                "Groups": [
                    {
                        "Keys": ["eng"],
                        "Metrics": {"UnblendedCost": {"Amount": "4.0", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["data"],
                        "Metrics": {"UnblendedCost": {"Amount": "2.0", "Unit": "USD"}},
                    },
                ],
            },
        ]
    }
    client = FakeCostClient(script=[_total_response(9.0), response])
    estimates = {
        e.line_item: e
        for e in CostExplorerCollector(client).collect(
            group_by=[COST_GROUP_TAG_OWNER], end_date=END
        )
    }
    assert estimates["owner_tag:eng"].amount_usd == pytest.approx(7.0)
    assert estimates["owner_tag:data"].amount_usd == pytest.approx(2.0)


def test_collect_multiple_group_by_keys_runs_separate_queries():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 6.0)),
        _grouped_response(("eng", 4.0)),
    ]
    client = FakeCostClient(script=script)
    collector = CostExplorerCollector(client)
    rows = {e.line_item: e for e in collector.collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER],
        end_date=END,
    )}
    assert rows["service:AmazonS3"].amount_usd == pytest.approx(6.0)
    assert rows["owner_tag:eng"].amount_usd == pytest.approx(4.0)
    assert len(rows) == 3
    # exactly one query per group-by key plus the total query
    grouped_calls = [c for c in client.calls if "GroupBy" in c]
    assert len(grouped_calls) == 2
    assert grouped_calls[0]["GroupBy"] == [
        {"Type": "DIMENSION", "Key": "SERVICE"}
    ]
    assert grouped_calls[1]["GroupBy"] == [{"Type": "TAG", "Key": "Owner"}]


def test_collect_output_is_sorted_and_deterministic():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 6.0)),
        _grouped_response(("eng", 4.0)),
    ]
    client = FakeCostClient(script=script)
    first = CostExplorerCollector(client).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER],
        end_date=END,
    )
    client = FakeCostClient(script=script)
    second = CostExplorerCollector(client).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE, COST_GROUP_TAG_OWNER],
        end_date=END,
    )
    assert first == second
    assert [e.line_item for e in first] == sorted(e.line_item for e in first)


def test_collect_does_not_mutate_inputs():
    group_by = [COST_GROUP_DIMENSION_SERVICE]
    snapshot = list(group_by)
    client = FakeCostClient(script=[_total_response(1.0), _grouped_response(("S3", 1.0))])
    CostExplorerCollector(client).collect(
        window_days=7, end_date=END, group_by=snapshot
    )
    assert snapshot == group_by
    expected = _period(END, 7)
    assert client.calls[0]["TimePeriod"] == expected


def test_collect_never_fabricates_per_resource_costs():
    script = [
        _total_response(25.0),
        _grouped_response(("AmazonS3", 10.0), ("AWS Lambda", 15.0)),
    ]
    client = FakeCostClient(script=script)
    rows = CostExplorerCollector(client).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )
    # Only aggregate line items (the account total and dimension/tag values);
    # never a resource id, arn, or per-resource breakdown.
    line_items = [e.line_item for e in rows]
    assert "total" in line_items
    for item in line_items:
        assert not item.lower().startswith(("arn:", "bucket", "lambda", "function"))
        assert "cost_data" not in item
    assert all(e.amount_usd >= 0.0 for e in rows)


def test_collect_negative_credits_excluded_with_surfaced_assumption():
    client = FakeCostClient(script=[_total_response(10.0, -2.0, 3.0)])
    estimate = CostExplorerCollector(client).collect(end_date=END)[0]
    assert estimate.amount_usd == pytest.approx(13.0)
    assert estimate.assumptions == [
        "negative credit entries were excluded from the aggregated amount"
    ]


# ---------------------------------------------------------------------------
# Trace events and failure semantics
# ---------------------------------------------------------------------------


def test_collect_records_start_and_success_events_with_counts():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 4.0), ("AWS Lambda", 6.0)),
    ]
    trace = TraceRecorder()
    client = FakeCostClient(script=script)
    CostExplorerCollector(client, trace=trace).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )
    statuses = [
        e.status
        for e in trace
        if e.event_type is TraceEventType.INVENTORY_QUERY
    ]
    assert TraceStatus.RUNNING in statuses
    successes = _succeed_events(trace)
    assert len(successes) == 2
    assert all(
        e.metadata["resource_type"] == SWSResourceType.COST_DATA.value
        for e in successes
    )
    assert successes[0].metadata["count"] == 1  # total row
    assert successes[1].metadata["count"] == 2  # grouped rows
    assert successes[1].metadata["group_by"] == COST_GROUP_DIMENSION_SERVICE


def test_collect_primary_failure_returns_empty_fail_closed():
    client = FakeCostClient(script=[_total_response(1.0)], fail_at={0})
    trace = TraceRecorder()
    result = CostExplorerCollector(client, trace=trace).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )
    assert result == []
    assert len(client.calls) == 1  # grouped query never attempted
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert (
        failures[0].metadata["category"]
        == CollectionFailureCategory.PRIMARY.value
    )
    assert _succeed_events(trace) == []


def test_collect_enrichment_grouped_failure_returns_totals():
    client = FakeCostClient(
        script=[_total_response(12.0), _grouped_response(("AmazonS3", 5.0))],
        fail_at={1},
    )
    trace = TraceRecorder()
    rows = CostExplorerCollector(client, trace=trace).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )
    assert [e.line_item for e in rows] == ["total"]
    assert rows[0].amount_usd == pytest.approx(12.0)
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert (
        failures[0].metadata["category"]
        == CollectionFailureCategory.ENRICHMENT.value
    )


def test_collect_parse_failure_skips_entry_but_keeps_remaining():
    client = FakeCostClient(
        script=[_total_response(3.5, "not-a-number", 7.25)]
    )
    trace = TraceRecorder()
    estimate = CostExplorerCollector(client, trace=trace).collect(end_date=END)[0]
    assert estimate.amount_usd == pytest.approx(10.75)
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert (
        failures[0].metadata["category"] == CollectionFailureCategory.PARSE.value
    )


def test_collect_grouped_parse_failures_skipped_and_traced():
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {"Keys": ["AmazonS3"], "Metrics": {"UnblendedCost": {"Amount": "5.0", "Unit": "USD"}}},
                    {"Keys": ["bad"], "Metrics": {"UnblendedCost": {"Amount": "nope", "Unit": "USD"}}},
                    {"Keys": [], "Metrics": {"UnblendedCost": {"Amount": "9.0", "Unit": "USD"}}},
                ],
            }
        ]
    }
    client = FakeCostClient(script=[_total_response(15.0), response])
    trace = TraceRecorder()
    rows = {e.line_item: e for e in CostExplorerCollector(client, trace=trace).collect(
        group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
    )}
    assert rows["service:AmazonS3"].amount_usd == pytest.approx(5.0)
    failures = _failed_events(trace)
    assert len(failures) == 2  # missing-key group plus unparseable amount
    assert all(
        failures[i].metadata["category"] == CollectionFailureCategory.PARSE.value
        for i in range(2)
    )


def test_collect_group_by_none_and_empty_behave_identically():
    script = [_total_response(8.5)]
    client = FakeCostClient(script=script)
    none_result = CostExplorerCollector(client).collect(end_date=END, group_by=None)
    client = FakeCostClient(script=script)
    empty_result = CostExplorerCollector(client).collect(end_date=END, group_by=[])
    assert none_result == empty_result
    assert all("GroupBy" not in c for c in client.calls)


def test_collect_datetime_end_date_is_normalized_to_date():
    client = FakeCostClient(script=[_total_response(1.0)])
    CostExplorerCollector(client).collect(
        window_days=7,
        end_date=datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc),
    )
    assert client.calls[0]["TimePeriod"] == _period(END, 7)