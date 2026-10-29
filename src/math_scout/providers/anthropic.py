"""Native Anthropic Messages API adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic
from pydantic import BaseModel, ValidationError

from .base import (
    EFFORT_LEVELS,
    ConfigurationError,
    GenerationRequest,
    GenerationResult,
    GenerationUsage,
    Pricing,
    Provider,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderEmptyOutputError,
    ProviderError,
    ProviderModelError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderRefusalError,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    native_response_metadata,
)

_ALL_EFFORTS = frozenset(EFFORT_LEVELS)


class AnthropicProvider(Provider):
    name = "anthropic"
    display_name = "Anthropic"
    credentials_help = (
        "No usable Anthropic credentials. Either export ANTHROPIC_API_KEY, or sign in "
        "with `ant auth login` and the SDK will pick up the stored profile."
    )
    models_url = "https://platform.claude.com/docs/en/models/overview"

    _REASONING_ONLY = frozenset({"claude-fable-5-1", "claude-opus-5-5"})

    # efforts, highest effort accepted without up-front thinking, max output
    # https://platform.claude.com/docs/en/build-with-claude/effort
    _CAPABILITIES: dict[str, tuple[frozenset[str], str | None, int]] = {
        "claude-fable-5-1": (_ALL_EFFORTS, None, 128_000),
        "claude-opus-5-5": (_ALL_EFFORTS, None, 128_000),
        "claude-sonnet-5-5": (_ALL_EFFORTS, "high", 128_000),
        "claude-haiku-5-5": (_ALL_EFFORTS, "high", 128_000),
        "claude-opus-5": (_ALL_EFFORTS, "high", 128_000),
        "claude-opus-4-8": (_ALL_EFFORTS, None, 64_000),
        "claude-opus-4-7": (_ALL_EFFORTS, None, 64_000),
        "claude-opus-4-6": (_ALL_EFFORTS - {"xhigh"}, None, 64_000),
        "claude-sonnet-5": (_ALL_EFFORTS, None, 128_000),
        "claude-sonnet-4-6": (_ALL_EFFORTS - {"xhigh"}, None, 64_000),
        "claude-haiku-4-5": (frozenset(), None, 64_000),
    }

    _PRICING = {
        "claude-fable-5-1": Pricing(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5),
        "claude-opus-5-5": Pricing(input=4.0, output=20.0, cache_read=0.2, cache_write=5.0),
        "claude-sonnet-5-5": Pricing(input=2.0, output=10.0, cache_read=0.1, cache_write=2.5),
        "claude-haiku-5-5": Pricing(
            input=0.1,
            output=0.5,
            cache_read=0.01,
            cache_write=0.125,
            long_context_threshold=100_000,
            long_context_input_multiplier=5.0,
            long_context_output_multiplier=5.0,
        ),
        "claude-opus-5": Pricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25),
        "claude-opus-4-8": Pricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25),
        "claude-opus-4-7": Pricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25),
        "claude-opus-4-6": Pricing(input=5.0, output=25.0, cache_read=0.5, cache_write=6.25),
        "claude-sonnet-5": Pricing(input=2.0, output=10.0, cache_read=0.2, cache_write=2.5),
        "claude-sonnet-4-6": Pricing(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75),
    }

    def __init__(self, client: anthropic.Anthropic | None = None):
        self._client_instance: anthropic.Anthropic | None = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client_instance is None:
            try:
                self._client_instance = anthropic.Anthropic()
            except TypeError as exc:
                # The Anthropic SDK uses a bare TypeError when neither an environment
                # key nor an `ant auth login` profile can be resolved.
                raise ProviderAuthenticationError(str(exc)) from exc
            except Exception as exc:  # credential resolution can fail during construction
                raise self._normalize(exc) from exc
        return self._client_instance

    @classmethod
    def validate_request(
        cls, model: str, *, reasoning: bool, effort: str, output_limit: int
    ) -> None:
        support = cls._CAPABILITIES.get(model)
        if support is None:
            return
        if not reasoning and model in cls._REASONING_ONLY:
            raise ConfigurationError(
                f"{model} always uses adaptive thinking and cannot run a reviewer "
                "without thinking. Choose another model for the fast tier."
            )
        efforts, disabled_max, max_output = support
        if not efforts:
            raise ConfigurationError(
                f"{model} cannot be used here: it rejects the effort and thinking "
                "settings this pipeline sends on every call. Choose another model for it."
            )
        if effort not in efforts:
            allowed = ", ".join(level for level in EFFORT_LEVELS if level in efforts)
            raise ConfigurationError(
                f"{model} does not support --effort {effort}. It accepts: {allowed}."
            )
        if (
            not reasoning
            and disabled_max is not None
            and EFFORT_LEVELS.index(effort) > EFFORT_LEVELS.index(disabled_max)
        ):
            raise ConfigurationError(
                f"A reviewer runs without thinking up front on {model}, which rejects "
                f"--effort above {disabled_max}. Use --effort {disabled_max} or lower, "
                "or give it a different model."
            )
        if output_limit > max_output:
            raise ConfigurationError(
                f"{model} accepts at most {max_output:,} output tokens; got {output_limit:,}."
            )

    def _normalize(self, exc: Exception) -> Exception:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderAuthenticationError(str(exc))
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ProviderPermissionError(str(exc))
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderModelError(str(exc))
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderRateLimitError(str(exc))
        if isinstance(exc, anthropic.APITimeoutError):
            return ProviderTimeoutError(str(exc))
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderConnectionError(str(exc))
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code >= 500:
                return ProviderServerError(str(exc), status_code=exc.status_code)
            return ProviderRequestError(str(exc))
        return exc

    def _invoke(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            normalized = self._normalize(exc)
            if normalized is exc:
                raise
            raise normalized from exc

    @staticmethod
    def _system(request: GenerationRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "text",
                "text": block.text,
                **({"cache_control": {"type": "ephemeral"}} if block.cacheable else {}),
            }
            for block in request.blocks
            if block.role == "system"
        ]

    @staticmethod
    def _messages(request: GenerationRequest) -> list[dict[str, Any]]:
        content = [
            {"type": "text", "text": block.text} for block in request.blocks if block.role == "user"
        ]
        return [{"role": "user", "content": content}]

    @staticmethod
    def _output_format(schema: type[BaseModel]) -> dict[str, Any]:
        # Sent as a raw format, not as `output_format=`: the SDK would then validate the
        # JSON when the text block closes, before the stop reason arrives, so a reply cut
        # off by max_tokens would raise instead of being reported as truncated.
        return {"type": "json_schema", "schema": anthropic.transform_schema(schema)}

    def _generation_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        thinking = "adaptive" if request.reasoning else "disabled"
        if not request.reasoning and request.model.model == "claude-sonnet-5-5":
            # Sonnet 5.5 rejects disabled; without tools this mode returns text only.
            thinking = "between_tools"
        output_config: dict[str, Any] = {"effort": request.effort}
        if request.output_schema is not None:
            output_config["format"] = self._output_format(request.output_schema)
        return {
            "model": request.model.model,
            "max_tokens": request.output_limit,
            "system": self._system(request),
            "messages": self._messages(request),
            "thinking": {"type": thinking},
            "output_config": output_config,
        }

    def preflight(self, model: str) -> None:
        self._invoke(
            lambda: self.client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
            )
        )

    def generate(self, request: GenerationRequest) -> GenerationResult:
        def call():
            with self.client.messages.stream(**self._generation_kwargs(request)) as stream:
                # The SDK copies only the usage fields it knows from message_delta into
                # the final message, so collect the rest here. Delta counts are totals.
                delta_usage: dict[str, Any] = {}
                for event in stream:
                    if event.type == "message_delta":
                        delta_usage.update(
                            (name, value)
                            for name, value in event.usage.to_dict(mode="json").items()
                            if value is not None
                        )
                return stream.get_final_message(), delta_usage, stream.request_id

        response, delta_usage, request_id = self._invoke(call)
        metadata = native_response_metadata(response, request_id=request_id)
        if delta_usage:
            metadata["usage"] = {**metadata.get("usage", {}), **delta_usage}
        stop_reason = getattr(response, "stop_reason", None)
        if stop_reason == "refusal":
            raise ProviderRefusalError("Anthropic refused the request.", response_metadata=metadata)
        truncated = stop_reason == "max_tokens"
        texts = [
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        ]
        text = "".join(texts) or None
        parsed = None
        if not truncated and request.output_schema is not None and text is not None:
            try:
                parsed = request.output_schema.model_validate_json(text)
            except ValidationError as exc:
                raise ProviderEmptyOutputError(
                    "Anthropic returned structured output that does not match the schema.",
                    response_metadata=metadata,
                ) from exc
        if not truncated and request.output_schema is not None and parsed is None:
            raise ProviderEmptyOutputError(
                "Anthropic returned no parsed structured output.", response_metadata=metadata
            )
        if not truncated and request.output_schema is None and text is None:
            raise ProviderEmptyOutputError(
                "Anthropic returned no text output.", response_metadata=metadata
            )
        usage = getattr(response, "usage", None)
        normalized_usage = None
        if usage is not None:
            normalized_usage = GenerationUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cached_input_tokens=getattr(usage, "cache_read_input_tokens", None),
                cache_write_input_tokens=getattr(usage, "cache_creation_input_tokens", None),
            )
        return GenerationResult(
            parsed=parsed,
            text=text,
            completed=not truncated,
            truncated=truncated,
            usage=normalized_usage,
            response_metadata=metadata,
        )

    def count_tokens(self, request: GenerationRequest) -> int:
        kwargs: dict[str, Any] = {
            "model": request.model.model,
            "system": self._system(request),
            "messages": self._messages(request),
        }
        if request.output_schema is not None:
            kwargs["output_config"] = {"format": self._output_format(request.output_schema)}
        response = self._invoke(lambda: self.client.messages.count_tokens(**kwargs))
        return response.input_tokens

    @classmethod
    def pricing(cls, model: str) -> Pricing | None:
        return cls._PRICING.get(model)
