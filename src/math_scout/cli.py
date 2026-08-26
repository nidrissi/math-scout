"""Command-line entry point for math-scout."""

from __future__ import annotations

import argparse
import sys

from rich import print

from . import __version__
from .providers import ProviderRegistry
from .reviewer import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_FAST,
    DEFAULT_MODEL_STRONG,
    EFFORT_LEVELS,
    MAX_OUTPUT_TOKENS,
    ConfigurationError,
    check_access,
    load_prompts,
    run_dry_run,
    run_pipeline,
    validate_settings,
)

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG = 2

MODEL_PRESETS: dict[str, tuple[str, str]] = {
    "opus-sonnet": ("anthropic:claude-opus-5", "anthropic:claude-sonnet-5"),
    "sol-luna": ("openai:gpt-5.6-sol", "openai:gpt-5.6-luna"),
}


def resolve_model_selection(
    *, preset: str | None, strong_model: str | None, fast_model: str | None
) -> tuple[str, str]:
    """Resolve a preset and per-tier overrides to the concrete model IDs used by a run."""
    preset_strong, preset_fast = (
        MODEL_PRESETS[preset] if preset is not None else (DEFAULT_MODEL_STRONG, DEFAULT_MODEL_FAST)
    )
    return strong_model or preset_strong, fast_model or preset_fast


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="math-scout",
        description=(
            "Run multiple LLM-based reviewers on a LaTeX document, aggregating their "
            "feedback into a final report."
        ),
    )
    parser.add_argument("input", help="Path to the input .tex file to review.")
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: <input_dir>/review/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count tokens and estimate input cost without sending generation requests.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt. Required when stdin is not a terminal.",
    )
    parser.add_argument(
        "--preset",
        choices=tuple(MODEL_PRESETS),
        default=None,
        metavar="NAME",
        help=(
            "Set both model tiers to a common pair: opus-sonnet or sol-luna. "
            "Explicit model flags override the corresponding preset tier."
        ),
    )
    parser.add_argument(
        "--strong-model",
        default=None,
        metavar="PROVIDER:MODEL",
        help=(
            "Model for FormalVerifier, AdversarialSkeptic, ClaimAuditor, and the final "
            "referee. Explicit models must use provider:model "
            f"(default without a preset: {DEFAULT_MODEL_STRONG})"
        ),
    )
    parser.add_argument(
        "--fast-model",
        default=None,
        metavar="PROVIDER:MODEL",
        help=(
            "Model for NotationAuditor and ExpositionReferee. Explicit models must use "
            f"provider:model (default without a preset: {DEFAULT_MODEL_FAST})"
        ),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        metavar="N",
        help=(
            "Output token limit per call, covering thinking and response text together "
            f"(default: {DEFAULT_MAX_TOKENS}, maximum {MAX_OUTPUT_TOKENS})"
        ),
    )
    parser.add_argument(
        "--effort",
        default=DEFAULT_EFFORT,
        choices=EFFORT_LEVELS,
        help=(
            "How hard the models work. The main cost and latency lever: lower spends "
            f"fewer tokens, higher reasons more (default: {DEFAULT_EFFORT})"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        strong_model, fast_model = resolve_model_selection(
            preset=args.preset,
            strong_model=args.strong_model,
            fast_model=args.fast_model,
        )
        prompts = load_prompts(strong_model=strong_model, fast_model=fast_model)
        validate_settings(prompts, max_tokens=args.max_tokens, effort=args.effort)
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG

    models = [r.model for r in prompts.reviewers.values()]
    models.append(prompts.strong_model)
    try:
        providers = ProviderRegistry.from_models(models)
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG

    try:
        # Free pre-flight: proves the credentials work and the model IDs are real
        # before the run writes anything or asks the user to approve spending.
        check_access(providers, models)
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG

    try:
        if args.dry_run:
            run_dry_run(
                providers,
                args.input,
                prompts=prompts,
                max_tokens=args.max_tokens,
                effort=args.effort,
            )
            return EXIT_OK

        result = run_pipeline(
            providers,
            args.input,
            args.output,
            prompts=prompts,
            max_tokens=args.max_tokens,
            effort=args.effort,
            assume_yes=args.yes,
        )
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG
    except KeyboardInterrupt:
        print("\n[yellow]Interrupted. Re-run the same command to resume.[/yellow]")
        return EXIT_INCOMPLETE

    if result.aborted:
        return EXIT_OK
    if result.fatal_error is not None:
        return EXIT_CONFIG
    if result.failures:
        print(
            f"[yellow]Finished with {len(result.failures)} failed reviewer call(s). "
            "Re-run the same command to retry only the missing ones.[/yellow]"
        )
        return EXIT_INCOMPLETE
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
