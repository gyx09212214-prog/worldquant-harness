from worldquant_harness.wq_expression_utils import (
    expression_components,
    expression_fields,
    expression_operators,
    field_signature,
    jaccard,
)


def test_expression_components_are_sorted_and_canonical():
    expression = "rank(ts_mean(close, 5) - rank(volume))"

    assert expression_components(expression) == {
        "fields": {"close", "volume"},
        "operators": {"rank", "ts_mean"},
    }
    assert expression_fields(expression) == ["close", "volume"]
    assert expression_operators(expression) == ["rank", "ts_mean"]
    assert field_signature(expression) == "close|volume"


def test_expression_helpers_handle_empty_input():
    assert expression_components(None) == {"fields": set(), "operators": set()}
    assert expression_fields("") == []
    assert expression_operators("") == []
    assert field_signature("") == ""


def test_expression_components_ignore_named_arguments_and_string_literals():
    expression = 'rank(group_rank(close, bucket(rank(cap), range="0.1,1,0.1")))'

    assert expression_components(expression) == {
        "fields": {"cap", "close"},
        "operators": {"bucket", "group_rank", "rank"},
    }


def test_expression_components_keep_fields_used_in_comparisons():
    expression = "trade_when(rank(volume / adv20) >= 0.5, rank(close), -1)"

    assert expression_fields(expression) == ["adv20", "close", "volume"]


def test_jaccard_normalizes_values():
    assert jaccard(["close", "volume"], ["volume", "vwap"]) == 0.3333
    assert jaccard([], []) == 0.0
