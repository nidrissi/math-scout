"""Tests for argument parsing and exit codes.

`main` is exercised with the network stubbed out, so these need no credentials.
"""

from __future__ import annotations

import pytest

from math_scout import cli
from math_scout.reviewer import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_FAST,
    DEFAULT_MODEL_STRONG,
    MAX_OUTPUT_TOKENS,
    ConfigurationError,
    PipelineResult,
    ReviewerFailure,
)


@pytest.fixture
def paper(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(
        "\\begin{document}\n\\section{One}\nbody\n\\end{document}\n", encoding="utf-8"
    )
    return source


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Neutralise everything that would reach out, so only argument handling is under test."""
    monkeypatch.setattr(
        cli.ProviderRegistry, "from_models", classmethod(lambda cls, models: object())
    )
    monkeypatch.setattr(cli, "check_access", lambda client, models: None)


def run(argv, monkeypatch, result=None, boom=None):
    def fake_pipeline(*args, **kwargs):
        if boom is not None:
            raise boom
        return result

    monkeypatch.setattr(cli, "run_pipeline", fake_pipeline)
    return cli.main(argv)


# ----------------------------------------------------------------------------- parsing


def test_defaults_match_the_library():
    args = cli.build_parser().parse_args(["paper.tex"])
    assert args.preset is None
    assert args.strong_model is None
    assert args.fast_model is None
    assert cli.resolve_model_selection(
        preset=args.preset,
        strong_model=args.strong_model,
        fast_model=args.fast_model,
    ) == (DEFAULT_MODEL_STRONG, DEFAULT_MODEL_FAST)
    assert args.max_tokens == DEFAULT_MAX_TOKENS
    assert args.effort == DEFAULT_EFFORT
    assert args.yes is False
    assert args.dry_run is False
    assert args.output is None


def test_effort_only_accepts_known_levels():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["paper.tex", "--effort", "turbo"])


def test_short_yes_flag_works():
    assert cli.build_parser().parse_args(["paper.tex", "-y"]).yes is True


def test_provider_qualified_models_are_accepted():
    args = cli.build_parser().parse_args(["paper.tex", "--strong-model", "openai:gpt-5.6-sol"])
    assert args.strong_model == "openai:gpt-5.6-sol"


@pytest.mark.parametrize(
    ("preset", "models"),
    [
        (
            "opus-sonnet",
            ("anthropic:claude-opus-5", "anthropic:claude-sonnet-5"),
        ),
        ("sol-luna", ("openai:gpt-5.6-sol", "openai:gpt-5.6-luna")),
    ],
)
def test_model_presets_select_both_tiers(preset, models):
    args = cli.build_parser().parse_args(["paper.tex", "--preset", preset])
    assert (
        cli.resolve_model_selection(
            preset=args.preset,
            strong_model=args.strong_model,
            fast_model=args.fast_model,
        )
        == models
    )


@pytest.mark.parametrize(
    ("flag", "override", "expected"),
    [
        (
            "--strong-model",
            "anthropic:claude-opus-5",
            ("anthropic:claude-opus-5", "openai:gpt-5.6-luna"),
        ),
        (
            "--fast-model",
            "anthropic:claude-sonnet-5",
            ("openai:gpt-5.6-sol", "anthropic:claude-sonnet-5"),
        ),
    ],
)
def test_explicit_model_overrides_one_preset_tier(flag, override, expected):
    args = cli.build_parser().parse_args(["paper.tex", "--preset", "sol-luna", flag, override])
    assert (
        cli.resolve_model_selection(
            preset=args.preset,
            strong_model=args.strong_model,
            fast_model=args.fast_model,
        )
        == expected
    )


def test_unknown_model_preset_is_rejected():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["paper.tex", "--preset", "turbo"])


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--strong-model", "claude-opus-5"),
        ("--fast-model", "gpt-5.6-luna"),
        ("--strong-model", "synthetic-model"),
    ],
)
def test_bare_explicit_model_is_rejected_before_any_side_effect(
    paper, tmp_path, monkeypatch, capsys, flag, value
):
    out = tmp_path / "must-not-exist"

    def unexpected(*args, **kwargs):
        pytest.fail("bare model reached provider construction, preflight, or generation")

    monkeypatch.setattr(cli.ProviderRegistry, "from_models", classmethod(unexpected))
    monkeypatch.setattr(cli, "check_access", unexpected)
    monkeypatch.setattr(cli, "run_pipeline", unexpected)

    code = cli.main([str(paper), "--output", str(out), flag, value, "--yes"])

    assert code == cli.EXIT_CONFIG
    output = capsys.readouterr().out
    assert "PROVIDER:MODEL" in output
    assert "--preset" in output
    assert not out.exists()


# --------------------------------------------------------------------------- exit codes


def test_clean_run_exits_zero(paper, monkeypatch, tmp_path):
    result = PipelineResult(output_dir=tmp_path, report_path=tmp_path / "final_report.md")
    assert run([str(paper)], monkeypatch, result=result) == cli.EXIT_OK


def test_declined_run_exits_zero(paper, monkeypatch, tmp_path):
    """Declining is a decision, not an error — the docs say 0 and so does the code."""
    result = PipelineResult(output_dir=tmp_path, aborted=True)
    assert run([str(paper)], monkeypatch, result=result) == cli.EXIT_OK


def test_partial_failure_exits_one(paper, monkeypatch, tmp_path):
    result = PipelineResult(
        output_dir=tmp_path,
        failures=[ReviewerFailure(section="One", reviewer="FormalVerifier", error="boom")],
    )
    assert run([str(paper)], monkeypatch, result=result) == cli.EXIT_INCOMPLETE


def test_fatal_error_exits_two(paper, monkeypatch, tmp_path):
    result = PipelineResult(output_dir=tmp_path, fatal_error="credentials rejected")
    assert run([str(paper)], monkeypatch, result=result) == cli.EXIT_CONFIG


def test_configuration_error_exits_two(paper, monkeypatch, capsys):
    code = run([str(paper)], monkeypatch, boom=ConfigurationError("nothing to review"))
    assert code == cli.EXIT_CONFIG
    assert "nothing to review" in capsys.readouterr().out


def test_interrupt_exits_one_and_says_how_to_resume(paper, monkeypatch, capsys):
    code = run([str(paper)], monkeypatch, boom=KeyboardInterrupt())
    assert code == cli.EXIT_INCOMPLETE
    assert "resume" in capsys.readouterr().out


# ------------------------------------------------------------------- settings validation


@pytest.mark.parametrize("value", ["0", "-1", str(MAX_OUTPUT_TOKENS + 1)])
def test_out_of_range_max_tokens_exits_two(paper, monkeypatch, value):
    code = run([str(paper), "--max-tokens", value], monkeypatch)
    assert code == cli.EXIT_CONFIG


def test_max_tokens_at_the_ceiling_is_accepted(paper, monkeypatch, tmp_path):
    result = PipelineResult(output_dir=tmp_path)
    code = run([str(paper), "--max-tokens", str(MAX_OUTPUT_TOKENS)], monkeypatch, result=result)
    assert code == cli.EXIT_OK


def test_unusable_model_exits_two_before_any_request(paper, monkeypatch, capsys):
    code = run([str(paper), "--fast-model", "anthropic:claude-haiku-4-5"], monkeypatch)
    assert code == cli.EXIT_CONFIG
    assert "cannot be used here" in capsys.readouterr().out


def test_missing_credentials_exit_two(paper, monkeypatch, capsys):
    def refuse(client, models):
        raise ConfigurationError("No usable Anthropic credentials. Export ANTHROPIC_API_KEY.")

    monkeypatch.setattr(cli, "check_access", refuse)
    assert run([str(paper)], monkeypatch) == cli.EXIT_CONFIG
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
