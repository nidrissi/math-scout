# LLM Reviewer

A multi-agent pipeline that reviews mathematical papers (LaTeX) using Claude. It runs four specialized reviewers over each section of a paper, deduplicates findings, and synthesizes a final referee report.

## Reviewers

| Agent | Model | Role |
|---|---|---|
| `FormalVerifier` | Opus | Proof gaps, invalid inferences, missing hypotheses |
| `AdversarialSkeptic` | Opus | Edge cases, brittle arguments, degenerate examples |
| `NotationAuditor` | Sonnet | Symbol consistency, undefined notation, broken references |
| `ExpositionReferee` | Sonnet | Readability, missing intuition, proof strategy clarity |

Each reviewer outputs structured JSON issues (`title`, `severity`, `type`, `location`, `quote`, `analysis`, `suggested_fix`, `confidence`). Duplicate issues are filtered using fuzzy string matching before the final synthesis.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install anthropic rapidfuzz rich
```

## Usage

```bash
export ANTHROPIC_API_KEY=your_key
python orchestrator.py path/to/paper.tex
```

## Output

All output is written to `outputs/`:

| File | Contents |
|---|---|
| `reviews/<section>_<reviewer>.json` | Raw JSON from each reviewer per section |
| `issues.jsonl` | All issues appended incrementally |
| `deduped_issues.json` | Deduplicated issues (fuzzy ratio > 88) |
| `final_report.md` | Final referee report in markdown |
