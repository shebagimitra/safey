# ============================================================
# COMPONENT: C2 - LLM Client
# PURPOSE: Issue OpenAI-compatible HTTP inference requests to vLLM on AMD ROCm.
# INPUT: Chat message sequences, generation hyperparameters, structured JSON schemas.
# OUTPUT: LLMResult with generated text, token usage, latency, and throughput metrics.
# CALLED BY: Reasoner (C5), Verifier (C6)      CALLS: common/config.py, common/trace.py, httpx
# IF IT BREAKS, CHECK: vLLM endpoint connectivity, LLM_BASE_URL formatting, model name mismatch.
# ============================================================

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from common.config import Settings, get_settings
from common.trace import ComponentError, trace

# [VERIFY] Server-side JSON schema enforcement via vLLM OpenAI-compatible API must be tested live.
# Default is False. When False, structured output schema is enforced downstream by C6 verifier harness.
ENABLE_SERVER_JSON_SCHEMA: bool = False

# [VERIFY] The exact request field name for Gemma 4 thinking mode in vLLM must be confirmed
# against vLLM Gemma 4 documentation. Do not guess. Default is None (do not send).
THINKING_FIELD: Optional[str] = None


class LLMResult(BaseModel):
    """Execution telemetry and output payload from an LLM inference call."""
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Generated assistant response text")
    input_tokens: int = Field(description="Prompt token count from usage telemetry")
    output_tokens: int = Field(description="Completion token count from usage telemetry")
    latency_s: float = Field(description="Measured end-to-end latency in seconds")
    tokens_per_s: float = Field(description="Calculated generation throughput (tokens/second)")
    model_name: str = Field(description="Model identifier returned by server or configuration")


def _resolve_endpoints(base_url: str) -> tuple[str, str]:
    """IN: Base URL string from configuration.
    OUT: Tuple of (chat_completions_url, models_url).
    FAILS WITH: Never fails."""
    # STEP 1: Normalize base URL path | takes: base_url | gives: normalized string
    clean_base = base_url.rstrip("/")
    # STEP 2: Format chat and models endpoint URLs | takes: clean_base | gives: tuple of URLs
    if clean_base.endswith("/v1"):
        return f"{clean_base}/chat/completions", f"{clean_base}/models"
    return f"{clean_base}/v1/chat/completions", f"{clean_base}/v1/models"


def chat(
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int = 2000,
    json_schema: Optional[dict[str, Any]] = None,
    thinking: Optional[bool] = None,
    timeout_s: float = 600.0,
    settings: Optional[Settings] = None,
) -> LLMResult:
    """IN: List of message dictionaries, temperature, max_tokens, json_schema, thinking, timeout_s, optional Settings.
    OUT: Validated LLMResult instance containing text and metrics.
    FAILS WITH: ComponentError on HTTP error, timeout, or invalid response."""
    # STEP 1: Resolve configuration settings | takes: settings | gives: active Settings object
    active_settings = settings or get_settings()
    chat_url, _ = _resolve_endpoints(active_settings.LLM_BASE_URL)

    # STEP 2: Build OpenAI-compatible request payload | takes: messages, params, flags | gives: payload dictionary
    payload: dict[str, Any] = {
        "model": active_settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    # Structured output schema configuration [VERIFY: must test against live vLLM ROCm endpoint]
    if json_schema is not None and ENABLE_SERVER_JSON_SCHEMA:
        if isinstance(json_schema, dict) and "type" in json_schema:
            payload["response_format"] = json_schema
        else:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "structured_output",
                    "schema": json_schema,
                    "strict": True,
                },
            }

    # Thinking mode configuration [VERIFY: field name against vLLM Gemma 4 docs]
    if thinking is not None:
        if THINKING_FIELD is not None:
            payload[THINKING_FIELD] = thinking
        else:
            # Field name unverified; skipping transmission until confirmed in vLLM documentation
            trace("C2", "Thinking mode requested but THINKING_FIELD is None; skipping extra field")

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {active_settings.LLM_API_KEY}",
    }

    trace(
        "C2",
        "Dispatching chat completion request",
        message_count=len(messages),
        temperature=temperature,
        max_tokens=max_tokens,
        server_schema=bool(json_schema and ENABLE_SERVER_JSON_SCHEMA),
    )

    # STEP 3: Execute HTTP POST request and measure latency | takes: payload, headers, timeout_s | gives: raw response
    start_time = time.perf_counter()
    try:
        with httpx.Client(timeout=timeout_s) as client:
            response = client.post(chat_url, json=payload, headers=headers)
            response.raise_for_status()
            response_data = response.json()
    except httpx.TimeoutException as exc:
        trace("C2", "Chat completion timed out", timeout_s=timeout_s)
        raise ComponentError("C2", "chat_request", f"LLM request timed out after {timeout_s}s: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        body_excerpt = exc.response.text[:200].replace("\n", " ")
        trace("C2", "Chat completion HTTP error", status_code=status_code)
        raise ComponentError("C2", "chat_response", f"LLM HTTP error {status_code}: {body_excerpt}") from exc
    except httpx.RequestError as exc:
        trace("C2", "Chat completion network failure", error=str(exc))
        raise ComponentError("C2", "chat_network", f"LLM network communication error: {exc}") from exc
    except Exception as exc:
        trace("C2", "Chat completion unexpected failure", error=str(exc))
        raise ComponentError("C2", "chat_execution", f"Unexpected error during LLM call: {exc}") from exc

    latency_s = time.perf_counter() - start_time

    # STEP 4: Parse response JSON and compute telemetry metrics | takes: response_data, latency_s | gives: LLMResult
    try:
        choices = response_data.get("choices", [])
        if not choices:
            raise ComponentError("C2", "parse_response", "LLM response missing 'choices' list")
        text_content = choices[0].get("message", {}).get("content", "")
        if text_content is None:
            text_content = ""

        usage = response_data.get("usage", {})
        input_tokens = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
        output_tokens = int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        tokens_per_s = round(output_tokens / latency_s, 2) if latency_s > 0 else 0.0
        returned_model = response_data.get("model", active_settings.LLM_MODEL)

        result = LLMResult(
            text=text_content,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_s=round(latency_s, 4),
            tokens_per_s=tokens_per_s,
            model_name=returned_model,
        )

        trace(
            "C2",
            "Chat completion succeeded",
            latency_s=result.latency_s,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            tokens_per_s=result.tokens_per_s,
        )
        return result
    except ComponentError:
        raise
    except Exception as exc:
        trace("C2", "Response parsing failed", error=str(exc))
        raise ComponentError("C2", "parse_response", f"Failed to parse LLM response payload: {exc}") from exc


def health_check(settings: Optional[Settings] = None, timeout_s: float = 10.0) -> bool:
    """IN: Optional Settings instance and timeout_s float.
    OUT: Boolean True if /v1/models returns 200 OK, False otherwise.
    FAILS WITH: Never raises ComponentError; returns False on connection or status failures."""
    # STEP 1: Resolve settings and models endpoint URL | takes: settings | gives: models_url
    try:
        active_settings = settings or get_settings()
        _, models_url = _resolve_endpoints(active_settings.LLM_BASE_URL)
        headers = {"Authorization": f"Bearer {active_settings.LLM_API_KEY}"}
    except Exception as exc:
        trace("C2", "Health check configuration resolution failed", error=str(exc))
        return False

    # STEP 2: Issue GET request to /v1/models | takes: models_url, timeout_s | gives: HTTP response
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.get(models_url, headers=headers)
            is_healthy = resp.status_code == 200
            trace("C2", "Health check probed endpoint", status_code=resp.status_code, is_healthy=is_healthy)
            return is_healthy
    except Exception as exc:
        trace("C2", "Health check probe failed", error=str(exc))
        return False


class _MockOpenAIHandler(BaseHTTPRequestHandler):
    """Local mock HTTP handler serving fixed OpenAI-style responses for self-testing."""

    def log_message(self, format: str, *args: Any) -> None:
        """IN: Format string and variable arguments.
        OUT: None.
        FAILS WITH: Never fails (suppresses noisy console logging during tests)."""
        pass

    def do_GET(self) -> None:
        """IN: Implicit HTTP GET request.
        OUT: Serialized JSON response to client socket.
        FAILS WITH: Never fails handler."""
        # STEP 1: Handle /models endpoint check | takes: self.path | gives: HTTP 200 or 404
        if "/models" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"object":"list","data":[{"id":"google/gemma-4-26B-A4B-it"}]}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        """IN: Implicit HTTP POST request.
        OUT: Serialized JSON response to client socket.
        FAILS WITH: Never fails handler."""
        # STEP 1: Route /chat/completions requests | takes: self.path | gives: HTTP response
        if "/chat/completions" in self.path:
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            body_str = body_bytes.decode("utf-8")

            if "SIMULATE_HTTP_500" in body_str:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Internal simulated failure"}}')
                return

            response_payload = {
                "id": "chatcmpl-mock-fixture-001",
                "object": "chat.completion",
                "created": 1677652288,
                "model": "google/gemma-4-26B-A4B-it",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "FIXTURE: Mock assistant diagnostic response",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 30,
                    "total_tokens": 45,
                },
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response_payload).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def run_selftest() -> None:
    """IN: None (invoked via CLI argument --selftest).
    OUT: None (prints PASS/FAIL and exits).
    FAILS WITH: SystemExit on test assertion failure."""
    # STEP 1: Start ephemeral local mock HTTP server | takes: None | gives: active HTTPServer and port
    try:
        server = HTTPServer(("127.0.0.1", 0), _MockOpenAIHandler)
        port = server.server_port
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        trace("C2", "Started local mock server for self-test", port=port)

        mock_settings = Settings(
            llm_base_url=f"http://127.0.0.1:{port}/v1",
            llm_api_key="mock_secret_key",
            llm_model="google/gemma-4-26B-A4B-it",
            evolus_base_url="http://127.0.0.1:9999",
            evolus_api_key="mock_key",
            evolus_app_id="mock_app",
            data_dir="data",
        )

        # STEP 2: Verify health_check probe against mock server | takes: mock_settings | gives: boolean True
        if not health_check(settings=mock_settings):
            print("FAIL: health_check returned False on valid mock endpoint")
            sys.exit(1)

        # STEP 3: Execute test chat call and assert fields and token math | takes: mock_settings | gives: LLMResult
        res = chat(
            messages=[{"role": "user", "content": "FIXTURE: ping query"}],
            temperature=0.0,
            max_tokens=500,
            settings=mock_settings,
        )

        if res.text != "FIXTURE: Mock assistant diagnostic response":
            print(f"FAIL: Unexpected response text: {res.text}")
            sys.exit(1)
        if res.input_tokens != 15:
            print(f"FAIL: Expected 15 input tokens, got {res.input_tokens}")
            sys.exit(1)
        if res.output_tokens != 30:
            print(f"FAIL: Expected 30 output tokens, got {res.output_tokens}")
            sys.exit(1)
        if res.latency_s <= 0:
            print(f"FAIL: Non-positive latency: {res.latency_s}")
            sys.exit(1)

        expected_tokens_per_s = round(30 / res.latency_s, 2)
        if abs(res.tokens_per_s - expected_tokens_per_s) > 0.05:
            print(f"FAIL: Token throughput math mismatch: got {res.tokens_per_s}, expected {expected_tokens_per_s}")
            sys.exit(1)
        if res.model_name != "google/gemma-4-26B-A4B-it":
            print(f"FAIL: Model name mismatch: {res.model_name}")
            sys.exit(1)

        # STEP 4: Verify error handling on HTTP 500 status | takes: failure simulation prompt | gives: ComponentError
        try:
            chat(
                messages=[{"role": "user", "content": "SIMULATE_HTTP_500"}],
                settings=mock_settings,
            )
            print("FAIL: Expected ComponentError on HTTP 500 response")
            sys.exit(1)
        except ComponentError as err:
            if err.component != "C2" or "500" not in err.message:
                print(f"FAIL: Unexpected error content: {err}")
                sys.exit(1)

        # STEP 5: Shutdown mock server and report PASS | takes: server | gives: PASS terminal output
        server.shutdown()
        server.server_close()
        trace("C2", "Mock self-test assertions passed successfully")
        print("PASS")
    except Exception as exc:
        trace("C2", "Self-test failure", error=str(exc))
        print(f"FAIL: {exc}")
        sys.exit(1)


def run_live_test() -> None:
    """IN: None (invoked via CLI argument --live).
    OUT: None (prints LLMResult to terminal).
    FAILS WITH: ComponentError or SystemExit on live endpoint failure."""
    # STEP 1: Load active environment settings | takes: None | gives: Settings
    try:
        active_settings = get_settings()
        trace("C2", "Running live test against configured endpoint", base_url=active_settings.LLM_BASE_URL, model=active_settings.LLM_MODEL)
    except Exception as exc:
        print(f"FAIL: Unable to load settings from configuration: {exc}")
        sys.exit(1)

    # STEP 2: Dispatch live query "Say OK" | takes: active_settings | gives: LLMResult
    try:
        live_result = chat(
            messages=[{"role": "user", "content": "Say OK"}],
            temperature=0.0,
            max_tokens=50,
            settings=active_settings,
        )
        # STEP 3: Display live LLMResult output | takes: live_result | gives: stdout
        print(json.dumps(live_result.model_dump(), indent=2))
        print("PASS")
    except Exception as exc:
        print(f"FAIL: Live inference request failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    if "--live" in sys.argv:
        run_live_test()
    elif "--selftest" in sys.argv:
        run_selftest()
    else:
        print("Usage: python -m model.llm_client --selftest | --live")
