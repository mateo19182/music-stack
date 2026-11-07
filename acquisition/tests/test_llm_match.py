import json
from unittest.mock import Mock

import httpx

from app.llm_match import Judge


def reply(verdicts, status=200):
    response = Mock(status_code=status)
    response.json.return_value = {"choices": [{"message": {"tool_calls": [{"function": {
        "name": "record_verdicts", "arguments": json.dumps({"verdicts": verdicts})}}]}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100}}
    response.raise_for_status = (lambda: None) if status == 200 else Mock(side_effect=httpx.HTTPStatusError(
        "bad", request=Mock(), response=Mock(status_code=status)))
    return response


CANDIDATES = [{"source": "soulseek", "directory": "Music\\A - B", "file_count": 9, "files": [{"filename": "x\\01.flac"}]},
              {"source": "torrent", "title": "A - B - 2024, FLAC (tracks)"}]


def judge(response):
    calls = []
    j = Judge({"llm_api_key": "k"}, post=lambda *a, **k: calls.append(k["json"]) or response)
    return j, calls


def test_verdicts_for_every_candidate_and_the_cost():
    j, calls = judge(reply([{"id": "c0", "match": True, "problem": "none", "reason": "ok"},
                            {"id": "c1", "match": False, "problem": "chapter_or_part", "reason": "chapter"}]))
    found = j.verdicts("A", "B", CANDIDATES, 2024)
    assert found[0]["match"] is True and found[1]["problem"] == "chapter_or_part"
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert set(sent["candidates"]) == {"c0", "c1"} and calls[0]["model"] == "claude-haiku-5-5"
    assert calls[0]["tool_choice"]["function"]["name"] == "record_verdicts"
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
    assert Judge({"llm_api_key": "k", "llm_matching": False}).enabled is False


def test_endpoint_model_and_confidence_come_from_config():
    sent = []
    response = reply([{"id": "c0", "match": False, "problem": "other_artist", "p_wanted": 0.3, "reason": "x"},
                      {"id": "c1", "match": True, "problem": "none", "p_wanted": 7, "reason": "y"},
                      {"id": "c2", "match": False, "problem": "version", "p_wanted": 0.9, "reason": "z"}])
    j = Judge({"llm_api_key": "k", "llm_base_url": "http://proxy/v1/", "match_model": "m"},
              post=lambda url, **k: sent.append((url, k)) or response)
    found = j.verdicts("A", "B", CANDIDATES + [{"source": "youtube", "title": "B"}])
    assert sent[0][0] == "http://proxy/v1/chat/completions" and sent[0][1]["json"]["model"] == "m"
    assert sent[0][1]["headers"]["Authorization"] == "Bearer k"
    assert found[0]["probability"] == 0.3
    assert found[1]["probability"] is None and found[2]["probability"] is None   # out of range, or disagrees with the verdict


def test_track_verdicts_show_each_file_with_its_length_and_the_reference():
    j, calls = judge(reply([{"id": "c0", "match": True, "problem": "none", "reason": "ok"}]))
    found = j.track_verdicts("100 gecs", "hand crushed by a mallet (Remix)",
                             [{"source": "soulseek", "filename": "Music\\gecs\\05 hand crushed.flac", "duration": 175}],
                             "1000 gecs and The Tree of Clues", {"title": "1000 gecs and The Tree of Clues", "tracks": [("hand crushed", 175)]})
    assert found[0]["match"] is True
    sent = json.loads(calls[0]["messages"][1]["content"])
    assert sent["candidates"]["c0"] == {"source": "soulseek file", "path": "Music/gecs/05 hand crushed.flac", "seconds": 175}
    assert sent["reference_tracklist"]["tracks"] == [{"title": "hand crushed", "seconds": 175}]
    assert "recording" in calls[0]["messages"][0]["content"]
