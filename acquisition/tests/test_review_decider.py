import json

import httpx

from app.review_decider import Decider


class Response:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


def records():
    library = {"artist": "Overmono", "title": "Good Lies", "album": "Good Lies", "format": "MP3", "bitrate": 320,
               "duration": 226, "path": "/library/Overmono/Good Lies/Good Lies.mp3"}
    return [{"id": "a", "title": "Good Lies", "artist": "Overmono", "album": "Good Lies", "format": "FLAC",
             "bitrate": "900000", "duration": "160.2", "possible_duplicates": [library]},
            {"id": "b", "title": "Is U", "artist": "Overmono", "possible_duplicates": []}]


PLANS = {"a": {"action": "ask", "questions": [{"kind": "version", "library": []}]},
         "b": {"action": "ask", "questions": [{"kind": "mismatch", "field": "title"}]}}


def test_the_model_sees_metadata_only_and_its_answers_are_checked():
    sent = []

    def post(url, **kw):
        sent.append(kw["json"])
        return Response(json.dumps({"decisions": [
            {"id": "a", "action": "replace", "replace": ["L1"], "reason": "same recording"},
            {"id": "a", "action": "skip", "replace": [], "reason": "a repeated id is ignored"},
            {"id": "x", "action": "add", "replace": [], "reason": "an invented id is ignored"}]}))

    decider = Decider({"openrouter_api_key": "k"}, post=post)
    job = {"candidate": {"requested_artist": "Overmono", "requested_album": "Good Lies", "source": "soulseek"}}
    decisions = decider.decide(job, records(), PLANS)
    assert decisions["a"]["action"] == "replace" and decisions["a"]["replace"][0]["path"].endswith("Good Lies.mp3")
    assert decisions["b"]["action"] == "unsure"   # left out by the model: the owner decides
    request = sent[0]
    assert request["model"] == "openai/gpt-6-luna" and request["reasoning"] == {"effort": "high"}
    data = json.loads(request["messages"][1]["content"])
    assert data["tracks"][0]["kbps"] == 900 and data["tracks"][0]["seconds"] == 160
    assert "/library" not in request["messages"][1]["content"]   # library paths stay on the server


def test_replace_without_a_library_copy_and_an_unreachable_model_leave_it_to_the_owner():
    answer = {"decisions": [{"id": "a", "action": "replace", "replace": ["L9"], "reason": "x"}]}
    decider = Decider({"openrouter_api_key": "k"}, post=lambda url, **kw: Response(json.dumps(answer)))
    assert decider.decide({}, records()[:1], PLANS)["a"]["action"] == "unsure"

    def down(url, **kw):
        raise httpx.ConnectError("down")

    assert Decider({"openrouter_api_key": "k"}, post=down).decide({}, records(), PLANS) is None
    assert not Decider({}).enabled
