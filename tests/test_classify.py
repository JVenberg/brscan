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
