"""Contract tests for the native Anthropic and OpenAI adapters."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import openai
import pytest
from openai.resources.responses.input_tokens import InputTokens
from openai.resources.responses.responses import Responses

from llm_reviewer.providers import (
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
from llm_reviewer.reviewer import Review


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
        prompt_cache_key="paper-scout:paper-hash",
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


def anthropic_response(*, parsed=None, text=None, stop_reason="end_turn"):
    content = [] if text is None else [SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(
        parsed_output=parsed,
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=3,
            cache_creation_input_tokens=4,
        ),
    )


def openai_response(*, parsed=None, text=None, status="completed", reason=None, output=None):
    return SimpleNamespace(
        output_parsed=parsed,
        output_text=text,
        status=status,
        incomplete_details=None if reason is None else SimpleNamespace(reason=reason),
        output=[] if output is None else output,
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=22,
            input_tokens_details=SimpleNamespace(cached_tokens=5, cache_write_tokens=6),
        ),
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


def test_luna_pricing_matches_current_provider_rates():
    assert OpenAIProvider.pricing("gpt-5.6-luna") == Pricing(
        input=0.2,
        output=1.2,
        cache_read=0.02,
        cache_write=0.25,
    )


def test_anthropic_structured_stream_shape_binds_to_installed_sdk():
    native = FakeAnthropicClient(anthropic_response(parsed=Review(issues=[])))
    provider = AnthropicProvider(native)

    result = provider.generate(request("anthropic:claude-opus-5"))

    assert result.parsed == Review(issues=[])
    (sent,) = native.messages.stream_calls
    inspect.signature(anthropic.resources.messages.Messages.stream).bind(None, **sent)
    assert sent["model"] == "claude-opus-5"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"] == {"effort": "high"}
    assert sent["output_format"] is Review
    assert all(block["cache_control"] == {"type": "ephemeral"} for block in sent["system"])


def test_anthropic_markdown_stream_and_token_count_bind_to_installed_sdk():
    native = FakeAnthropicClient(anthropic_response(text="# Report"))
    provider = AnthropicProvider(native)
    text_request = request("anthropic:claude-opus-5", schema=False, reasoning=False)

    assert provider.generate(text_request).text == "# Report"
    assert provider.count_tokens(text_request) == 321

    (stream_call,) = native.messages.stream_calls
    (count_call,) = native.messages.count_calls
    inspect.signature(anthropic.resources.messages.Messages.stream).bind(None, **stream_call)
    inspect.signature(anthropic.resources.messages.Messages.count_tokens).bind(None, **count_call)
    assert stream_call["thinking"] == {"type": "disabled"}
    assert "output_format" not in stream_call


def test_anthropic_truncation_and_native_errors_are_normalized():
    truncated = AnthropicProvider(
        FakeAnthropicClient(anthropic_response(stop_reason="max_tokens"))
    ).generate(request("anthropic:claude-opus-5"))
    assert truncated.truncated is True
    assert truncated.completed is False

    response = httpx.Response(401, request=httpx.Request("POST", "https://example.test"))
    error = anthropic.AuthenticationError("bad key", response=response, body=None)
    provider = AnthropicProvider(FakeAnthropicClient(error=error))
    with pytest.raises(ProviderAuthenticationError):
        provider.generate(request("anthropic:claude-opus-5"))


def test_openai_structured_stream_uses_developer_blocks_breakpoints_and_cache_key():
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = OpenAIProvider(native)

    result = provider.generate(request("openai:gpt-5.6-sol"))

    assert result.parsed == Review(issues=[])
    (sent,) = native.responses.stream_calls
    inspect.signature(Responses.stream).bind(None, **sent)
    assert sent["model"] == "gpt-5.6-sol"
    assert sent["text_format"] is Review
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["max_output_tokens"] == 12_345
    assert sent["prompt_cache_key"] == "paper-scout:paper-hash"
    assert sent["prompt_cache_options"] == {"mode": "explicit"}
    developer, user = sent["input"]
    assert developer["role"] == "developer"
    assert user["role"] == "user"
    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"} for block in developer["content"]
    )
    assert all(block["type"] == "input_text" for block in user["content"])


def test_openai_count_is_exact_request_shape_and_never_generates():
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = OpenAIProvider(native)

    assert provider.count_tokens(request("openai:gpt-5.6-sol")) == 654

    assert native.responses.stream_calls == []
    (sent,) = native.responses.count_calls
    inspect.signature(InputTokens.count).bind(None, **sent)
    assert sent["reasoning"] == {"effort": "high"}
    assert sent["text"]["format"]["type"] == "json_schema"
    assert sent["text"]["format"]["strict"] is True
    assert sent["text"]["format"]["schema"]["additionalProperties"] is False
    assert "prompt_cache_key" not in sent  # routing metadata is not a count-endpoint field


def test_openai_non_reasoning_uses_none_and_has_no_duplicate_retry_request():
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = OpenAIProvider(native)
    non_reasoning = request("openai:gpt-5.6-sol", reasoning=False)

    provider.generate(non_reasoning)

    assert native.responses.stream_calls[0]["reasoning"] == {"effort": "none"}
    assert provider.retry_effort(non_reasoning) is None


def test_openai_known_non_reasoning_model_omits_reasoning_and_rejects_reasoning_task():
    native = FakeOpenAIClient(openai_response(parsed=Review(issues=[])))
    provider = OpenAIProvider(native)
    non_reasoning = request("openai:gpt-4o", reasoning=False)

    provider.generate(non_reasoning)
    assert "reasoning" not in native.responses.stream_calls[0]
    with pytest.raises(ConfigurationError, match="non-reasoning model"):
        OpenAIProvider.validate_request(
            "gpt-4o", reasoning=True, effort="high", output_limit=10_000
        )


def test_openai_text_truncation_refusal_empty_output_and_usage_are_normalized():
    truncated = OpenAIProvider(
        FakeOpenAIClient(openai_response(status="incomplete", reason="max_output_tokens"))
    ).generate(request("openai:gpt-5.6-sol", schema=False))
    assert truncated.truncated is True

    refusal = SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="cannot comply")])
    with pytest.raises(ProviderRefusalError, match="cannot comply"):
        OpenAIProvider(FakeOpenAIClient(openai_response(output=[refusal]))).generate(
            request("openai:gpt-5.6-sol", schema=False)
        )

    with pytest.raises(ProviderEmptyOutputError):
        OpenAIProvider(FakeOpenAIClient(openai_response())).generate(
            request("openai:gpt-5.6-sol", schema=False)
        )

    complete = OpenAIProvider(FakeOpenAIClient(openai_response(text="# Report"))).generate(
        request("openai:gpt-5.6-sol", schema=False)
    )
    assert complete.text == "# Report"
    assert complete.usage.cached_input_tokens == 5
    assert complete.usage.cache_write_input_tokens == 6


def test_openai_native_rate_limit_is_normalized():
    response = httpx2.Response(429, request=httpx2.Request("POST", "https://example.test"))
    error = openai.RateLimitError("slow down", response=response, body=None)
    provider = OpenAIProvider(FakeOpenAIClient(error=error))

    with pytest.raises(ProviderRateLimitError):
        provider.generate(request("openai:gpt-5.6-sol"))
