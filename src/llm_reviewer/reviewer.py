from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import anthropic
from pydantic import BaseModel
from rich import print

DEFAULT_MODEL_STRONG = "claude-opus-5"
DEFAULT_MODEL_FAST = "claude-sonnet-5"

# The SDK refuses a non-streaming request whose estimated duration exceeds ten minutes,
# which works out at max_tokens > 21_333 (see _calculate_nonstreaming_timeout in
# anthropic/_base_client.py). Staying under that keeps every call non-streaming.
MAX_NONSTREAMING_TOKENS = 21_333
DEFAULT_MAX_TOKENS = 16_000

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"

# On these models an explicitly disabled thinking config is rejected at the top two
# effort levels. Reviewers that run without thinking therefore cannot use them there.
THINKING_DISABLED_EFFORT_CAPPED = frozenset({"claude-opus-5"})
EFFORT_LEVELS_REJECTING_DISABLED_THINKING = frozenset({"xhigh", "max"})

# The two thinking configurations the pipeline uses, spelled out once.
THINKING_ADAPTIVE: anthropic.types.ThinkingConfigParam = {"type": "adaptive"}
THINKING_DISABLED: anthropic.types.ThinkingConfigParam = {"type": "disabled"}

# USD per million tokens. Cache reads are 0.1x the input rate.
# Models absent from this table still work; only cost estimation is unavailable.
# Sonnet 5 is listed at its standard rate; an introductory $2/$10 runs to 2026-08-31,
# so estimates are deliberately conservative until then.
MODEL_PRICING = {
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1},
}

ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"

FINAL_REFEREE_PROMPT = "final_referee.md"

# Section titles (case-insensitive, LaTeX-stripped) that are skipped during review.
_SKIP_SECTIONS = frozenset({"references", "bibliography", "acknowledgments", "acknowledgements"})

# Matches \section{...} and \section*{...} headings.
# Hacky: at most one level of nested braces in section titles is supported,
# which is typically enough.
SECTION_RE = re.compile(r"\\section\*?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")

# Matches LaTeX commands: \cmd{text} → text, or bare \cmd → "".
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\{([^{}]*)\}|\\[a-zA-Z]+")

# Matches \input{...} and \include{...} file references.
INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]+)\}")

# How deep \input chains may nest before we stop expanding them.
MAX_INPUT_DEPTH = 10

# Material between \begin{document} and the first \section is reviewed as its own
# chunk, but only when it holds this much prose beyond the abstract and title.
FRONT_MATTER_MIN_CHARS = 500
FRONT_MATTER_NAME = "Front matter"


CREDENTIALS_HELP = (
    "No usable Anthropic credentials. Either export ANTHROPIC_API_KEY, or sign in with "
    "`ant auth login` and the SDK will pick up the stored profile."
)

# Errors that will recur identically on every subsequent call, so there is no point
# continuing the run: a rejected key, a revoked permission, an unknown model ID.
FATAL_API_ERRORS = (
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
)


class ConfigurationError(Exception):
    """A problem with the input, environment, or invocation — not an API failure."""


@dataclass
class Chunk:
    """A named slice of a LaTeX document corresponding to one \\section{}."""

    name: str  # section title extracted from \section{...}
    content: str  # raw LaTeX from this section's heading to the next one


class SeverityLevel(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MODERATE = "moderate"
    MINOR = "minor"

    @property
    def numerical_level(self) -> int:
        order = {
            SeverityLevel.CRITICAL: 4,
            SeverityLevel.MAJOR: 3,
            SeverityLevel.MODERATE: 2,
            SeverityLevel.MINOR: 1,
        }
        return order[self]


class Issue(BaseModel):
    title: str
    severity: SeverityLevel
    type: str
    location: str
    quote: str
    analysis: str
    suggested_fix: str
    confidence: float


class IssueWithReviewer(Issue):
    reviewer: str


def attribute_issue(issue: Issue, reviewer_name: str) -> IssueWithReviewer:
    """Return a copy of *issue* with the *reviewer* field set to *reviewer_name*."""
    return IssueWithReviewer(**issue.model_dump(), reviewer=reviewer_name)


class Review(BaseModel):
    issues: list[Issue]


@dataclass(frozen=True)
class ReviewerSpec:
    """Static description of a reviewer, independent of which models are in use."""

    prompt: str  # filename under prompts/
    tier: str  # "strong" or "fast"
    thinking: bool  # whether this reviewer's task benefits from extended thinking


@dataclass(frozen=True)
class ReviewerConfig:
    """A reviewer resolved against a concrete model, with its prompt text loaded."""

    name: str
    model: str
    prompt_text: str
    thinking: bool

    @property
    def thinking_config(self) -> anthropic.types.ThinkingConfigParam:
        return THINKING_ADAPTIVE if self.thinking else THINKING_DISABLED


@dataclass(frozen=True)
class LoadedPrompts:
    """All prompt text the pipeline needs, read and validated before any spending."""

    reviewers: dict[str, ReviewerConfig]
    final_referee: str


@dataclass
class ReviewerFailure:
    """One reviewer call that failed, recorded so the run can report its own gaps."""

    section: str
    reviewer: str
    error: str


@dataclass
class PipelineResult:
    output_dir: Path
    issues: list[IssueWithReviewer] = field(default_factory=list)
    failures: list[ReviewerFailure] = field(default_factory=list)
    report_path: Path | None = None
    aborted: bool = False
    # Set when the run stopped early because every remaining call would fail the same way.
    fatal_error: str | None = None


# The tier is resolved to a concrete model ID by load_prompts().
#
# Thinking is decided per reviewer, on the shape of its task rather than its tier:
# judging whether a proof step follows, or building a counterexample, is multi-step
# reasoning; checking that a symbol was defined or a \ref resolves is scanning and
# matching, and gains nothing from thinking but costs tokens and latency for it.
REVIEWER_SPECS: dict[str, ReviewerSpec] = {
    "FormalVerifier": ReviewerSpec("formal_verifier.md", "strong", thinking=True),
    "AdversarialSkeptic": ReviewerSpec("adversarial_skeptic.md", "strong", thinking=True),
    "NotationAuditor": ReviewerSpec("notation_auditor.md", "fast", thinking=False),
    "ExpositionReferee": ReviewerSpec("exposition_referee.md", "fast", thinking=False),
}


def _read_prompt(filename: str) -> str:
    path = PROMPTS / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read prompt file {path}: {exc}") from exc


def load_prompts(
    strong_model: str = DEFAULT_MODEL_STRONG,
    fast_model: str = DEFAULT_MODEL_FAST,
) -> LoadedPrompts:
    """Read every prompt file up front so a missing one fails before any API spending."""
    models = {"strong": strong_model, "fast": fast_model}
    reviewers = {
        name: ReviewerConfig(
            name=name,
            model=models[spec.tier],
            prompt_text=_read_prompt(spec.prompt),
            thinking=spec.thinking,
        )
        for name, spec in REVIEWER_SPECS.items()
    }
    return LoadedPrompts(reviewers=reviewers, final_referee=_read_prompt(FINAL_REFEREE_PROMPT))


def validate_settings(prompts: LoadedPrompts, max_tokens: int, effort: str) -> None:
    """Reject invocations the API would refuse, before any request is made."""
    if max_tokens < 1:
        raise ConfigurationError("--max-tokens must be a positive integer.")
    if max_tokens > MAX_NONSTREAMING_TOKENS:
        raise ConfigurationError(
            f"--max-tokens is capped at {MAX_NONSTREAMING_TOKENS:,}: above that the SDK "
            "requires streaming, which this pipeline does not use."
        )
    if effort not in EFFORT_LEVELS:
        raise ConfigurationError(
            f"--effort must be one of {', '.join(EFFORT_LEVELS)}; got {effort!r}."
        )

    if effort in EFFORT_LEVELS_REJECTING_DISABLED_THINKING:
        blocked = sorted(
            reviewer.name
            for reviewer in prompts.reviewers.values()
            if not reviewer.thinking and reviewer.model in THINKING_DISABLED_EFFORT_CAPPED
        )
        if blocked:
            raise ConfigurationError(
                f"{', '.join(blocked)} run without thinking, which "
                f"{sorted(THINKING_DISABLED_EFFORT_CAPPED)[0]} rejects at --effort "
                f"{effort}. Use --effort high or lower, or give those reviewers a "
                "different --fast-model."
            )


def check_access(client: anthropic.Anthropic, models: list[str]) -> None:
    """Verify credentials and every model ID before the run spends anything.

    Token counting is free, so this costs one round trip per distinct model and turns
    what would otherwise be dozens of identical mid-run failures into one clear message.
    """
    for model in sorted(set(models)):
        try:
            client.messages.count_tokens(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
            )
        except TypeError as exc:
            # The SDK raises a bare TypeError when it cannot resolve any credential.
            raise ConfigurationError(CREDENTIALS_HELP) from exc
        except anthropic.AuthenticationError as exc:
            raise ConfigurationError(
                f"{CREDENTIALS_HELP} The credential that was found was rejected: {exc}"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise ConfigurationError(
                f"These credentials are not allowed to use {model!r}: {exc}"
            ) from exc
        except anthropic.NotFoundError as exc:
            raise ConfigurationError(
                f"Model {model!r} is not available to this account. Check the ID against "
                f"https://platform.claude.com/docs/en/about-claude/models/overview ({exc})"
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ConfigurationError(f"Cannot reach the Anthropic API: {exc}") from exc


def _safe_filename(name: str) -> str:
    """Strip LaTeX commands and replace path-unsafe characters for use as a filename stem."""
    plain = _LATEX_CMD_RE.sub(lambda m: m.group(1) or "", name)
    safe = re.sub(r"[^\w\-]", "_", plain)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:80] or "section"


def chunk_stem(index: int, chunk: Chunk) -> str:
    """Filename stem for a chunk, prefixed by position so distinct sections never collide."""
    return f"{index:02d}_{_safe_filename(chunk.name)}"


def extract_text(response) -> str:
    """Concatenate all text blocks from a Claude response, warning on unexpected block types."""
    texts = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type in ("thinking", "redacted_thinking"):
            # Expected whenever thinking is on; the report itself is in the text blocks.
            continue
        else:
            print(f"[yellow]Unexpected content block type {block.type!r}: {block}[/yellow]")
    if not texts:
        raise ValueError(f"No text block in response (stop_reason={response.stop_reason!r})")
    if response.stop_reason == "max_tokens":
        print("[yellow]Warning: response was truncated (stop_reason=max_tokens).[/yellow]")
    return "".join(texts)


def _call_with_retry(fn, retries: int = 3, base_delay: float = 5.0):
    """Call *fn* with exponential-backoff retries on transient API and server errors."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except (
            anthropic.RateLimitError,
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
        ) as exc:
            if attempt == retries:
                raise
            delay = base_delay * (2**attempt)
            print(
                f"[yellow]Transient error ({exc.__class__.__name__}), "
                f"retrying in {delay:.0f}s…[/yellow]"
            )
            time.sleep(delay)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 and attempt < retries:
                delay = base_delay * (2**attempt)
                print(f"[yellow]Server error {exc.status_code}, retrying in {delay:.0f}s…[/yellow]")
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("unreachable")


def strip_comment(line: str) -> str:
    """Return the part of *line* before its first unescaped % (a LaTeX comment)."""
    out = []
    i = 0
    while i < len(line):
        char = line[i]
        if char == "\\" and i + 1 < len(line):
            out.append(line[i : i + 2])
            i += 2
            continue
        if char == "%":
            break
        out.append(char)
        i += 1
    return "".join(out)


def _split_comment(line: str) -> tuple[str, str]:
    """Split *line* into its code part and its trailing comment (which may be empty)."""
    code = strip_comment(line)
    return code, line[len(code) :]


def _resolve_reference(reference: str, search_dirs: list[Path]) -> Path | None:
    """Locate the file an \\input/\\include reference points at, trying .tex if bare."""
    reference = reference.strip()
    candidates = [reference]
    if not Path(reference).suffix:
        candidates.append(reference + ".tex")
    for directory in search_dirs:
        for candidate in candidates:
            path = directory / candidate
            if path.is_file():
                return path
    return None


def resolve_inputs(
    path: Path | str,
    root: Path | None = None,
    _seen: frozenset[Path] | None = None,
    _depth: int = 0,
) -> str:
    """Read a .tex file and inline every \\input{} and \\include{} it references.

    References resolve relative to *root* (the main document's directory, mirroring how
    LaTeX searches) and then relative to the including file. Commented-out references are
    ignored, cycles are broken, and a reference that cannot be found is left in place with
    a warning rather than aborting the run.
    """
    path = Path(path)
    root = path.parent if root is None else Path(root)
    seen = frozenset() if _seen is None else _seen

    text = path.read_text(encoding="utf-8")
    if _depth >= MAX_INPUT_DEPTH:
        print(
            f"[yellow]Warning: \\input nesting deeper than {MAX_INPUT_DEPTH} at {path}; "
            "not expanding further.[/yellow]"
        )
        return text

    seen = seen | {path.resolve()}
    search_dirs = [root, path.parent]

    def expand(match: re.Match[str]) -> str:
        reference = match.group(1)
        target = _resolve_reference(reference, search_dirs)
        if target is None:
            print(
                f"[yellow]Warning: cannot find {reference!r} referenced from {path}; "
                "leaving the reference unexpanded.[/yellow]"
            )
            return match.group(0)
        if target.resolve() in seen:
            print(f"[yellow]Warning: circular \\input of {target} from {path}; skipping.[/yellow]")
            return ""
        return resolve_inputs(target, root=root, _seen=seen, _depth=_depth + 1)

    out = []
    for line in text.splitlines(keepends=True):
        code, comment = _split_comment(line)
        out.append(INPUT_RE.sub(expand, code) + comment)
    return "".join(out)


def load_tex(path: str) -> str:
    """Read a .tex file, inlining any \\input{}/\\include{} it references."""
    return resolve_inputs(path)


# Matches \begin{abstract}...\end{abstract} across newlines.
ABSTRACT_RE = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)

# Matches \begin{theorem}...\end{theorem} and \begin{definition}...\end{definition} across newlines.
THEOREM_DEFINITION_RE = re.compile(
    r"\\begin\{(theorem|definition)\}(.*?)\\end\{\1\}",
    re.DOTALL,
)

PREAMBLE_RE = re.compile(
    r"^(.*?)\\begin\{document\}",
    re.DOTALL,
)

# One level of nested braces, so a title containing e.g. \mathbb{Z} still matches.
TITLE_RE = re.compile(
    r"\\title\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
)

BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}")


def _front_matter(tex: str, first_section_start: int) -> str | None:
    """Return the body text before the first \\section, if it holds enough real prose."""
    match = BEGIN_DOCUMENT_RE.search(tex)
    body_start = match.end() if match else 0
    if body_start >= first_section_start:
        return None

    front = tex[body_start:first_section_start]
    # The title and abstract already reach reviewers through the global context, so they
    # should not on their own justify an extra chunk.
    probe = ABSTRACT_RE.sub("", front)
    probe = re.sub(r"\\(?:maketitle|tableofcontents)\b", "", probe)
    if len(probe.strip()) < FRONT_MATTER_MIN_CHARS:
        return None
    return front


def chunk_by_section(tex: str) -> list[Chunk]:
    """Split a LaTeX string into Chunks at each \\section boundary.

    Non-mathematical sections are skipped, and body text preceding the first section is
    kept as a leading chunk when it holds enough prose to be worth reviewing.
    """
    matches = list(SECTION_RE.finditer(tex))
    chunks = []

    if matches:
        front = _front_matter(tex, matches[0].start())
        if front is not None:
            chunks.append(Chunk(name=FRONT_MATTER_NAME, content=front))

    for i, match in enumerate(matches):
        title = match.group(1)
        # Strip LaTeX commands and normalize for skip-list comparison.
        plain = _LATEX_CMD_RE.sub(lambda m: m.group(1) or "", title).strip().lower()
        if plain in _SKIP_SECTIONS:
            continue
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tex)
        chunks.append(Chunk(name=title, content=tex[start:end]))
    return chunks


def extract_global_context(tex: str) -> str:
    """Extract the abstract, preamble, and up to 15 theorems/definitions as shared context."""
    parts = []

    title_match = TITLE_RE.search(tex)
    if title_match:
        title = title_match.group(1).strip()
        if title:
            parts.insert(0, f"# PAPER TITLE\n\n{title}")

    abstract_match = ABSTRACT_RE.search(tex)
    if abstract_match:
        parts.append("# ABSTRACT\n")
        parts.append(abstract_match.group(1))

    preamble_match = PREAMBLE_RE.search(tex)
    if preamble_match:
        preamble = preamble_match.group(1).strip()
        if preamble:
            parts.append("\n# PREAMBLE\n")
            parts.append(preamble)

    theorem_parts = []
    for i, match in enumerate(THEOREM_DEFINITION_RE.finditer(tex)):
        if i >= 15:
            break
        env_type = match.group(1).capitalize()
        content = match.group(2).strip()
        theorem_parts.append(f"## {env_type} {i + 1}\n\n{content}")

    if theorem_parts:
        parts.append("\n# MAIN THEOREMS AND DEFINITIONS\n")
        parts.extend(theorem_parts)

    return "\n\n".join(parts)


def load_known_issues(issues_file: Path) -> list[IssueWithReviewer]:
    """Load all issues from a JSONL file; returns an empty list if the file does not exist."""
    if not issues_file.exists():
        return []
    issues = []
    with open(issues_file, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                issues.append(IssueWithReviewer.model_validate_json(line))
            except ValueError as exc:
                raise ConfigurationError(
                    f"{issues_file}:{lineno} is not a valid issue record: {exc}. "
                    "Delete the output directory to start a fresh run."
                ) from exc
    return issues


def _format_issue_full(iss: IssueWithReviewer) -> str:
    return (
        f"- **[{iss.reviewer} - {iss.severity.upper()}] {iss.title}**\n"
        f"  *Location:* {iss.location}\n"
        f"  *Analysis:* {iss.analysis}\n"
        f"  *Fix:* {iss.suggested_fix}\n"
    )


def format_all_issues(issues: list[IssueWithReviewer]) -> str:
    if not issues:
        return "No previously detected issues."
    return "\n".join(_format_issue_full(iss) for iss in issues)


def summarize_known_issues(issues: list[IssueWithReviewer], limit: int = 15) -> str:
    """Format the highest-severity known issues for injection into reviewer prompts."""
    if not issues:
        return "No previously detected issues."
    sorted_issues = sorted(issues, key=lambda iss: iss.severity.numerical_level, reverse=True)
    return "\n".join(
        f"- [{iss.reviewer} - {iss.severity.upper()}] {iss.location}: {iss.title}"
        for iss in sorted_issues[:limit]
    )


def format_coverage_note(failures: list[ReviewerFailure]) -> str:
    """Describe reviewer passes that failed, so the final report can state its own gaps."""
    if not failures:
        return ""
    lines = [
        "# COVERAGE GAPS",
        "",
        "The following reviewer passes failed, so their findings are missing from the",
        "issue list below. Say so explicitly in the Summary section of your report and",
        "name the affected sections.",
        "",
    ]
    lines += [
        f"- {failure.reviewer} on section '{failure.section}': {failure.error}"
        for failure in failures
    ]
    return "\n".join(lines) + "\n\n"


def _build_system(global_context: str, system_prompt: str) -> list[anthropic.types.TextBlockParam]:
    """Build the system prompt list with cache breakpoints on both stable blocks."""
    return [
        {
            "type": "text",
            "text": f"# GLOBAL CONTEXT\n{global_context}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "# REVIEWER PROMPT\n" + system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _build_messages(known_issues: str, to_review: str) -> list[anthropic.types.MessageParam]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"# DETECTED ISSUES\n{known_issues}"},
                {"type": "text", "text": to_review},
            ],
        }
    ]


def _api_call_with_schema(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
    max_tokens: int,
    thinking: anthropic.types.ThinkingConfigParam,
    effort: str,
):
    # The SDK merges output_format into output_config, so passing both is supported.
    return client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=_build_system(global_context, system_prompt),
        messages=_build_messages(known_issues, to_review),
        thinking=thinking,
        output_config={"effort": effort},
        output_format=Review,
    )


def _api_call(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
    max_tokens: int,
    thinking: anthropic.types.ThinkingConfigParam,
    effort: str,
):
    return client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_build_system(global_context, system_prompt),
        messages=_build_messages(known_issues, to_review),
        thinking=thinking,
        output_config={"effort": effort},
    )


def _count_tokens(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
    with_schema: bool,
) -> int:
    """Return the input-token count for one API call without generating a response."""
    return client.messages.count_tokens(
        model=model,
        system=_build_system(global_context, system_prompt),
        messages=_build_messages(known_issues, to_review),
        output_format=Review if with_schema else anthropic.omit,
    ).input_tokens


def _format_section(chunk: Chunk) -> str:
    """Format a chunk as the 'section under review' block passed to every reviewer prompt."""
    return (
        f"# SECTION UNDER REVIEW\n\nSECTION TITLE: {chunk.name}\n\n```latex\n{chunk.content}\n```"
    )


def call_reviewer(
    client: anthropic.Anthropic,
    reviewer: ReviewerConfig,
    chunk: Chunk,
    global_context: str,
    known_issues: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
):
    """Send a section chunk to a reviewer and return its parsed JSON issue report."""
    response = _call_with_retry(
        lambda: _api_call_with_schema(
            client=client,
            model=reviewer.model,
            system_prompt=reviewer.prompt_text,
            global_context=global_context,
            known_issues=known_issues,
            to_review=_format_section(chunk),
            max_tokens=max_tokens,
            thinking=reviewer.thinking_config,
            effort=effort,
        )
    )

    if not response or not response.parsed_output:
        # Thinking shares the max_tokens budget with the response, so a truncated reply
        # is a realistic failure rather than a theoretical one. Say which it was.
        if response is not None and response.stop_reason == "max_tokens":
            raise ValueError(
                f"{reviewer.name} hit the {max_tokens:,}-token limit on section "
                f"'{chunk.name}' before finishing its JSON. Raise --max-tokens (up to "
                f"{MAX_NONSTREAMING_TOKENS:,}) or lower --effort."
            )
        raise ValueError(
            f"No parsed output from reviewer {reviewer.name} on section '{chunk.name}'"
        )

    return response.parsed_output


def append_issues(issues: list[IssueWithReviewer], issues_file: Path) -> None:
    """Append each issue as a JSON line to the JSONL issues file."""
    with open(issues_file, "a", encoding="utf-8") as f:
        for issue in issues:
            f.write(issue.model_dump_json() + "\n")


def run_final_referee(
    client: anthropic.Anthropic,
    tex: str,
    issues: list[IssueWithReviewer],
    global_context: str,
    system_prompt: str,
    model: str = DEFAULT_MODEL_STRONG,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    coverage_note: str = "",
) -> str:
    """Synthesize a markdown referee report from the paper and the collected issues."""
    # This pass weighs and prioritises every finding across the whole paper, so it
    # thinks — and it runs once per review, so the extra cost is marginal.
    response = _call_with_retry(
        lambda: _api_call(
            client=client,
            model=model,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=coverage_note + format_all_issues(issues),
            to_review=f"# FULL PAPER\n```latex\n{tex}\n```",
            max_tokens=max_tokens,
            thinking=THINKING_ADAPTIVE,
            effort=effort,
        )
    )

    return extract_text(response)


def _prepare(tex_path: str) -> tuple[str, list[Chunk], str]:
    """Load a .tex file, split it into section chunks, and extract global context."""
    try:
        tex = load_tex(tex_path)
    except OSError as exc:
        raise ConfigurationError(f"Cannot read {tex_path}: {exc}") from exc
    chunks = chunk_by_section(tex)
    if not chunks:
        raise ConfigurationError(
            f"No reviewable sections found in {tex_path}. The pipeline splits on "
            "\\section{...}; a document using only \\chapter or \\subsection, or one whose "
            "sections all fall in the skip list (references, bibliography, "
            "acknowledgments), leaves nothing to review."
        )
    global_context = extract_global_context(tex)
    return tex, chunks, global_context


def _confirm(assume_yes: bool) -> bool:
    """Ask the user to confirm the run, or fail loudly when there is nobody to ask."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise ConfigurationError(
            "Refusing to start a paid run without confirmation: stdin is not a terminal. "
            "Pass --yes to run non-interactively."
        )
    print("Continue? (y/N)")
    return input().strip().lower() in ("y", "yes")


def run_pipeline(
    client: anthropic.Anthropic,
    tex_path: str,
    output_dir: Path | str | None = None,
    prompts: LoadedPrompts | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    assume_yes: bool = False,
) -> PipelineResult:
    """Run the full multi-reviewer pipeline on a .tex file and write all outputs to *output_dir*."""
    prompts = load_prompts() if prompts is None else prompts

    output_dir = Path(tex_path).parent / "review" if output_dir is None else Path(output_dir)
    reviews_dir = output_dir / "reviews"
    issues_file = output_dir / "issues.jsonl"
    chunks_dir = output_dir / "chunks"

    output_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(exist_ok=True)
    chunks_dir.mkdir(exist_ok=True)
    tex, chunks, global_context = _prepare(tex_path)

    result = PipelineResult(output_dir=output_dir)

    for index, chunk in enumerate(chunks):
        chunk_path = chunks_dir / f"{chunk_stem(index, chunk)}.tex"
        chunk_path.write_text(chunk.content, encoding="utf-8")
        print(
            f"[green]Extracted chunk:[/green] {chunk.name} "
            f"(written to {chunk_path}, {len(chunk.content)} chars)"
        )

    preview = global_context[:500]
    ellipsis = "…" if len(global_context) > 500 else ""
    print(
        f"[green]Extracted global context ({len(global_context)} chars):[/green]\n"
        f"[gray]{preview}{ellipsis}[/gray]"
    )

    if not _confirm(assume_yes):
        print("[red]Aborting review.[/red]")
        result.aborted = True
        return result

    print("[bold blue]Starting multi-reviewer analysis...[/bold blue]")

    all_issues: list[IssueWithReviewer] = load_known_issues(issues_file)
    for index, chunk in enumerate(chunks):
        if result.fatal_error is not None:
            break
        print(f"[bold blue]Reviewing:[/bold blue] {chunk.name}")
        known_issues = summarize_known_issues(all_issues)
        stem = chunk_stem(index, chunk)

        for reviewer_name, reviewer in prompts.reviewers.items():
            out_path = reviews_dir / f"{stem}_{reviewer_name}.json"

            if out_path.exists():
                print(f"  -> {reviewer_name} [yellow](resuming from disk)[/yellow]")
                continue

            print(f"  -> {reviewer_name}")

            try:
                review = call_reviewer(
                    client=client,
                    reviewer=reviewer,
                    chunk=chunk,
                    global_context=global_context,
                    known_issues=known_issues,
                    max_tokens=max_tokens,
                    effort=effort,
                )
            except FATAL_API_ERRORS as exc:
                # Credentials or the model itself are wrong, so every remaining call
                # would fail identically. Stop rather than burn through the whole paper.
                print(f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]")
                result.fatal_error = (
                    f"Aborting: {type(exc).__name__} from the API means every remaining "
                    f"call would fail the same way. {exc}"
                )
                break
            except Exception as exc:
                print(f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]")
                result.failures.append(
                    ReviewerFailure(section=chunk.name, reviewer=reviewer_name, error=str(exc))
                )
                continue

            print(f"  [green]Success: {len(review.issues)} issues detected.[/green]")

            attributed_issues = [attribute_issue(iss, reviewer_name) for iss in review.issues]

            # Write per-chunk JSON first so it acts as the authoritative resume marker.
            # If the process dies between here and append_issues, the JSON exists but
            # issues.jsonl is incomplete — on resume the JSON check prevents re-running
            # the reviewer, yet those issues were never appended. This is preferable to
            # the reverse order, which causes duplicate issues on resume.
            out_path.write_text(review.model_dump_json(indent=2), encoding="utf-8")
            append_issues(attributed_issues, issues_file)
            all_issues.extend(attributed_issues)

    result.issues = all_issues

    all_issues_path = output_dir / "all_issues.json"
    all_issues_path.write_text(
        json.dumps([i.model_dump(mode="json") for i in all_issues], indent=2),
        encoding="utf-8",
    )

    if result.fatal_error is not None:
        print(f"\n[red]{result.fatal_error}[/red]")
        print(
            f"[yellow]Issues collected so far are preserved in {all_issues_path}. "
            "Fix the problem and re-run the same command to resume.[/yellow]"
        )
        return result

    if result.failures:
        print(
            f"\n[bold yellow]{len(result.failures)} reviewer call(s) failed; the report "
            "below is based on incomplete coverage:[/bold yellow]"
        )
        for failure in result.failures:
            print(
                f"  [yellow]- {failure.reviewer} on '{failure.section}': {failure.error}[/yellow]"
            )

    print("[bold blue]Running final referee synthesis...[/bold blue]")
    try:
        final_report = run_final_referee(
            client=client,
            tex=tex,
            issues=all_issues,
            global_context=global_context,
            system_prompt=prompts.final_referee,
            model=prompts.reviewers["FormalVerifier"].model,
            max_tokens=max_tokens,
            effort=effort,
            coverage_note=format_coverage_note(result.failures),
        )
    except Exception as exc:
        print(f"[red]ERROR: final referee synthesis failed: {exc}[/red]")
        print(
            f"[yellow]Collected issues are preserved in {all_issues_path}; "
            "re-run the same command to retry the synthesis.[/yellow]"
        )
        result.failures.append(
            ReviewerFailure(section="(whole paper)", reviewer="FinalReferee", error=str(exc))
        )
        return result

    final_report_path = output_dir / "final_report.md"
    final_report_path.write_text(final_report, encoding="utf-8")
    result.report_path = final_report_path

    print("\n[bold green]Review complete.[/bold green]")
    print(f"Issues detected: {len(all_issues)}")

    return result


def run_dry_run(
    client: anthropic.Anthropic,
    tex_path: str,
    prompts: LoadedPrompts | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,  # noqa: ARG001 - does not affect input token counts
) -> None:
    """Count input tokens and estimate cost without sending any generation requests."""
    prompts = load_prompts() if prompts is None else prompts

    print("[yellow]DRY RUN — token counting only, no generation requests will be sent.[/yellow]")
    print(
        "[yellow]Warning: rough lower-bound estimate. The 'known issues' context fed to each "
        "reviewer (which grows as earlier reviewers produce output) is not counted here "
        "because it depends on actual generation. Real input cost will also be lower than "
        "shown once prompt caching kicks in (cache reads are 90 % cheaper).[/yellow]\n"
    )

    tex, chunks, global_context = _prepare(tex_path)
    empty_known_issues = summarize_known_issues([])

    token_totals: dict[str, int] = {}
    call_counts: dict[str, int] = {}

    def record(model: str, tokens: int) -> None:
        token_totals[model] = token_totals.get(model, 0) + tokens
        call_counts[model] = call_counts.get(model, 0) + 1

    for chunk in chunks:
        for reviewer_name, reviewer in prompts.reviewers.items():
            n = _count_tokens(
                client,
                reviewer.model,
                reviewer.prompt_text,
                global_context,
                empty_known_issues,
                _format_section(chunk),
                with_schema=True,
            )
            record(reviewer.model, n)
            print(f"  [blue]{chunk.name}[/blue] / [cyan]{reviewer_name}[/cyan]: {n:,} input tokens")

    # The final referee runs on the same model as the strong reviewers.
    final_model = prompts.reviewers["FormalVerifier"].model
    n = _count_tokens(
        client,
        final_model,
        prompts.final_referee,
        global_context,
        "[]",  # no issues available in dry run
        f"# FULL PAPER\n```latex\n{tex}\n```",
        with_schema=False,
    )
    record(final_model, n)
    print(f"  [blue]Final referee[/blue]: {n:,} input tokens")

    total_input = sum(token_totals.values())
    unpriced = sorted(model for model in token_totals if model not in MODEL_PRICING)

    input_cost = sum(
        count / 1e6 * MODEL_PRICING[model]["input"]
        for model, count in token_totals.items()
        if model in MODEL_PRICING
    )
    max_output_cost = sum(
        count * max_tokens / 1e6 * MODEL_PRICING[model]["output"]
        for model, count in call_counts.items()
        if model in MODEL_PRICING
    )

    print("\n[bold]Input tokens by model (worst-case, no cache hits):[/bold]")
    for model, count in token_totals.items():
        pricing = MODEL_PRICING.get(model)
        if pricing is None:
            print(f"  {model}: {count:,} tokens [yellow](pricing unknown)[/yellow]")
            continue
        rate = pricing["input"]
        cache_rate = pricing["cache_read"]
        print(
            f"  {model}: {count:,} tokens @ ${rate}/M = ${count / 1e6 * rate:.2f} "
            f"(cached reads: ${cache_rate}/M = ${count / 1e6 * cache_rate:.2f})"
        )
    print(f"  Total input: {total_input:,} tokens")

    if unpriced:
        print(
            "\n[yellow]No pricing on file for: "
            + ", ".join(unpriced)
            + ". The token counts above are complete, but the cost figures below exclude "
            "these models. Current rates: https://platform.claude.com/docs/en/pricing[/yellow]"
        )

    label = "Estimated input cost (no cache)"
    if unpriced:
        label += ", priced models only"
    print(f"[bold green]{label}: ${input_cost:.2f}[/bold green]")
    print(
        f"[bold green]Max output cost: ${max_output_cost:.2f}[/bold green]  "
        f"(a ceiling nobody reaches: it assumes all {sum(call_counts.values())} calls "
        f"emit the full {max_tokens:,} output tokens, and two of the four reviewers "
        "run without thinking)"
    )
    print(f"[bold green]Max total cost: ${input_cost + max_output_cost:.2f}[/bold green]")
