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
- `review/chunks/<NN>_<section>.tex` — the exact LaTeX each reviewer saw
- `review/reviews/<NN>_<section>_<reviewer>.json` — per-chunk reviewer output; the source of truth for issues
- `review/state.json` — resume bookkeeping: completed reviews keyed by chunk hash, plus the settings that produced them
- `review/all_issues.json` — the issues behind the current report, rebuilt from `reviews/` each run
- `review/issues.jsonl` — append-only log of every issue ever produced here; may contain superseded entries
- `review/final_report.md` — final synthesis in markdown

Use `--dry-run` to count tokens and estimate cost without sending generation requests, and `--yes` to skip the confirmation prompt in non-interactive contexts.

Exit codes: `0` clean (or declined at the prompt — declining is a choice, not an error), `1` finished with failed reviewer calls, `2` misconfigured (no credentials, unknown or unusable model, nothing to review, no terminal to confirm on without `--yes`).

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

**Reviewers** (defined in `REVIEWER_SPECS` as `ReviewerSpec(prompt, tier, thinking)`, resolved to `ReviewerConfig` by `load_prompts`):

| Name | Tier | Thinking | Focus |
|---|---|---|---|
| `FormalVerifier` | strong | adaptive | Proof gaps, invalid inferences, hidden assumptions |
| `AdversarialSkeptic` | strong | adaptive | Edge cases, brittle arguments, suspicious steps |
| `NotationAuditor` | fast | disabled | Notation consistency, undefined symbols, circular refs |
| `ExpositionReferee` | fast | disabled | Readability, missing intuition, proof strategy clarity |

Tiers map to concrete model IDs via `--strong-model` / `--fast-model`, defaulting to `DEFAULT_MODEL_STRONG` and `DEFAULT_MODEL_FAST`. All four reviewers return the same JSON schema: `{ issues: [ { title, severity, type, location, quote, analysis, suggested_fix, confidence } ] }`.

Thinking is a property of the task, not the tier — the two happen to align because the tiers were chosen on the same reasoning-depth axis. `ReviewerConfig.thinking_config` turns the flag into the API parameter. The final referee always thinks. `--effort` applies uniformly to every call.

Prompt caching is enabled on the global-context and reviewer-prompt system blocks. Cache reads are a tenth of the input rate, but the actual saving here is **unmeasured**: successive calls to the same reviewer are separated by three other calls, so whether entries survive the default five-minute TTL depends on how long those take. Check `usage.cache_read_input_tokens` before quoting a number, and consider a `ttl: "1h"` breakpoint if they are expiring. The minimum cacheable prefix is model-dependent (512 tokens on Opus 5, 1024 on Sonnet 5), so a very short global context silently will not cache.

The final referee (`prompts/final_referee.md`) receives global context + all issues + full paper and produces a structured markdown report. When reviewer calls have failed, `format_coverage_note` prepends the gaps so the report states its own limits.

## Invariants worth preserving

- **Resume hangs off `state.json`, not off filenames.** Chunks are identified by `chunk_key` (a hash of title + text), so an inserted section does not shift every identity, and an edited section is re-reviewed while its neighbours are not. Filenames keep their index prefix purely so output sorts in document order. A settings change (models, effort, max tokens) is refused rather than silently reusing incomparable reviews.
- **`reviews/*.json` is the source of truth for issues, not `issues.jsonl`.** `all_issues.json` is rebuilt from the stored reviews each run, so a crash between writing a review and appending to the log costs at most a repeated call. `issues.jsonl` is an append-only log and may hold superseded entries.
- **Section structure is matched against `mask_non_content(tex)`**, never the raw text, so a commented-out or verbatim-quoted `\section` neither invents a chunk nor truncates its neighbour. The mask preserves character offsets, so matches still index into the original.
- **`\input` targets are confined to the document's directory** (`_is_within`). Papers come from other people; without this, `\input{/etc/passwd}` would exfiltrate to the API.
- **Reviewer prompts keep their lanes.** Each prompt says what *not* to flag because another reviewer covers it. Widening one produces duplicates, not coverage.
- **Errors that would recur identically** (`FATAL_API_ERRORS`) stop the run; everything else is recorded as a per-reviewer failure and the run continues.
- **Every call is non-streaming**, so `max_tokens` must stay at or below `MAX_NONSTREAMING_TOKENS` (21333) — past that the SDK raises `ValueError` rather than making the request. `validate_settings` enforces it. Raising the cap means moving the pipeline to `messages.stream`.
- **Tests must not need the network or credentials.** Stub the client, as `FakeClient` does.

## Dependencies

`anthropic`, `pydantic`, `rich`; `pytest` and `ruff` for development. All declared in `pyproject.toml` and locked in `uv.lock` — install with `uv sync`.
