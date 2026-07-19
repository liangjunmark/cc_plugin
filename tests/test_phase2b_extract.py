from __future__ import annotations

import pytest


def test_extract_branch_record_accepts_fenced_json() -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record(
        '```json\n{"candidate_answer":"25","target_interpretation":"guarantee both flavors","worst_case_claim":"take all round apple peach and watermelon","controllable_premises":["shape is controllable"]}\n```'
    )

    assert record.candidate_answer == "25"
    assert record.target_interpretation == "guarantee both flavors"
    assert "shape is controllable" in record.controllable_premises


def test_extract_audit_record_salvages_tagged_text() -> None:
    from proxy.phase2b_extract import extract_audit_record

    record = extract_audit_record(
        "RESULT: fail\nFATAL: treated controllable shape as random draw\nREVISED_ANSWER: 21"
    )

    assert record.survives is False
    assert "controllable_shape_as_random" in record.fatal_error_tags
    assert record.revised_answer_if_any == "21"


def test_extract_audit_record_does_not_salvage_untagged_conflicting_numbers() -> None:
    from proxy.phase2b_extract import extract_audit_record

    record = extract_audit_record(
        "FAIL - target_mistake: The provided branch answer (14) is incorrect. "
        "The correct minimum number to guarantee the condition is 15. "
        "The 16th draw would guarantee it. Thus, 14 is a fatal target mistake."
    )

    assert record.survives is False
    assert record.revised_answer_if_any is None


def test_extract_audit_record_prefers_last_explicit_verdict_when_model_self_corrects() -> None:
    from proxy.phase2b_extract import extract_audit_record

    record = extract_audit_record(
        "FAIL, target_mistake\n"
        "The branch seems wrong at first glance.\n"
        "Re-checking the arithmetic shows the branch is actually correct.\n"
        "PASS"
    )

    assert record.survives is True
    assert record.fatal_error_tags == []


def test_extract_attack_record_tracks_ledger_conflicts_and_worst_case_tags() -> None:
    from proxy.phase2b_extract import extract_attack_record

    record = extract_attack_record(
        "RESULT: fail\nFATAL: worst-case construction illegal\nLEDGER_CONFLICT: shape controllable by touch\nREVISED_ANSWER: 21"
    )

    assert record.survives is False
    assert "invalid_worst_case_construction" in record.fatal_error_tags
    assert "shape_controllable_by_touch" in record.ledger_conflicts


def test_extract_attack_record_does_not_salvage_untagged_conflicting_numbers() -> None:
    from proxy.phase2b_extract import extract_attack_record

    record = extract_attack_record(
        "FAIL. The branch answer of 22 is incorrect. "
        "The correct answer is 29, making the branch answer of 22 incorrect."
    )

    assert record.survives is False
    assert record.revised_answer_if_any is None


def test_extract_attack_record_prefers_last_explicit_verdict_when_model_self_corrects() -> None:
    from proxy.phase2b_extract import extract_attack_record

    record = extract_attack_record(
        "FAIL\n"
        "Initial attack claims the branch breaks.\n"
        "After checking the branch again, no valid attack remains.\n"
        "PASS"
    )

    assert record.survives is True
    assert record.fatal_error_tags == []
    assert record.ledger_conflicts == []


def test_extract_branch_record_falls_back_to_numeric_answer_and_keywords() -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record("最坏情况里把形状当成可控条件，再构造反例，答案 21。")

    assert record.candidate_answer == "21"
    assert "可控" in "".join(record.controllable_premises)


@pytest.mark.parametrize(
    ("raw_text", "candidate_answer"),
    [("[]", None), ('"21"', "21"), ("null", None)],
)
def test_extract_branch_record_falls_back_for_non_object_json(
    raw_text: str, candidate_answer: str | None
) -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record(raw_text)

    assert record.candidate_answer == candidate_answer


def test_extract_branch_record_coerces_string_controllable_premise() -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record('{"controllable_premises":"shape is controllable"}')

    assert record.controllable_premises == ["shape is controllable"]


@pytest.mark.parametrize(
    "raw_text",
    [
        '{"controllable_premises":null}',
        '{"controllable_premises":42}',
        '{"controllable_premises":{"unexpected":"value"}}',
    ],
)
def test_extract_branch_record_ignores_malformed_controllable_premises(
    raw_text: str,
) -> None:
    from proxy.phase2b_extract import extract_branch_record

    record = extract_branch_record(raw_text)

    assert record.controllable_premises == []


def test_extract_numeric_answer_accepts_cjk_adjacent_number() -> None:
    from proxy.phase2b_extract import extract_numeric_answer

    assert extract_numeric_answer("答案21。") == "21"


@pytest.mark.parametrize(
    "text",
    [
        "答案21，共3种口味。",
        "答案为：21，共3种口味。",
        "The answer is: 21, with 3 flavours.",
    ],
)
def test_extract_numeric_answer_prefers_answer_associated_number(text: str) -> None:
    from proxy.phase2b_extract import extract_numeric_answer

    assert extract_numeric_answer(text) == "21"
