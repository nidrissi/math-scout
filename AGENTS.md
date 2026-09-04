# AGENTS.md

This file provides guidance to AI agents when working with code in this repository.

## What this project does

`math-scout` is a multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a `.tex` file into sections, runs five specialized LLM-based reviewers in sequence — three over each section, two over the whole source — and synthesizes a final referee report in markdown. Anthropic and OpenAI models can be used separately or together.

## Running the pipeline

```bash
uv sync
uv run math-scout path/to/paper.tex [--output <dir>]
```

Credentials are required only for the providers selected by the resolved model configuration. Anthropic uses `ANTHROPIC_API_KEY` or a profile stored by `ant auth login`; OpenAI uses `OPENAI_API_KEY`. Never pass a key inline on the command line.

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
src/math_scout/reviewer.py    # all pipeline logic
src/math_scout/cli.py         # argparse, credential pre-flight, exit codes
src/math_scout/providers/*.py # provider-neutral contracts and native SDK adapters
src/math_scout/prompts/*.md   # shared protocol, reviewer and final-referee prompts (package data)
tests/test_reviewer.py        # orchestration and pure-function tests
tests/test_providers.py       # native adapter contract tests with stubbed clients
tests/test_cli.py             # argument parsing and exit codes
```

## Architecture

**Pipeline stages** (`run_pipeline`):
1. Load `.tex`, inlining `\input{}`/`\include{}` (`resolve_inputs`), then extract global context (title + abstract + preamble + up to 15 theorems via regex)
2. Split into `Chunk`s by `\section{}` boundaries; skip non-mathematical sections (References, Bibliography, Acknowledgments). Body text before the first `\section` becomes a "Front matter" chunk when it holds enough prose
3. For each chunk × each section-scoped reviewer: call its configured provider, save per-chunk JSON, append issues to `issues.jsonl`
4. Then once over the whole source (a `Chunk` named `WHOLE_PAPER_NAME`) for each paper-scoped reviewer, so they see every section finding
5. Run final referee over the full paper + all issues → `final_report.md`

`cli.py` calls `check_access` first: a free input-token-count probe through each selected provider for every distinct model. This validates credentials and model IDs before anything is written or any generation request is sent.

**Reviewers** (defined in `REVIEWER_SPECS` as `ReviewerSpec(prompt, tier, thinking, scope)`, resolved to `ReviewerConfig` by `load_prompts`):

| Name | Tier | Thinking | Scope | Focus |
|---|---|---|---|---|
| `FormalVerifier` | strong | adaptive | section | Proof gaps, invalid inferences, hidden assumptions, load-bearing computations |
| `AdversarialSkeptic` | strong | adaptive | section | Edge cases, brittle arguments, suspicious steps |
| `ExpositionReferee` | fast | disabled | section | Readability, missing intuition, proof strategy clarity |
| `NotationAuditor` | fast | adaptive | **paper** | Notation consistency, convention drift, undefined and broken references, circularity |
| `ClaimAuditor` | strong | adaptive | **paper** | Overclaiming: abstract and introduction against what the theorems prove |

Tiers map to provider-qualified model IDs via `--strong-model` / `--fast-model`, defaulting to `DEFAULT_MODEL_STRONG` and `DEFAULT_MODEL_FAST`. `--preset opus-sonnet` and `--preset sol-luna` select common same-provider pairs; an explicit tier flag can override either half. Every reviewer returns the same JSON schema: `{ issues: [ { title, severity, type, location, quote, analysis, suggested_fix, confidence } ] }`.

Thinking is a property of the task, not the tier — and scope can move a reviewer across that line, as it did for `NotationAuditor`: whole-paper consistency means tracing the order results are established in and collapsing every occurrence of a symbol into one finding, which is not the lookup that per-section consistency was. The provider adapters translate the `GenerationRequest.reasoning` flag and effort level into their native API parameters. The final referee always thinks. `--effort` applies uniformly to every call, with provider-specific validation and retry mapping.

Scope is also a property of the task. Consistency and overclaiming are relations between two places in the document, so they are not decidable from one section; asking a section-scoped reviewer for them yields guesses that the final referee has to pay to discard. `LoadedPrompts.scoped()` partitions the reviewers, and `_format_review_target` labels the payload `SECTION UNDER REVIEW` or `FULL PAPER UNDER REVIEW` accordingly.

**Prompt composition.** Every reviewer call sends three system blocks: global context, `prompts/review_protocol.md`, then that reviewer's own prompt. The protocol holds the payload map, the single severity and confidence scales, and the field-by-field output contract; the individual prompts hold only a lane and its exclusions. The final referee passes `review_protocol=None` — it writes markdown, not the issue schema.

Each stable block is marked as a cache breakpoint, and the first two are byte-identical across reviewers, so reviewers on one model can share a prefix. Anthropic receives native ephemeral cache controls; OpenAI GPT-5.6 receives equivalent developer `input_text` breakpoints, explicit-only cache mode, and a deterministic paper-specific cache key. The saving is still **unmeasured** — inspect the provider-normalized usage before quoting a number.

The final referee (`prompts/final_referee.md`) receives global context + all issues + full paper and produces a structured markdown report with seven fixed sections. When reviewer calls have failed, `format_coverage_note` goes in as its own message block ahead of `DETECTED ISSUES` so the report states its own limits without the note reading as a finding.

## Invariants worth preserving

- **Resume hangs off `state.json`, not off filenames.** Chunks are identified by `chunk_key` (a hash of title + text), so an inserted section does not shift every identity, and an edited section is re-reviewed while its neighbours are not. Filenames carry an index prefix so output sorts in document order, and `retarget_reviews` moves a reused review whenever its chunk's position changes, so that ordering stays true across runs. `sweep` then deletes anything neither the current chunks nor `state.json` account for. `state.json` remains the authority on which file holds which chunk's review; the filenames are for humans. A settings change (models, effort, max tokens, or the prompt text itself via `prompts_digest`) is refused rather than silently reusing incomparable reviews. The whole-paper chunk's key must be passed to `state.prune` alongside the section keys, or its reviews are discarded on every resume.
- **Existing output is untrusted input.** A paper bundle can arrive with `review/state.json`
  and a pre-populated output tree. Stored review names must remain validated direct
  children of `reviews/`, and pipeline-managed directories and files must never follow
  symbolic links; otherwise resume moves or ordinary writes can escape the output tree.
- **`reviews/*.json` is the source of truth for issues, not `issues.jsonl`.** `all_issues.json` is rebuilt from the stored reviews each run, so a crash between writing a review and appending to the log costs at most a repeated call. `issues.jsonl` is an append-only log and may hold superseded entries.
- **Section structure is matched against `mask_non_content(tex)`**, never the raw text, so a commented-out or verbatim-quoted `\section` neither invents a chunk nor truncates its neighbour. The mask preserves character offsets, so matches still index into the original.
- **`\input` targets are confined to the document's directory** (`_is_within`). Papers come from other people; without this, `\input{/etc/passwd}` would exfiltrate to the API.
- **Reviewer prompts keep their lanes.** Each prompt says what *not* to flag, naming the reviewer that covers it. Widening one produces duplicates, not coverage. Anything that should hold for every reviewer belongs in `review_protocol.md`, not copied into five files where it will drift.
- **Every schema field a reviewer fills is read downstream.** `_format_issue_full` carries `type`, `location`, `quote` and `confidence` into the final referee: the quote is how it checks a finding against the source, and the confidence is how it tells a demonstrated defect from a lead. A field the prompts calibrate but the report never sees is wasted tokens on every call.
- **No pass may assert prior art.** Nothing here can read a reference, so a claim that a result is already known would be a fabrication. The protocol, `claim_auditor.md` and `final_referee.md` each forbid it independently; keep all three.
- **Errors that would recur identically** (`FATAL_PROVIDER_ERRORS`) stop the run; everything else is recorded as a per-reviewer failure and the run continues.
- **Every generation call streams** through its provider's native SDK. Anthropic finalizes a `ParsedMessage` with `get_final_message()`; OpenAI finalizes its response with `get_final_response()`. Structured output and usage are normalized behind `GenerationResult`. `MAX_OUTPUT_TOKENS` (64000) is only a CLI typo guard; model-specific output limits live in the provider capability tables, which are checked before a run.
- **`max_tokens` is headroom, not a budget to spend.** Thinking and the response share it, and a call cut off mid-JSON is billed in full and yields nothing — so a cap set too low *causes* cost rather than limiting it. `DEFAULT_MAX_TOKENS` is 32000 for that reason. When a reviewer truncates anyway, `call_reviewer` retries once at one `--effort` level down (`lower_effort`) and `run_final_referee` does the same; a second truncation is a real failure. Note that a retried review was produced at lower effort than `state.json` records for the run.
- **Tests must not need the network or credentials.** Stub the client, as `FakeClient` does.

## Dependencies

`anthropic`, `openai`, `pydantic`, `rich`; `httpx2`, `pytest`, and `ruff` for development. All are declared in `pyproject.toml` and locked in `uv.lock` — install with `uv sync`.
