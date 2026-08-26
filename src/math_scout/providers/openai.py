"""Native OpenAI Responses API adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import openai as openai_sdk
from openai.lib._pydantic import to_strict_json_schema

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
    next_lower_effort,
)

_ALL_EFFORTS = frozenset(("none", *EFFORT_LEVELS))


class OpenAIProvider(Provider):
    name = "openai"
    display_name = "OpenAI"
    credentials_help = "No usable OpenAI credentials. Export OPENAI_API_KEY and try again."
    models_url = "https://developers.openai.com/api/docs/models"

    # efforts (None means non-reasoning), max output, explicit cache breakpoints
    _CAPABILITIES: dict[str, tuple[frozenset[str] | None, int, bool]] = {
        "gpt-5.6": (_ALL_EFFORTS, 128_000, True),
        "gpt-5.6-sol": (_ALL_EFFORTS, 128_000, True),
        "gpt-5.6-terra": (_ALL_EFFORTS, 128_000, True),
        "gpt-5.6-luna": (_ALL_EFFORTS, 128_000, True),
        "gpt-4o": (None, 16_384, False),
        "gpt-4o-2024-08-06": (None, 16_384, False),
        "gpt-4o-2024-11-20": (None, 16_384, False),
        "gpt-4.1": (None, 32_768, False),
    }

    _PRICING = {
        "gpt-5.6": Pricing(input=5.0, output=30.0, cache_read=0.5, cache_write=6.25),
        "gpt-5.6-sol": Pricing(input=5.0, output=30.0, cache_read=0.5, cache_write=6.25),
        "gpt-5.6-terra": Pricing(input=2.5, output=15.0, cache_read=0.25, cache_write=3.125),
        "gpt-5.6-luna": Pricing(input=0.2, output=1.2, cache_read=0.02, cache_write=0.25),
    }

    def __init__(self, client: openai_sdk.OpenAI | None = None):
        self._client_instance = client

    @property
    def client(self) -> openai_sdk.OpenAI:
        if self._client_instance is None:
            try:
                self._client_instance = openai_sdk.OpenAI()
            except Exception as exc:
                raise self._normalize(exc) from exc
        return self._client_instance

    @classmethod
    def validate_request(
        cls, model: str, *, reasoning: bool, effort: str, output_limit: int
    ) -> None:
        support = cls._CAPABILITIES.get(model)
        if support is None:
            return
        efforts, max_output, _ = support
        if reasoning and efforts is None:
            raise ConfigurationError(
                f"openai:{model} is a non-reasoning model and cannot run a reviewer "
                "whose task requires reasoning. Choose a reasoning model for that tier."
            )
        effective_effort = effort if reasoning else "none"
        if efforts is not None and effective_effort not in efforts:
            allowed = ", ".join(level for level in ("none", *EFFORT_LEVELS) if level in efforts)
            raise ConfigurationError(
                f"openai:{model} does not support reasoning effort {effective_effort}. "
                f"It accepts: {allowed}."
            )
        if output_limit > max_output:
            raise ConfigurationError(
                f"openai:{model} accepts at most {max_output:,} output tokens; "
                f"got {output_limit:,}."
            )

    @classmethod
    def _support(cls, model: str) -> tuple[frozenset[str] | None, int, bool] | None:
        return cls._CAPABILITIES.get(model)

    def _normalize(self, exc: Exception) -> Exception:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, openai_sdk.AuthenticationError):
            return ProviderAuthenticationError(str(exc))
        if isinstance(exc, openai_sdk.PermissionDeniedError):
            return ProviderPermissionError(str(exc))
        if isinstance(exc, openai_sdk.NotFoundError):
            return ProviderModelError(str(exc))
        if isinstance(exc, openai_sdk.RateLimitError):
            return ProviderRateLimitError(str(exc))
        if isinstance(exc, openai_sdk.APITimeoutError):
            return ProviderTimeoutError(str(exc))
        if isinstance(exc, openai_sdk.APIConnectionError):
            return ProviderConnectionError(str(exc))
        if isinstance(exc, openai_sdk.APIStatusError):
            if exc.status_code >= 500:
                return ProviderServerError(str(exc), status_code=exc.status_code)
            return ProviderRequestError(str(exc))
        if isinstance(exc, openai_sdk.OpenAIError):
            # Missing OPENAI_API_KEY is raised while constructing the client, before an
            # HTTP status exists. Other request errors have already matched above.
            if "api_key" in str(exc).lower():
                return ProviderAuthenticationError(str(exc))
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

    @classmethod
    def _supports_explicit_cache(cls, model: str) -> bool:
        support = cls._support(model)
        return bool(support and support[2])

    @classmethod
    def _input(cls, request: GenerationRequest) -> list[dict[str, Any]]:
        system_content = []
        explicit = cls._supports_explicit_cache(request.model.model)
        for block in request.blocks:
            if block.role != "system":
                continue
            item: dict[str, Any] = {"type": "input_text", "text": block.text}
            if block.cacheable and explicit:
                item["prompt_cache_breakpoint"] = {"mode": "explicit"}
            system_content.append(item)
        user_content = [
            {"type": "input_text", "text": block.text}
            for block in request.blocks
            if block.role == "user"
        ]
        messages: list[dict[str, Any]] = []
        if system_content:
            messages.append({"type": "message", "role": "developer", "content": system_content})
        if user_content:
            messages.append({"type": "message", "role": "user", "content": user_content})
        return messages

    @classmethod
    def _reasoning(cls, request: GenerationRequest) -> dict[str, str] | None:
        support = cls._support(request.model.model)
        if request.reasoning:
            return {"effort": request.effort}
        if support is not None and support[0] is None:
            return None
        return {"effort": "none"}

    @staticmethod
    def _text_format(request: GenerationRequest) -> dict[str, Any] | None:
        if request.output_schema is None:
            return None
        return {
            "format": {
                "type": "json_schema",
                "name": request.output_schema.__name__,
                "strict": True,
                "schema": to_strict_json_schema(request.output_schema),
            }
        }

    def _common_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model.model,
            "input": self._input(request),
        }
        reasoning = self._reasoning(request)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        if request.prompt_cache_key is not None:
            kwargs["prompt_cache_key"] = request.prompt_cache_key
        if self._supports_explicit_cache(request.model.model):
            kwargs["prompt_cache_options"] = {"mode": "explicit"}
        return kwargs

    def _generation_kwargs(self, request: GenerationRequest) -> dict[str, Any]:
        kwargs = self._common_kwargs(request)
        kwargs["max_output_tokens"] = request.output_limit
        if request.output_schema is not None:
            kwargs["text_format"] = request.output_schema
        return kwargs

    def preflight(self, model: str) -> None:
        self._invoke(
            lambda: self.client.responses.input_tokens.count(
                model=model,
                input=[{"role": "user", "content": "ping"}],
            )
        )

    @staticmethod
    def _refusal(response: Any) -> str | None:
        for output in getattr(response, "output", []) or []:
            for content in getattr(output, "content", []) or []:
                if getattr(content, "type", None) == "refusal":
                    return getattr(content, "refusal", None) or "OpenAI refused the request."
        return None

    def generate(self, request: GenerationRequest) -> GenerationResult:
        def call():
            with self.client.responses.stream(**self._generation_kwargs(request)) as stream:
                return stream.get_final_response()

        response = self._invoke(call)
        refusal = self._refusal(response)
        if refusal is not None:
            raise ProviderRefusalError(refusal)

        status = getattr(response, "status", None)
        details = getattr(response, "incomplete_details", None)
        truncated = status == "incomplete" and getattr(details, "reason", None) == (
            "max_output_tokens"
        )
        if status == "incomplete" and not truncated:
            reason = getattr(details, "reason", None) or "unknown reason"
            if reason == "content_filter":
                raise ProviderRefusalError(
                    "OpenAI stopped the response because of content filtering."
                )
            raise ProviderRequestError(f"OpenAI response was incomplete: {reason}.")
        if status == "failed":
            error = getattr(response, "error", None)
            raise ProviderRequestError(f"OpenAI response failed: {error or 'unknown error'}.")

        parsed = getattr(response, "output_parsed", None)
        text = getattr(response, "output_text", None) or None
        if not truncated and request.output_schema is not None and parsed is None:
            raise ProviderEmptyOutputError("OpenAI returned no parsed structured output.")
        if not truncated and request.output_schema is None and text is None:
            raise ProviderEmptyOutputError("OpenAI returned no text output.")

        usage = getattr(response, "usage", None)
        normalized_usage = None
        if usage is not None:
            details = getattr(usage, "input_tokens_details", None)
            normalized_usage = GenerationUsage(
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cached_input_tokens=getattr(details, "cached_tokens", None),
                cache_write_input_tokens=getattr(details, "cache_write_tokens", None),
            )
        return GenerationResult(
            parsed=parsed,
            text=text,
            completed=status == "completed",
            truncated=truncated,
            usage=normalized_usage,
        )

    def count_tokens(self, request: GenerationRequest) -> int:
        kwargs: dict[str, Any] = {
            "model": request.model.model,
            "input": self._input(request),
        }
        reasoning = self._reasoning(request)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        text = self._text_format(request)
        if text is not None:
            kwargs["text"] = text
        response = self._invoke(lambda: self.client.responses.input_tokens.count(**kwargs))
        return response.input_tokens

    def retry_effort(self, request: GenerationRequest) -> str | None:
        if not request.reasoning:
            # Known non-reasoning models omit the field; reasoning models receive
            # effort=none. Either way lowering the CLI effort would send the same call.
            return None
        return next_lower_effort(request.effort)

    @classmethod
    def pricing(cls, model: str) -> Pricing | None:
        return cls._PRICING.get(model)
