"""Tests for the pure, API-free parts of the pipeline.

Nothing here touches the network or needs credentials: every function under test either
transforms LaTeX text or reads files from a tmp_path fixture.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest

from llm_reviewer.reviewer import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_FAST,
    DEFAULT_MODEL_STRONG,
    EFFORT_LEVELS,
    FRONT_MATTER_NAME,
    MAX_NONSTREAMING_TOKENS,
    MODEL_PRICING,
    REVIEWER_SPECS,
    Chunk,
    ConfigurationError,
    IssueWithReviewer,
    Review,
    SeverityLevel,
    _safe_filename,
    call_reviewer,
    check_access,
    chunk_by_section,
    chunk_stem,
    extract_global_context,
    format_coverage_note,
    load_known_issues,
    load_prompts,
    resolve_inputs,
    strip_comment,
    summarize_known_issues,
    validate_settings,
)
from llm_reviewer.reviewer import (
    ReviewerFailure as Failure,
)


def paper(body: str, preamble: str = "\\usepackage{amsmath}") -> str:
    return f"{preamble}\n\\begin{{document}}\n{body}\n\\end{{document}}\n"


def issue(severity: str, title: str = "t", reviewer: str = "R") -> IssueWithReviewer:
    return IssueWithReviewer(
        title=title,
        severity=severity,
        type="logic",
        location="Section 1",
        quote="q",
        analysis="a",
        suggested_fix="f",
        confidence=0.5,
        reviewer=reviewer,
    )


# --------------------------------------------------------------------------- chunking


def test_chunk_by_section_splits_on_sections():
    tex = paper("\\section{One}\naaa\n\\section{Two}\nbbb")
    chunks = chunk_by_section(tex)
    assert [c.name for c in chunks] == ["One", "Two"]
    assert "aaa" in chunks[0].content
    assert "aaa" not in chunks[1].content


@pytest.mark.parametrize(
    "title", ["References", "bibliography", "Acknowledgments", "ACKNOWLEDGEMENTS"]
)
def test_chunk_by_section_skips_non_mathematical_sections(title):
    tex = paper(f"\\section{{Main}}\nx\n\\section{{{title}}}\ny")
    assert [c.name for c in chunk_by_section(tex)] == ["Main"]


def test_chunk_by_section_handles_one_level_of_nested_braces():
    tex = paper("\\section{The $\\mathbb{Z}$-case}\nx")
    assert chunk_by_section(tex)[0].name == "The $\\mathbb{Z}$-case"


def test_chunk_by_section_returns_nothing_without_sections():
    """A \\chapter-only document yields no chunks, which the pipeline turns into an error."""
    assert chunk_by_section(paper("\\chapter{One}\ntext")) == []


def test_front_matter_chunk_created_for_substantial_intro():
    intro = "Introductory prose. " * 40  # comfortably over the threshold
    tex = paper(f"\\maketitle\n{intro}\n\\section{{One}}\nx")
    chunks = chunk_by_section(tex)
    assert chunks[0].name == FRONT_MATTER_NAME
    assert "Introductory prose." in chunks[0].content
    assert [c.name for c in chunks[1:]] == ["One"]


def test_front_matter_chunk_skipped_when_only_title_and_abstract():
    tex = paper(
        "\\maketitle\n\\begin{abstract}"
        + ("Long abstract text. " * 60)
        + "\\end{abstract}\n\\section{One}\nx"
    )
    assert [c.name for c in chunk_by_section(tex)] == ["One"]


def test_front_matter_chunk_skipped_when_intro_is_short():
    tex = paper("\\maketitle\nA sentence.\n\\section{One}\nx")
    assert [c.name for c in chunk_by_section(tex)] == ["One"]


# -------------------------------------------------------------------------- filenames


def test_safe_filename_strips_latex_and_unsafe_characters():
    assert _safe_filename("Proof of \\emph{Theorem} 1!") == "Proof_of_Theorem_1"


def test_safe_filename_truncates_to_80_characters():
    assert len(_safe_filename("a" * 200)) == 80


def test_safe_filename_falls_back_when_nothing_survives():
    assert _safe_filename("$$$") == "section"


def test_safe_filename_collides_but_chunk_stem_does_not():
    # Bare LaTeX commands are stripped entirely, so these two distinct titles map to
    # the same stem. The index prefix is what keeps their output files apart.
    a, b = Chunk("The \\alpha case", ""), Chunk("The \\beta case", "")
    assert _safe_filename(a.name) == _safe_filename(b.name)
    assert chunk_stem(3, a) != chunk_stem(4, b)
    assert chunk_stem(3, a).startswith("03_")


# ---------------------------------------------------------------------- \input files


def test_strip_comment_ignores_escaped_percent():
    assert strip_comment(r"50\% done % trailing") == r"50\% done "
    assert strip_comment("no comment here") == "no comment here"


def test_resolve_inputs_inlines_nested_references(tmp_path):
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "intro.tex").write_text(
        "\\section{Intro}\nintro body\n\\input{sections/deep}\n"
    )
    (tmp_path / "sections" / "deep.tex").write_text("deep body\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input{sections/intro}\n\\include{sections/deep.tex}\n")

    resolved = resolve_inputs(main)
    assert "intro body" in resolved
    assert resolved.count("deep body") == 2
    assert "\\input{" not in resolved


def test_resolve_inputs_ignores_commented_references(tmp_path):
    (tmp_path / "skipped.tex").write_text("SHOULD NOT APPEAR\n")
    main = tmp_path / "main.tex"
    main.write_text("% \\input{skipped}\ntext\n")

    resolved = resolve_inputs(main)
    assert "SHOULD NOT APPEAR" not in resolved
    assert "% \\input{skipped}" in resolved


def test_resolve_inputs_resolves_relative_to_including_file(tmp_path):
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "a.tex").write_text("\\input{b}\n")
    (tmp_path / "parts" / "b.tex").write_text("sibling body\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input{parts/a}\n")

    assert "sibling body" in resolve_inputs(main)


def test_resolve_inputs_warns_and_keeps_missing_reference(tmp_path, capsys):
    main = tmp_path / "main.tex"
    main.write_text("\\input{nope}\nrest\n")

    resolved = resolve_inputs(main)
    assert "\\input{nope}" in resolved
    assert "rest" in resolved
    assert "cannot find" in capsys.readouterr().out


def test_resolve_inputs_breaks_cycles(tmp_path):
    (tmp_path / "a.tex").write_text("A\n\\input{b}\n")
    (tmp_path / "b.tex").write_text("B\n\\input{a}\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input{a}\n")

    resolved = resolve_inputs(main)
    assert "A" in resolved and "B" in resolved


def test_chunking_sees_sections_from_included_files(tmp_path):
    (tmp_path / "proofs.tex").write_text("\\section{Proofs}\nproof body\n")
    main = tmp_path / "main.tex"
    main.write_text(paper("\\section{Intro}\nx\n\\input{proofs}"))

    chunks = chunk_by_section(resolve_inputs(main))
    assert [c.name for c in chunks] == ["Intro", "Proofs"]


# --------------------------------------------------------------------- global context


def test_extract_global_context_collects_title_abstract_and_preamble():
    tex = paper(
        "\\begin{abstract}We prove things.\\end{abstract}\n"
        "\\begin{theorem}Statement.\\end{theorem}",
        preamble="\\usepackage{amsmath}\n\\title{A Result}",
    )
    context = extract_global_context(tex)
    assert context.startswith("# PAPER TITLE\n\nA Result")
    assert "We prove things." in context
    assert "\\usepackage{amsmath}" in context
    assert "## Theorem 1" in context


def test_extract_global_context_handles_braces_in_title():
    tex = paper("body", preamble="\\title{Cohomology of $\\mathbb{Z}$}")
    assert "Cohomology of $\\mathbb{Z}$" in extract_global_context(tex)


def test_extract_global_context_caps_theorems_at_fifteen():
    body = "".join(f"\\begin{{theorem}}T{i}\\end{{theorem}}\n" for i in range(20))
    context = extract_global_context(paper(body))
    assert "## Theorem 15" in context
    assert "## Theorem 16" not in context


# ----------------------------------------------------------------------------- issues


def test_summarize_known_issues_sorts_by_severity():
    issues = [issue("minor", "m"), issue("critical", "c"), issue("moderate", "o")]
    lines = summarize_known_issues(issues).splitlines()
    # Each line reads "- [<reviewer> - <SEVERITY>] <location>: <title>".
    assert [line.split()[3] for line in lines] == ["CRITICAL]", "MODERATE]", "MINOR]"]


def test_summarize_known_issues_respects_limit():
    issues = [issue("major", f"t{i}") for i in range(30)]
    assert len(summarize_known_issues(issues, limit=15).splitlines()) == 15


def test_summarize_known_issues_handles_empty_list():
    assert summarize_known_issues([]) == "No previously detected issues."


def test_severity_ordering_is_descending():
    order = [
        SeverityLevel.CRITICAL,
        SeverityLevel.MAJOR,
        SeverityLevel.MODERATE,
        SeverityLevel.MINOR,
    ]
    levels = [s.numerical_level for s in order]
    assert levels == sorted(levels, reverse=True)


def test_load_known_issues_returns_empty_for_missing_file(tmp_path):
    assert load_known_issues(tmp_path / "absent.jsonl") == []


def test_load_known_issues_skips_blank_lines(tmp_path):
    path = tmp_path / "issues.jsonl"
    path.write_text(f"{issue('major').model_dump_json()}\n\n\n")
    assert len(load_known_issues(path)) == 1


def test_load_known_issues_reports_a_corrupt_line(tmp_path):
    path = tmp_path / "issues.jsonl"
    path.write_text(f"{issue('major').model_dump_json()}\nnot json\n")
    with pytest.raises(ConfigurationError, match="issues.jsonl:2"):
        load_known_issues(path)


def test_format_coverage_note_is_empty_without_failures():
    assert format_coverage_note([]) == ""


def test_format_coverage_note_names_every_failure():
    note = format_coverage_note(
        [Failure("Intro", "FormalVerifier", "boom"), Failure("Proofs", "NotationAuditor", "bang")]
    )
    assert "COVERAGE GAPS" in note
    assert "FormalVerifier on section 'Intro': boom" in note
    assert "NotationAuditor on section 'Proofs': bang" in note


# ----------------------------------------------------------------------- access check


class FakeParsed:
    def __init__(self, parsed_output, stop_reason="end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


class FakeClient:
    """Stands in for anthropic.Anthropic, recording what the pipeline sent."""

    def __init__(self, error: Exception | None = None, stop_reason: str = "end_turn"):
        self.error = error
        self.stop_reason = stop_reason
        self.models_probed: list[str] = []
        self.parse_calls: list[dict] = []
        self.messages = self

    def count_tokens(self, model, messages):  # noqa: ARG002 - mirrors the SDK signature
        self.models_probed.append(model)
        if self.error is not None:
            raise self.error
        return object()

    def parse(self, **kwargs):
        self.parse_calls.append(kwargs)
        parsed = None if self.stop_reason == "max_tokens" else Review(issues=[])
        return FakeParsed(parsed, stop_reason=self.stop_reason)


def api_error(cls: type, status: int, message: str = "nope"):
    response = httpx.Response(status, request=httpx.Request("POST", "https://example"))
    return cls(message, response=response, body=None)


def test_check_access_probes_each_distinct_model_once():
    client = FakeClient()
    check_access(client, ["strong", "fast", "strong", "fast"])
    assert client.models_probed == ["fast", "strong"]


def test_check_access_reports_missing_credentials():
    # The SDK raises a bare TypeError when no credential can be resolved at all.
    client = FakeClient(TypeError("Could not resolve authentication method."))
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        check_access(client, ["strong"])


def test_check_access_reports_rejected_credentials():
    client = FakeClient(api_error(anthropic.AuthenticationError, 401, "invalid x-api-key"))
    with pytest.raises(ConfigurationError, match="rejected"):
        check_access(client, ["strong"])


def test_check_access_reports_an_unknown_model():
    client = FakeClient(api_error(anthropic.NotFoundError, 404, "model not found"))
    with pytest.raises(ConfigurationError, match="'made-up-model' is not available"):
        check_access(client, ["made-up-model"])


def test_check_access_reports_an_unreachable_api():
    client = FakeClient(
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://example"))
    )
    with pytest.raises(ConfigurationError, match="Cannot reach"):
        check_access(client, ["strong"])


# ---------------------------------------------------------------------------- prompts


def test_load_prompts_reads_every_prompt_including_the_final_referee():
    prompts = load_prompts(strong_model="strong-x", fast_model="fast-y")
    assert set(prompts.reviewers) == {
        "FormalVerifier",
        "AdversarialSkeptic",
        "NotationAuditor",
        "ExpositionReferee",
    }
    assert prompts.final_referee.strip()
    assert all(r.prompt_text.strip() for r in prompts.reviewers.values())


def test_load_prompts_applies_the_requested_models():
    prompts = load_prompts(strong_model="strong-x", fast_model="fast-y")
    assert prompts.reviewers["FormalVerifier"].model == "strong-x"
    assert prompts.reviewers["AdversarialSkeptic"].model == "strong-x"
    assert prompts.reviewers["NotationAuditor"].model == "fast-y"
    assert prompts.reviewers["ExpositionReferee"].model == "fast-y"


# --------------------------------------------------------------------------- defaults


def test_default_models_are_priced():
    """Cost estimation silently degrades if a default model has no pricing entry."""
    assert DEFAULT_MODEL_STRONG in MODEL_PRICING
    assert DEFAULT_MODEL_FAST in MODEL_PRICING


def test_default_max_tokens_stays_under_the_non_streaming_ceiling():
    """Above this the SDK raises ValueError instead of making the call."""
    assert 0 < DEFAULT_MAX_TOKENS <= MAX_NONSTREAMING_TOKENS


def test_default_effort_is_a_valid_level():
    assert DEFAULT_EFFORT in EFFORT_LEVELS


# --------------------------------------------------------------------------- thinking


def test_thinking_is_enabled_for_the_deductive_reviewers_only():
    thinking = {name: spec.thinking for name, spec in REVIEWER_SPECS.items()}
    assert thinking == {
        "FormalVerifier": True,
        "AdversarialSkeptic": True,
        "NotationAuditor": False,
        "ExpositionReferee": False,
    }


@pytest.mark.parametrize(
    ("reviewer_name", "expected"),
    [("FormalVerifier", "adaptive"), ("NotationAuditor", "disabled")],
)
def test_call_reviewer_sends_the_reviewer_s_thinking_config(reviewer_name, expected):
    client = FakeClient()
    prompts = load_prompts()
    call_reviewer(
        client=client,
        reviewer=prompts.reviewers[reviewer_name],
        chunk=Chunk("Intro", "body"),
        global_context="ctx",
        known_issues="none",
        max_tokens=1234,
        effort="medium",
    )
    (sent,) = client.parse_calls
    assert sent["thinking"] == {"type": expected}
    assert sent["output_config"] == {"effort": "medium"}
    assert sent["max_tokens"] == 1234


def test_call_reviewer_names_truncation_as_the_cause():
    client = FakeClient(stop_reason="max_tokens")
    prompts = load_prompts()
    with pytest.raises(ValueError, match="Raise --max-tokens"):
        call_reviewer(
            client=client,
            reviewer=prompts.reviewers["FormalVerifier"],
            chunk=Chunk("Intro", "body"),
            global_context="ctx",
            known_issues="none",
        )


# ------------------------------------------------------------------- settings guard


@pytest.mark.parametrize("effort", EFFORT_LEVELS)
def test_validate_settings_accepts_the_default_configuration(effort):
    validate_settings(load_prompts(), max_tokens=DEFAULT_MAX_TOKENS, effort=effort)


def test_validate_settings_rejects_max_tokens_above_the_ceiling():
    with pytest.raises(ConfigurationError, match="capped at"):
        validate_settings(load_prompts(), max_tokens=MAX_NONSTREAMING_TOKENS + 1, effort="high")


def test_validate_settings_rejects_non_positive_max_tokens():
    with pytest.raises(ConfigurationError, match="positive integer"):
        validate_settings(load_prompts(), max_tokens=0, effort="high")


def test_validate_settings_rejects_an_unknown_effort_level():
    with pytest.raises(ConfigurationError, match="--effort must be one of"):
        validate_settings(load_prompts(), max_tokens=DEFAULT_MAX_TOKENS, effort="turbo")


@pytest.mark.parametrize("effort", ["xhigh", "max"])
def test_validate_settings_rejects_disabled_thinking_on_a_capped_model(effort):
    """Opus 5 refuses thinking:disabled above `high`, and both thinking-off reviewers
    would land there if someone pointed --fast-model at it."""
    prompts = load_prompts(fast_model="claude-opus-5")
    with pytest.raises(ConfigurationError, match="NotationAuditor"):
        validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort=effort)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_validate_settings_allows_a_capped_model_at_lower_effort(effort):
    prompts = load_prompts(fast_model="claude-opus-5")
    validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort=effort)
