"""Contract tests for the native Anthropic and OpenAI adapters."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import cast

import anthropic
import httpx2
import openai
import pytest
from anthropic.resources.messages import Messages
from anthropic.types import ParsedMessage
from openai.resources.responses.input_tokens import InputTokens
from openai.resources.responses.responses import Responses
from openai.types.responses import ParsedResponse

from math_scout.providers import (
    AnthropicProvider,
    ConfigurationError,
    GenerationRequest,
    ModelRef,
    OpenAIProvider,
    Pricing,
    PromptBlock,
    ProviderAuthenticationError,
    ProviderEmptyOutputError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderRegistry,
)
from math_scout.reviewer import Review


def request(
    model: str,
    *,
    schema: bool = True,
    reasoning: bool = True,
    effort: str = "high",
) -> GenerationRequest:
    return GenerationRequest(
        model=ModelRef.parse(model),
        blocks=(
            PromptBlock("system", "global", cacheable=True),
            PromptBlock("system", "protocol", cacheable=True),
            PromptBlock("system", "lane", cacheable=True),
            PromptBlock("user", "issues"),
            PromptBlock("user", "paper"),
        ),
        reasoning=reasoning,
        effort=effort,
        output_limit=12_345,
        output_schema=Review if schema else None,
        prompt_cache_key="math-scout:paper-hash",
    )


class StreamManager:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def __enter__(self):
        if self.error is not None:
            raise self.error
        return self

    def __exit__(self, *exc_info):
        return False

    def get_final_message(self):
        return self.result

    def get_final_response(self):
        return self.result


class FakeAnthropicMessages:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.stream_calls = []
        self.count_calls = []

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return StreamManager(self.result, self.error)

    def count_tokens(self, **kwargs):
        self.count_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(input_tokens=321)


class FakeAnthropicClient:
    def __init__(self, result=None, error=None):
        self.messages = FakeAnthropicMessages(result, error)


class FakeOpenAIInputTokens:
    def __init__(self, owner):
        self.owner = owner

    def count(self, **kwargs):
        self.owner.count_calls.append(kwargs)
        if self.owner.error is not None:
            raise self.owner.error
        return SimpleNamespace(input_tokens=654)


class FakeOpenAIResponses:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.stream_calls = []
        self.count_calls = []
        self.input_tokens = FakeOpenAIInputTokens(self)

    def stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return StreamManager(self.result, self.error)


class FakeOpenAIClient:
    def __init__(self, result=None, error=None):
        self.responses = FakeOpenAIResponses(result, error)


def anthropic_provider(client: FakeAnthropicClient) -> AnthropicProvider:
    """The fakes stand in structurally; the SDK client parameters are nominal types."""
    return AnthropicProvider(cast(anthropic.Anthropic, client))


def openai_provider(client: FakeOpenAIClient) -> OpenAIProvider:
    return OpenAIProvider(cast(openai.OpenAI, client))


def anthropic_response(*, parsed=None, text=None, stop_reason="end_turn"):
    content = []
    if text is not None or parsed is not None:
        content = [
            {"type": "text", "text": text or parsed.model_dump_json(), "parsed_output": parsed}
        ]
    return ParsedMessage[Review](
        id="msg_test",
        model="claude-opus-5-5",
        role="assistant",
        type="message",
        content=content,
        stop_reason=stop_reason,
        usage=dict(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=4,
            cache_creation={"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 0},
            future_token_details={"thinking": 12},
        ),
        future_response_field={"native": True},
    )


def openai_response(*, parsed=None, text=None, status="completed", reason=None, output=None):
    if output is None:
        output = []
        if text is not None or parsed is not None:
            output = [
                dict(
                    id="msg_test",
                    type="message",
                    role="assistant",
                    status="completed",
                    content=[
                        dict(
                            type="output_text",
                            text=text or parsed.model_dump_json(),
                            annotations=[],
                            parsed=parsed,
                        )
                    ],
                )
            ]
    return ParsedResponse(
        id="resp_test",
        created_at=123.0,
        model="gpt-6.1-sol",
        object="response",
        parallel_tool_calls=True,
        tool_choice="auto",
        tools=[],
        status=status,
        incomplete_details=None if reason is None else {"reason": reason},
        output=output,
        usage=dict(
            input_tokens=11,
            output_tokens=22,
            total_tokens=33,
            input_tokens_details={"cached_tokens": 5, "cache_write_tokens": 6},
            output_tokens_details={"reasoning_tokens": 17, "future_detail": 9},
        ),
        future_response_field={"native": True},
    )


def test_model_ref_parses_provider_qualified_ids():
    anthropic_model = ModelRef.parse("anthropic:claude-opus-5")
    openai_model = ModelRef.parse("openai:gpt-5.6-sol")

    assert anthropic_model == ModelRef("anthropic", "claude-opus-5")
    assert anthropic_model.qualified == "anthropic:claude-opus-5"
    assert openai_model.qualified == "openai:gpt-5.6-sol"


@pytest.mark.parametrize("value", ["claude-opus-5", "gpt-5.6-sol", "synthetic-model"])
def test_model_ref_rejects_every_unqualified_id(value):
    with pytest.raises(ConfigurationError, match="PROVIDER:MODEL.*--preset"):
        ModelRef.parse(value)


def test_registry_instantiates_only_referenced_providers():
    registry = ProviderRegistry.from_models([ModelRef.parse("openai:gpt-5.6-sol")])
    assert isinstance(registry.for_model(ModelRef.parse("openai:gpt-5.6-sol")), OpenAIProvider)
    with pytest.raises(ConfigurationError, match="No 'anthropic' provider"):
        registry.for_model(ModelRef.parse("anthropic:claude-opus-5"))


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate", "cache_read", "cache_write"),
    [
        ("gpt-6-astra", 10.0, 50.0, 1.0, 12.5),
        ("gpt-6.1-sol", 2.0, 10.0, 0.1, 2.5),
        ("gpt-6.1-terra", 1.0, 5.0, 0.05, 1.25),
        ("gpt-6-luna", 0.1, 0.5, 0.01, 0.125),
        ("gpt-5.6", 4.0, 20.0, 0.4, 5.0),
        ("gpt-5.6-sol", 4.0, 20.0, 0.4, 5.0),
        ("gpt-5.6-terra", 2.0, 12.0, 0.2, 2.5),
        ("gpt-5.6-luna", 0.2, 1.2, 0.02, 0.25),
    ],
)
def test_openai_pricing_matches_current_provider_rates(
    model, input_rate, output_rate, cache_read, cache_write
):
    pricing = OpenAIProvider.pricing(model)
    assert pricing == Pricing(
        input=input_rate,
        output=output_rate,
        cache_read=cache_read,
        cache_write=cache_write,
        long_context_threshold=272_000,
        long_context_input_multiplier=2.0,
        long_context_output_multiplier=1.5,
    )
    assert pricing is not None
    assert pricing.input_rate(272_000) == input_rate
    assert pricing.input_rate(272_001) == input_rate * 2
    assert pricing.output_rate(272_001) == output_rate * 1.5
    assert pricing.cache_read_rate(272_001) == cache_read * 2
    assert pricing.cache_write_rate(272_001) == cache_write * 2


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_gpt_6_models_accept_every_supported_reasoning_effort(model, effort):
    OpenAIProvider.validate_request(model, reasoning=True, effort=effort, output_limit=128_000)


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol"])
def test_reasoning_only_openai_models_reject_disabled_reasoning(model):
    with pytest.raises(ConfigurationError, match="does not support reasoning effort none"):
        OpenAIProvider.validate_request(model, reasoning=False, effort="high", output_limit=64_000)


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_gpt_6_models_enforce_their_output_limit(model):
    with pytest.raises(ConfigurationError, match="at most 128,000 output tokens"):
        OpenAIProvider.validate_request(model, reasoning=True, effort="high", output_limit=128_001)


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate", "cache_read", "cache_write"),
    [
        ("claude-opus-5-5", 4.0, 20.0, 0.2, 5.0),
        ("claude-sonnet-5-5", 2.0, 10.0, 0.1, 2.5),
        ("claude-fable-5-1", 10.0, 50.0, 0.25, 12.5),
    ],
)
def test_current_claude_pricing(model, input_rate, output_rate, cache_read, cache_write):
    assert AnthropicProvider.pricing(model) == Pricing(
        input=input_rate, output=output_rate, cache_read=cache_read, cache_write=cache_write
    )


def test_haiku_5_5_pricing_changes_only_above_100k_input_tokens():
    pricing = AnthropicProvider.pricing("claude-haiku-5-5")
    assert pricing is not None
    assert pricing.input_rate(100_000) == 0.1
    assert pricing.output_rate(100_000) == 0.5
    assert pricing.cache_read_rate(100_000) == 0.01
    assert pricing.cache_write_rate(100_000) == 0.125
    assert pricing.input_rate(100_001) == 0.5
    assert pricing.output_rate(100_001) == 2.5
    assert pricing.cache_read_rate(100_001) == 0.05
    assert pricing.cache_write_rate(100_001) == 0.625


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-fable-5-1"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_reasoning_only_claude_models_reject_disabled_thinking(model, effort):
    with pytest.raises(ConfigurationError, match="always uses adaptive thinking"):
        AnthropicProvider.validate_request(
            model, reasoning=False, effort=effort, output_limit=32_000
        )


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-haiku-5-5"])
@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_current_fast_claude_models_reject_high_effort_without_thinking(model, effort):
    with pytest.raises(ConfigurationError, match="above high"):
        AnthropicProvider.validate_request(
            model, reasoning=False, effort=effort, output_limit=32_000
        )


@pytest.mark.parametrize(
    "model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]
)
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_current_claude_models_accept_reasoning_and_enforce_output_limits(model, effort):
    AnthropicProvider.validate_request(model, reasoning=True, effort=effort, output_limit=128_000)
    with pytest.raises(ConfigurationError, match="at most 128,000"):
        AnthropicProvider.validate_request(
            model, reasoning=True, effort=effort, output_limit=128_001
        )


def test_sonnet_5_pricing_matches_current_provider_rates():
    assert AnthropicProvider.pricing("claude-sonnet-5") == Pricing(
        input=2.0,
        output=10.0,
        cache_read=0.2,
        cache_write=2.5,
    )


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-haiku-5-5",
    ],
)
def test_anthropic_structured_stream_shape_binds_to_installed_sdk(model):
    native = FakeAnthropicClient(anthropic_response(parsed=Review(issues=[])))
    provider = anthropic_provider(native)

    result = provider.generate(request(f"anthropic:{model}"))

    assert result.parsed == Review(issues=[])
    (sent,) = native.messages.stream_calls
    inspect.signature(Messages.stream).bind(None, **sent)
    assert sent["model"] == model
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "high"}
    assert sent["output_format"] is Review
    assert all(block["cache_control"] == {"type": "ephemeral"} for block in sent["system"])


@pytest.mark.parametrize(
    ("model", "thinking"),
    [
        ("claude-opus-5", "disabled"),
        ("claude-sonnet-5-5", "between_tools"),
        ("claude-haiku-5-5", "disabled"),
    ],
)
@pytest.mark.parametrize("schema", [False, True])
def test_anthropic_without_thinking_stream_and_token_count_bind_to_sdk(model, thinking, schema):
    native = FakeAnthropicClient(anthropic_response(text="# Report", parsed=Review(issues=[])))
    provider = anthropic_provider(native)
    text_request = request(f"anthropic:{model}", schema=schema, reasoning=False)
    provider.validate_request(model, reasoning=False, effort="high", output_limit=12_345)

    assert provider.generate(text_request).text == "# Report"
    assert provider.count_tokens(text_request) == 321

    (stream_call,) = native.messages.stream_calls
    (count_call,) = native.messages.count_calls
    inspect.signature(Messages.stream).bind(None, **stream_call)
    inspect.signature(Messages.count_tokens).bind(None, **count_call)
    assert stream_call["thinking"] == {"type": thinking}
    if schema:
        assert stream_call["output_format"] is Review
        assert count_call["output_format"] is Review
    else:
        assert "output_format" not in stream_call
        assert "output_format" not in count_call


def test_anthropic_truncation_and_native_errors_are_normalized():
    truncated = anthropic_provider(
        FakeAnthropicClient(anthropic_response(stop_reason="max_tokens"))
    ).generate(request("anthropic:claude-opus-5"))
    assert truncated.truncated is True
    assert truncated.completed is False

    response = httpx2.Response(401, request=httpx2.Request("POST", "https://example.test"))
    error = anthropic.AuthenticationError("bad key", response=response, body=None)
    provider = anthropic_provider(FakeAnthropicClient(error=error))
    with pytest.raises(ProviderAuthenticationError):
        provider.generate(request("anthropic:claude-opus-5"))


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_openai_structured_stream_uses_developer_blocks_breakpoints_and_cache_key(model):
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = openai_provider(native)

    result = provider.generate(request(f"openai:{model}"))

    assert result.parsed == Review(issues=[])
    (sent,) = native.responses.stream_calls
    inspect.signature(Responses.stream).bind(None, **sent)
    assert sent["model"] == model
    assert sent["text_format"] is Review
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["max_output_tokens"] == 12_345
    assert sent["prompt_cache_key"] == "math-scout:paper-hash"
    assert sent["prompt_cache_options"] == {"mode": "explicit"}
    developer, user = sent["input"]
    assert developer["role"] == "developer"
    assert user["role"] == "user"
    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"} for block in developer["content"]
    )
    assert all(block["type"] == "input_text" for block in user["content"])


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_openai_count_is_exact_request_shape_and_never_generates(model):
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = openai_provider(native)

    assert provider.count_tokens(request(f"openai:{model}")) == 654

    assert native.responses.stream_calls == []
    (sent,) = native.responses.count_calls
    inspect.signature(InputTokens.count).bind(None, **sent)
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["additionalProperties"] is False
    assert "prompt_cache_key" not in sent  # routing metadata is not a count-endpoint field


@pytest.mark.parametrize("model", ["gpt-5.6-sol", "gpt-6.1-terra", "gpt-6-luna"])
def test_openai_non_reasoning_uses_none_and_has_no_duplicate_retry_request(model):
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = openai_provider(native)
    non_reasoning = request(f"openai:{model}", reasoning=False)
    provider.validate_request(model, reasoning=False, effort="high", output_limit=12_345)

    provider.generate(non_reasoning)

    assert native.responses.stream_calls[0]["reasoning"] == {"effort": "none"}
    assert provider.retry_effort(non_reasoning) is None


def test_openai_known_non_reasoning_model_omits_reasoning_and_rejects_reasoning_task():
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = openai_provider(native)
    non_reasoning = request("openai:gpt-4o", reasoning=False)

    provider.generate(non_reasoning)
    assert "reasoning" not in native.responses.stream_calls[0]
    with pytest.raises(ConfigurationError, match="non-reasoning model"):
        OpenAIProvider.validate_request(
            "gpt-4o", reasoning=True, effort="high", output_limit=10_000
        )


def test_openai_text_truncation_refusal_empty_output_and_usage_are_normalized():
    truncated = openai_provider(
        FakeOpenAIClient(openai_response(status="incomplete", reason="max_output_tokens"))
    ).generate(request("openai:gpt-5.6-sol", schema=False))
    assert truncated.truncated is True

    refusal = dict(
        id="msg_refused",
        type="message",
        role="assistant",
        status="completed",
        content=[{"type": "refusal", "refusal": "cannot comply"}],
    )
    with pytest.raises(ProviderRefusalError, match="cannot comply"):
        openai_provider(FakeOpenAIClient(openai_response(output=[refusal]))).generate(
            request("openai:gpt-5.6-sol", schema=False)
        )

    with pytest.raises(ProviderEmptyOutputError):
        openai_provider(FakeOpenAIClient(openai_response())).generate(
            request("openai:gpt-5.6-sol", schema=False)
        )

    complete = openai_provider(FakeOpenAIClient(openai_response(text="# Report"))).generate(
        request("openai:gpt-5.6-sol", schema=False)
    )
    assert complete.text == "# Report"
    assert complete.usage is not None
    assert complete.usage.cached_input_tokens == 5
    assert complete.usage.cache_write_input_tokens == 6


def test_openai_native_rate_limit_is_normalized():
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://example.test"))
    error = openai.RateLimitError("slow down", response=response, body=None)
    provider = openai_provider(FakeOpenAIClient(error=error))

    with pytest.raises(ProviderRateLimitError):
        provider.generate(request("openai:gpt-5.6-sol"))


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
def test_native_response_metadata_keeps_nested_usage_and_unknown_fields(provider_name):
    if provider_name == "openai":
        response = openai_response(parsed=Review(issues=[]))
        provider = openai_provider(FakeOpenAIClient(response))
        model = "openai:gpt-6.1-sol"
    else:
        response = anthropic_response(parsed=Review(issues=[]))
        provider = anthropic_provider(FakeAnthropicClient(response))
        model = "anthropic:claude-opus-5-5"
    response._request_id = "request_test"

    result = provider.generate(request(model))
    metadata = result.response_metadata
    assert metadata["usage"] == response.usage.model_dump(mode="json")
    assert metadata["future_response_field"] == {"native": True}
    assert metadata["id"] == response.id
    assert metadata["model"] == response.model
    assert metadata["request_id"] == "request_test"
    assert "content" not in metadata
    assert "output" not in metadata
    assert "parsed_output" not in metadata
    if provider_name == "openai":
        assert metadata["usage"]["output_tokens_details"]["reasoning_tokens"] == 17
        assert metadata["usage"]["output_tokens_details"]["future_detail"] == 9
    else:
        assert metadata["usage"]["cache_creation"]["ephemeral_5m_input_tokens"] == 4
        assert metadata["usage"]["future_token_details"] == {"thinking": 12}


@pytest.mark.parametrize("provider_name", ["openai", "anthropic"])
def test_failed_and_truncated_responses_retain_native_metadata(provider_name):
    if provider_name == "openai":
        truncated = openai_response(status="incomplete", reason="max_output_tokens")
        failed = openai_response(status="incomplete", reason="content_filter")
        build, client_type = openai_provider, FakeOpenAIClient
        model = "openai:gpt-6.1-sol"
    else:
        truncated = anthropic_response(stop_reason="max_tokens")
        failed = anthropic_response(stop_reason="refusal")
        build, client_type = anthropic_provider, FakeAnthropicClient
        model = "anthropic:claude-opus-5-5"
    result = build(client_type(truncated)).generate(request(model))
    assert result.truncated
    assert result.response_metadata["usage"] == truncated.usage.model_dump(mode="json")
    with pytest.raises(ProviderRefusalError) as caught:
        build(client_type(failed)).generate(request(model))
    assert caught.value.response_metadata["usage"] == failed.usage.model_dump(mode="json")
