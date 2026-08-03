# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## What this project does

`llm-reviewer` is a multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a `.tex` file into sections, runs four specialized Claude-based reviewers on each section in sequence, and synthesizes a final referee report in markdown.

## Running the pipeline

```bash
uv sync
uv run llm-reviewer path/to/paper.tex [--output <dir>]
```

Credentials come from the Anthropic SDK's own resolution: `ANTHROPIC_API_KEY` in the environment, or a profile stored by `ant auth login`. Never pass a key inline on the command line.

Outputs land in `<input_dir>/review/` by default (override with `--output`):
- `review/issues.jsonl` — raw issues appended per reviewer call (JSONL, one issue per line)
- `review/chunks/<NN>_<section>.tex` — the exact LaTeX each reviewer saw
- `review/reviews/<NN>_<section>_<reviewer>.json` — per-chunk reviewer output, and the resume marker
- `review/all_issues.json` — all collected issues as a JSON array
- `review/final_report.md` — final synthesis in markdown

Use `--dry-run` to count tokens and estimate cost without sending generation requests, and `--yes` to skip the confirmation prompt in non-interactive contexts.

Exit codes: `0` clean, `1` finished with failed reviewer calls, `2` misconfigured (no credentials, unknown model, nothing to review, no confirmation).

## Layout

```
src/llm_reviewer/reviewer.py    # all pipeline logic
src/llm_reviewer/cli.py         # argparse, credential pre-flight, exit codes
src/llm_reviewer/prompts/*.md   # reviewer and final-referee prompts (package data)
tests/test_reviewer.py          # pure-function tests; no network, no credentials
```

## Architecture

**Pipeline stages** (`run_pipeline`):
1. Load `.tex`, inlining `\input{}`/`\include{}` (`resolve_inputs`), then extract global context (title + abstract + preamble + up to 15 theorems via regex)
2. Split into `Chunk`s by `\section{}` boundaries; skip non-mathematical sections (References, Bibliography, Acknowledgments). Body text before the first `\section` becomes a "Front matter" chunk when it holds enough prose
3. For each chunk × each reviewer: call Claude, save per-chunk JSON, append issues to `issues.jsonl`
4. Run final referee over the full paper + all issues → `final_report.md`

`cli.py` calls `check_access` first: a free `count_tokens` probe per distinct model that validates credentials and model IDs before anything is written or any money is spent.

**Reviewers** (defined in `REVIEWER_SPECS`, resolved to models by `load_prompts`):

| Name | Tier | Focus |
|---|---|---|
| `FormalVerifier` | strong | Proof gaps, invalid inferences, hidden assumptions |
| `AdversarialSkeptic` | strong | Edge cases, brittle arguments, suspicious steps |
| `NotationAuditor` | fast | Notation consistency, undefined symbols, circular refs |
| `ExpositionReferee` | fast | Readability, missing intuition, proof strategy clarity |

Tiers map to concrete model IDs via `--strong-model` / `--fast-model`, defaulting to `DEFAULT_MODEL_STRONG` and `DEFAULT_MODEL_FAST`. All four reviewers return the same JSON schema: `{ issues: [ { title, severity, type, location, quote, analysis, suggested_fix, confidence } ] }`.

Prompt caching is enabled on the global-context and reviewer-prompt system blocks, cutting repeated input token cost by ~90 % after the first call per reviewer. Note the minimum cacheable prefix is model-dependent (2048 tokens on Opus 4.7), so a short global context silently will not cache.

The final referee (`prompts/final_referee.md`) receives global context + all issues + full paper and produces a structured markdown report. When reviewer calls have failed, `format_coverage_note` prepends the gaps so the report states its own limits.

## Invariants worth preserving

- **Per-chunk JSON is written before `issues.jsonl` is appended.** It is the resume marker; the ordering trades a rare lost-issue window for never duplicating issues on resume. See the comment in `run_pipeline`.
- **Reviewer prompts keep their lanes.** Each prompt says what *not* to flag because another reviewer covers it. Widening one produces duplicates, not coverage.
- **Errors that would recur identically** (`FATAL_API_ERRORS`) stop the run; everything else is recorded as a per-reviewer failure and the run continues.
- **Tests must not need the network or credentials.** Stub the client, as `FakeClient` does.

## Dependencies

`anthropic`, `pydantic`, `rich`; `pytest` and `ruff` for development. All declared in `pyproject.toml` and locked in `uv.lock` — install with `uv sync`.
