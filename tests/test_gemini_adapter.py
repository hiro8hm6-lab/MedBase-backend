import json

import httpx
import pytest
from google import genai
from google.genai import types

from medbase.config import Settings
from medbase.fixtures import fixture_data
from medbase.llm import GeminiProvider, ProviderQuotaError, wait_for_retry
from medbase.schemas import AnswerBatch


def test_google_sdk_request_uses_configured_model_and_schema_without_network(tmp_path):
    captured = []
    payload = {"answers": fixture_data()["answers"][:1]}

    def handle(request):
        captured.append(request)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": json.dumps(payload)}], "role": "model"},
                            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20},
        })

    settings = Settings(_env_file=None, gemini_api_key="synthetic-test-key")
    provider = GeminiProvider(settings, tmp_path)
    provider.client = genai.Client(api_key="synthetic-test-key", http_options=types.HttpOptions(
        httpx_client=httpx.Client(transport=httpx.MockTransport(handle))))
    actual = provider.generate("explain", "Return an answer", AnswerBatch, [], {"questions": []})
    assert actual == payload
    assert "gemini-3.8-flash:generateContent" in str(captured[0].url)
    body = json.loads(captured[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" in body["generationConfig"]
    assert "synthetic-test-key" not in (tmp_path / "llm_usage.jsonl").read_text()


def test_transient_request_retry_honors_server_delay(monkeypatch):
    from google.genai.errors import ClientError

    waits = []
    monkeypatch.setattr("medbase.llm.time.sleep", waits.append)
    error = ClientError(429, {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "42s"}
    ]}})
    wait_for_retry(error, 0)
    wait_for_retry(ClientError(503, {}), 2)
    wait_for_retry(ValueError("invalid JSON"), 0)
    assert waits == [42, 20]


@pytest.mark.parametrize("code,details", [
    (429, [{"violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}
    ]}]),
    (402, []),
])
def test_usage_limit_stops_subsequent_requests_without_exposing_details(tmp_path, code, details):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(code, json={"error": {"code": code, "status": "RESOURCE_EXHAUSTED",
            "message": "private-provider-message", "details": details}})

    provider = GeminiProvider(Settings(_env_file=None, gemini_api_key="synthetic-test-key"), tmp_path)
    provider.client = genai.Client(api_key="synthetic-test-key", http_options=types.HttpOptions(
        retry_options=types.HttpRetryOptions(attempts=1),
        httpx_client=httpx.Client(transport=httpx.MockTransport(handle))))
    for _ in range(2):
        with pytest.raises(ProviderQuotaError, match="1日|402") as caught:
            provider.generate("explain", "Answer", AnswerBatch, [], {})
        assert "private-provider-message" not in str(caught.value)
    assert len(requests) == 1
