from __future__ import annotations

import argparse
from enum import Enum
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
import anthropic
from rich import print
from pydantic import BaseModel

MODEL_STRONG = "claude-opus-4-7"
MODEL_FAST = "claude-sonnet-4-6"
MAX_TOKENS = 8192
MODEL_PRICING = {
    MODEL_STRONG: {
        "input": 5.0,
        "output": 25.0,
        "cache_write": 6.25,
        "cache_read": 0.5,
    },
    MODEL_FAST: {"input": 3.0, "output": 15.0, "cache_write": 3.75, "cache_read": 0.3},
}

ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"

# Section titles (case-insensitive, LaTeX-stripped) that are skipped during review.
_SKIP_SECTIONS = frozenset(
    {"references", "bibliography", "acknowledgments", "acknowledgements"}
)

# Matches \section{...} and \section*{...} headings.
# Hacky: at most one level of nested braces in section titles is supported, which is typically enough.
SECTION_RE = re.compile(r"\\section\*?\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}")

# Matches LaTeX commands: \cmd{text} → text, or bare \cmd → "".
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\{([^{}]*)\}|\\[a-zA-Z]+")


@dataclass
class Chunk:
    """A named slice of a LaTeX document corresponding to one \\section{}."""

    name: str  # section title extracted from \section{...}
    content: str  # raw LaTeX from this section's heading to the next one


class SeverityLevel(str, Enum):
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
    return IssueWithReviewer(
        title=issue.title,
        severity=issue.severity,
        type=issue.type,
        location=issue.location,
        quote=issue.quote,
        analysis=issue.analysis,
        suggested_fix=issue.suggested_fix,
        confidence=issue.confidence,
        reviewer=reviewer_name,
    )


class Review(BaseModel):
    issues: list[Issue]


# Maps reviewer name → {prompt: filename under prompts/, model: model ID}.
# prompt_text is populated at runtime by _load_prompts().
REVIEWERS: dict[str, dict] = {
    "FormalVerifier": {
        "prompt": "formal_verifier.md",
        "model": MODEL_STRONG,
    },
    "AdversarialSkeptic": {
        "prompt": "adversarial_skeptic.md",
        "model": MODEL_STRONG,
    },
    "NotationAuditor": {
        "prompt": "notation_auditor.md",
        "model": MODEL_FAST,
    },
    "ExpositionReferee": {
        "prompt": "exposition_referee.md",
        "model": MODEL_FAST,
    },
}


def _load_prompts() -> None:
    """Pre-read all reviewer prompt files and cache the text in the REVIEWERS dict."""
    for config in REVIEWERS.values():
        config["prompt_text"] = (PROMPTS / config["prompt"]).read_text(encoding="utf-8")


def _safe_filename(name: str) -> str:
    """Strip LaTeX commands and replace path-unsafe characters for use as a filename stem."""
    plain = _LATEX_CMD_RE.sub(lambda m: m.group(1) or "", name)
    safe = re.sub(r"[^\w\-]", "_", plain)
    safe = re.sub(r"_+", "_", safe).strip("_")
    return safe[:80] or "section"


def extract_text(response) -> str:
    """Concatenate all text blocks from a Claude response, warning on unexpected block types."""
    texts = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        else:
            print(
                f"[yellow]Unexpected content block type {block.type!r}: {block}[/yellow]"
            )
    if not texts:
        raise ValueError(
            f"No text block in response (stop_reason={response.stop_reason!r})"
        )
    if response.stop_reason == "max_tokens":
        print(
            "[yellow]Warning: response was truncated (stop_reason=max_tokens).[/yellow]"
        )
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
                f"[yellow]Transient error ({exc.__class__.__name__}), retrying in {delay:.0f}s…[/yellow]"
            )
            time.sleep(delay)
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500 and attempt < retries:
                delay = base_delay * (2**attempt)
                print(
                    f"[yellow]Server error {exc.status_code}, retrying in {delay:.0f}s…[/yellow]"
                )
                time.sleep(delay)
            else:
                raise
    raise RuntimeError("unreachable")


def load_tex(path: str) -> str:
    """Read a .tex file and return its full contents as a UTF-8 string."""
    return Path(path).read_text(encoding="utf-8")


def chunk_by_section(tex: str) -> list[Chunk]:
    """Split a LaTeX string into Chunks at each \\section boundary, skipping non-mathematical sections."""
    matches = list(SECTION_RE.finditer(tex))
    chunks = []
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


def extract_global_context(tex: str) -> str:
    """Extract the abstract, preamble, and up to 15 theorems/definitions as shared context."""
    parts = []

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
        for line in f:
            line = line.strip()
            if line:
                issues.append(IssueWithReviewer.model_validate_json(line))
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
    """Format the highest-severity known issues as a bullet list for injection into reviewer prompts."""
    if not issues:
        return "No previously detected issues."
    sorted_issues = sorted(
        issues, key=lambda iss: iss.severity.numerical_level, reverse=True
    )
    return "\n".join(
        f"- [{iss.reviewer} - {iss.severity.upper()}] {iss.location}: {iss.title}"
        for iss in sorted_issues[:limit]
    )


def _build_system(
    global_context: str, system_prompt: str
) -> list[anthropic.types.TextBlockParam]:
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


def _build_messages(
    known_issues: str, to_review: str
) -> list[anthropic.types.MessageParam]:
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
):
    return client.messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=_build_system(global_context, system_prompt),
        messages=_build_messages(known_issues, to_review),
        output_format=Review,
    )


def _api_call(
    client: anthropic.Anthropic,
    model: str,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
):
    return client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=_build_system(global_context, system_prompt),
        messages=_build_messages(known_issues, to_review),
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
    return f"# SECTION UNDER REVIEW\n\nSECTION TITLE: {chunk.name}\n\n```latex\n{chunk.content}\n```"


def call_reviewer(
    client: anthropic.Anthropic,
    reviewer_name: str,
    chunk: Chunk,
    global_context: str,
    known_issues: str,
):
    """Send a section chunk to a named reviewer and return its parsed JSON issue report."""
    config = REVIEWERS[reviewer_name]
    model = config["model"]
    system_prompt = config["prompt_text"]

    response = _call_with_retry(
        lambda: _api_call_with_schema(
            client=client,
            model=model,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=known_issues,
            to_review=_format_section(chunk),
        )
    )

    if not response or not response.parsed_output:
        raise ValueError(
            f"No parsed output from reviewer {reviewer_name} on section '{chunk.name}'"
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
) -> str:
    """Synthesize a final markdown referee report from the full paper and the collected issue list."""
    system_prompt = (PROMPTS / "final_referee.md").read_text(encoding="utf-8")

    response = _call_with_retry(
        lambda: _api_call(
            client=client,
            model=MODEL_STRONG,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=format_all_issues(issues),
            to_review=f"# FULL PAPER\n```latex\n{tex}\n```",
        )
    )

    return extract_text(response)


def _prepare(tex_path: str) -> tuple[str, list[Chunk], str]:
    """Load a .tex file, split it into section chunks, and extract global context."""
    tex = load_tex(tex_path)
    chunks = chunk_by_section(tex)
    global_context = extract_global_context(tex)
    return tex, chunks, global_context


def run_pipeline(
    client: anthropic.Anthropic, tex_path: str, output_dir: Path | str | None = None
) -> None:
    """Run the full multi-reviewer pipeline on a .tex file and write all outputs to *output_dir*."""
    _load_prompts()

    if output_dir is None:
        output_dir = Path(tex_path).parent / "review"
    else:
        output_dir = Path(output_dir)
    reviews_dir = output_dir / "reviews"
    issues_file = output_dir / "issues.jsonl"
    chunks_dir = output_dir / "chunks"

    output_dir.mkdir(parents=True, exist_ok=True)
    reviews_dir.mkdir(exist_ok=True)
    chunks_dir.mkdir(exist_ok=True)
    tex, chunks, global_context = _prepare(tex_path)

    for chunk in chunks:
        chunk_path = chunks_dir / f"{_safe_filename(chunk.name)}.tex"
        chunk_path.write_text(chunk.content, encoding="utf-8")
        print(
            f"[green]Extracted chunk:[/green] {chunk.name} (written to {chunk_path}, {len(chunk.content)} chars)"
        )

    print(
        f"[green]Extracted global context ({len(global_context)} chars):[/green]\n[gray]{global_context[:500]}{'…' if len(global_context) > 500 else ''}[/gray]"
    )

    print("Continue? [y/N]")
    if input().strip().lower() not in ("y", "yes"):
        print("[red]Aborting review.[/red]")
        return

    print("[bold blue]Starting multi-reviewer analysis...[/bold blue]")

    all_issues: list[IssueWithReviewer] = load_known_issues(issues_file)
    for chunk in chunks:
        print(f"[bold blue]Reviewing:[/bold blue] {chunk.name}")
        known_issues = summarize_known_issues(all_issues)
        safe_name = _safe_filename(chunk.name)

        for reviewer_name in REVIEWERS:
            out_path = reviews_dir / f"{safe_name}_{reviewer_name}.json"

            if out_path.exists():
                print(f"  -> {reviewer_name} [yellow](resuming from disk)[/yellow]")
                continue

            print(f"  -> {reviewer_name}")

            try:
                review = call_reviewer(
                    client=client,
                    reviewer_name=reviewer_name,
                    chunk=chunk,
                    global_context=global_context,
                    known_issues=known_issues,
                )
            except Exception as exc:
                print(
                    f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]"
                )
                continue

            print(f"  [green]Success: {len(review.issues)} issues detected.[/green]")

            attributed_issues = [
                attribute_issue(iss, reviewer_name) for iss in review.issues
            ]

            # Write per-chunk JSON first so it acts as the authoritative resume marker.
            # If the process dies between here and append_issues, the JSON exists but
            # issues.jsonl is incomplete — on resume the JSON check prevents re-running
            # the reviewer, yet those issues were never appended. This is preferable to
            # the reverse order, which causes duplicate issues on resume.
            out_path.write_text(review.model_dump_json(indent=2), encoding="utf-8")
            append_issues(attributed_issues, issues_file)
            all_issues.extend(attributed_issues)

    all_issues_path = output_dir / "all_issues.json"
    all_issues_path.write_text(
        json.dumps([i.model_dump(mode="json") for i in all_issues], indent=2),
        encoding="utf-8",
    )

    print("[bold blue]Running final referee synthesis...[/bold blue]")
    final_report = run_final_referee(
        client=client,
        tex=tex,
        issues=all_issues,
        global_context=global_context,
    )

    final_report_path = output_dir / "final_report.md"
    final_report_path.write_text(final_report, encoding="utf-8")

    print("\n[bold green]Review complete.[/bold green]")
    print(f"Issues detected: {len(all_issues)}")


def run_dry_run(client: anthropic.Anthropic, tex_path: str) -> None:
    """Count input tokens and estimate cost without sending any generation requests."""
    _load_prompts()

    print(
        "[yellow]DRY RUN — token counting only, no generation requests will be sent.[/yellow]"
    )
    print(
        "[yellow]Warning: rough lower-bound estimate. The 'known issues' context fed to each "
        "reviewer (which grows as earlier reviewers produce output) is not counted here "
        "because it depends on actual generation. Real input cost will also be lower than "
        "shown once prompt caching kicks in (cache reads are 90 % cheaper).[/yellow]\n"
    )

    tex, chunks, global_context = _prepare(tex_path)
    empty_known_issues = summarize_known_issues([])

    token_totals: dict[str, int] = {MODEL_STRONG: 0, MODEL_FAST: 0}
    call_counts: dict[str, int] = {MODEL_STRONG: 0, MODEL_FAST: 0}

    for chunk in chunks:
        for reviewer_name, config in REVIEWERS.items():
            model = config["model"]
            system_prompt = config["prompt_text"]
            to_review = _format_section(chunk)
            n = _count_tokens(
                client,
                model,
                system_prompt,
                global_context,
                empty_known_issues,
                to_review,
                with_schema=True,
            )
            token_totals[model] += n
            call_counts[model] += 1
            print(
                f"  [blue]{chunk.name}[/blue] / [cyan]{reviewer_name}[/cyan]: {n:,} input tokens"
            )

    # Final referee
    system_prompt = (PROMPTS / "final_referee.md").read_text(encoding="utf-8")
    n = _count_tokens(
        client,
        MODEL_STRONG,
        system_prompt,
        global_context,
        "[]",  # no issues available in dry run
        f"# FULL PAPER\n```latex\n{tex}\n```",
        with_schema=False,
    )
    token_totals[MODEL_STRONG] += n
    call_counts[MODEL_STRONG] += 1
    print(f"  [blue]Final referee[/blue]: {n:,} input tokens")

    total_input = sum(token_totals.values())
    input_cost = sum(
        count / 1e6 * MODEL_PRICING[model]["input"]
        for model, count in token_totals.items()
    )
    max_output_cost = sum(
        call_counts[model] * MAX_TOKENS / 1e6 * MODEL_PRICING[model]["output"]
        for model in call_counts
    )

    print("\n[bold]Input tokens by model (worst-case, no cache hits):[/bold]")
    for model, count in token_totals.items():
        rate = MODEL_PRICING[model]["input"]
        cache_rate = MODEL_PRICING[model]["cache_read"]
        print(
            f"  {model}: {count:,} tokens @ ${rate}/M = ${count / 1e6 * rate:.2f} "
            f"(cached reads: ${cache_rate}/M = ${count / 1e6 * cache_rate:.2f})"
        )
    print(f"  Total input: {total_input:,} tokens")
    print(
        f"[bold green]Estimated input cost (no cache): ${input_cost:.2f}[/bold green]"
    )
    print(
        f"[bold green]Max output cost: ${max_output_cost:.2f}[/bold green]  "
        f"(if all {sum(call_counts.values())} calls use {MAX_TOKENS:,} output tokens)"
    )
    print(
        f"[bold green]Max total cost: ${input_cost + max_output_cost:.2f}[/bold green]"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "reviewer.py",
        description="Run multiple LLM-based reviewers on a LaTeX document, aggregating their feedback into a final report.",
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
    args = parser.parse_args()

    API_KEY = os.environ["ANTHROPIC_API_KEY"]
    client = anthropic.Anthropic(api_key=API_KEY)

    if args.dry_run:
        run_dry_run(client, args.input)
    else:
        run_pipeline(client, args.input, args.output)
