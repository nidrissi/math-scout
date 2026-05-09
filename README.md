# LLM Reviewer

A multi-agent pipeline that reviews mathematical papers (LaTeX) using Claude. It runs four specialized reviewers over each section of a paper and synthesizes a final referee report.

## Reviewers

| Agent | Model | Role |
|---|---|---|
| `FormalVerifier` | Opus | Proof gaps, invalid inferences, missing hypotheses |
| `AdversarialSkeptic` | Opus | Edge cases, brittle arguments, degenerate examples |
| `NotationAuditor` | Sonnet | Symbol consistency, undefined notation, broken references |
| `ExpositionReferee` | Sonnet | Readability, missing intuition, proof strategy clarity |

Each reviewer outputs structured JSON issues (`title`, `severity`, `type`, `location`, `quote`, `analysis`, `suggested_fix`, `confidence`). Non-mathematical sections (References, Bibliography, Acknowledgments) are skipped automatically.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install anthropic rich
```

## Usage

```bash
export ANTHROPIC_API_KEY=your_key
python reviewer.py path/to/paper.tex
python reviewer.py path/to/paper.tex --output path/to/output_dir
python reviewer.py path/to/paper.tex --dry-run   # token count + cost estimate only
```

The `--output` argument is optional. When omitted, output is written to a `review/` directory next to the input file.

If the pipeline is interrupted, re-running the same command resumes from where it left off — completed reviewer calls are detected from the per-chunk JSON files and skipped.

## Output

All output is written to the output directory (default: `<input_dir>/review/`):

| File | Contents |
|---|---|
| `reviews/<section>_<reviewer>.json` | Raw JSON from each reviewer per section |
| `issues.jsonl` | All issues appended incrementally (one JSON object per line) |
| `all_issues.json` | All issues as a JSON array |
| `final_report.md` | Final referee report in markdown |
