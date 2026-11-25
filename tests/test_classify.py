import json

import pytest

anthropic = pytest.importorskip("anthropic")
httpx2 = pytest.importorskip("httpx2")

from brscan import classify as classify_mod  # noqa: E402

FILING = {
    "document_type": "grocery receipt", "folder": "Receipts", "vendor": "Safeway",
    "date": "2026-11-20", "amount": "71.29", "filename": "Safeway receipt",
    "title": "Safeway grocery receipt", "summary": "Weekly groceries.",
}


@pytest.fixture
def page(tmp_path):
    from PIL import Image
    p = tmp_path / "page-001.jpg"
    Image.new("RGB", (3000, 2000), "white").save(p, "JPEG")
    return p


def fake_api(monkeypatch, stop_reason="end_turn", text=json.dumps(FILING)):
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant",
            "model": seen["body"]["model"],
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 10},
        })

    real = anthropic.Anthropic
    http_client = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler))
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda **kw: real(http_client=http_client, max_retries=0, **kw))
    return seen


def test_request_shape_and_parsed_result(monkeypatch, page):
    seen = fake_api(monkeypatch)
    result = classify_mod.classify([page], ["Receipts", "Taxes"], api_key="sk-test", effort="low")

    assert result.folder == "Receipts" and result.amount == "71.29"
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == classify_mod.MAX_TOKENS
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "low"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "temperature" not in body and "thinking" not in body
    assert classify_mod.FALLBACK_BETA in seen["headers"]["anthropic-beta"]
    assert seen["headers"]["authorization"] == "Bearer sk-test"
    content = body["messages"][0]["content"]
    assert "Receipts, Taxes" in content[0]["text"]
    assert content[1]["type"] == "image"


@pytest.mark.parametrize("stop_reason,text", [
    ("refusal", ""),
    ("max_tokens", '{"document_type": "gro'),
    ("end_turn", '{"document_type": "receipt"}'),
])
def test_unusable_responses_raise(monkeypatch, page, stop_reason, text):
    fake_api(monkeypatch, stop_reason=stop_reason, text=text)
    with pytest.raises(classify_mod.ClassifyError):
        classify_mod.classify([page], [], api_key="sk-test")


def batch_json(*firsts):
    return json.dumps({"documents": [{"first_page": f, "filing": FILING} for f in firsts]})


def test_split_labels_pages_and_returns_zero_based_starts(monkeypatch, page):
    seen = fake_api(monkeypatch, text=batch_json(2, 1, 9))
    pages = [page, page, page]
    labels = ["sheet 1, front", "sheet 1, back", "sheet 2, front"]
    result = classify_mod.split(pages, ["Receipts"], api_key="sk-test", labels=labels)

    assert [start for start, _ in result] == [0, 1]
    assert result[0][1].folder == "Receipts"
    content = seen["body"]["messages"][0]["content"]
    assert content[1] == {"type": "text", "text": "Page 1 (sheet 1, front)"}
    assert content[2]["type"] == "image"
    assert content[5] == {"type": "text", "text": "Page 3 (sheet 2, front)"}


def test_split_chunks_large_batches(monkeypatch, page):
    calls = []
    seen = fake_api(monkeypatch, text=batch_json(1, 2))
    real = classify_mod._ask
    monkeypatch.setattr(classify_mod, "_ask", lambda *a: calls.append(1) or real(*a))
    monkeypatch.setattr(classify_mod, "SPLIT_CHUNK", 2)
    result = classify_mod.split([page, page, page], [], api_key="sk-test")
    assert len(calls) == 2
    assert [start for start, _ in result] == [0, 1, 2]
    assert seen["body"]["output_config"]["format"]["type"] == "json_schema"


def test_split_with_no_documents_raises(monkeypatch, page):
    fake_api(monkeypatch, text=json.dumps({"documents": []}))
    with pytest.raises(classify_mod.ClassifyError):
        classify_mod.split([page], [], api_key="sk-test")
