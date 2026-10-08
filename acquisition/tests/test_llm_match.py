import json
from unittest.mock import Mock

import httpx

from app.llm_match import Judge


def reply(verdicts, status=200):
    response = Mock(status_code=status)
    response.json.return_value = {"content": [{"type": "tool_use", "input": {"verdicts": verdicts}}],
                                  "usage": {"input_tokens": 1000, "output_tokens": 100}}
    response.raise_for_status = (lambda: None) if status == 200 else Mock(side_effect=httpx.HTTPStatusError(
        "bad", request=Mock(), response=Mock(status_code=status)))
    return response


CANDIDATES = [{"source": "soulseek", "directory": "Music\\A - B", "file_count": 9, "files": [{"filename": "x\\01.flac"}]},
              {"source": "torrent", "title": "A - B - 2024, FLAC (tracks)"}]


def judge(response):
    calls = []
    j = Judge({"anthropic_api_key": "k"}, post=lambda *a, **k: calls.append(k["json"]) or response)
    return j, calls


def test_verdicts_for_every_candidate_and_the_cost():
    j, calls = judge(reply([{"id": "c0", "match": True, "problem": "none", "reason": "ok"},
                            {"id": "c1", "match": False, "problem": "chapter_or_part", "reason": "chapter"}]))
    found = j.verdicts("A", "B", CANDIDATES, 2024)
    assert found[0]["match"] is True and found[1]["problem"] == "chapter_or_part"
    sent = json.loads(calls[0]["messages"][0]["content"])
    assert set(sent["candidates"]) == {"c0", "c1"} and "temperature" not in calls[0]
    assert round(j.cost(), 6) == round((1000 * 0.10 + 100 * 0.50) / 1e6, 6)


def test_missing_invented_or_malformed_answers_fall_back():
    for verdicts in ([{"id": "c0", "match": True}],                                  # one missing
                     [{"id": "c0", "match": True}, {"id": "c9", "match": True}],     # invented id
                     ["c0", "c1"], "not json", None):
        j, _ = judge(reply(verdicts))
        assert j.verdicts("A", "B", CANDIDATES) is None
    j, _ = judge(reply(json.dumps([{"id": "c0", "match": True, "problem": "none", "reason": ""},
                                   {"id": "c1", "match": True, "problem": "none", "reason": ""}])))
    assert len(j.verdicts("A", "B", CANDIDATES)) == 2   # a JSON-encoded list is still read


def test_api_errors_and_a_missing_key_fall_back():
    j, _ = judge(reply([], status=400))
    assert j.verdicts("A", "B", CANDIDATES) is None
    assert Judge({}).verdicts("A", "B", CANDIDATES) is None
    assert Judge({"anthropic_api_key": "k", "llm_matching": False}).enabled is False


def test_openrouter_tool_calls_and_reported_cost():
    response = Mock(status_code=200, raise_for_status=lambda: None)
    response.json.return_value = {"choices": [{"message": {"tool_calls": [{"function": {"name": "record_verdicts", "arguments": json.dumps(
        {"verdicts": [{"id": "c0", "match": True, "problem": "none", "reason": ""},
                      {"id": "c1", "match": False, "problem": "other_artist", "reason": "x"}]})}}]}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 80, "cost": 0.0021}}
    sent = []
    j = Judge({"match_provider": "openrouter", "openrouter_api_key": "k"}, post=lambda url, **k: sent.append((url, k)) or response)
    assert j.model == "typesafe/jev-router"
    found = j.verdicts("A", "B", CANDIDATES)
    assert found[1]["problem"] == "other_artist"
    assert sent[0][0].startswith("https://openrouter.ai/") and sent[0][1]["json"]["tool_choice"]["function"]["name"] == "record_verdicts"
    assert j.cost() == 0.0021


def test_decisions_api_one_yes_no_question_per_candidate():
    response = Mock(status_code=200, raise_for_status=lambda: None)
    response.json.return_value = {"answers": {"c0": {"type": "noul", "noul": 0.93}, "c1": {"type": "noul", "noul": 0.08}},
                                  "usage": {"input_tokens": 600, "output_tokens": 4, "cost": 0.000025}}
    sent = []
    j = Judge({"match_provider": "decisions", "openrouter_api_key": "k"}, post=lambda url, **k: sent.append((url, k["json"])) or response)
    assert j.model == "~typesafe/jev-latest"
    found = j.verdicts("A", "B", CANDIDATES)
    assert found[0]["match"] is True and found[1]["match"] is False and found[0]["reason"] == "93% likely the album"
    url, body = sent[0]
    assert url.endswith("/api/alpha/decisions") and set(body["questions"]) == {"c0", "c1"}
    assert body["questions"]["c0"]["type"] == "noul" and "candidates" in body["state"]
    assert j.cost() == 0.000025
    response.json.return_value = {"answers": {"c0": {"type": "noul", "noul": 0.9}}, "usage": {}}   # one missing
    assert j.verdicts("A", "B", CANDIDATES) is None


def test_jev_can_answer_none_of_these():
    response = Mock(status_code=200, raise_for_status=lambda: None)
    response.json.return_value = {"answers": {"c0": {"type": "noul", "noul": 0.6}, "c1": {"type": "noul", "noul": 0.1},
                                              "best": {"type": "choice", "choice": "none", "probabilities": {"c0": 0.3, "c1": 0, "none": 0.7}}},
                                  "usage": {"cost": 0.00002}}
    sent = []
    j = Judge({"match_provider": "decisions", "openrouter_api_key": "k", "match_none_option": True},
              post=lambda url, **k: sent.append(k["json"]) or response)
    found = j.verdicts("A", "B", CANDIDATES)
    assert not any(v["match"] for v in found.values()) and found[0]["problem"] == "none_chosen"
    assert set(sent[0]["questions"]["best"]["criteria"]) == {"c0", "c1", "none"}
    response.json.return_value["answers"]["best"] = {"type": "choice", "choice": "c0"}
    assert j.verdicts("A", "B", CANDIDATES)[0]["match"] is True
