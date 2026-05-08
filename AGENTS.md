# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## What this project does

`llm-reviewer` is a multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a `.tex` file into sections, runs four specialized Claude-based reviewers on each section in sequence, deduplicates the collected issues, and synthesizes a final referee report in markdown.

## Running the pipeline

```bash
source .venv/bin/activate
ANTHROPIC_API_KEY=<key> python orchestrator.py path/to/paper.tex
```

Outputs land in `outputs/`:
- `outputs/issues.jsonl` — raw issues appended per reviewer call
- `outputs/reviews/<section>_<reviewer>.json` — per-chunk reviewer output
- `outputs/deduped_issues.json` — deduplicated issues (fuzzy ratio > 88 filtered)
- `outputs/final_report.md` — final synthesis in markdown

## Architecture

All logic is in a single file: `orchestrator.py`.

**Pipeline stages** (`run_pipeline`):
1. Load `.tex`, extract global context (abstract + up to 10 theorems via regex)
2. Split into `Chunk`s by `\section{}` boundaries
3. For each chunk × each reviewer: call Claude, append issues to `issues.jsonl`, save JSON
4. Deduplicate all issues using `rapidfuzz.fuzz.ratio` on the `analysis` field
5. Run final referee over the full paper + deduped issues → `final_report.md`

**Reviewers** (defined in `REVIEWERS` dict, prompts in `prompts/`):

| Name | Model | Focus |
|---|---|---|
| `FormalVerifier` | Opus (strong) | Proof gaps, invalid inferences, hidden assumptions |
| `AdversarialSkeptic` | Opus (strong) | Edge cases, brittle arguments, suspicious steps |
| `NotationAuditor` | Sonnet (fast) | Notation consistency, undefined symbols, circular refs |
| `ExpositionReferee` | Sonnet (fast) | Readability, missing intuition, proof strategy clarity |

All four reviewers return the same JSON schema: `{ reviewer, section, issues: [ { title, severity, type, location, quote, analysis, suggested_fix, confidence } ] }`.

The final referee (`prompts/final_referee.md`) receives global context + deduped issues + full paper and produces a structured markdown report (Summary → Main concerns → Minor concerns → Exposition → Recommendation → Revisions).

## Dependencies

`anthropic`, `rapidfuzz`, `rich` — all installed in `.venv/`.
