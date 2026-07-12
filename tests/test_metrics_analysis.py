import pytest

from metrics_analysis import format_metrics_for_llm


@pytest.mark.parametrize(
    "prom_response,expected",
    [
        pytest.param({"data": {"result": []}}, "No data returned for this query.", id="no_results"),
        pytest.param({}, "No data returned for this query.", id="missing_data_key"),
        pytest.param(
            {
                "data": {
                    "result": [
                        {
                            "metric": {"job": "app", "instance": "app:9464"},
                            "values": [[1700000000, "1"], [1700000015, "2"]],
                        }
                    ]
                }
            },
            "Series [job=app, instance=app:9464]:\n  22:13:20 -> 1\n  22:13:35 -> 2",
            id="single_series_with_labels",
        ),
        pytest.param(
            {"data": {"result": [{"metric": {}, "values": [[1700000000, "1"]]}]}},
            "Series [no labels]:\n  22:13:20 -> 1",
            id="no_labels",
        ),
        pytest.param(
            {
                "data": {
                    "result": [
                        {"metric": {"instance": "a"}, "values": [[1700000000, "1"]]},
                        {"metric": {"instance": "b"}, "values": [[1700000000, "2"]]},
                    ]
                }
            },
            "Series [instance=a]:\n  22:13:20 -> 1\nSeries [instance=b]:\n  22:13:20 -> 2",
            id="multiple_series",
        ),
    ],
)
def test_format_metrics_for_llm(prom_response, expected):
    assert format_metrics_for_llm(prom_response) == expected
