# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) — with the caveat that before
1.0 anything may change between releases.

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
- `--fast-model claude-haiku-4-5` is refused with an explanation: Haiku 4.5 rejects the
  effort and thinking settings every call carries, so it could not have worked.
- A typo in the input path no longer leaves an empty output directory behind.

### Removed

- The unused `RapidFuzz` dependency, and `.github/requirements.txt` in favour of
  `pyproject.toml`.

[0.1.0a1]: https://github.com/nidrissi/llm-reviewer/releases/tag/v0.1.0a1
