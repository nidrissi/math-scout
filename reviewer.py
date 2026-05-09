from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

import anthropic
from rapidfuzz import fuzz
from rich import print

API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL_STRONG = "claude-opus-4-7"
MODEL_FAST = "claude-sonnet-4-6"

client = anthropic.Anthropic(api_key=API_KEY)


ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"


@dataclass
class Chunk:
    """A named slice of a LaTeX document corresponding to one \\section{}."""

    name: str  # section title extracted from \section{...}
    content: str  # raw LaTeX from this section's heading to the next one


_SEVERITY = {"type": "string", "enum": ["critical", "major", "moderate", "minor"]}
_CONFIDENCE = {"type": "number", "minimum": 0.0, "maximum": 1.0}


def _reviewer_schema(reviewer_name: str) -> dict:
    """Return the top-level JSON Schema for a reviewer response."""
    return {
        "type": "object",
        "properties": {
            "reviewer": {"type": "string", "enum": [reviewer_name]},
            "section": {"type": "string"},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "severity": _SEVERITY,
                        "type": {"type": "string"},
                        "location": {"type": "string"},
                        "quote": {"type": "string"},
                        "analysis": {"type": "string"},
                        "suggested_fix": {"type": "string"},
                        "confidence": _CONFIDENCE,
                    },
                    "required": [
                        "title",
                        "severity",
                        "type",
                        "location",
                        "quote",
                        "analysis",
                        "suggested_fix",
                        "confidence",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["reviewer", "section", "issues"],
        "additionalProperties": False,
    }


# Maps reviewer name → {prompt: filename under prompts/, model: model ID, schema: JSON Schema}.
REVIEWERS = {
    "FormalVerifier": {
        "prompt": "formal_verifier.md",
        "model": MODEL_STRONG,
        "schema": _reviewer_schema("FormalVerifier"),
    },
    "AdversarialSkeptic": {
        "prompt": "adversarial_skeptic.md",
        "model": MODEL_STRONG,
        "schema": _reviewer_schema("AdversarialSkeptic"),
    },
    "NotationAuditor": {
        "prompt": "notation_auditor.md",
        "model": MODEL_FAST,
        "schema": _reviewer_schema("NotationAuditor"),
    },
    "ExpositionReferee": {
        "prompt": "exposition_referee.md",
        "model": MODEL_FAST,
        "schema": _reviewer_schema("ExpositionReferee"),
    },
}

# Matches \section{...} and \section*{...} headings.
SECTION_RE = re.compile(r"\\section\*?\{(.+?)\}")


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


def load_tex(path: str) -> str:
    """Read a .tex file and return its full contents as a UTF-8 string."""
    return Path(path).read_text(encoding="utf-8")


def chunk_by_section(tex: str) -> List[Chunk]:
    """Split a LaTeX string into Chunks at each \\section boundary."""
    matches = list(SECTION_RE.finditer(tex))

    chunks = []

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tex)

        title = match.group(1)
        content = tex[start:end]

        chunks.append(Chunk(name=title, content=content))

    return chunks


# Matches \begin{abstract}...\end{abstract} across newlines.
ABSTRACT_RE = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)

# Matches \begin{theorem}...\end{theorem} across newlines.
THEOREM_RE = re.compile(
    r"\\begin\{theorem\}(.*?)\\end\{theorem\}",
    re.DOTALL,
)


def extract_global_context(tex: str) -> str:
    """Extract the abstract and up to 10 theorems to serve as shared context in every reviewer prompt."""
    parts = []

    abstract_match = ABSTRACT_RE.search(tex)
    if abstract_match:
        parts.append("# ABSTRACT\n")
        parts.append(abstract_match.group(1))

    theorem_matches = THEOREM_RE.findall(tex)

    if theorem_matches:
        parts.append("\n# MAIN THEOREMS\n")
        parts.extend(theorem_matches[:10])

    return "\n\n".join(parts)


def load_known_issues(issues_file: Path) -> List[dict]:
    """Load all issues from a JSONL file; returns an empty list if the file does not exist."""
    if not issues_file.exists():
        return []

    issues = []

    with open(issues_file, "rb") as f:
        for line in f:
            issues.append(json.loads(line))

    return issues


def summarize_known_issues(issues: List[dict], limit: int = 15) -> str:
    """Format the first *limit* known issues as a bullet list for injection into reviewer prompts."""
    if not issues:
        return "No previously detected issues."

    lines = []

    for issue in issues[:limit]:
        lines.append(f"- [{issue['severity']}] {issue['location']}: {issue['title']}")

    return "\n".join(lines)


def _api_call(
    model: str,
    system_prompt: str,
    global_context: str,
    known_issues: str,
    to_review: str,
    json_schema: dict[str, object] | None = None,
):
    return client.messages.create(
        model=model,
        max_tokens=16384,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"# GLOBAL CONTEXT\n{global_context}",
                        "cache_control": {"type": "ephemeral"},
                    },
                    {
                        "type": "text",
                        "text": f"# DETECTED ISSUES\n{known_issues}",
                    },
                    {
                        "type": "text",
                        "text": to_review,
                    },
                ],
            },
        ],
        output_config={"format": {"type": "json_schema", "schema": json_schema}}
        if json_schema
        else anthropic.omit,
    )


def call_reviewer(
    reviewer_name: str,
    chunk: Chunk,
    global_context: str,
    known_issues: str,
) -> dict:
    """Send a section chunk to a named reviewer and return its parsed JSON issue report."""
    config = REVIEWERS[reviewer_name]
    model = config["model"]
    system_prompt = (PROMPTS / config["prompt"]).read_text(encoding="utf-8")

    response = _call_with_retry(
        lambda: _api_call(
            model=model,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=known_issues,
            to_review=f"# SECTION UNDER REVIEW\n\nSECTION TITLE: {chunk.name}\n\n```latex\n{chunk.content}\n```",
            json_schema=config["schema"],
        )
    )
    text = extract_text(response)
    return json.loads(text)


def append_issues(review_json: dict, issues_file: Path) -> None:
    """Append each issue from a reviewer's JSON response as a separate line to the JSONL issues file."""
    issues = review_json.get("issues", [])
    with open(issues_file, "ab") as f:
        for issue in issues:
            f.write(json.dumps(issue).encode("utf-8"))
            f.write(b"\n")


def deduplicate_issues(issues: List[dict]) -> List[dict]:
    """Remove near-duplicate issues using fuzzy ratio on the analysis field; drops anything above 88."""
    deduped = []
    for issue in issues:
        duplicate = False

        for existing in deduped:
            score = fuzz.ratio(
                issue["analysis"],
                existing["analysis"],
            )

            if score > 88:
                duplicate = True
                break

        if not duplicate:
            deduped.append(issue)

    return deduped


def run_final_referee(
    tex: str,
    deduped_issues: List[dict],
    global_context: str,
) -> str:
    """Synthesize a final markdown referee report from the full paper and the deduplicated issue list."""
    system_prompt = (PROMPTS / "final_referee.md").read_text(encoding="utf-8")
    issues_json = json.dumps(
        deduped_issues,
        indent=2,
    )

    response = _call_with_retry(
        lambda: _api_call(
            model=MODEL_STRONG,
            system_prompt=system_prompt,
            global_context=global_context,
            known_issues=issues_json,
            to_review=f"# FULL PAPER\n```latex\n{tex}\n```",
        )
    )

    return extract_text(response)


def run_pipeline(tex_path: str, output_dir: Path | str | None = None) -> None:
    """Run the full multi-reviewer pipeline on a .tex file and write all outputs to *output_dir*."""
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
    tex = load_tex(tex_path)

    chunks = chunk_by_section(tex)
    for chunk in chunks:
        chunk_path = chunks_dir / f"{chunk.name}.tex"
        chunk_path.write_text(chunk.content, encoding="utf-8")
        print(
            f"[green]Extracted chunk:[/green] {chunk.name} (written to {chunk_path}, {len(chunk.content)} chars)"
        )

    global_context = extract_global_context(tex)
    print(
        f"[green]Extracted global context ({len(global_context)} chars):[/green]\n[gray]{global_context[:500]}{'…' if len(global_context) > 500 else ''}[/gray]"
    )

    print("Continue? [y/N]")
    if input().strip().lower() not in ("y", "yes"):
        print("[red]Aborting review.[/red]")
        return

    print("[bold blue]Starting multi-reviewer analysis...[/bold blue]")

    all_reviews = []
    for chunk in chunks:
        print(f"[bold blue]Reviewing:[/bold blue] {chunk.name}")

        known_issues = summarize_known_issues(load_known_issues(issues_file))

        for reviewer_name in REVIEWERS:
            out_path = reviews_dir / f"{chunk.name}_{reviewer_name}.json"

            if out_path.exists():
                print(f"  -> {reviewer_name} [yellow](resuming from disk)[/yellow]")
                all_reviews.append(json.loads(out_path.read_text(encoding="utf-8")))
                continue

            print(f"  -> {reviewer_name}")

            try:
                review = call_reviewer(
                    reviewer_name,
                    chunk,
                    global_context,
                    known_issues,
                )
            except Exception as exc:
                print(
                    f"  [red]ERROR: {reviewer_name} on '{chunk.name}' failed: {exc}[/red]"
                )
                continue

            print(
                f"  [green]Success: {len(review.get('issues', []))} issues detected.[/green]"
            )

            append_issues(review, issues_file)
            all_reviews.append(review)

            out_path.write_text(
                json.dumps(review, indent=2),
                encoding="utf-8",
            )

    all_issues = []

    for review in all_reviews:
        all_issues.extend(review.get("issues", []))

    deduped = deduplicate_issues(all_issues)

    summary_path = output_dir / "deduped_issues.json"

    summary_path.write_text(
        json.dumps(deduped, indent=2),
        encoding="utf-8",
    )

    print("[bold blue]Running final referee synthesis...[/bold blue]")
    final_report = run_final_referee(
        tex,
        deduped,
        global_context,
    )

    final_report_path = output_dir / "final_report.md"

    final_report_path.write_text(
        final_report,
        encoding="utf-8",
    )

    print("\n[bold green]Review complete.[/bold green]")
    print(f"Unique issues detected: {len(deduped)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument(
        "--output",
        default=None,
        help="Output directory (default: <input_dir>/review/)",
    )
    args = parser.parse_args()

    run_pipeline(args.input, args.output)
