"""Tests for the pure, API-free parts of the pipeline.

Nothing here touches the network or needs credentials: every function under test either
transforms LaTeX text or reads files from a tmp_path fixture.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from math_scout.providers import (
    GenerationRequest,
    GenerationResult,
    ModelRef,
    Provider,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderModelError,
    ProviderRateLimitError,
    ProviderRegistry,
    ProviderServerError,
    provider_type,
)
from math_scout.reviewer import (
    DEFAULT_EFFORT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL_FAST,
    DEFAULT_MODEL_STRONG,
    EFFORT_LEVELS,
    FRONT_MATTER_NAME,
    MAX_ATTEMPTS_PER_CALL,
    MAX_OUTPUT_TOKENS,
    MOVING_SUFFIX,
    REVIEWER_SPECS,
    WHOLE_PAPER_NAME,
    Chunk,
    ConfigurationError,
    Issue,
    IssueWithReviewer,
    Review,
    ReviewerScope,
    SeverityLevel,
    _build_messages,
    _build_system,
    _format_issue_full,
    _safe_filename,
    call_reviewer,
    check_access,
    chunk_by_section,
    chunk_stem,
    extract_global_context,
    format_coverage_note,
    load_known_issues,
    load_prompts,
    lower_effort,
    mask_non_content,
    resolve_inputs,
    run_dry_run,
    run_final_referee,
    run_pipeline,
    run_settings,
    strip_comment,
    summarize_known_issues,
    validate_settings,
)
from math_scout.reviewer import (
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
    assert _safe_filename("ab" * 100) == ("ab" * 100)[:80]


def test_safe_filename_falls_back_when_nothing_survives():
    assert _safe_filename("$$$") == "section"


def test_safe_filename_collides_but_chunk_stem_does_not():
    # Bare LaTeX commands are stripped entirely, so these two distinct titles map to
    # the same stem. The index prefix is what keeps their output files apart.
    a, b = Chunk("The \\alpha case", ""), Chunk("The \\beta case", "")
    assert _safe_filename(a.name) == _safe_filename(b.name)
    assert chunk_stem(3, a) != chunk_stem(4, b)
    assert chunk_stem(3, a).startswith("003_")


def test_chunk_stem_index_still_sorts_past_a_hundred_chunks():
    stems = sorted(chunk_stem(i, Chunk("S", "")) for i in (9, 99, 100))
    assert stems == [chunk_stem(9, Chunk("S", "")), "099_S", "100_S"]


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


def test_resolve_inputs_breaks_cycles(tmp_path, capsys):
    (tmp_path / "a.tex").write_text("A\n\\input{b}\n")
    (tmp_path / "b.tex").write_text("B\n\\input{a}\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input{a}\n")

    resolved = resolve_inputs(main)
    # Each file is inlined exactly once and the cycle is reported, rather than the
    # recursion merely bottoming out against the depth limit.
    assert resolved.count("A") == 1
    assert resolved.count("B") == 1
    assert "circular" in capsys.readouterr().out


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


def test_the_final_referee_is_given_the_quote_and_the_confidence():
    """Both used to be collected, stored, and then dropped before the report was written.

    Without the quote the referee cannot check a finding against the source, and without
    the confidence it cannot tell a demonstrated defect from a lead.
    """
    rendered = _format_issue_full(
        issue("major", "Bad step").model_copy(
            update={"quote": "\\begin{align}\n  x = y\n\\end{align}"}
        )
    )
    assert "\\begin{align}" in rendered
    assert "x = y" in rendered
    assert "confidence 0.50" in rendered
    assert "(logic)" in rendered  # the type slug, for grouping


def test_a_quote_containing_a_backtick_does_not_break_out_of_its_fence():
    """LaTeX uses ` as an opening quote, so a three-backtick fence is not always enough."""
    rendered = _format_issue_full(
        issue("minor").model_copy(update={"quote": "``quoted'' and ```three```"})
    )
    assert "````latex" in rendered


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


class FakeProvider(Provider):
    """Provider-neutral test double used by all orchestration tests."""

    name = "anthropic"
    display_name = "Anthropic"
    credentials_help = "No usable Anthropic credentials. Export ANTHROPIC_API_KEY."
    models_url = "https://example.test/models"

    def __init__(
        self,
        error: Exception | None = None,
        truncated: bool = False,
        issues_per_call: int = 0,
        truncate_first: int | None = None,
    ):
        self.error = error
        self.truncated = truncated
        self.issues_per_call = issues_per_call
        self.truncate_first = truncate_first
        self.models_probed: list[str] = []
        self.parse_calls: list[GenerationRequest] = []
        self.create_calls: list[GenerationRequest] = []
        self.count_calls: list[GenerationRequest] = []

    @classmethod
    def validate_request(cls, model, *, reasoning, effort, output_limit):
        return None

    @classmethod
    def pricing(cls, model):
        return None

    def preflight(self, model: str) -> None:
        self.models_probed.append(model)
        if self.error is not None:
            raise self.error

    def count_tokens(self, request: GenerationRequest) -> int:
        self.count_calls.append(request)
        if self.error is not None:
            raise self.error
        return 100

    def _truncates(self) -> bool:
        """Whether this call runs out of budget, honouring `truncate_first` if set."""
        if self.truncate_first is not None:
            return len(self.parse_calls) + len(self.create_calls) <= self.truncate_first
        return self.truncated

    def generate(self, request: GenerationRequest) -> GenerationResult:
        calls = self.parse_calls if request.output_schema is not None else self.create_calls
        calls.append(request)
        if self.error is not None:
            raise self.error
        if self._truncates():
            return GenerationResult(completed=False, truncated=True)
        if request.output_schema is None:
            return GenerationResult(text="# Summary\n\nreport")
        target = next(
            block.text
            for block in request.blocks
            if block.role == "user"
            and block.text.startswith(("# SECTION UNDER REVIEW", "# FULL PAPER UNDER REVIEW"))
        )
        location = (
            target.split("SECTION TITLE: ")[1].split("\n")[0]
            if "SECTION TITLE: " in target
            else WHOLE_PAPER_NAME
        )
        issues = [
            Issue(
                title=f"issue {n}",
                severity="major",
                type="logic",
                location=location,
                quote="q",
                analysis="a",
                suggested_fix="f",
                confidence=0.5,
            )
            for n in range(self.issues_per_call)
        ]
        return GenerationResult(parsed=Review(issues=issues))


class FakeOpenAIProvider(FakeProvider):
    name = "openai"
    display_name = "OpenAI"
    credentials_help = "No usable OpenAI credentials. Export OPENAI_API_KEY."


def registry(*providers: Provider) -> ProviderRegistry:
    return ProviderRegistry({provider.name: provider for provider in providers})


def test_check_access_probes_each_distinct_model_once():
    provider = FakeProvider()
    check_access(
        registry(provider),
        [
            ModelRef.parse(name)
            for name in (
                "anthropic:strong",
                "anthropic:fast",
                "anthropic:strong",
                "anthropic:fast",
            )
        ],
    )
    assert provider.models_probed == ["fast", "strong"]


def test_check_access_routes_distinct_models_to_each_provider_without_generation():
    anthropic_provider = FakeProvider()
    openai_provider = FakeOpenAIProvider()
    providers = registry(anthropic_provider, openai_provider)

    check_access(
        providers,
        [
            ModelRef.parse("anthropic:claude-opus-5"),
            ModelRef.parse("openai:gpt-5.6-sol"),
        ],
    )

    assert anthropic_provider.models_probed == ["claude-opus-5"]
    assert openai_provider.models_probed == ["gpt-5.6-sol"]
    assert anthropic_provider.parse_calls == openai_provider.parse_calls == []


def test_check_access_reports_missing_credentials():
    # The SDK raises a bare TypeError when no credential can be resolved at all.
    client = FakeProvider(ProviderAuthenticationError("Could not resolve authentication method."))
    with pytest.raises(ConfigurationError, match="ANTHROPIC_API_KEY"):
        check_access(registry(client), [ModelRef.parse("anthropic:strong")])


def test_check_access_reports_rejected_credentials():
    client = FakeProvider(ProviderAuthenticationError("invalid x-api-key"))
    with pytest.raises(ConfigurationError, match="rejected"):
        check_access(registry(client), [ModelRef.parse("anthropic:strong")])


def test_check_access_reports_openai_credentials_help():
    provider = FakeOpenAIProvider(ProviderAuthenticationError("bad key"))
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        check_access(registry(provider), [ModelRef.parse("openai:gpt-5.6-sol")])


def test_check_access_reports_an_unknown_model():
    client = FakeProvider(ProviderModelError("model not found"))
    with pytest.raises(ConfigurationError, match="anthropic:made-up-model.*not available"):
        check_access(registry(client), [ModelRef.parse("anthropic:made-up-model")])


def test_check_access_reports_an_unreachable_api():
    client = FakeProvider(ProviderConnectionError("offline"))
    with pytest.raises(ConfigurationError, match="Cannot reach"):
        check_access(registry(client), [ModelRef.parse("anthropic:strong")])


# ---------------------------------------------------------------------------- prompts


def test_load_prompts_reads_every_prompt_including_the_final_referee():
    prompts = load_prompts(strong_model="anthropic:strong-x", fast_model="anthropic:fast-y")
    assert set(prompts.reviewers) == {
        "FormalVerifier",
        "AdversarialSkeptic",
        "NotationAuditor",
        "ExpositionReferee",
        "ClaimAuditor",
    }
    assert prompts.final_referee.strip()
    assert prompts.review_protocol.strip()
    assert all(r.prompt_text.strip() for r in prompts.reviewers.values())


def test_load_prompts_applies_the_requested_models():
    prompts = load_prompts(strong_model="anthropic:strong-x", fast_model="anthropic:fast-y")
    assert prompts.reviewers["FormalVerifier"].model == ModelRef.parse("anthropic:strong-x")
    assert prompts.reviewers["AdversarialSkeptic"].model == ModelRef.parse("anthropic:strong-x")
    assert prompts.reviewers["ClaimAuditor"].model == ModelRef.parse("anthropic:strong-x")
    assert prompts.reviewers["NotationAuditor"].model == ModelRef.parse("anthropic:fast-y")
    assert prompts.reviewers["ExpositionReferee"].model == ModelRef.parse("anthropic:fast-y")
    assert prompts.strong_model == ModelRef.parse("anthropic:strong-x")


def test_load_prompts_rejects_unqualified_programmatic_model():
    with pytest.raises(ConfigurationError, match="PROVIDER:MODEL.*--preset"):
        load_prompts(strong_model="strong-x")


def test_load_prompts_accepts_mixed_provider_models():
    prompts = load_prompts(
        strong_model="openai:gpt-5.6-sol", fast_model="anthropic:claude-sonnet-5"
    )
    assert prompts.reviewers["FormalVerifier"].model == ModelRef("openai", "gpt-5.6-sol")
    assert prompts.reviewers["ExpositionReferee"].model == ModelRef("anthropic", "claude-sonnet-5")
    assert prompts.strong_model == ModelRef("openai", "gpt-5.6-sol")


def test_the_issue_block_is_named_once_and_matches_what_the_pipeline_sends():
    """The prompts used to point at a `KNOWN ISSUES` block that was never built.

    The real name belongs in the protocol, which every reviewer is sent; a reviewer prompt
    repeating it is drift waiting to happen, and naming a different one is the old bug.
    """
    prompts = load_prompts()
    reviewer_texts = [r.prompt_text for r in prompts.reviewers.values()]
    all_texts = [*reviewer_texts, prompts.review_protocol, prompts.final_referee]

    heading = _build_messages("issues", "target")[0].text.split("\n")[0]
    assert heading == "# DETECTED ISSUES"
    assert "DETECTED ISSUES" in prompts.review_protocol
    assert not any("DETECTED ISSUES" in text for text in reviewer_texts)
    assert not any("KNOWN ISSUES" in text for text in all_texts)


def test_the_review_protocol_is_its_own_cached_system_block():
    blocks = _build_system("ctx", "lane", "protocol")
    assert [b.text for b in blocks] == [
        "# GLOBAL CONTEXT\nctx",
        "# REVIEW PROTOCOL\nprotocol",
        "# REVIEWER PROMPT\nlane",
    ]
    # Every block is a cache breakpoint, and the first two are identical across
    # reviewers, so reviewers on one model share a prefix instead of writing their own.
    assert all(b.cacheable for b in blocks)


def test_the_final_referee_gets_no_review_protocol():
    """It writes markdown; the protocol describes the JSON issue schema."""
    blocks = _build_system("ctx", "referee")
    assert [b.text for b in blocks] == ["# GLOBAL CONTEXT\nctx", "# REVIEWER PROMPT\nreferee"]


def test_every_reviewer_call_in_a_run_carries_the_protocol(tmp_path):
    """Asserting on _build_system alone would not notice the pipeline forgetting to pass
    it: the protocol would silently vanish from every call and the suite would stay green."""
    client, _ = review(tmp_path, sections(*THREE))
    protocol = load_prompts().review_protocol

    for call in client.parse_calls:
        system = [block for block in call.blocks if block.role == "system"]
        headings = [block.text.split("\n")[0] for block in system]
        assert headings == ["# GLOBAL CONTEXT", "# REVIEW PROTOCOL", "# REVIEWER PROMPT"]
        assert system[1].text == "# REVIEW PROTOCOL\n" + protocol


# ----------------------------------------------------------------------------- scope


def test_cross_section_reviewers_read_the_whole_paper():
    scopes = {name: spec.scope for name, spec in REVIEWER_SPECS.items()}
    assert scopes == {
        "FormalVerifier": ReviewerScope.SECTION,
        "AdversarialSkeptic": ReviewerScope.SECTION,
        "ExpositionReferee": ReviewerScope.SECTION,
        # Consistency and overclaiming are both properties of two places in the
        # document, so neither is decidable from a single section.
        "NotationAuditor": ReviewerScope.PAPER,
        "ClaimAuditor": ReviewerScope.PAPER,
    }


def test_paper_scoped_reviewers_are_not_told_they_are_reading_a_section():
    client = FakeProvider()
    prompts = load_prompts()
    call_reviewer(
        providers=registry(client),
        reviewer=prompts.reviewers["NotationAuditor"],
        chunk=Chunk(WHOLE_PAPER_NAME, "whole source"),
        global_context="ctx",
        known_issues="none",
    )
    (sent,) = client.parse_calls
    target = next(
        block.text
        for block in sent.blocks
        if block.role == "user" and block.text.startswith("# FULL PAPER UNDER REVIEW")
    )
    assert target.startswith("# FULL PAPER UNDER REVIEW")
    assert "SECTION TITLE" not in target


# --------------------------------------------------------------------------- defaults


def test_default_models_are_priced():
    """Cost estimation silently degrades if a default model has no pricing entry."""
    for value in (DEFAULT_MODEL_STRONG, DEFAULT_MODEL_FAST):
        model = ModelRef.parse(value)
        assert provider_type(model.provider).pricing(model.model) is not None


def test_default_max_tokens_stays_under_the_non_streaming_ceiling():
    """Above this the SDK raises ValueError instead of making the call."""
    assert 0 < DEFAULT_MAX_TOKENS <= MAX_OUTPUT_TOKENS


def test_default_effort_is_a_valid_level():
    assert DEFAULT_EFFORT in EFFORT_LEVELS


# --------------------------------------------------------------------------- thinking


def test_only_the_single_pass_reviewer_runs_without_thinking():
    """Thinking follows the shape of the task. Everything here is multi-step except
    reading one section for passages that will not land, which is one call per passage."""
    thinking = {name: spec.thinking for name, spec in REVIEWER_SPECS.items()}
    assert thinking == {
        "FormalVerifier": True,
        "AdversarialSkeptic": True,
        "ClaimAuditor": True,
        # Whole-paper consistency is not the lookup that per-section consistency was.
        "NotationAuditor": True,
        "ExpositionReferee": False,
    }


@pytest.mark.parametrize(
    ("reviewer_name", "expected"),
    [("FormalVerifier", "adaptive"), ("ExpositionReferee", "disabled")],
)
def test_call_reviewer_sends_the_reviewer_s_thinking_config(reviewer_name, expected):
    client = FakeProvider()
    prompts = load_prompts()
    call_reviewer(
        providers=registry(client),
        reviewer=prompts.reviewers[reviewer_name],
        chunk=Chunk("Intro", "body"),
        global_context="ctx",
        known_issues="none",
        max_tokens=1234,
        effort="medium",
    )
    (sent,) = client.parse_calls
    assert sent.reasoning is (expected == "adaptive")
    assert sent.effort == "medium"
    assert sent.output_limit == 1234


def test_call_reviewer_names_truncation_as_the_cause():
    client = FakeProvider(truncated=True)
    prompts = load_prompts()
    with pytest.raises(ValueError, match="Raise --max-tokens"):
        call_reviewer(
            providers=registry(client),
            reviewer=prompts.reviewers["FormalVerifier"],
            chunk=Chunk("Intro", "body"),
            global_context="ctx",
            known_issues="none",
        )


def test_call_reviewer_retries_a_truncated_call_one_effort_level_down():
    """A truncated call is billed in full and returns nothing, so the retry is free of
    any trade-off but depth: without it the section loses that reviewer entirely."""
    client = FakeProvider(truncate_first=1, issues_per_call=1)
    prompts = load_prompts()
    review = call_reviewer(
        providers=registry(client),
        reviewer=prompts.reviewers["FormalVerifier"],
        chunk=Chunk("Intro", "body"),
        global_context="ctx",
        known_issues="none",
        effort="high",
    )
    assert len(review.issues) == 1
    efforts = [call.effort for call in client.parse_calls]
    assert efforts == ["high", "medium"]


def test_call_reviewer_retries_a_normalized_transient_error(monkeypatch):
    class FlakyProvider(FakeProvider):
        def __init__(self):
            super().__init__(issues_per_call=1)
            self.attempts = 0

        def generate(self, request):
            self.attempts += 1
            if self.attempts == 1:
                raise ProviderRateLimitError("slow down")
            return super().generate(request)

    monkeypatch.setattr("math_scout.reviewer.time.sleep", lambda delay: None)
    provider = FlakyProvider()
    prompts = load_prompts()

    result = call_reviewer(
        providers=registry(provider),
        reviewer=prompts.reviewers["FormalVerifier"],
        chunk=Chunk("Intro", "body"),
        global_context="ctx",
        known_issues="none",
    )

    assert len(result.issues) == 1
    assert provider.attempts == 2


def test_a_truncated_call_bills_at_most_max_attempts():
    """`run_dry_run` prices its ceiling off MAX_ATTEMPTS_PER_CALL, so if the retry
    structure ever grows another attempt, that ceiling silently stops being one."""
    client = FakeProvider(truncated=True)
    prompts = load_prompts()
    with pytest.raises(ValueError):
        call_reviewer(
            providers=registry(client),
            reviewer=prompts.reviewers["FormalVerifier"],
            chunk=Chunk("Intro", "body"),
            global_context="ctx",
            known_issues="none",
            effort="high",
        )
    assert len(client.parse_calls) == MAX_ATTEMPTS_PER_CALL

    referee = FakeProvider(truncated=True)
    with pytest.raises(ValueError):
        run_final_referee(
            providers=registry(referee),
            tex="paper",
            issues=[],
            global_context="ctx",
            system_prompt="be a referee",
            model=ModelRef.parse("anthropic:strong"),
            effort="high",
        )
    assert len(referee.create_calls) == MAX_ATTEMPTS_PER_CALL


def test_call_reviewer_does_not_retry_below_the_lowest_effort():
    """`low` has nothing under it, so a truncation there is final rather than a loop."""
    client = FakeProvider(truncated=True)
    prompts = load_prompts()
    with pytest.raises(ValueError, match="Raise --max-tokens"):
        call_reviewer(
            providers=registry(client),
            reviewer=prompts.reviewers["FormalVerifier"],
            chunk=Chunk("Intro", "body"),
            global_context="ctx",
            known_issues="none",
            effort="low",
        )
    assert len(client.parse_calls) == 1


def test_final_referee_retries_a_truncated_synthesis():
    """This call reads the whole paper and every issue, and losing it loses the report."""
    client = FakeProvider(truncate_first=1)
    report = run_final_referee(
        providers=registry(client),
        tex="paper",
        issues=[],
        global_context="ctx",
        system_prompt="be a referee",
        model=ModelRef.parse("anthropic:strong"),
        effort="high",
    )
    assert report.startswith("# Summary")
    efforts = [call.effort for call in client.create_calls]
    assert efforts == ["high", "medium"]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_lower_effort_walks_down_the_scale_and_stops(effort):
    below = lower_effort(effort)
    if effort == "low":
        assert below is None
    else:
        assert EFFORT_LEVELS.index(below) == EFFORT_LEVELS.index(effort) - 1


# ------------------------------------------------------------------- settings guard


@pytest.mark.parametrize("effort", EFFORT_LEVELS)
def test_validate_settings_accepts_the_default_configuration(effort):
    validate_settings(load_prompts(), max_tokens=DEFAULT_MAX_TOKENS, effort=effort)


def test_validate_settings_rejects_max_tokens_above_the_ceiling():
    with pytest.raises(ConfigurationError, match="capped at"):
        validate_settings(load_prompts(), max_tokens=MAX_OUTPUT_TOKENS + 1, effort="high")


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
    prompts = load_prompts(fast_model="anthropic:claude-opus-5")
    with pytest.raises(ConfigurationError, match="runs without thinking"):
        validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort=effort)


@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_validate_settings_allows_a_capped_model_at_lower_effort(effort):
    prompts = load_prompts(fast_model="anthropic:claude-opus-5")
    validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort=effort)


def test_validate_settings_rejects_a_model_that_cannot_take_effort_at_all():
    prompts = load_prompts(fast_model="anthropic:claude-haiku-4-5")
    with pytest.raises(ConfigurationError, match="cannot be used here"):
        validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort="high")


def test_validate_settings_rejects_an_effort_the_model_lacks():
    """xhigh arrived with Opus 4.7; 4.6 accepts only low/medium/high/max."""
    prompts = load_prompts(strong_model="anthropic:claude-opus-4-6")
    with pytest.raises(ConfigurationError, match="does not support --effort xhigh"):
        validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort="xhigh")


def test_validate_settings_ignores_models_it_has_no_table_entry_for():
    prompts = load_prompts(strong_model="anthropic:some-future-model")
    validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort="max")


def test_state_settings_qualify_every_provider_model():
    defaults = run_settings(load_prompts(), DEFAULT_MAX_TOKENS, "high")
    assert set(defaults["models"].values()) == {
        "anthropic:claude-opus-5",
        "anthropic:claude-sonnet-5",
    }

    mixed = run_settings(
        load_prompts(strong_model="openai:gpt-5.6-sol"), DEFAULT_MAX_TOKENS, "high"
    )
    assert mixed["models"]["FormalVerifier"] == "openai:gpt-5.6-sol"
    assert mixed["models"]["ExpositionReferee"] == "anthropic:claude-sonnet-5"


def test_validate_settings_rejects_unknown_provider():
    prompts = load_prompts(strong_model="other:model")
    with pytest.raises(ConfigurationError, match="Unknown model provider 'other'"):
        validate_settings(prompts, max_tokens=DEFAULT_MAX_TOKENS, effort="high")


def test_default_priced_models_can_actually_be_driven():
    """The default priced models accept the request shape the pipeline sends."""
    validate_settings(load_prompts(), max_tokens=DEFAULT_MAX_TOKENS, effort="high")


# ---------------------------------------------------------------- pre-flight hardening


def test_check_access_reports_a_rate_limit_instead_of_raising_it():
    client = FakeProvider(ProviderRateLimitError("slow down"))
    with pytest.raises(ConfigurationError, match="Rate limited"):
        check_access(registry(client), [ModelRef.parse("anthropic:strong")])


def test_check_access_reports_a_server_error_instead_of_raising_it():
    client = FakeProvider(ProviderServerError("boom", status_code=500))
    with pytest.raises(ConfigurationError, match="returned 500"):
        check_access(registry(client), [ModelRef.parse("anthropic:strong")])


# ------------------------------------------------------------------------- masking


def test_commented_out_section_neither_splits_nor_invents_a_chunk():
    tex = paper(
        "\\section{Real One}\nReal content.\n"
        "%\\section{Old Draft Title}\nStill part of Real One.\n"
        "\\section{Conclusion}\nend"
    )
    chunks = chunk_by_section(tex)
    assert [c.name for c in chunks] == ["Real One", "Conclusion"]
    assert "Still part of Real One." in chunks[0].content


def test_section_inside_verbatim_is_not_a_heading():
    tex = paper(
        "\\section{Real One}\nbefore\n"
        "\\begin{verbatim}\n\\section{References}\n\\end{verbatim}\n"
        "after\n\\section{Conclusion}\nend"
    )
    chunks = chunk_by_section(tex)
    assert [c.name for c in chunks] == ["Real One", "Conclusion"]
    # Without masking the skip-list match would have swallowed this text entirely.
    assert "after" in chunks[0].content


def test_commented_out_title_does_not_win_over_the_real_one():
    tex = paper("body", preamble="%\\title{Wrong}\n\\title{Right}")
    context = extract_global_context(tex)
    # The preamble is quoted verbatim further down, comments included, so check the
    # title heading itself rather than the whole context.
    title_block = context.split("# PREAMBLE")[0]
    assert "Right" in title_block
    assert "Wrong" not in title_block


def test_mask_non_content_preserves_length_and_line_structure():
    tex = "abc % comment\n\\begin{verbatim}\nxyz\n\\end{verbatim}\ntail\n"
    masked = mask_non_content(tex)
    assert len(masked) == len(tex)
    assert masked.count("\n") == tex.count("\n")
    assert masked.startswith("abc ")
    assert "comment" not in masked
    assert "xyz" not in masked
    assert "tail" in masked


# ------------------------------------------------------------------ \input confinement


def test_resolve_inputs_refuses_an_absolute_path_outside_the_document(tmp_path, capsys):
    outside = tmp_path / "secret.tex"
    outside.write_text("SECRET\n")
    project = tmp_path / "project"
    project.mkdir()
    main = project / "main.tex"
    main.write_text(f"\\input{{{outside}}}\nbody\n")

    resolved = resolve_inputs(main)
    assert "SECRET" not in resolved
    assert "Refusing to inline" in capsys.readouterr().out


def test_resolve_inputs_refuses_a_parent_directory_escape(tmp_path, capsys):
    (tmp_path / "secret.tex").write_text("SECRET\n")
    project = tmp_path / "project"
    project.mkdir()
    main = project / "main.tex"
    main.write_text("\\input{../secret}\nbody\n")

    resolved = resolve_inputs(main)
    assert "SECRET" not in resolved
    assert "Refusing to inline" in capsys.readouterr().out


def test_resolve_inputs_handles_a_dotted_filename(tmp_path):
    (tmp_path / "ch1.2.tex").write_text("DOTTED\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input{ch1.2}\n")
    assert "DOTTED" in resolve_inputs(main)


def test_resolve_inputs_handles_the_brace_less_form(tmp_path):
    (tmp_path / "part.tex").write_text("BRACELESS\n")
    main = tmp_path / "main.tex"
    main.write_text("\\input part\n")
    assert "BRACELESS" in resolve_inputs(main)


def test_non_utf8_source_is_a_configuration_error(tmp_path):
    bad = tmp_path / "latin1.tex"
    bad.write_bytes(b"\\section{Caf\xe9}\nbody\n")
    with pytest.raises(ConfigurationError, match="not valid UTF-8"):
        resolve_inputs(bad)


# -------------------------------------------------------------------------- resume


def sections(*names_and_bodies: tuple[str, str], front: str = "") -> str:
    body = front + "".join(f"\\section{{{n}}}\n{b}\n" for n, b in names_and_bodies)
    return f"\\title{{T}}\n\\begin{{document}}\n{body}\\end{{document}}\n"


def review(tmp_path, tex: str, **kwargs):
    """Run the pipeline over *tex* into a fixed output dir, returning (client, result)."""
    source = tmp_path / "paper.tex"
    source.write_text(tex, encoding="utf-8")
    client = FakeProvider(issues_per_call=1)
    result = run_pipeline(
        registry(client),
        str(source),
        tmp_path / "out",
        prompts=load_prompts(**kwargs.pop("models", {})),
        assume_yes=True,
        **kwargs,
    )
    return client, result


THREE = (("Alpha", "aaa"), ("Beta", "bbb"), ("Gamma", "ggg"))

# Three sections seen by each of the three section-scoped reviewers, plus one pass over
# the whole source by each of the two paper-scoped ones.
SECTION_CALLS = 3
PAPER_CALLS = 2
THREE_CALLS = 3 * SECTION_CALLS + PAPER_CALLS


def test_mixed_providers_route_reviewers_and_final_referee_by_tier(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(sections(("Alpha", "aaa")), encoding="utf-8")
    anthropic_provider = FakeProvider(issues_per_call=1)
    openai_provider = FakeOpenAIProvider(issues_per_call=1)
    prompts = load_prompts(
        strong_model="openai:gpt-5.6-sol", fast_model="anthropic:claude-sonnet-5"
    )

    result = run_pipeline(
        registry(anthropic_provider, openai_provider),
        str(source),
        tmp_path / "out",
        prompts=prompts,
        assume_yes=True,
    )

    # Strong: two section reviewers + ClaimAuditor + final referee.
    assert len(openai_provider.parse_calls) == 3
    assert len(openai_provider.create_calls) == 1
    # Fast: ExpositionReferee + NotationAuditor.
    assert len(anthropic_provider.parse_calls) == 2
    assert anthropic_provider.create_calls == []
    assert len(result.issues) == 5


def test_openai_only_run_routes_every_generation_to_one_provider(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(sections(("Alpha", "aaa")), encoding="utf-8")
    openai_provider = FakeOpenAIProvider(issues_per_call=1)
    prompts = load_prompts(strong_model="openai:gpt-5.6-sol", fast_model="openai:gpt-5.6-sol")

    result = run_pipeline(
        registry(openai_provider),
        str(source),
        tmp_path / "out",
        prompts=prompts,
        assume_yes=True,
    )

    assert len(openai_provider.parse_calls) == 5
    assert len(openai_provider.create_calls) == 1
    assert len(result.issues) == 5


def test_fatal_provider_error_stops_the_pipeline_after_one_call(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(sections(("Alpha", "aaa")), encoding="utf-8")
    provider = FakeProvider(ProviderAuthenticationError("revoked"))

    result = run_pipeline(
        registry(provider),
        str(source),
        tmp_path / "out",
        prompts=load_prompts(),
        assume_yes=True,
    )

    assert result.fatal_error is not None
    assert len(provider.parse_calls) == 1
    assert result.report_path is None


def test_resume_makes_no_calls_and_no_duplicates_when_nothing_changed(tmp_path):
    tex = sections(*THREE)
    first, r1 = review(tmp_path, tex)
    assert len(first.parse_calls) == THREE_CALLS
    assert len(r1.issues) == THREE_CALLS

    second, r2 = review(tmp_path, tex)
    assert second.parse_calls == []
    assert len(r2.issues) == THREE_CALLS
    assert Counter(i.location for i in r2.issues) == Counter(i.location for i in r1.issues)


def test_the_whole_paper_pass_runs_once_and_survives_a_resume(tmp_path):
    """Its key is not one of the section keys, so prune() has to be told about it."""
    _, first = review(tmp_path, sections(*THREE))
    assert sum(i.location == WHOLE_PAPER_NAME for i in first.issues) == PAPER_CALLS

    client, again = review(tmp_path, sections(*THREE))
    assert client.parse_calls == []
    assert sum(i.location == WHOLE_PAPER_NAME for i in again.issues) == PAPER_CALLS


def test_resume_reviews_only_the_new_chunk_when_one_is_inserted(tmp_path):
    """The regression this replaced: a shifted index re-ran everything and, because
    issues.jsonl is append-only, reported every prior issue twice."""
    review(tmp_path, sections(*THREE))
    client, result = review(tmp_path, sections(*THREE, front="Intro prose. " * 60))

    # The new Front matter chunk, plus the whole-paper pass: the source changed, so its
    # key changed too, and the sections either side of the insertion are untouched.
    assert len(client.parse_calls) == SECTION_CALLS + PAPER_CALLS
    assert len(result.issues) == 4 * SECTION_CALLS + PAPER_CALLS
    assert max(Counter(i.location for i in result.issues).values()) == SECTION_CALLS


# ------------------------------------------------------------------ output hygiene


def stems_in(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_an_insertion_leaves_no_file_at_a_stale_position(tmp_path):
    """Before this, chunks/ held both 00_Alpha.tex and 01_Alpha.tex with nothing to say
    which was current, and reviews/ sorted in an order the paper never had."""
    review(tmp_path, sections(*THREE))
    review(tmp_path, sections(*THREE, front="Intro prose. " * 60))
    out = tmp_path / "out"

    assert stems_in(out / "chunks") == [
        "000_Front_matter.tex",
        "001_Alpha.tex",
        "002_Beta.tex",
        "003_Gamma.tex",
        "004_whole_paper.tex",
    ]
    # Sorted, the review files now read in document order, which is the whole point of
    # the index prefix.
    positions = [name.split("_")[0] for name in stems_in(out / "reviews")]
    assert positions == sorted(positions)
    assert {name.split("_", 1)[1].rsplit("_", 1)[0] for name in stems_in(out / "reviews")} == {
        "Front_matter",
        "Alpha",
        "Beta",
        "Gamma",
        "whole_paper",
    }


def test_renaming_a_reused_review_does_not_re_run_it(tmp_path):
    """The rename is bookkeeping. Paying for the review again would defeat the point."""
    review(tmp_path, sections(*THREE))
    client, result = review(tmp_path, sections(*THREE, front="Intro prose. " * 60))

    # Match the section heading, not the text: the whole-paper pass sees "Alpha" too.
    targets = [
        next(
            block.text
            for block in call.blocks
            if block.role == "user"
            and block.text.startswith(("# SECTION UNDER REVIEW", "# FULL PAPER UNDER REVIEW"))
        )
        for call in client.parse_calls
    ]
    assert not any("SECTION TITLE: Alpha" in t for t in targets)
    assert (tmp_path / "out" / "reviews" / "001_Alpha_FormalVerifier.json").is_file()
    assert len(result.issues) == 4 * SECTION_CALLS + PAPER_CALLS


def test_a_deleted_section_takes_its_reviews_with_it(tmp_path):
    review(tmp_path, sections(*THREE))
    review(tmp_path, sections(*THREE[:2]))
    out = tmp_path / "out"

    assert not any("Gamma" in name for name in stems_in(out / "reviews"))
    assert not any("Gamma" in name for name in stems_in(out / "chunks"))


def test_two_sections_sharing_a_name_can_swap_without_clobbering_each_other(tmp_path):
    """A rename's target is another rename's source here, so a direct move would
    overwrite a live review with a different chunk's findings."""
    # A trailing section keeps either swapped chunk from being the last one, which would
    # absorb \end{document} and change its key — then nothing would be reused and there
    # would be no rename to collide.
    a, b, tail = ("Lemmas", "aaa"), ("Lemmas", "bbb"), ("Tail", "zzz")
    review(tmp_path, sections(a, b, tail))
    client, result = review(tmp_path, sections(b, a, tail))
    out = tmp_path / "out"

    # Both swapped reviews were reused, so both had to be renamed past each other.
    targets = [
        next(
            block.text
            for block in call.blocks
            if block.role == "user"
            and block.text.startswith(("# SECTION UNDER REVIEW", "# FULL PAPER UNDER REVIEW"))
        )
        for call in client.parse_calls
    ]
    assert not any("SECTION TITLE: Lemmas" in t for t in targets)
    assert stems_in(out / "chunks") == [
        "000_Lemmas.tex",
        "001_Lemmas.tex",
        "002_Tail.tex",
        "003_whole_paper.tex",
    ]
    assert (out / "chunks" / "000_Lemmas.tex").read_text().endswith("bbb\n")
    assert (out / "chunks" / "001_Lemmas.tex").read_text().endswith("aaa\n")
    assert len(result.issues) == 3 * SECTION_CALLS + PAPER_CALLS


def test_debris_from_an_interrupted_rename_is_swept(tmp_path):
    review(tmp_path, sections(*THREE))
    reviews = tmp_path / "out" / "reviews"
    (reviews / f"000_Alpha_FormalVerifier.json{MOVING_SUFFIX}").write_text("{}", encoding="utf-8")

    client, result = review(tmp_path, sections(*THREE))

    assert not list(reviews.glob(f"*{MOVING_SUFFIX}"))
    assert client.parse_calls == []  # sweeping debris must not invalidate a real review
    assert len(result.issues) == THREE_CALLS


def test_resume_state_cannot_move_a_file_from_outside_reviews(tmp_path):
    review(tmp_path, sections(*THREE))
    state_path = tmp_path / "out" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    key = next(iter(state["completed"]))
    reviewer = next(iter(state["completed"][key]))
    victim = tmp_path / "victim.json"
    victim.write_text("sentinel", encoding="utf-8")
    state["completed"][key][reviewer] = str(victim.resolve())
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Unsafe stored review filename"):
        review(tmp_path, sections(*THREE))

    assert victim.read_text(encoding="utf-8") == "sentinel"


def test_pipeline_refuses_symlink_in_managed_output_tree(tmp_path):
    source = tmp_path / "paper.tex"
    source.write_text(sections(("Alpha", "aaa")), encoding="utf-8")
    chunks = tmp_path / "out" / "chunks"
    chunks.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("sentinel", encoding="utf-8")
    (chunks / "000_Alpha.tex").symlink_to(victim)
    client = FakeProvider(issues_per_call=1)

    with pytest.raises(ConfigurationError, match="symbolic link"):
        run_pipeline(
            registry(client),
            str(source),
            tmp_path / "out",
            prompts=load_prompts(),
            assume_yes=True,
        )

    assert victim.read_text(encoding="utf-8") == "sentinel"
    assert client.parse_calls == []


def test_declining_the_run_deletes_nothing(tmp_path, monkeypatch):
    review(tmp_path, sections(*THREE))
    before = stems_in(tmp_path / "out" / "reviews")

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    source = tmp_path / "paper.tex"
    source.write_text(sections(*THREE[:1]), encoding="utf-8")
    result = run_pipeline(
        registry(FakeProvider(issues_per_call=1)),
        str(source),
        tmp_path / "out",
        prompts=load_prompts(),
    )

    assert result.aborted is True
    assert stems_in(tmp_path / "out" / "reviews") == before


def test_resume_re_reviews_a_chunk_whose_text_changed(tmp_path):
    review(tmp_path, sections(*THREE))
    client, result = review(tmp_path, sections(("Alpha", "REWRITTEN"), *THREE[1:]))

    assert len(client.parse_calls) == SECTION_CALLS + PAPER_CALLS
    reviewed = [
        next(
            block.text
            for block in call.blocks
            if block.role == "user"
            and block.text.startswith(("# SECTION UNDER REVIEW", "# FULL PAPER UNDER REVIEW"))
        )
        for call in client.parse_calls
    ]
    assert all("REWRITTEN" in text for text in reviewed)
    assert len(result.issues) == THREE_CALLS


def test_resume_refuses_when_a_reviewer_prompt_changed(tmp_path):
    """Editing a prompt changes the findings as surely as changing the model does."""
    review(tmp_path, sections(*THREE))

    edited = load_prompts()
    verifier = edited.reviewers["FormalVerifier"]
    edited.reviewers["FormalVerifier"] = replace(
        verifier, prompt_text=verifier.prompt_text + "\nAlso flag split infinitives.\n"
    )

    with pytest.raises(ConfigurationError, match="the reviewer prompts have changed"):
        run_pipeline(
            registry(FakeProvider(issues_per_call=1)),
            str(tmp_path / "paper.tex"),
            tmp_path / "out",
            prompts=edited,
            assume_yes=True,
        )


def test_resume_refuses_when_the_models_changed(tmp_path):
    review(tmp_path, sections(*THREE))
    with pytest.raises(ConfigurationError, match="different settings"):
        review(
            tmp_path,
            sections(*THREE),
            models={"strong_model": "anthropic:claude-opus-4-7"},
        )


def test_resume_refuses_old_bare_anthropic_state_as_different_settings(tmp_path):
    review(tmp_path, sections(*THREE))
    state = tmp_path / "out" / "state.json"
    state.write_text(state.read_text(encoding="utf-8").replace("anthropic:", ""), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="different settings"):
        review(tmp_path, sections(*THREE))


def test_resume_refuses_when_the_provider_changes(tmp_path):
    review(tmp_path, sections(*THREE))
    source = tmp_path / "paper.tex"
    prompts = load_prompts(strong_model="openai:gpt-5.6-sol")
    with pytest.raises(ConfigurationError, match="different settings"):
        run_pipeline(
            registry(FakeProvider(), FakeOpenAIProvider()),
            str(source),
            tmp_path / "out",
            prompts=prompts,
            assume_yes=True,
        )


def test_resume_refuses_when_effort_changed(tmp_path):
    review(tmp_path, sections(*THREE))
    with pytest.raises(ConfigurationError, match="different settings"):
        review(tmp_path, sections(*THREE), effort="low")


def test_resume_recovers_issues_lost_from_the_append_only_log(tmp_path):
    """A crash between writing a review and appending to issues.jsonl must not lose
    findings: the stored reviews are the source of truth."""
    review(tmp_path, sections(*THREE))
    log = tmp_path / "out" / "issues.jsonl"
    log.write_text("".join(log.read_text().splitlines(keepends=True)[:3]), encoding="utf-8")

    client, result = review(tmp_path, sections(*THREE))
    assert client.parse_calls == []
    assert len(result.issues) == THREE_CALLS


def test_resume_re_runs_a_reviewer_whose_stored_review_is_corrupt(tmp_path):
    review(tmp_path, sections(*THREE))
    stored = sorted((tmp_path / "out" / "reviews").glob("*.json"))[0]
    stored.write_text("{ not json", encoding="utf-8")

    client, result = review(tmp_path, sections(*THREE))
    assert len(client.parse_calls) == 1
    assert len(result.issues) == THREE_CALLS


def test_the_coverage_note_is_not_filed_under_detected_issues(tmp_path):
    """It reports failed reviewer calls; nested in the issue list it reads as a finding."""
    report = run_final_referee(
        providers=registry(client := FakeProvider()),
        tex="\\section{One}\nx\n",
        issues=[issue("major")],
        global_context="ctx",
        system_prompt="referee",
        model=ModelRef.parse("anthropic:strong"),
        coverage_note=format_coverage_note([Failure("Intro", "FormalVerifier", "boom")]),
    )
    assert report.startswith("# Summary")

    (sent,) = client.create_calls
    blocks = [block.text for block in sent.blocks if block.role == "user"]
    assert "COVERAGE GAPS" in blocks[0]
    assert blocks[1].startswith("# DETECTED ISSUES")
    assert "COVERAGE GAPS" not in blocks[1]
    assert blocks[2].startswith("# FULL PAPER")


def test_dry_run_counts_the_paper_reviewers_once_not_once_per_section(tmp_path, capsys):
    source = tmp_path / "paper.tex"
    source.write_text(sections(*THREE), encoding="utf-8")
    client = FakeProvider()

    run_dry_run(registry(client), str(source), prompts=load_prompts())

    out = capsys.readouterr().out
    for name in ("NotationAuditor", "ClaimAuditor"):
        assert out.count(name) == 1
    assert out.count("FormalVerifier") == 3
    # Three sections x three section reviewers, two paper passes, one final referee.
    assert len(client.count_calls) == THREE_CALLS + 1


def test_dry_run_groups_counts_by_qualified_model_and_marks_unknown_pricing(tmp_path, capsys):
    source = tmp_path / "paper.tex"
    source.write_text(sections(("Alpha", "aaa")), encoding="utf-8")
    anthropic_provider = FakeProvider()
    openai_provider = FakeOpenAIProvider()
    prompts = load_prompts(strong_model="openai:gpt-5.6-sol", fast_model="anthropic:future-fast")

    run_dry_run(registry(anthropic_provider, openai_provider), str(source), prompts=prompts)

    out = capsys.readouterr().out
    assert "openai:gpt-5.6-sol" in out
    assert "anthropic:future-fast" in out
    assert "pricing unknown" in out


def test_reviews_without_a_state_file_are_refused_rather_than_ignored(tmp_path):
    review(tmp_path, sections(*THREE))
    (tmp_path / "out" / "state.json").unlink()
    with pytest.raises(ConfigurationError, match="no record of what produced them"):
        review(tmp_path, sections(*THREE))


def test_a_typo_in_the_input_path_creates_no_output_directory(tmp_path):
    out = tmp_path / "out"
    with pytest.raises(ConfigurationError):
        run_pipeline(
            registry(FakeProvider()),
            str(tmp_path / "nope.tex"),
            out,
            prompts=load_prompts(),
            assume_yes=True,
        )
    assert not out.exists()


def test_declining_the_confirmation_stops_before_any_call(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    source = tmp_path / "paper.tex"
    source.write_text(sections(*THREE), encoding="utf-8")
    client = FakeProvider(issues_per_call=1)

    result = run_pipeline(registry(client), str(source), tmp_path / "out", prompts=load_prompts())

    assert result.aborted is True
    assert client.parse_calls == []
