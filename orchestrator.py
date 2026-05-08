from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List

import orjson
from anthropic import Anthropic
from rapidfuzz import fuzz
from rich import print

API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL_STRONG = "claude-opus-4-7"
MODEL_FAST = "claude-sonnet-4-6"

client = Anthropic(api_key=API_KEY)


ROOT = Path(__file__).parent.resolve()
PROMPTS = ROOT / "prompts"
OUTPUTS = ROOT / "outputs"
CHUNKS_DIR = OUTPUTS / "chunks"
REVIEWS_DIR = OUTPUTS / "reviews"
ISSUES_FILE = OUTPUTS / "issues.jsonl"


@dataclass
class Chunk:
    name: str
    content: str


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

SECTION_RE = re.compile(r"\\section\*?\{(.+?)\}")


def load_tex(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def chunk_by_section(tex: str) -> List[Chunk]:
    matches = list(SECTION_RE.finditer(tex))

    chunks = []

    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(tex)

        title = match.group(1)
        content = tex[start:end]

        chunks.append(Chunk(name=title, content=content))

    return chunks


ABSTRACT_RE = re.compile(
    r"\\begin\{abstract\}(.*?)\\end\{abstract\}",
    re.DOTALL,
)


THEOREM_RE = re.compile(
    r"\\begin\{theorem\}(.*?)\\end\{theorem\}",
    re.DOTALL,
)


def extract_global_context(tex: str) -> str:
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


def load_known_issues() -> List[dict]:
    if not ISSUES_FILE.exists():
        return []

    issues = []

    with open(ISSUES_FILE, "rb") as f:
        for line in f:
            issues.append(orjson.loads(line))

    return issues


def summarize_known_issues(issues: List[dict], limit: int = 15) -> str:
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
):
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
Return ONLY valid JSON. """
    response = client.messages.create(
        model=config["model"],
        max_tokens=16384,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": user_prompt,
            }
        ],
    )

    text = response.content[0].text

    return json.loads(text)


def append_issues(review_json: dict):
    issues = review_json.get("issues", [])
    with open(ISSUES_FILE, "ab") as f:
        for issue in issues:
            f.write(orjson.dumps(issue))
            f.write(b"\n")


def deduplicate_issues(issues: List[dict]) -> List[dict]:
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
):
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
    response = client.messages.create(
        model=MODEL_STRONG,
        max_tokens=16384,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": user_prompt,
            }
        ],
    )

    return response.content[0].text


def run_pipeline(tex_path: str):
    OUTPUTS.mkdir(exist_ok=True)
    CHUNKS_DIR.mkdir(exist_ok=True)
    REVIEWS_DIR.mkdir(exist_ok=True)
    tex = load_tex(tex_path)

    chunks = chunk_by_section(tex)
    global_context = extract_global_context(tex)

    all_reviews = []

    for chunk in chunks:
        print(f"[bold blue]Reviewing:[/bold blue] {chunk.name}")

        known_issues = summarize_known_issues(load_known_issues())

        for reviewer_name in REVIEWERS:
            print(f"  -> {reviewer_name}")

            review = call_reviewer(
                reviewer_name,
                chunk,
                global_context,
                known_issues,
            )

            append_issues(review)
            all_reviews.append(review)

            out_path = REVIEWS_DIR / f"{chunk.name}_{reviewer_name}.json"

            out_path.write_text(
                json.dumps(review, indent=2),
                encoding="utf-8",
            )

    all_issues = []

    for review in all_reviews:
        all_issues.extend(review.get("issues", []))

    deduped = deduplicate_issues(all_issues)

    summary_path = OUTPUTS / "deduped_issues.json"

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

    final_report_path = OUTPUTS / "final_report.md"

    final_report_path.write_text(
        final_report,
        encoding="utf-8",
    )

    print("\n[bold green]Review complete.[/bold green]")
    print(f"Unique issues detected: {len(deduped)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    args = parser.parse_args()

    run_pipeline(args.input)
