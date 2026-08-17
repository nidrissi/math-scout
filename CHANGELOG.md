# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat that before
1.0 anything may change between releases.

## [Unreleased]

**This invalidates existing output directories.** State now records qualified Anthropic
model IDs, so prior state containing bare IDs no longer matches. Delete `review/`, or pass
a different `--output`; existing paid reviews are not migrated.

### Added

- Native OpenAI Responses API support alongside Anthropic. Explicit model flags require
  `provider:model`, so strong and fast tiers can use different providers while the final
  referee continues to follow the strong tier. Bare model IDs are rejected rather than
  inferred as Anthropic.
- Provider-neutral generation, token-counting, capability, pricing, retry, and error
  contracts, with credential-free adapter tests bound to both installed SDK signatures.
- `--preset opus-sonnet` and `--preset sol-luna` select the common Anthropic and OpenAI
  strong/fast pairs; explicit tier flags can override either half of a preset.

### Changed

- Dry runs group exact token counts by provider-qualified model and keep working when
  pricing metadata is unavailable. Every model in resume settings is stored with its
  provider; historical bare Anthropic state is rejected by the settings-mismatch guard.
- Prompt caching is translated natively: Anthropic cache controls are preserved, while
  OpenAI GPT-5.6 requests use developer `input_text` breakpoints and a deterministic
  paper-specific cache key.
- OpenAI Luna dry-run estimates use its current $0.20 input and $1.20 output rates per
  million tokens.

## [0.1.0a2] — 2026-08-16

**This invalidates existing output directories**, on two counts: the `--max-tokens`
default has changed, and the reviewer prompts have (see the prompt audit below). Both are
part of the settings a resume is keyed on. Delete `review/`, or pass a different
`--output`.

### Fixed

- Reviewers ran out of output budget mid-answer on ordinary sections and the whole run
  degraded. On one 100k-character paper, 11 of 23 reviewer calls and the final referee
  were cut off before finishing their JSON, and the report was synthesised from partial
  coverage. Truncated calls are billed in full and return nothing usable, so roughly 60%
  of that run's spend bought discarded tokens.

  The cause was `max_tokens`, which thinking and the response share, pinned at 16000
  because every call was non-streaming — the SDK refuses a non-streaming request
  estimated to run past ten minutes, which capped the budget at 21333. Every generation
  call now streams, so the cap is the model's own output limit instead. This is why
  raising the default is not a cost increase: unused budget is never billed, so the old
  cap was *causing* the spend rather than limiting it.

  Only the reviewer with thinking disabled was unaffected, on every section size — which
  is what identified thinking as what the budget was going to.

- `chunks/` and `reviews/` accumulated files from earlier runs. Nothing removed them, and
  a reused review kept the filename it was first written under, so after inserting or
  deleting a section the index prefixes no longer matched the document — `chunks/` could
  hold both `00_Alpha.tex` and `01_Alpha.tex` with no way to tell which was current.
  Reviews are now renamed into their new positions when a chunk moves, and anything
  neither the current chunks nor `state.json` account for is deleted. Nothing is re-run:
  the rename is bookkeeping, not a re-review.
- The chunk index is three digits, so the prefix still sorts past a hundred sections.
  Existing output directories rename themselves on the next run.

### Changed

- `--max-tokens` defaults to 32000 (was 16000) and accepts up to 64000 (was 21333). Every
  supported model allows at least 64K output tokens; the Opus 5 and Sonnet 5 families
  allow 128K.
- A call that exhausts its budget is retried once at one `--effort` level down, trading
  some depth on that section for producing a review at all. A second truncation is
  reported as a failed call and the run continues, as before.
- `--dry-run` prints two output ceilings rather than one: assuming no call truncates, and
  assuming every call truncates and is retried. The single figure it printed before stopped
  being an upper bound once a call could bill two attempts.

Prompt audit: the reviewer set, the prompts, and the plumbing between them.

**This invalidates existing output directories.** `run_settings` now records a hash of
every prompt that shapes a stored review — the protocol and the five reviewer prompts, but
not `final_referee.md`, which shapes only the report and is regenerated on every run. A
`review/` produced before this change is refused rather than mixed with findings from the
new prompts. Delete it, or pass a different `--output`.

### Added

- `ClaimAuditor`, a fifth reviewer, reading the whole paper: it compares what the abstract
  and introduction promise against what the theorems actually state and prove. Nothing
  previously checked for overclaiming, and the recommendation was purely defect-driven —
  a correct but unremarkable paper scored **Accept**.
- `prompts/review_protocol.md`, sent to every reviewer ahead of its own prompt. It holds
  one severity scale, one confidence scale, and one field-by-field description of the
  output schema. The four prompts previously carried their own copies, which had drifted,
  and which fed a single filter in the final referee as though they were comparable.
- A reviewer `scope`: `NotationAuditor` now reads the whole paper in one pass instead of
  each section separately. Consistency is a relation between two occurrences, and it could
  previously see only one of them; its own prompt told it to hedge with "appears to", and
  the final referee had a rule for discarding the false positives that produced. On a
  multi-section paper this is also fewer calls than before.
- A `# Questions for the authors` section in the report, so a low-confidence finding has
  somewhere to go other than being inflated into a main concern or dropped.
- Explicit instructions, in three separate prompts, never to assert prior art: nothing
  here can read a reference, so a claim that a result is already known would be invented.

### Fixed

- Every reviewer prompt told the model to skip concerns already in a `KNOWN ISSUES` block.
  No such block was ever sent — the pipeline sends `DETECTED ISSUES` — so the
  de-duplication instruction had never once fired.
- `type`, `quote` and `confidence` were collected from every reviewer, written to
  `reviews/*.json`, and then dropped before the final referee saw anything. Four prompts
  were calibrating a confidence score that reached nothing. All three now reach the report,
  and the referee is told how to use them: the quote to check a finding against the source,
  the confidence to tell a demonstrated defect from a lead.
- The final referee prompt described its input as "deduplicated issues". They were never
  deduplicated. It now says so, and deduplicating is stated as its job.
- The `COVERAGE GAPS` note about failed reviewer calls was concatenated onto the front of
  the issue list, so it appeared under the `DETECTED ISSUES` heading as though it were a
  finding. It is now its own message block.
- Nothing checked arithmetic: `FormalVerifier` was told not to look for algebraic mistakes
  and no other reviewer covered them. It now works through computations the argument rests
  on, while still leaving routine algebra alone.
- Reviewers are now told what they can and cannot see, including that the `## Theorem N`
  headings in the global context are extraction indices rather than the paper's own
  numbering — a reviewer citing "Theorem 3" could mean neither.
- `ExpositionReferee` findings are capped at `major` and routed to the report's exposition
  section. A hard-to-follow proof was previously able to enter Main concerns beside a
  theorem that does not hold.

### Changed

- `NotationAuditor` now runs with thinking enabled. Whole-paper scope turned its job into
  tracing the order in which results are actually established and collapsing every
  occurrence of a symbol into one finding — neither is the lookup that per-section
  consistency checking was. It stays on the fast tier: the work is bookkeeping, not
  mathematics.
- The shared severity scale now says how it applies to findings that are not about
  correctness. Grading exposition and notation purely against the paper's claims left
  them no rung above `minor`, since by construction they leave the claims alone.
- "A statement carries a hypothesis its proof never uses" and "a proof needs a hypothesis
  its statement does not grant" were claimable by three reviewers under three type slugs,
  and `notation_auditor.md` asserted and disclaimed the same defect twelve lines apart.
  Each direction now has exactly one owner.
- The final referee's routing rules are ordered and disjoint, so a finding lands in one
  section rather than matching two rules with no stated precedence.
- "Used but never defined anywhere in the paper" belonged to nobody: section reviewers
  are told to presume such a symbol is defined elsewhere, and nothing picked it up
  afterwards. It is `NotationAuditor`'s, which is the only pass that can see the whole
  document.

## [0.1.0a1] — 2026-08-03

First public release. Everything before this lived only in the author's working tree.

### Added

- MIT license, packaging metadata, and a `llm-reviewer` command installable with
  `uvx --from git+https://github.com/nidrissi/llm-reviewer`.
- Multi-file LaTeX support: `\input{}` and `\include{}` are resolved and inlined before
  chunking, with commented-out references ignored, cycles broken, and missing targets
  warned about rather than fatal.
- `--yes/-y` for non-interactive runs, plus a clear error instead of an `EOFError`
  traceback when stdin is not a terminal and the flag is absent.
- `--strong-model`, `--fast-model`, `--max-tokens`, and `--effort` flags. Cost estimation
  covers the current Opus, Sonnet, and Haiku models; unknown model IDs still run, and
  `--dry-run` reports their token counts without a price.
- Per-reviewer extended thinking, chosen on the shape of each task: `FormalVerifier`,
  `AdversarialSkeptic`, and the final referee think; `NotationAuditor` and
  `ExpositionReferee` do not, because symbol and reference checking is scanning rather
  than reasoning.
- Invalid invocations are refused before any request: `--max-tokens` above the
  non-streaming ceiling of 21333, and a disabled-thinking reviewer on a model that
  rejects that at `xhigh` or `max` effort.
- A free pre-flight check that verifies credentials and every model ID before the run
  writes anything or asks for confirmation.
- Failure reporting: reviewer failures are collected, summarised, passed to the final
  referee so the report states its own coverage gaps, and reflected in the exit code
  (`0` clean, `1` incomplete, `2` misconfigured).
- Body text between `\begin{document}` and the first `\section` is now reviewed as a
  "Front matter" chunk when it holds enough prose.
- Test suite, ruff configuration, and GitHub Actions CI on Python 3.11 and 3.12.

### Changed

- Default models are now `claude-opus-5` and `claude-sonnet-5`. Opus 5 costs the same as
  Opus 4.7 and lowers the minimum cacheable prompt from 2048 to 512 tokens, so short
  global contexts now benefit from prompt caching. Sonnet 5 uses a newer tokenizer that
  produces roughly 30% more tokens for the same text, so the two fast reviewers cost
  proportionally more per run at the same per-token rate.
- `--max-tokens` defaults to 16000, up from 8192, because it now covers extended thinking
  and response text together. It is capped at 21333, above which the SDK requires
  streaming.
- A reviewer that runs out of tokens mid-JSON now says so and names the fix, instead of
  reporting a generic "no parsed output".
- Credentials resolve through the Anthropic SDK, so an `ant auth login` profile works in
  addition to `ANTHROPIC_API_KEY`.
- Chunk output filenames are prefixed with the section index, so two sections whose titles
  reduce to the same slug no longer overwrite each other.
- Errors that would recur identically on every call — rejected credentials, an unknown
  model — now stop the run instead of repeating once per reviewer per section.
- A failure in the final referee pass no longer discards the run; collected issues stay on
  disk and re-running resumes from them.
- `\title{}` containing braces (for example `\title{Cohomology of $\mathbb{Z}$}`) is now
  extracted correctly.

### Fixed

- Resume is tracked in `review/state.json` against a hash of each section's text, not by
  the existence of a review file. Previously, editing the paper and re-running silently
  reused reviews of text that no longer existed and reported them as current, while
  inserting a section shifted every filename and duplicated every prior issue in the
  final report. Changing models, effort, or token limits between runs is now refused
  rather than silently ignored.
- Issues are rebuilt from the stored reviews rather than from `issues.jsonl`, closing the
  window where a crash between writing a review and appending to the log lost findings
  that were sitting intact on disk.
- Section headings are matched against comment- and verbatim-masked text. A commented-out
  `\section` no longer splits the section it sits in or invents one, and a `\section`
  quoted inside `verbatim` no longer deletes the text that follows it from the review.
- `\input` and `\include` are confined to the document's own directory. An absolute path
  or a `../` escape previously read the target and sent it to the API.
- A non-UTF-8 source produces a clear error naming the file instead of an uncaught
  `UnicodeDecodeError`; Latin-1 is common in older LaTeX.
- The pre-flight reports rate limits and server errors instead of raising them as
  tracebacks, and retries briefly first.
- `--fast-model anthropic:claude-haiku-4-5` is refused with an explanation: Haiku 4.5
  rejects the effort and thinking settings every call carries, so it could not have
  worked.
- A typo in the input path no longer leaves an empty output directory behind.

### Removed

- The unused `RapidFuzz` dependency, and `.github/requirements.txt` in favour of
  `pyproject.toml`.

[0.1.0a2]: https://github.com/nidrissi/llm-reviewer/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/nidrissi/llm-reviewer/releases/tag/v0.1.0a1
