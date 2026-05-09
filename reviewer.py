from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List

import anthropic as _anthropic

from anthropic import Anthropic
from rapidfuzz import fuzz
from rich import print

API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL_STRONG = "claude-opus-4-7"
MODEL_FAST = "claude-sonnet-4-6"

client = Anthropic(api_key=API_KEY)


ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"


@dataclass
class Chunk:
    """A named slice of a LaTeX document corresponding to one \\section{}."""

    name: str  # section title extracted from \section{...}
    content: str  # raw LaTeX from this section's heading to the next one


# Maps reviewer name → {prompt: filename under prompts/, model: model ID}.
REVIEWERS = {
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
            _anthropic.RateLimitError,
            _anthropic.APIConnectionError,
            _anthropic.APITimeoutError,
        ) as exc:
            if attempt == retries:
                raise
            delay = base_delay * (2**attempt)
            print(
                f"[yellow]Transient error ({exc.__class__.__name__}), retrying in {delay:.0f}s…[/yellow]"
            )
            time.sleep(delay)
        except _anthropic.APIStatusError as exc:
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


def call_reviewer(
    reviewer_name: str,
    chunk: Chunk,
    global_context: str,
    known_issues: str,
) -> dict:
    """Send a section chunk to a named reviewer and return its parsed JSON issue report."""
    config = REVIEWERS[reviewer_name]

    system_prompt = (PROMPTS / config["prompt"]).read_text(encoding="utf-8")
    user_prompt = f"""
# GLOBAL CONTEXT

{global_context}

# KNOWN ISSUES

{known_issues}

# SECTION UNDER REVIEW

SECTION TITLE: {chunk.name}

```latex
{chunk.content}
```
Return ONLY valid JSON."""

    def _api_call():
        return client.messages.create(
            model=config["model"],
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
                    "content": user_prompt,
                }
            ],
        )

    response = _call_with_retry(_api_call)
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

    user_prompt = f"""
# GLOBAL CONTEXT
{global_context}
# DETECTED ISSUES
{issues_json}
# FULL PAPER
```latex
{tex}
```
Produce a final referee report in markdown."""
    response = _call_with_retry(
        lambda: client.messages.create(
            model=MODEL_STRONG,
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
                    "content": user_prompt,
                }
            ],
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
