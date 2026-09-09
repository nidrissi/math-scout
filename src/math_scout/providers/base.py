"""Provider-neutral request, result, capability, and error contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


class ConfigurationError(Exception):
    """A problem with the input, environment, or invocation."""


@dataclass(frozen=True, order=True)
class ModelRef:
    """A model identifier together with the native API provider that serves it."""

    provider: str
    model: str

    @classmethod
    def parse(cls, value: str | ModelRef) -> ModelRef:
        if isinstance(value, cls):
            return value
        provider, separator, model = value.partition(":")
        if not separator:
            raise ConfigurationError(
                f"Invalid model {value!r}; expected PROVIDER:MODEL. "
                "Use --preset opus-sonnet, sol-luna, or astra-sol-luna for a known "
                "model combination."
            )
        provider = provider.strip().lower()
        model = model.strip()
        if not provider or not model:
            raise ConfigurationError(f"Invalid model {value!r}; expected PROVIDER:MODEL.")
        return cls(provider=provider, model=model)

    @property
    def qualified(self) -> str:
        return f"{self.provider}:{self.model}"

    def __str__(self) -> str:
        return self.qualified


@dataclass(frozen=True)
class PromptBlock:
    """One ordered text block in a generation request."""

    role: Literal["system", "user"]
    text: str
    cacheable: bool = False


@dataclass(frozen=True)
class GenerationRequest:
    """Everything a provider needs to perform one text generation."""

    model: ModelRef
    blocks: tuple[PromptBlock, ...]
    reasoning: bool
    effort: str
    output_limit: int
    output_schema: type[BaseModel] | None = None
    prompt_cache_key: str | None = None


@dataclass(frozen=True)
class GenerationUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    """A normalized completed or truncated provider response."""

    parsed: BaseModel | None = None
    text: str | None = None
    completed: bool = True
    truncated: bool = False
    usage: GenerationUsage | None = None


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens. Missing metadata never prevents a request."""

    input: float
    output: float
    cache_read: float | None = None
    cache_write: float | None = None
    long_context_threshold: int | None = None
    long_context_input_multiplier: float = 1.0
    long_context_output_multiplier: float = 1.0

    def uses_long_context_pricing(self, input_tokens: int) -> bool:
        return (
            self.long_context_threshold is not None and input_tokens > self.long_context_threshold
        )

    def input_rate(self, input_tokens: int) -> float:
        multiplier = (
            self.long_context_input_multiplier
            if self.uses_long_context_pricing(input_tokens)
            else 1.0
        )
        return self.input * multiplier

    def output_rate(self, input_tokens: int) -> float:
        multiplier = (
            self.long_context_output_multiplier
            if self.uses_long_context_pricing(input_tokens)
            else 1.0
        )
        return self.output * multiplier

    def cache_read_rate(self, input_tokens: int) -> float | None:
        if self.cache_read is None:
            return None
        multiplier = (
            self.long_context_input_multiplier
            if self.uses_long_context_pricing(input_tokens)
            else 1.0
        )
        return self.cache_read * multiplier

    def cache_write_rate(self, input_tokens: int) -> float | None:
        if self.cache_write is None:
            return None
        multiplier = (
            self.long_context_input_multiplier
            if self.uses_long_context_pricing(input_tokens)
            else 1.0
        )
        return self.cache_write * multiplier


class ProviderError(Exception):
    """Base class for errors normalized across native provider SDKs."""


class ProviderAuthenticationError(ProviderError):
    pass


class ProviderPermissionError(ProviderError):
    pass


class ProviderModelError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderConnectionError(ProviderError):
    pass


class ProviderTimeoutError(ProviderError):
    pass


class ProviderRefusalError(ProviderError):
    pass


class ProviderServerError(ProviderError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ProviderRequestError(ProviderError):
    pass


class ProviderEmptyOutputError(ProviderError):
    pass


FATAL_PROVIDER_ERRORS = (
    ProviderAuthenticationError,
    ProviderPermissionError,
    ProviderModelError,
)

TRANSIENT_PROVIDER_ERRORS = (
    ProviderRateLimitError,
    ProviderConnectionError,
    ProviderTimeoutError,
    ProviderServerError,
)


def next_lower_effort(effort: str) -> str | None:
    """Return the next application effort level down, if there is one."""
    index = EFFORT_LEVELS.index(effort)
    return EFFORT_LEVELS[index - 1] if index > 0 else None


class Provider(ABC):
    """Native-provider adapter used by the provider-neutral orchestration."""

    name: str
    display_name: str
    credentials_help: str
    models_url: str

    @classmethod
    @abstractmethod
    def validate_request(
        cls, model: str, *, reasoning: bool, effort: str, output_limit: int
    ) -> None:
        """Reject a known-incompatible request before any paid generation."""

    @abstractmethod
    def preflight(self, model: str) -> None:
        """Verify credentials and model access without generating output."""

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult:
        """Stream one structured or text response and return its final result."""

    @abstractmethod
    def count_tokens(self, request: GenerationRequest) -> int:
        """Return the provider's exact input-token count for *request*."""

    def retry_effort(self, request: GenerationRequest) -> str | None:
        """Map a truncation retry to a genuinely different provider request."""
        return next_lower_effort(request.effort)

    @classmethod
    @abstractmethod
    def pricing(cls, model: str) -> Pricing | None:
        """Return trustworthy current pricing metadata when it is known."""
