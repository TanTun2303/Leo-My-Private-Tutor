import json

import pytest

from leo_memory.schemas import SummaryV1
from leo_memory.summarizer import SummaryError, enforce_limits, summarize

from .conftest import FakeLLM, summary_json

MSGS = [{"role": "user", "content": "I keep confusing relaxation with visiting order in Dijkstra."}]


def test_valid_output_and_prompt_shape():
    llm = FakeLLM([summary_json()])
    s = summarize(llm, "Dijkstra practice", {"summary": "old"}, MSGS, 60)
    assert s.topics == ["dijkstra"]
    system, user = llm.prompts[0]
    assert "at most 60 words" in system["content"]
    payload = json.loads(user["content"])
    assert payload["previous_summary"] == {"summary": "old"} and payload["title"] == "Dijkstra practice"


def test_limits_enforced():
    long = " ".join(["word"] * 200)
    s = enforce_limits(
        SummaryV1(
            summary=long,
            key_points=[f"point {i} " + " ".join(["w"] * 30) for i in range(9)],
            topics=["Dijkstra", "dijkstra", "Heaps", "a", "b", "c", "d", "e"],
            weak_spots=["x", "y", "z", "w"],
            mastered=["m1", "m2", "m3", "m4"],
            open_questions=["q1", "q2", "q3", "q4"],
        ),
        60,
    )
    assert len(s.summary.split()) <= 60 and len(s.summary) <= 600
    assert len(s.key_points) == 5 and all(len(k.split()) <= 20 for k in s.key_points)
    assert s.topics == ["dijkstra", "heaps", "a", "b", "c", "d"]
    assert len(s.weak_spots) == len(s.mastered) == len(s.open_questions) == 3


def test_latex_stripped_from_memory():
    s = enforce_limits(SummaryV1(summary="The user solved $T(n)=2T(n/2)+n$ via \\(master\\) theorem."), 60)
    assert "$" not in s.summary and "\\(" not in s.summary


def test_retry_once_then_succeed():
    llm = FakeLLM(["not json", "```json\n" + summary_json() + "\n```"])
    assert summarize(llm, None, None, MSGS, 60).summary
    assert len(llm.prompts) == 2


def test_two_invalid_outputs_fail():
    llm = FakeLLM(["{}", json.dumps({"summary": ""})])
    with pytest.raises(SummaryError):
        summarize(llm, None, None, MSGS, 60)
