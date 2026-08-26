"""Provider adapter registry and lazy native-client factory."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .anthropic import AnthropicProvider
from .base import ConfigurationError, ModelRef, Provider
from .openai import OpenAIProvider

PROVIDER_TYPES: dict[str, type[Provider]] = {
    AnthropicProvider.name: AnthropicProvider,
    OpenAIProvider.name: OpenAIProvider,
}


def provider_type(name: str) -> type[Provider]:
    try:
        return PROVIDER_TYPES[name]
    except KeyError as exc:
        supported = ", ".join(sorted(PROVIDER_TYPES))
        raise ConfigurationError(
            f"Unknown model provider {name!r}. Supported providers: {supported}."
        ) from exc


class ProviderRegistry:
    """One shared native client adapter for every referenced provider."""

    def __init__(self, providers: Mapping[str, Provider]):
        self._providers = dict(providers)

    @classmethod
    def from_models(cls, models: Iterable[ModelRef]) -> ProviderRegistry:
        names = sorted({model.provider for model in models})
        return cls({name: provider_type(name)() for name in names})

    def for_model(self, model: ModelRef) -> Provider:
        try:
            return self._providers[model.provider]
        except KeyError as exc:
            raise ConfigurationError(
                f"No {model.provider!r} provider was configured for {model.qualified}."
            ) from exc

    def __iter__(self):
        return iter(self._providers.values())
