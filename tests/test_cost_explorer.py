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
    MAX_COST_EXPLORER_PAGES,
    MAX_COST_GROUP_BY_KEYS,
    MAX_COST_WINDOW_DAYS,
    CollectionFailureCategory,
    SWSResourceType,
    TraceEventType,
    TraceStatus,
)
from sws_agent.cost_explorer import (
    CostExplorerCollector,
    EMPTY_TAG_VALUE_ASSUMPTION,
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


def _total_response(*amounts, token=None):
    """A Cost Explorer response with one ResultsByTime entry per amount."""
    response = {
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
    if token is not None:
        response["NextPageToken"] = token
    return response


def _grouped_response(*groups, token=None):
    """A Cost Explorer response; groups are ``(key_value, amount)`` pairs."""
    response = {
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
    if token is not None:
        response["NextPageToken"] = token
    return response


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
    # two daily entries share one tag value and are summed; real Cost Explorer
    # TAG group keys are ``Owner$<value>``
    response = {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-01-01", "End": "2026-01-02"},
                "Groups": [
                    {
                        "Keys": ["Owner$eng"],
                        "Metrics": {"UnblendedCost": {"Amount": "3.0", "Unit": "USD"}},
                    }
                ],
            },
            {
                "TimePeriod": {"Start": "2026-01-02", "End": "2026-01-03"},
                "Groups": [
                    {
                        "Keys": ["Owner$eng"],
                        "Metrics": {"UnblendedCost": {"Amount": "4.0", "Unit": "USD"}},
                    },
                    {
                        "Keys": ["Owner$data"],
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


# ---------------------------------------------------------------------------
# Real Cost Explorer TAG group-key contract (regressions)
# ---------------------------------------------------------------------------


def test_tag_group_key_prefix_is_stripped_to_value():
    """Real TAG group keys are ``Owner$<value>``; the value is the label."""
    script = [
        _total_response(10.0),
        _grouped_response(("Owner$alice", 7.5)),
    ]
    client = FakeCostClient(script=script)
    estimates = {
        e.line_item: e
        for e in CostExplorerCollector(client).collect(
            group_by=[COST_GROUP_TAG_OWNER], end_date=END
        )
    }
    assert set(estimates) == {"total", "owner_tag:alice"}
    assert estimates["owner_tag:alice"].amount_usd == pytest.approx(7.5)
    assert estimates["owner_tag:alice"].assumptions == []


def test_tag_group_untagged_empty_value_is_explicit_and_assumed():
    """``Owner$`` (no tag value) becomes an explicit ``owner_tag:`` row."""
    script = [
        _total_response(5.0),
        _grouped_response(("Owner$alice", 3.0), ("Owner$", 2.0)),
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimates = {
        e.line_item: e
        for e in CostExplorerCollector(client, trace=trace).collect(
            group_by=[COST_GROUP_TAG_OWNER], end_date=END
        )
    }
    assert set(estimates) == {"total", "owner_tag:alice", "owner_tag:"}
    assert estimates["owner_tag:"].amount_usd == pytest.approx(2.0)
    assert estimates["owner_tag:"].assumptions == [EMPTY_TAG_VALUE_ASSUMPTION]
    assert estimates["owner_tag:alice"].assumptions == []
    # an untagged group is a real value, not a failure
    assert _failed_events(trace) == []


def test_tag_group_mismatched_prefix_is_parse_failure_and_skipped():
    """A key that is not ``Owner$...`` is never accepted as an owner value."""
    script = [
        _total_response(4.0),
        _grouped_response(("Environment$prod", 4.0)),
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimates = CostExplorerCollector(client, trace=trace).collect(
        group_by=[COST_GROUP_TAG_OWNER], end_date=END
    )
    assert [e.line_item for e in estimates] == ["total"]
    failures = _failed_events(trace)
    assert len(failures) == 1
    assert (
        failures[0].metadata["category"] == CollectionFailureCategory.PARSE.value
    )


# ---------------------------------------------------------------------------
# NextPageToken pagination (regressions)
# ---------------------------------------------------------------------------


def test_total_query_pagination_accumulates_pages():
    script = [
        _total_response(10.0, token="page-2"),
        _total_response(5.0),
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimate = CostExplorerCollector(client, trace=trace).collect(end_date=END)[0]
    assert estimate.amount_usd == pytest.approx(15.0)
    assert len(client.calls) == 2
    assert "NextPageToken" not in client.calls[0]
    assert client.calls[1]["NextPageToken"] == "page-2"
    assert _failed_events(trace) == []
    # final page carries no token: collection is complete, not truncated
    assert "truncated" not in (_succeed_events(trace)[0].metadata or {})


def test_grouped_query_pagination_accumulates_pages():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 4.0), token="page-2"),
        _grouped_response(("AmazonS3", 6.0)),
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimates = {
        e.line_item: e
        for e in CostExplorerCollector(client, trace=trace).collect(
            group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
        )
    }
    assert estimates["service:AmazonS3"].amount_usd == pytest.approx(10.0)
    assert len(client.calls) == 3
    assert client.calls[2]["NextPageToken"] == "page-2"
    assert _failed_events(trace) == []


def test_total_query_pagination_bound_marks_truncated_not_complete():
    """Every page claims more data: stop at the cap and surface truncation."""
    script = [
        _total_response(1.0, token="next")
        for _ in range(MAX_COST_EXPLORER_PAGES + 1)
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimate = CostExplorerCollector(client, trace=trace).collect(end_date=END)[0]
    assert len(client.calls) == MAX_COST_EXPLORER_PAGES  # 21st call never made
    assert estimate.amount_usd == pytest.approx(float(MAX_COST_EXPLORER_PAGES))
    successes = _succeed_events(trace)
    assert successes[0].metadata.get("truncated") is True
    assert _failed_events(trace) == []


def test_grouped_query_pagination_bound_marks_truncated_not_complete():
    script = [
        _total_response(1.0),
        *[
            _grouped_response(("AmazonS3", 1.0), token="next")
            for _ in range(MAX_COST_EXPLORER_PAGES + 1)
        ],
    ]
    client = FakeCostClient(script=script)
    trace = TraceRecorder()
    estimates = {
        e.line_item: e
        for e in CostExplorerCollector(client, trace=trace).collect(
            group_by=[COST_GROUP_DIMENSION_SERVICE], end_date=END
        )
    }
    assert estimates["service:AmazonS3"].amount_usd == pytest.approx(
        float(MAX_COST_EXPLORER_PAGES)
    )
    grouped_successes = [
        e
        for e in _succeed_events(trace)
        if e.metadata.get("group_by") == COST_GROUP_DIMENSION_SERVICE
    ]
    assert grouped_successes[0].metadata.get("truncated") is True
    assert _failed_events(trace) == []


def test_collect_multiple_group_by_keys_runs_separate_queries():
    script = [
        _total_response(10.0),
        _grouped_response(("AmazonS3", 6.0)),
        _grouped_response(("Owner$eng", 4.0)),
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
        _grouped_response(("Owner$eng", 4.0)),
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