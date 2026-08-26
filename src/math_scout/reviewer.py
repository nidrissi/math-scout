from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from textwrap import indent

from pydantic import BaseModel
from rich import print

from .providers import (
    EFFORT_LEVELS,
    FATAL_PROVIDER_ERRORS,
    TRANSIENT_PROVIDER_ERRORS,
    ConfigurationError,
    GenerationRequest,
    GenerationResult,
    ModelRef,
    PromptBlock,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderModelError,
    ProviderPermissionError,
    ProviderRateLimitError,
    ProviderRegistry,
    ProviderRequestError,
    ProviderServerError,
    ProviderTimeoutError,
    next_lower_effort,
    provider_type,
)

DEFAULT_MODEL_STRONG = "anthropic:claude-opus-5"
DEFAULT_MODEL_FAST = "anthropic:claude-sonnet-5"

# Every call streams, so `max_tokens` is bounded by provider model capabilities rather
# than an SDK's non-streaming timeout. This application ceiling remains a typo guard;
# adapters may impose a lower known model-specific limit during settings validation.
MAX_OUTPUT_TOKENS = 64_000

# Thinking shares this budget with the response. Reviewers reading a 20k-character
# section routinely spent all of a 16_000 budget thinking and were cut off before the
# JSON began, which bills the full budget and returns nothing. Headroom is close to free:
# an unused budget costs nothing, an exhausted one costs everything and yields no review.
DEFAULT_MAX_TOKENS = 32_000

# A truncated call is retried once at lower effort, so one logical call bills at most two
# attempts. `run_dry_run` prices the worst case with this; the retry structure in
# `call_reviewer` and `run_final_referee` is what it has to stay in step with, and
# `test_a_truncated_call_bills_at_most_max_attempts` is what keeps them honest.
MAX_ATTEMPTS_PER_CALL = 2

DEFAULT_EFFORT = "high"

ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"

FINAL_REFEREE_PROMPT = "final_referee.md"

# Sent to every reviewer ahead of its own prompt: the payload map, the shared severity
# and confidence scales, and the field-by-field output contract. Keeping it in one file
# is what stops the four rubrics drifting apart, and it makes the system prefix
# identical across reviewers on the same model, so they share a cache entry.
REVIEW_PROTOCOL_PROMPT = "review_protocol.md"

# Resume bookkeeping, written alongside the reviews.
STATE_FILE = "state.json"
STATE_VERSION = 1

# Suffix a review file wears while it is being renamed into a new position. Anything
# still carrying it is the debris of an interrupted rename and gets swept.
MOVING_SUFFIX = ".moving"

# Values read from state.json are untrusted: a paper bundle can arrive with a pre-existing
# review directory. Stored reviews are always direct children of reviews/ and their names
# are made exclusively from safe chunk stems and reviewer names.
_STORED_REVIEW_RE = re.compile(r"[\w-]+\.json")
_CHUNK_KEY_RE = re.compile(r"[0-9a-f]{16}")

# Section titles (case-insensitive, LaTeX-stripped) that are skipped during review.
_SKIP_SECTIONS = frozenset({"references", "bibliography", "acknowledgments", "acknowledgements"})

# Matches \section{...} and \section*{...} headings.
# Hacky: at most one level of nested braces in section titles is supported,
# which is typically enough.
SECTION_RE = re.compile(r"\\section\*?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")

# Matches LaTeX commands: \cmd{text} → text, or bare \cmd → "".
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\{([^{}]*)\}|\\[a-zA-Z]+")

# Matches \input{...} and \include{...}, plus TeX's brace-less \input foo form.
INPUT_RE = re.compile(r"\\(?:input|include)\s*(?:\{([^{}]+)\}|\s([^\s{}\\%]+))")

# How deep \input chains may nest before we stop expanding them.
MAX_INPUT_DEPTH = 10

# Material between \begin{document} and the first \section is reviewed as its own
# chunk, but only when it holds this much prose beyond the abstract and title.
FRONT_MATTER_MIN_CHARS = 500
FRONT_MATTER_NAME = "Front matter"

# Name of the pseudo-chunk handed to paper-scoped reviewers. It carries the whole
# document, so it goes through the same chunk machinery — files, hashing, resume — as a
# real section.
WHOLE_PAPER_NAME = "(whole paper)"


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


class ReviewerScope(StrEnum):
    """How much of the document a reviewer is shown at once."""

    SECTION = "section"  # one chunk per call, once per section
    PAPER = "paper"  # the whole source, in a single call


@dataclass(frozen=True)
class ReviewerSpec:
    """Static description of a reviewer, independent of which models are in use."""

    prompt: str  # filename under prompts/
    tier: str  # "strong" or "fast"
    thinking: bool  # whether this reviewer's task benefits from extended thinking
    scope: ReviewerScope = ReviewerScope.SECTION


@dataclass(frozen=True)
class ReviewerConfig:
    """A reviewer resolved against a concrete model, with its prompt text loaded."""

    name: str
    model: ModelRef
    prompt_text: str
    thinking: bool
    scope: ReviewerScope = ReviewerScope.SECTION


@dataclass(frozen=True)
class LoadedPrompts:
    """All prompt text the pipeline needs, read and validated before any spending."""

    reviewers: dict[str, ReviewerConfig]
    final_referee: str
    review_protocol: str
    # The final referee runs on the same model as the strong reviewers. Recorded here
    # rather than looked up through a reviewer name, which would break on a rename. No
    # default: it must agree with the strong reviewers' models, and only load_prompts
    # is in a position to make that true.
    strong_model: ModelRef

    def scoped(self, scope: ReviewerScope) -> dict[str, ReviewerConfig]:
        """The reviewers that run at *scope*, in their declared order."""
        return {name: r for name, r in self.reviewers.items() if r.scope is scope}


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
# reasoning; reading one section for passages that will not land is a single judgement
# per passage, and gains nothing from thinking but costs tokens and latency for it.
#
# Scope follows the shape of the question. Whether an inference holds is decidable from
# the argument in front of you; whether a symbol means the same thing on page 4 as on
# page 19, or whether the abstract promises what Theorem 1.1 delivers, is not decidable
# from any one section, and asking a section-scoped reviewer for it only produces guesses
# the final referee then has to discard. Paper-scoped reviewers run last, so they see
# every section finding.
#
# Scope can move a reviewer across the thinking line, and it did: NotationAuditor thinks
# because whole-paper consistency is not the lookup that per-section consistency was.
# Tracing the order in which results are actually established, and collapsing every
# occurrence of one symbol into a single finding, are both multi-step over a long
# document. It stays on the fast tier — the work is bookkeeping, not mathematics.
#
# Insertion order is the order reviewers run in.
REVIEWER_SPECS: dict[str, ReviewerSpec] = {
    "FormalVerifier": ReviewerSpec("formal_verifier.md", "strong", thinking=True),
    "AdversarialSkeptic": ReviewerSpec("adversarial_skeptic.md", "strong", thinking=True),
    "ExpositionReferee": ReviewerSpec("exposition_referee.md", "fast", thinking=False),
    "NotationAuditor": ReviewerSpec(
        "notation_auditor.md", "fast", thinking=True, scope=ReviewerScope.PAPER
    ),
    "ClaimAuditor": ReviewerSpec(
        "claim_auditor.md", "strong", thinking=True, scope=ReviewerScope.PAPER
    ),
}


def _read_prompt(filename: str) -> str:
    path = PROMPTS / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Cannot read prompt file {path}: {exc}") from exc


def load_prompts(
    strong_model: str | ModelRef = DEFAULT_MODEL_STRONG,
    fast_model: str | ModelRef = DEFAULT_MODEL_FAST,
) -> LoadedPrompts:
    """Read every prompt file up front so a missing one fails before any API spending."""
    models = {"strong": ModelRef.parse(strong_model), "fast": ModelRef.parse(fast_model)}
    reviewers = {
        name: ReviewerConfig(
            name=name,
            model=models[spec.tier],
            prompt_text=_read_prompt(spec.prompt),
            thinking=spec.thinking,
            scope=spec.scope,
        )
        for name, spec in REVIEWER_SPECS.items()
    }
    return LoadedPrompts(
        reviewers=reviewers,
        final_referee=_read_prompt(FINAL_REFEREE_PROMPT),
        review_protocol=_read_prompt(REVIEW_PROTOCOL_PROMPT),
        strong_model=models["strong"],
    )


def validate_settings(prompts: LoadedPrompts, max_tokens: int, effort: str) -> None:
    """Reject invocations the API would refuse, before any request is made."""
    if max_tokens < 1:
        raise ConfigurationError("--max-tokens must be a positive integer.")
    if max_tokens > MAX_OUTPUT_TOKENS:
        raise ConfigurationError(
            f"--max-tokens is capped at {MAX_OUTPUT_TOKENS:,} here. A model's own output "
            "limit may be lower still, in which case the API rejects the request."
        )
    if effort not in EFFORT_LEVELS:
        raise ConfigurationError(
            f"--effort must be one of {', '.join(EFFORT_LEVELS)}; got {effort!r}."
        )

    requests = [
        (reviewer.name, reviewer.model, reviewer.thinking)
        for reviewer in prompts.reviewers.values()
    ]
    requests.append(("FinalReferee", prompts.strong_model, True))
    for reviewer_name, model, reasoning in sorted(requests, key=lambda item: item[0]):
        try:
            adapter = provider_type(model.provider)
            adapter.validate_request(
                model.model,
                reasoning=reasoning,
                effort=effort,
                output_limit=max_tokens,
            )
        except ConfigurationError as exc:
            message = str(exc)
            if reviewer_name not in message:
                message += f" (used by {reviewer_name})"
            raise ConfigurationError(message) from exc


def check_access(providers: ProviderRegistry, models: list[ModelRef]) -> None:
    """Verify credentials and every model ID before the run spends anything.

    Token counting is free, so this costs one round trip per distinct model and turns
    what would otherwise be dozens of identical mid-run failures into one clear message.
    """
    for model in sorted(set(models)):
        provider = providers.for_model(model)
        try:
            # Retry briefly so a rate limit or a momentary 5xx is a short wait rather
            # than a failed pre-flight.
            _call_with_retry(
                lambda model=model, provider=provider: provider.preflight(model.model),
                retries=1,
                base_delay=2.0,
            )
        except ProviderAuthenticationError as exc:
            raise ConfigurationError(
                f"{provider.credentials_help} The credential that was found was rejected: {exc}"
            ) from exc
        except ProviderPermissionError as exc:
            raise ConfigurationError(
                f"These {provider.display_name} credentials are not allowed to use "
                f"{model.qualified!r}: {exc}"
            ) from exc
        except ProviderModelError as exc:
            raise ConfigurationError(
                f"Model {model.qualified!r} is not available to this account. Check the ID "
                f"against {provider.models_url} ({exc})"
            ) from exc
        except ProviderConnectionError as exc:
            raise ConfigurationError(
                f"Cannot reach the {provider.display_name} API: {exc}"
            ) from exc
        except ProviderTimeoutError as exc:
            raise ConfigurationError(
                f"Timed out while checking {model.qualified!r}: {exc}"
            ) from exc
        except ProviderRateLimitError as exc:
            raise ConfigurationError(
                f"Rate limited while checking access to {model.qualified!r}; "
                f"try again shortly. {exc}"
            ) from exc
        except ProviderServerError as exc:
            status = exc.status_code if exc.status_code is not None else "a server error"
            raise ConfigurationError(
                f"The {provider.display_name} API returned {status} while checking access "
                f"to {model.qualified!r}: {exc}"
            ) from exc
        except ProviderRequestError as exc:
            raise ConfigurationError(
                f"The {provider.display_name} API rejected the access check for "
                f"{model.qualified!r}: {exc}"
            ) from exc


def _safe_filename(name: str) -> str:
    """Strip LaTeX commands and replace path-unsafe characters for use as a filename stem."""
    plain = _LATEX_CMD_RE.sub(lambda m: m.group(1) or "", name)
    safe = re.sub(r"[^\w\-]", "_", plain)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:80] or "section"


def chunk_stem(index: int, chunk: Chunk) -> str:
    """Filename stem for a chunk, prefixed by position so distinct sections never collide.

    Three digits, so the prefix still sorts lexicographically past a hundred chunks.
    Widening it renames every stored review, which `retarget_reviews` does on the next
    run without re-reviewing anything.
    """
    return f"{index:03d}_{_safe_filename(chunk.name)}"


def _call_with_retry(fn, retries: int = 3, base_delay: float = 5.0):
    """Call *fn* with exponential backoff on normalized transient provider errors."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except TRANSIENT_PROVIDER_ERRORS as exc:
            if attempt == retries:
                raise
            delay = base_delay * (2**attempt)
            if isinstance(exc, ProviderServerError):
                detail = (
                    f"server error {exc.status_code}"
                    if exc.status_code is not None
                    else "server error"
                )
            else:
                detail = exc.__class__.__name__
            print(f"[yellow]Transient error ({detail}), retrying in {delay:.0f}s…[/yellow]")
            time.sleep(delay)
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


# Environments whose bodies are literal text, not markup: a \section inside one is
# printed, not a heading. `comment` is not verbatim but is likewise not document text.
_LITERAL_ENVS = ("verbatim", "Verbatim", "lstlisting", "minted", "comment", "alltt")
_LITERAL_ENV_RE = re.compile(
    r"\\begin\{(" + "|".join(_LITERAL_ENVS) + r")\*?\}.*?\\end\{\1\*?\}",
    re.DOTALL,
)


def mask_non_content(tex: str) -> str:
    """Blank out comments and literal environments, preserving every character position.

    Section headings, abstracts, and theorem environments are matched against the result
    so that commented-out or verbatim-quoted markup is not mistaken for real structure,
    while offsets still index into the original string.
    """
    chars = list(tex)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            if chars[i] != "\n":  # keep line structure so line numbers still line up
                chars[i] = " "

    for match in _LITERAL_ENV_RE.finditer(tex):
        blank(match.start(), match.end())

    # Comments are masked after literal environments, so a % inside verbatim is already
    # gone and cannot swallow the rest of that line.
    offset = 0
    for line in "".join(chars).splitlines(keepends=True):
        code = strip_comment(line)
        blank(offset + len(code), offset + len(line.rstrip("\n")))
        offset += len(line)

    return "".join(chars)


def _resolve_reference(reference: str, search_dirs: list[Path]) -> Path | None:
    """Locate the file an \\input/\\include reference points at, trying .tex if bare."""
    reference = reference.strip()
    candidates = [reference]
    if not reference.endswith(".tex"):
        candidates.append(reference + ".tex")
    for directory in search_dirs:
        for candidate in candidates:
            # Path("/a") / "/etc/passwd" is "/etc/passwd", so an absolute reference
            # escapes the join entirely. Callers must confine the result themselves.
            path = directory / candidate
            if path.is_file():
                return path
    return None


def _is_within(path: Path, root: Path) -> bool:
    """Whether *path* lies inside the *root* directory tree, following symlinks."""
    return path.resolve().is_relative_to(root.resolve())


def resolve_inputs(
    path: Path | str,
    root: Path | None = None,
    _seen: frozenset[Path] | None = None,
    _depth: int = 0,
) -> str:
    """Read a .tex file and inline every \\input{} and \\include{} it references.

    References resolve relative to *root* (the main document's directory, mirroring how
    LaTeX searches) and then relative to the including file, and may not escape *root*.
    Commented-out references are ignored, cycles are broken, and a reference that cannot
    be found is left in place with a warning rather than aborting the run.
    """
    path = Path(path)
    root = path.parent if root is None else Path(root)
    seen = frozenset() if _seen is None else _seen

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError(
            f"{path} is not valid UTF-8 ({exc.reason} at byte {exc.start}). Older LaTeX "
            "sources are often Latin-1; convert with `iconv -f latin1 -t utf-8`."
        ) from exc
    if _depth >= MAX_INPUT_DEPTH:
        print(
            f"[yellow]Warning: \\input nesting deeper than {MAX_INPUT_DEPTH} at {path}; "
            "not expanding further.[/yellow]"
        )
        return text

    seen = seen | {path.resolve()}
    search_dirs = [root, path.parent]

    def expand(match: re.Match[str]) -> str:
        reference = match.group(1) or match.group(2)
        target = _resolve_reference(reference, search_dirs)
        if target is None:
            print(
                f"[yellow]Warning: cannot find {reference!r} referenced from {path}; "
                "leaving the reference unexpanded.[/yellow]"
            )
            return match.group(0)
        if not _is_within(target, root):
            # Papers arrive from other people. Without this, \input{/etc/passwd} would
            # read any file the user can read and ship it to the API and into the report.
            print(
                f"[yellow]Refusing to inline {reference!r} from {path}: it resolves to "
                f"{target.resolve()}, outside {root.resolve()}.[/yellow]"
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


def _front_matter(tex: str, masked: str, first_section_start: int) -> str | None:
    """Return the body text before the first \\section, if it holds enough real prose."""
    match = BEGIN_DOCUMENT_RE.search(masked)
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
    kept as a leading chunk when it holds enough prose to be worth reviewing. Headings
    are matched against the comment- and verbatim-masked text, so a commented-out
    \\section neither invents a chunk nor truncates the one it sits in.
    """
    masked = mask_non_content(tex)
    matches = list(SECTION_RE.finditer(masked))
    chunks = []

    if matches:
        front = _front_matter(tex, masked, matches[0].start())
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
    """Extract the abstract, preamble, and up to 15 theorems/definitions as shared context.

    Everything is located in the masked text and sliced from the original, so a
    commented-out title or a theorem quoted inside verbatim is not picked up.
    """
    parts = []
    masked = mask_non_content(tex)

    title_match = TITLE_RE.search(masked)
    if title_match:
        title = tex[title_match.start(1) : title_match.end(1)].strip()
        if title:
            parts.insert(0, f"# PAPER TITLE\n\n{title}")

    abstract_match = ABSTRACT_RE.search(masked)
    if abstract_match:
        parts.append("# ABSTRACT\n")
        parts.append(tex[abstract_match.start(1) : abstract_match.end(1)])

    preamble_match = PREAMBLE_RE.search(masked)
    if preamble_match:
        preamble = tex[preamble_match.start(1) : preamble_match.end(1)].strip()
        if preamble:
            parts.append("\n# PREAMBLE\n")
            parts.append(preamble)

    theorem_parts = []
    for i, match in enumerate(THEOREM_DEFINITION_RE.finditer(masked)):
        if i >= 15:
            break
        env_type = match.group(1).capitalize()
        content = tex[match.start(2) : match.end(2)].strip()
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
    """Render one finding for the final referee, carrying every field the reviewer set.

    The quote is what lets the referee honour its instruction to read the source before
    endorsing a finding — it can search for it — and the confidence is what tells it which
    findings are claims and which are leads. Dropping either leaves the referee guessing.
    """
    parts = [
        f"- **[{iss.reviewer} - {iss.severity.upper()} - confidence {iss.confidence:.2f}] "
        f"{iss.title}** ({iss.type})\n",
        f"  *Location:* {iss.location}\n",
    ]
    if iss.quote.strip():
        # Fenced verbatim rather than inline, so the referee can search the source for it
        # exactly as the reviewer copied it. LaTeX uses ` as an opening quote, so the
        # fence has to clear the longest backtick run in the text it wraps.
        fence = "`" * max(3, _longest_backtick_run(iss.quote) + 1)
        block = indent("\n".join([fence + "latex", iss.quote, fence]), "  ")
        parts.append(f"  *Quote:*\n{block}\n")
    parts.append(f"  *Analysis:* {iss.analysis}\n")
    parts.append(f"  *Fix:* {iss.suggested_fix}\n")
    return "".join(parts)


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


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


def _build_system(
    global_context: str,
    system_prompt: str,
    review_protocol: str | None = None,
) -> list[PromptBlock]:
    """Build the system prompt list with a cache breakpoint on every stable block.

    The global context and the review protocol are byte-identical across reviewers, so
    they form a shared prefix: reviewers on the same model read them from one cache entry
    rather than writing four. The final referee passes *review_protocol* as None — the
    protocol describes the JSON issue schema, and the referee's contract is markdown.
    """
    blocks = [
        PromptBlock(role="system", text=f"# GLOBAL CONTEXT\n{global_context}", cacheable=True),
    ]
    if review_protocol is not None:
        blocks.append(
            PromptBlock(
                role="system",
                text="# REVIEW PROTOCOL\n" + review_protocol,
                cacheable=True,
            )
        )
    blocks.append(
        PromptBlock(role="system", text="# REVIEWER PROMPT\n" + system_prompt, cacheable=True)
    )
    return blocks


def _build_messages(
    known_issues: str,
    to_review: str,
    preamble: str = "",
) -> list[PromptBlock]:
    """Assemble the user turn: an optional preamble, the known issues, then the target.

    The preamble is its own block rather than a prefix on the issue list, so a note about
    failed reviewer calls is not presented under the `DETECTED ISSUES` heading as though
    it were a finding.
    """
    content: list[PromptBlock] = []
    if preamble:
        content.append(PromptBlock(role="user", text=preamble))
    content.append(PromptBlock(role="user", text=f"# DETECTED ISSUES\n{known_issues}"))
    content.append(PromptBlock(role="user", text=to_review))
    return content


def _generation_request(
    model: ModelRef,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
    *,
    reasoning: bool,
    effort: str,
    max_tokens: int,
    with_schema: bool,
    review_protocol: str | None = None,
    preamble: str = "",
    prompt_cache_key: str | None = None,
) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        blocks=tuple(
            _build_system(global_context, system_prompt, review_protocol)
            + _build_messages(known_issues, to_review, preamble)
        ),
        reasoning=reasoning,
        effort=effort,
        output_limit=max_tokens,
        output_schema=Review if with_schema else None,
        prompt_cache_key=prompt_cache_key,
    )


def _format_review_target(chunk: Chunk, scope: ReviewerScope) -> str:
    """Format the text a reviewer is to read, labelled with how much of the paper it is.

    A paper-scoped reviewer is handed the whole source through the same Chunk machinery,
    so the heading has to say which it is getting: `SECTION TITLE: (whole paper)` would
    invite it to treat the document as one section.
    """
    if scope is ReviewerScope.PAPER:
        return f"# FULL PAPER UNDER REVIEW\n\n```latex\n{chunk.content}\n```"
    return (
        f"# SECTION UNDER REVIEW\n\nSECTION TITLE: {chunk.name}\n\n```latex\n{chunk.content}\n```"
    )


def lower_effort(effort: str) -> str | None:
    """The next effort level down, or None at the bottom.

    Lowering effort is always allowed: `disabled_thinking_max_effort` is an upper bound,
    so no model that accepted a level can reject the one below it.
    """
    return next_lower_effort(effort)


def prompt_cache_key(tex: str) -> str:
    """Return a deterministic, paper-specific cache-routing key."""
    digest = hashlib.sha256(tex.encode("utf-8")).hexdigest()[:32]
    return f"math-scout:{digest}"


def call_reviewer(
    providers: ProviderRegistry,
    reviewer: ReviewerConfig,
    chunk: Chunk,
    global_context: str,
    known_issues: str,
    review_protocol: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    cache_key: str | None = None,
):
    """Send a chunk to a reviewer and return its parsed JSON issue report.

    A reviewer that exhausts `max_tokens` thinking is retried once a level down. That
    trades some depth on one section for a review at all: the truncated attempt is billed
    in full and returns nothing, so the alternative is paying the same and losing the
    section's coverage entirely.
    """

    provider = providers.for_model(reviewer.model)
    cache_key = prompt_cache_key(global_context) if cache_key is None else cache_key

    def attempt(at_effort: str) -> tuple[GenerationRequest, GenerationResult]:
        request = _generation_request(
            model=reviewer.model,
            system_prompt=reviewer.prompt_text,
            global_context=global_context,
            known_issues=known_issues,
            to_review=_format_review_target(chunk, reviewer.scope),
            reasoning=reviewer.thinking,
            effort=at_effort,
            max_tokens=max_tokens,
            with_schema=True,
            review_protocol=review_protocol,
            prompt_cache_key=cache_key,
        )
        return request, _call_with_retry(lambda: provider.generate(request))

    request, response = attempt(effort)

    # Thinking shares the max_tokens budget with the response, so a truncated reply is a
    # realistic failure rather than a theoretical one. One step down, then give up: if a
    # whole level of thinking did not free enough budget, the budget is what is short.
    retry_effort = provider.retry_effort(request)
    if response.parsed is None and response.truncated and retry_effort is not None:
        print(
            f"[yellow]  {reviewer.name} exhausted {max_tokens:,} tokens on "
            f"'{chunk.name}' at --effort {effort}; retrying at {retry_effort}.[/yellow]"
        )
        _, response = attempt(retry_effort)

    if response.parsed is None:
        if response.truncated:
            raise ValueError(
                f"{reviewer.name} hit the {max_tokens:,}-token limit on section "
                f"'{chunk.name}' before finishing its JSON. Raise --max-tokens (up to "
                f"{MAX_OUTPUT_TOKENS:,}) or lower --effort."
            )
        raise ValueError(
            f"No parsed output from reviewer {reviewer.name} on section '{chunk.name}'"
        )

    if not isinstance(response.parsed, Review):
        raise TypeError(
            f"Provider returned {type(response.parsed).__name__}, expected Review for "
            f"{reviewer.name}."
        )
    return response.parsed


def append_issues(issues: list[IssueWithReviewer], issues_file: Path) -> None:
    """Append each issue as a JSON line to the JSONL issues file."""
    with open(issues_file, "a", encoding="utf-8") as f:
        for issue in issues:
            f.write(issue.model_dump_json() + "\n")


def run_final_referee(
    providers: ProviderRegistry,
    tex: str,
    issues: list[IssueWithReviewer],
    global_context: str,
    system_prompt: str,
    model: ModelRef,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    coverage_note: str = "",
    cache_key: str | None = None,
) -> str:
    """Synthesize a markdown referee report from the paper and the collected issues."""

    # This pass weighs and prioritises every finding across the whole paper, so it
    # thinks — and it runs once per review, so the extra cost is marginal.
    provider = providers.for_model(model)
    cache_key = prompt_cache_key(tex) if cache_key is None else cache_key

    def attempt(at_effort: str) -> tuple[GenerationRequest, GenerationResult]:
        request = _generation_request(
            model=model,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=format_all_issues(issues),
            to_review=f"# FULL PAPER\n```latex\n{tex}\n```",
            reasoning=True,
            effort=at_effort,
            max_tokens=max_tokens,
            with_schema=False,
            preamble=coverage_note,
            prompt_cache_key=cache_key,
        )
        return request, _call_with_retry(lambda: provider.generate(request))

    request, response = attempt(effort)

    # This call reads the whole paper and every issue, so it is the likeliest of all of
    # them to think past the budget. Losing it loses the report, which is the deliverable.
    retry_effort = provider.retry_effort(request)
    if response.truncated and retry_effort is not None:
        print(
            f"[yellow]Final referee exhausted {max_tokens:,} tokens at --effort "
            f"{effort}; retrying at {retry_effort}.[/yellow]"
        )
        _, response = attempt(retry_effort)

    if response.truncated:
        raise ValueError(
            f"Final referee hit the {max_tokens:,}-token limit before finishing. Raise "
            f"--max-tokens (up to {MAX_OUTPUT_TOKENS:,}) or lower --effort."
        )
    if response.text is None:
        raise ValueError("No text output from the final referee.")
    return response.text


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


def chunk_key(chunk: Chunk) -> str:
    """A stable identity for a chunk, derived from its title and its text.

    Resume decisions hang off this rather than off a filename. Filenames carry the
    chunk's position so output sorts in document order, which makes them change whenever
    a section is inserted or removed; the key does not, and it also changes exactly when
    the text does, so edited sections are re-reviewed and untouched ones are not.
    """
    digest = hashlib.sha256()
    digest.update(chunk.name.encode("utf-8"))
    digest.update(b"\0")
    digest.update(chunk.content.encode("utf-8"))
    return digest.hexdigest()[:16]


def prompts_digest(prompts: LoadedPrompts) -> str:
    """A hash over every prompt that shapes a reviewer's findings.

    Editing a prompt changes what the reviewer says as surely as changing its model does,
    so resume has to notice. Without this, a run made before a prompt was rewritten would
    be silently mixed with one made after, and the report would attribute both to the
    current prompts.
    """
    digest = hashlib.sha256()
    for name, reviewer in sorted(prompts.reviewers.items()):
        digest.update(f"{name}\0{reviewer.scope}\0".encode())
        digest.update(reviewer.prompt_text.encode("utf-8"))
        digest.update(b"\0")
    digest.update(prompts.review_protocol.encode("utf-8"))
    return digest.hexdigest()[:16]


def run_settings(prompts: LoadedPrompts, max_tokens: int, effort: str) -> dict:
    """The knobs that change what a reviewer would say, recorded so resume can compare."""
    return {
        "models": {name: r.model.qualified for name, r in sorted(prompts.reviewers.items())},
        "max_tokens": max_tokens,
        "effort": effort,
        "prompts": prompts_digest(prompts),
    }


@dataclass
class ResumeState:
    """Which (chunk, reviewer) pairs are already done, and under what settings."""

    settings: dict
    completed: dict[str, dict[str, str]] = field(default_factory=dict)

    def is_done(self, key: str, reviewer: str) -> bool:
        return reviewer in self.completed.get(key, {})

    def review_file(self, key: str, reviewer: str) -> str:
        return _validate_stored_review_filename(self.completed[key][reviewer])

    def mark(self, key: str, reviewer: str, filename: str) -> None:
        self.completed.setdefault(key, {})[reviewer] = _validate_stored_review_filename(filename)

    def prune(self, live_keys: set[str]) -> None:
        """Forget chunks that are no longer in the document."""
        for key in set(self.completed) - live_keys:
            del self.completed[key]

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {"version": STATE_VERSION, "settings": self.settings, "completed": self.completed},
                indent=2,
            ),
            encoding="utf-8",
        )


def _validate_stored_review_filename(filename: object, state_path: Path | None = None) -> str:
    """Return a safe reviews/ basename, rejecting paths supplied through state.json."""
    if isinstance(filename, str) and _STORED_REVIEW_RE.fullmatch(filename):
        return filename
    location = f" in {state_path}" if state_path is not None else ""
    raise ConfigurationError(
        f"Unsafe stored review filename{location}: {filename!r}. State may name only "
        "a .json file directly inside the reviews directory."
    )


def _validate_completed_state(
    completed: object, state_path: Path, reviewer_names: set[str]
) -> dict[str, dict[str, str]]:
    """Validate the untrusted part of state.json before any stored path is used."""
    if not isinstance(completed, dict):
        raise ConfigurationError(f"Invalid completed-review map in {state_path}.")

    for key, reviewers in completed.items():
        if not isinstance(key, str) or _CHUNK_KEY_RE.fullmatch(key) is None:
            raise ConfigurationError(f"Invalid chunk key in {state_path}: {key!r}.")
        if not isinstance(reviewers, dict):
            raise ConfigurationError(f"Invalid reviewer map for chunk {key!r} in {state_path}.")
        for reviewer, filename in reviewers.items():
            if reviewer not in reviewer_names:
                raise ConfigurationError(
                    f"Unknown reviewer {reviewer!r} in completed-review map {state_path}."
                )
            _validate_stored_review_filename(filename, state_path)

    return completed


def _validate_managed_output_tree(output_dir: Path) -> None:
    """Refuse symlinks in paths the pipeline reads, overwrites, moves, or deletes.

    Output defaults beside the input paper, so a paper obtained from someone else can
    arrive with a crafted review/ tree. Following one of its symlinks would let chunk or
    report writes escape the output directory even when every generated filename is safe.
    """
    managed_dirs = (output_dir, output_dir / "reviews", output_dir / "chunks")
    managed_files = (
        output_dir / STATE_FILE,
        output_dir / "issues.jsonl",
        output_dir / "all_issues.json",
        output_dir / "final_report.md",
    )

    for path in (*managed_dirs, *managed_files):
        if path.is_symlink():
            raise ConfigurationError(
                f"Refusing to use symbolic link in pipeline-managed output: {path}"
            )

    for directory in managed_dirs:
        if directory.exists() and not directory.is_dir():
            raise ConfigurationError(
                f"Pipeline-managed output path is not a directory: {directory}"
            )

    for directory in managed_dirs[1:]:
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_symlink():
                raise ConfigurationError(
                    f"Refusing to use symbolic link in pipeline-managed output: {path}"
                )


def load_resume_state(output_dir: Path, settings: dict) -> ResumeState:
    """Load prior progress for this output directory, or start fresh.

    Refuses rather than guesses when the previous run used different models, effort, or
    token limits: its findings describe a different pipeline, and silently reusing them
    would present stale output as current.
    """
    state_path = output_dir / STATE_FILE
    reviews_dir = output_dir / "reviews"

    if not state_path.exists():
        if reviews_dir.is_dir() and any(reviews_dir.glob("*.json")):
            raise ConfigurationError(
                f"{reviews_dir} holds reviews but {state_path} is missing, so there is no "
                "record of what produced them. Delete the output directory or pass a "
                "different --output."
            )
        return ResumeState(settings=settings)

    try:
        stored = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Cannot read {state_path}: {exc}") from exc

    if not isinstance(stored, dict):
        raise ConfigurationError(f"Invalid resume state object in {state_path}.")

    if stored.get("version") != STATE_VERSION:
        raise ConfigurationError(
            f"{state_path} was written by a different version of math-scout. Delete the "
            "output directory or pass a different --output."
        )

    if stored.get("settings") != settings:
        changed = _describe_settings_change(stored.get("settings") or {}, settings)
        raise ConfigurationError(
            f"{output_dir} holds a run made with different settings ({changed}). Reusing "
            "those reviews would report findings that the current settings did not "
            "produce. Delete the output directory or pass a different --output."
        )

    completed = _validate_completed_state(
        stored.get("completed"), state_path, set(settings["models"])
    )
    return ResumeState(settings=settings, completed=completed)


def _describe_settings_change(old: dict, new: dict) -> str:
    differences = []
    for name in sorted(set(old) | set(new)):
        if old.get(name) == new.get(name):
            continue
        if name == "prompts":
            # The value is a hash; printing it tells the reader nothing.
            differences.append("prompts: the reviewer prompts have changed since that run")
        else:
            differences.append(f"{name}: {old.get(name)!r} -> {new.get(name)!r}")
    return "; ".join(differences) or "unknown difference"


def _load_review(path: Path) -> Review | None:
    """Read a stored reviewer result, treating anything unreadable as not done."""
    try:
        return Review.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def retarget_reviews(state: ResumeState, stems: dict[str, str], reviews_dir: Path) -> int:
    """Move stored reviews to the filenames this run's chunk positions imply.

    A reused review keeps whatever name it was written under, and the index prefix in
    that name encodes the chunk's position at the time. Insert a section and every later
    chunk's position shifts while its stored review does not, so the directory stops
    sorting in document order and a reader cannot tell which file is current.

    Renaming is safe to interrupt: the move is atomic, and a crash before *state* is
    saved leaves it naming a file that is gone, which `_load_review` reports as not done
    and the run repeats. That costs one call and loses nothing, the same trade the rest
    of the resume bookkeeping makes.
    """
    planned: list[tuple[str, str, str, str]] = []
    for key, reviewers in state.completed.items():
        if key not in stems:
            continue
        for reviewer, stored in reviewers.items():
            stored = _validate_stored_review_filename(stored)
            wanted = _validate_stored_review_filename(f"{stems[key]}_{reviewer}.json")
            if stored == wanted:
                continue

            stored_path = reviews_dir / stored
            moving_path = reviews_dir / (stored + MOVING_SUFFIX)
            wanted_path = reviews_dir / wanted

            for path in (stored_path, moving_path, wanted_path):
                if path.is_symlink():
                    raise ConfigurationError(
                        f"Refusing to use symbolic link in pipeline-managed output: {path}"
                    )

            if stored_path.is_file() or moving_path.is_file():
                planned.append((key, reviewer, stored, wanted))
            elif wanted_path.is_file():
                # Rename finished previously but state.json was not saved.
                state.completed[key][reviewer] = wanted

    if not planned:
        return 0

    # Two phases. One rename's target can be another's source, so avoid clobbering.
    for _, _, stored, _ in planned:
        stored_path = reviews_dir / stored
        if stored_path.is_file():
            stored_path.replace(reviews_dir / (stored + MOVING_SUFFIX))

    for key, reviewer, stored, wanted in planned:
        moving_path = reviews_dir / (stored + MOVING_SUFFIX)
        if moving_path.is_file():
            moving_path.replace(reviews_dir / wanted)
        state.completed[key][reviewer] = wanted

    return len(planned)


def sweep(directory: Path, live: set[str], patterns: tuple[str, ...]) -> list[str]:
    """Delete files matching *patterns* that are not in the *live* set, returning names.

    These directories are written and owned by the pipeline — `load_resume_state` already
    refuses to run against a `reviews/` it has no record of producing — so a file nothing
    accounts for is left over from an earlier run. Keeping it is not harmless: it is
    indistinguishable from current output.
    """
    removed = []
    for pattern in patterns:
        for path in sorted(directory.glob(pattern)):
            if path.is_file() and path.name not in live:
                path.unlink()
                removed.append(path.name)
    return removed


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
    providers: ProviderRegistry,
    tex_path: str,
    output_dir: Path | str | None = None,
    prompts: LoadedPrompts | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
    assume_yes: bool = False,
) -> PipelineResult:
    """Run the full multi-reviewer pipeline on a .tex file and write all outputs to *output_dir*."""
    prompts = load_prompts() if prompts is None else prompts

    # Validate the input before creating anything, so a typo'd path leaves no litter.
    tex, chunks, global_context = _prepare(tex_path)
    cache_key = prompt_cache_key(tex)

    output_dir = Path(tex_path).parent / "review" if output_dir is None else Path(output_dir)
    reviews_dir = output_dir / "reviews"
    issues_file = output_dir / "issues.jsonl"
    chunks_dir = output_dir / "chunks"

    section_reviewers = prompts.scoped(ReviewerScope.SECTION)
    paper_reviewers = prompts.scoped(ReviewerScope.PAPER)

    # Paper-scoped reviewers are handed the whole source as one more chunk, so they get
    # the same identity, storage and resume behaviour as a section without a second
    # bookkeeping path. It goes last, so they see every section finding.
    paper_chunk = Chunk(name=WHOLE_PAPER_NAME, content=tex)
    paper_key = chunk_key(paper_chunk)
    paper_stem = chunk_stem(len(chunks), paper_chunk)

    settings = run_settings(prompts, max_tokens=max_tokens, effort=effort)
    _validate_managed_output_tree(output_dir)
    state = load_resume_state(output_dir, settings)
    keys = [chunk_key(chunk) for chunk in chunks]
    # The whole-paper key has to be live too, or every resume discards its reviews.
    state.prune(set(keys) | {paper_key})

    output_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(exist_ok=True)
    chunks_dir.mkdir(exist_ok=True)

    result = PipelineResult(output_dir=output_dir)

    stems = [chunk_stem(index, chunk) for index, chunk in enumerate(chunks)]
    written = list(zip(stems, chunks, strict=True))
    if paper_reviewers:
        written.append((paper_stem, paper_chunk))
    for stem, chunk in written:
        chunk_path = chunks_dir / f"{stem}.tex"
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

    scheduled = [(key, name) for key in keys for name in section_reviewers]
    scheduled += [(paper_key, name) for name in paper_reviewers]
    done = sum(state.is_done(key, name) for key, name in scheduled)
    pending = len(scheduled) - done
    plan = f"[bold]{pending} reviewer call(s) to make"
    if done:
        plan += f", {done} already complete and reused"
    print(plan + ", then one final referee call.[/bold]")

    if not _confirm(assume_yes):
        print("[red]Aborting review.[/red]")
        result.aborted = True
        return result

    # Only now, with the run committed. Writing output before the prompt is one thing;
    # deleting a previous run's on the way to a question the user may answer "no" is
    # another, and a declined run should leave the directory exactly as it found it.
    stem_by_key = dict(zip(keys, stems, strict=True)) | {paper_key: paper_stem}
    moved = retarget_reviews(state, stem_by_key, reviews_dir)
    if moved:
        state.save(output_dir / STATE_FILE)
        print(
            f"[yellow]Renamed {moved} stored review(s) to match the current section order.[/yellow]"
        )

    live_reviews = {name for entry in state.completed.values() for name in entry.values()}
    removed = sweep(reviews_dir, live_reviews, ("*.json", f"*{MOVING_SUFFIX}"))
    removed += sweep(chunks_dir, {f"{stem}.tex" for stem, _ in written}, ("*.tex",))
    if removed:
        print(f"[yellow]Removed {len(removed)} stale file(s): {', '.join(removed)}[/yellow]")

    print("[bold blue]Starting multi-reviewer analysis...[/bold blue]")

    # Rebuilt from the stored reviews rather than from issues.jsonl, so a run that was
    # interrupted between writing a review and appending to the log loses nothing.
    all_issues: list[IssueWithReviewer] = []

    def review_chunk(
        key: str, stem: str, chunk: Chunk, reviewers: dict[str, ReviewerConfig]
    ) -> None:
        """Run every reviewer in *reviewers* over *chunk*, recording results and failures.

        Sets ``result.fatal_error`` and returns early when the API says every remaining
        call would fail the same way.
        """
        print(f"[bold blue]Reviewing:[/bold blue] {chunk.name}")
        known_issues = summarize_known_issues(all_issues)

        for reviewer_name, reviewer in reviewers.items():
            out_path = reviews_dir / f"{stem}_{reviewer_name}.json"

            if state.is_done(key, reviewer_name):
                stored = _load_review(reviews_dir / state.review_file(key, reviewer_name))
                if stored is not None:
                    print(f"  -> {reviewer_name} [yellow](reusing stored review)[/yellow]")
                    all_issues.extend(attribute_issue(iss, reviewer_name) for iss in stored.issues)
                    continue
                print(
                    f"  -> {reviewer_name} [yellow](stored review unreadable, re-running)[/yellow]"
                )

            print(f"  -> {reviewer_name}")

            try:
                review = call_reviewer(
                    providers=providers,
                    reviewer=reviewer,
                    chunk=chunk,
                    global_context=global_context,
                    known_issues=known_issues,
                    review_protocol=prompts.review_protocol,
                    max_tokens=max_tokens,
                    effort=effort,
                    cache_key=cache_key,
                )
            except FATAL_PROVIDER_ERRORS as exc:
                # Credentials or the model itself are wrong, so every remaining call
                # would fail identically. Stop rather than burn through the whole paper.
                print(f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]")
                result.fatal_error = (
                    f"Aborting: {type(exc).__name__} from the API means every remaining "
                    f"call would fail the same way. {exc}"
                )
                return
            except Exception as exc:
                print(f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]")
                result.failures.append(
                    ReviewerFailure(section=chunk.name, reviewer=reviewer_name, error=str(exc))
                )
                continue

            print(f"  [green]Success: {len(review.issues)} issues detected.[/green]")

            attributed_issues = [attribute_issue(iss, reviewer_name) for iss in review.issues]

            # The review file holds the findings; state.json records that it is complete.
            # Writing the review first means a crash in between costs one repeated call
            # and never loses an issue. issues.jsonl is an append-only log that may
            # therefore contain superseded entries; all_issues.json is the current truth.
            out_path.write_text(review.model_dump_json(indent=2), encoding="utf-8")
            append_issues(attributed_issues, issues_file)
            state.mark(key, reviewer_name, out_path.name)
            state.save(output_dir / STATE_FILE)
            all_issues.extend(attributed_issues)

    for key, stem, chunk in zip(keys, stems, chunks, strict=True):
        if result.fatal_error is not None:
            break
        review_chunk(key, stem, chunk, section_reviewers)

    # Cross-section questions — does this symbol mean the same thing throughout, does the
    # abstract promise what the theorems deliver — are not answerable from one section,
    # so their reviewers get the whole source once, after the section passes.
    if paper_reviewers and result.fatal_error is None:
        review_chunk(paper_key, paper_stem, paper_chunk, paper_reviewers)

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
            providers=providers,
            tex=tex,
            issues=all_issues,
            global_context=global_context,
            system_prompt=prompts.final_referee,
            model=prompts.strong_model,
            max_tokens=max_tokens,
            effort=effort,
            coverage_note=format_coverage_note(result.failures),
            cache_key=cache_key,
        )
    except FATAL_PROVIDER_ERRORS as exc:
        result.fatal_error = (
            f"Aborting: {type(exc).__name__} from the API means every remaining call "
            f"would fail the same way. {exc}"
        )
        print(f"[red]ERROR: final referee synthesis failed: {exc}[/red]")
        print(
            f"[yellow]Collected issues are preserved in {all_issues_path}; "
            "fix the problem and re-run the same command to resume.[/yellow]"
        )
        return result
    except Exception as exc:
        print(f"[red]ERROR: final referee synthesis failed: {exc}[/red]")
        print(
            f"[yellow]Collected issues are preserved in {all_issues_path}; "
            "re-run the same command to retry the synthesis.[/yellow]"
        )
        result.failures.append(
            ReviewerFailure(section=WHOLE_PAPER_NAME, reviewer="FinalReferee", error=str(exc))
        )
        return result

    final_report_path = output_dir / "final_report.md"
    final_report_path.write_text(final_report, encoding="utf-8")
    result.report_path = final_report_path

    print("\n[bold green]Review complete.[/bold green]")
    print(f"Issues detected: {len(all_issues)}")

    return result


def run_dry_run(
    providers: ProviderRegistry,
    tex_path: str,
    prompts: LoadedPrompts | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    effort: str = DEFAULT_EFFORT,
) -> None:
    """Count exact request inputs and optionally estimate cost, without generation."""
    prompts = load_prompts() if prompts is None else prompts

    print("[yellow]DRY RUN — token counting only, no generation requests will be sent.[/yellow]")
    print(
        "[yellow]Each displayed count is exact for the request shown to the provider. "
        "The run total remains a lower bound because the known-issues block grows from "
        "generated findings, which do not exist during a dry run. Cost estimates also "
        "exclude unpriced models and do not predict provider-specific cache hits.[/yellow]\n"
    )

    tex, chunks, global_context = _prepare(tex_path)
    empty_known_issues = summarize_known_issues([])
    cache_key = prompt_cache_key(tex)
    token_totals: dict[ModelRef, int] = {}
    call_counts: dict[ModelRef, int] = {}

    def record(model: ModelRef, tokens: int) -> None:
        token_totals[model] = token_totals.get(model, 0) + tokens
        call_counts[model] = call_counts.get(model, 0) + 1

    def count_request(request: GenerationRequest) -> int:
        return providers.for_model(request.model).count_tokens(request)

    def count_reviewer(chunk: Chunk, reviewer: ReviewerConfig) -> None:
        request = _generation_request(
            model=reviewer.model,
            system_prompt=reviewer.prompt_text,
            global_context=global_context,
            known_issues=empty_known_issues,
            to_review=_format_review_target(chunk, reviewer.scope),
            reasoning=reviewer.thinking,
            effort=effort,
            max_tokens=max_tokens,
            with_schema=True,
            review_protocol=prompts.review_protocol,
            prompt_cache_key=cache_key,
        )
        n = count_request(request)
        record(reviewer.model, n)
        print(f"  [blue]{chunk.name}[/blue] / [cyan]{reviewer.name}[/cyan]: {n:,} input tokens")

    for chunk in chunks:
        for reviewer in prompts.scoped(ReviewerScope.SECTION).values():
            count_reviewer(chunk, reviewer)

    paper_chunk = Chunk(name=WHOLE_PAPER_NAME, content=tex)
    for reviewer in prompts.scoped(ReviewerScope.PAPER).values():
        count_reviewer(paper_chunk, reviewer)

    final_model = prompts.strong_model
    final_request = _generation_request(
        model=final_model,
        system_prompt=prompts.final_referee,
        global_context=global_context,
        known_issues="[]",
        to_review=f"# FULL PAPER\n```latex\n{tex}\n```",
        reasoning=True,
        effort=effort,
        max_tokens=max_tokens,
        with_schema=False,
        prompt_cache_key=cache_key,
    )
    n = count_request(final_request)
    record(final_model, n)
    print(f"  [blue]Final referee[/blue]: {n:,} input tokens")

    def pricing_for(model: ModelRef):
        return provider_type(model.provider).pricing(model.model)

    total_input = sum(token_totals.values())
    unpriced = sorted(model for model in token_totals if pricing_for(model) is None)
    input_cost = sum(
        count / 1e6 * pricing.input
        for model, count in token_totals.items()
        if (pricing := pricing_for(model)) is not None
    )
    max_output_cost = sum(
        count * max_tokens / 1e6 * pricing.output
        for model, count in call_counts.items()
        if (pricing := pricing_for(model)) is not None
    )
    max_output_cost_with_retries = max_output_cost * MAX_ATTEMPTS_PER_CALL

    print("\n[bold]Input tokens by qualified model (no assumed cache hits):[/bold]")
    for model, count in token_totals.items():
        pricing = pricing_for(model)
        if pricing is None:
            print(f"  {model.qualified}: {count:,} tokens [yellow](pricing unknown)[/yellow]")
            continue
        cache = ""
        if pricing.cache_read is not None:
            cache = (
                f" (cached reads: ${pricing.cache_read}/M = "
                f"${count / 1e6 * pricing.cache_read:.2f})"
            )
        if pricing.cache_write is not None:
            cache += f" (cache writes: ${pricing.cache_write}/M)"
        print(
            f"  {model.qualified}: {count:,} tokens @ ${pricing.input}/M = "
            f"${count / 1e6 * pricing.input:.2f}{cache}"
        )
    print(f"  Total input: {total_input:,} tokens")

    if unpriced:
        print(
            "\n[yellow]Pricing unknown for: "
            + ", ".join(model.qualified for model in unpriced)
            + ". Token counts are complete, but the cost figures below exclude those "
            "models. Check the relevant provider's current pricing page.[/yellow]"
        )

    label = "Estimated input cost (no cache)"
    if unpriced:
        label += ", priced models only"
    print(f"[bold green]{label}: ${input_cost:.2f}[/bold green]")
    no_thinking = sum(1 for r in prompts.reviewers.values() if not r.thinking)
    print(
        f"[bold green]Max output cost: ${max_output_cost:.2f}[/bold green]  "
        f"(a ceiling nobody reaches: it assumes all {sum(call_counts.values())} calls "
        f"emit the full {max_tokens:,} output tokens, and {no_thinking} of "
        f"{len(prompts.reviewers)} reviewers have reasoning disabled. --max-tokens is "
        "headroom against truncation, not expected spend)"
    )
    print(
        f"[bold green]Max output cost if every retry-eligible call truncates: "
        f"${max_output_cost_with_retries:.2f}[/bold green]  "
        f"(the conservative application ceiling allows up to {MAX_ATTEMPTS_PER_CALL} "
        "attempts per logical call)"
    )
    print(
        f"[bold green]Max total cost: ${input_cost + max_output_cost:.2f}[/bold green] "
        f"to [bold green]${input_cost * MAX_ATTEMPTS_PER_CALL + max_output_cost_with_retries:.2f}"
        f"[/bold green] with retries"
    )
