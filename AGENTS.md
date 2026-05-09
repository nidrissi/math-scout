# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## What this project does

`llm-reviewer` is a multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a `.tex` file into sections, runs four specialized Claude-based reviewers on each section in sequence, and synthesizes a final referee report in markdown.

## Running the pipeline

```bash
source .venv/bin/activate
ANTHROPIC_API_KEY=<key> python reviewer.py path/to/paper.tex [--output <dir>]
```

Outputs land in `<input_dir>/review/` by default (override with `--output`):
- `review/issues.jsonl` — raw issues appended per reviewer call (JSONL, one issue per line)
- `review/reviews/<section>_<reviewer>.json` — per-chunk reviewer output
- `review/all_issues.json` — all collected issues as a JSON array
- `review/final_report.md` — final synthesis in markdown

Use `--dry-run` to count tokens and estimate cost without sending generation requests.

## Architecture

All logic is in a single file: `reviewer.py`.

**Pipeline stages** (`run_pipeline`):
1. Load `.tex`, extract global context (abstract + preamble + up to 15 theorems via regex)
2. Split into `Chunk`s by `\section{}` boundaries; skip non-mathematical sections (References, Bibliography, Acknowledgments)
3. For each chunk × each reviewer: call Claude, save per-chunk JSON, append issues to `issues.jsonl`
4. Run final referee over the full paper + all issues → `final_report.md`

**Reviewers** (defined in `REVIEWERS` dict, prompts in `prompts/`):

| Name | Model | Focus |
|---|---|---|
| `FormalVerifier` | Opus (strong) | Proof gaps, invalid inferences, hidden assumptions |
| `AdversarialSkeptic` | Opus (strong) | Edge cases, brittle arguments, suspicious steps |
| `NotationAuditor` | Sonnet (fast) | Notation consistency, undefined symbols, circular refs |
| `ExpositionReferee` | Sonnet (fast) | Readability, missing intuition, proof strategy clarity |

All four reviewers return the same JSON schema: `{ issues: [ { title, severity, type, location, quote, analysis, suggested_fix, confidence } ] }`.

Prompt caching is enabled on the global-context and reviewer-prompt system blocks, cutting repeated input token cost by ~90 % after the first call per reviewer.

The final referee (`prompts/final_referee.md`) receives global context + all issues + full paper and produces a structured markdown report.

## Dependencies

`anthropic`, `rich` — all installed in `.venv/`.
