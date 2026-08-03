"""Command-line entry point for llm-reviewer."""

from __future__ import annotations

import argparse
import sys

import anthropic
from rich import print

from . import __version__
from .reviewer import (
    CREDENTIALS_HELP,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_FAST,
    DEFAULT_MODEL_STRONG,
    ConfigurationError,
    check_access,
    load_prompts,
    run_dry_run,
    run_pipeline,
)

EXIT_OK = 0
EXIT_INCOMPLETE = 1
EXIT_CONFIG = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-reviewer",
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
        "--strong-model",
        default=DEFAULT_MODEL_STRONG,
        metavar="ID",
        help=(
            "Model for FormalVerifier, AdversarialSkeptic, and the final referee "
            f"(default: {DEFAULT_MODEL_STRONG})"
        ),
    )
    parser.add_argument(
        "--fast-model",
        default=DEFAULT_MODEL_FAST,
        metavar="ID",
        help=(f"Model for NotationAuditor and ExpositionReferee (default: {DEFAULT_MODEL_FAST})"),
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        metavar="N",
        help=(
            "Output token limit per call. On models where extended thinking is on by "
            "default, this caps thinking and response text together, so raise it when "
            f"switching to one (default: {DEFAULT_MAX_TOKENS})"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.max_tokens < 1:
        print("[red]--max-tokens must be a positive integer.[/red]")
        return EXIT_CONFIG

    try:
        prompts = load_prompts(strong_model=args.strong_model, fast_model=args.fast_model)
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG

    client = anthropic.Anthropic()

    try:
        # Free pre-flight: proves the credentials work and the model IDs are real
        # before the run writes anything or asks the user to approve spending.
        check_access(client, [r.model for r in prompts.reviewers.values()])
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG

    try:
        if args.dry_run:
            run_dry_run(client, args.input, prompts=prompts, max_tokens=args.max_tokens)
            return EXIT_OK

        result = run_pipeline(
            client,
            args.input,
            args.output,
            prompts=prompts,
            max_tokens=args.max_tokens,
            assume_yes=args.yes,
        )
    except ConfigurationError as exc:
        print(f"[red]{exc}[/red]")
        return EXIT_CONFIG
    except anthropic.AuthenticationError:
        print(f"[red]{CREDENTIALS_HELP}[/red]")
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
