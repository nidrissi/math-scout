"""Tests for argument parsing and exit codes.

`main` is exercised with the network stubbed out, so these need no credentials.
"""

from __future__ import annotations

import pytest

from llm_reviewer import cli
from llm_reviewer.reviewer import (
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
    monkeypatch.setattr(cli.anthropic, "Anthropic", lambda: object())
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
    assert args.strong_model == DEFAULT_MODEL_STRONG
    assert args.fast_model == DEFAULT_MODEL_FAST
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
    code = run([str(paper), "--fast-model", "claude-haiku-4-5"], monkeypatch)
    assert code == cli.EXIT_CONFIG
    assert "cannot be used here" in capsys.readouterr().out


def test_missing_credentials_exit_two(paper, monkeypatch, capsys):
    def refuse(client, models):
        raise ConfigurationError(cli.CREDENTIALS_HELP)

    monkeypatch.setattr(cli, "check_access", refuse)
    assert run([str(paper)], monkeypatch) == cli.EXIT_CONFIG
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out
