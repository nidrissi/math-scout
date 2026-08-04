# LLM Reviewer

[![CI](https://github.com/nidrissi/llm-reviewer/actions/workflows/ci.yml/badge.svg)](https://github.com/nidrissi/llm-reviewer/actions/workflows/ci.yml)

A multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a
`.tex` file into sections, runs four specialised Claude-based reviewers over each one,
and synthesises a final referee report in markdown.

> **Alpha software.** It works and it is useful, but interfaces, prompts, and output
> formats will change without notice, and there is no backwards-compatibility promise
> before 1.0. Please report what breaks.

## What it is, and what it isn't

It is a **first-pass reading aid**. It is good at the mechanical parts of refereeing:
spotting undefined notation, unstated hypotheses, gaps between "clearly" and the actual
argument, and passages that assume more than they say.

It is **not a referee**, and its output is not a referee report you can rely on. It will
miss real errors and it will confidently flag correct arguments as broken. Every issue it
raises needs a human to verify it before you act on it, and a clean report is not evidence
that a paper is correct. Treat it as a colleague who read the paper quickly, not as a
proof checker.

## Reviewers

| Agent | Tier | Thinks | Role |
|---|---|---|---|
| `FormalVerifier` | strong | yes | Proof gaps, invalid inferences, missing hypotheses |
| `AdversarialSkeptic` | strong | yes | Edge cases, brittle arguments, degenerate examples |
| `NotationAuditor` | fast | no | Symbol consistency, undefined notation, broken references |
| `ExpositionReferee` | fast | no | Readability, missing intuition, proof strategy clarity |

Each reviewer returns structured JSON issues (`title`, `severity`, `type`, `location`,
`quote`, `analysis`, `suggested_fix`, `confidence`). A final referee pass then synthesises
all of them into one report. Non-mathematical sections (References, Bibliography,
Acknowledgments) are skipped automatically.

Extended thinking is enabled per reviewer, on the shape of the task rather than the tier.
Deciding whether a proof step actually follows, or building a counterexample, is multi-step
reasoning and benefits from it. Checking that a symbol was defined before use, or that a
`\ref` resolves, is scanning and matching — it gains nothing from thinking and would cost
tokens and latency for it. The final referee thinks too: it weighs and prioritises every
finding across the whole paper, and runs only once per review.

## Requirements

- Python 3.11 or newer
- An Anthropic API key with credit on it

## Install

With [uv](https://docs.astral.sh/uv/), no checkout needed:

```bash
uvx --from git+https://github.com/nidrissi/llm-reviewer llm-reviewer paper.tex --dry-run
```

Or from a clone:

```bash
git clone https://github.com/nidrissi/llm-reviewer
cd llm-reviewer
uv sync
uv run llm-reviewer paper.tex --dry-run
```

## Credentials

Either export a key:

```bash
export ANTHROPIC_API_KEY=...
```

…or sign in once with the [Anthropic CLI](https://platform.claude.com/docs/en/api/sdks/cli)
and the stored profile is picked up automatically:

```bash
ant auth login
```

Don't pass the key inline on the command line — it ends up in your shell history.

## Cost

**Start with `--dry-run`.** A full review makes one API call per section per reviewer,
plus one whole-paper call at the end, and reads the entire paper each time. On a long
paper that adds up to real money.

```bash
llm-reviewer paper.tex --dry-run
```

This counts input tokens and prints a cost estimate without sending a single generation
request. The estimate is a rough lower bound: it excludes the accumulated "known issues"
context that grows during a run, and it does not model prompt-cache savings, which cut
repeat input cost substantially.

Before a real run starts, the tool prints the chunks it extracted and asks you to confirm.

## Privacy

Running this **sends the full text of your paper to the Anthropic API**. If the paper is
unpublished, under embargo, covered by a collaboration agreement, or contains anything you
are not free to disclose to a third party, that matters — decide deliberately. See
Anthropic's [privacy policy](https://www.anthropic.com/legal/privacy) and
[data usage terms](https://privacy.claude.com/en/articles/10023548-how-do-you-use-my-data)
for how the data is handled.

## Usage

```bash
llm-reviewer paper.tex                          # review, with a confirmation prompt
llm-reviewer paper.tex --output /tmp/review     # choose the output directory
llm-reviewer paper.tex --dry-run                # token count + cost estimate only
llm-reviewer paper.tex --yes                    # skip the prompt (needed in CI/scripts)
```

| Flag | Meaning |
|---|---|
| `--output DIR` | Where to write results (default: `<input_dir>/review/`) |
| `--dry-run` | Count tokens and estimate cost; send no generation requests |
| `-y`, `--yes` | Skip the confirmation prompt. Required when stdin is not a terminal |
| `--strong-model ID` | Model for the two deep reviewers and the final referee |
| `--fast-model ID` | Model for the two lighter reviewers |
| `--max-tokens N` | Output token limit per call (default 16000, maximum 21333) |
| `--effort LEVEL` | `low`, `medium`, `high`, `xhigh`, or `max` (default `high`) |
| `--version` | Print the version |

Exit codes: `0` success, or you declined at the confirmation prompt; `1` finished but
some reviewer calls failed; `2` bad configuration — no credentials, an unknown or
unusable model, nothing to review, or no terminal to confirm on without `--yes`.

### Multi-file papers

`\input{...}` and `\include{...}` are resolved and inlined before chunking, relative to
the main file's directory and then the including file's. Commented-out references are
ignored, cycles are broken, and a reference that can't be found is left alone with a
warning rather than aborting the run.

### Choosing models and effort

Defaults are `claude-opus-5` for the strong tier and `claude-sonnet-5` for the fast tier.
To pin to older models:

```bash
llm-reviewer paper.tex --strong-model claude-opus-4-7 --fast-model claude-sonnet-4-6
```

Cost estimates are available for the models listed in `MODEL_PRICING`; any other ID still
runs, but `--dry-run` will report token counts without a price.

`--effort` is the main cost and latency lever — it controls how much the models reason and
spend overall. `high` is the default; drop to `medium` or `low` on a long paper or a quick
pass, raise to `xhigh` or `max` on something that warrants it.

Two constraints are worth knowing:

- `--max-tokens` covers thinking *and* response text together, and is capped at 21333.
  Above that the SDK requires streaming, which this pipeline does not use.
- Some models reject a disabled-thinking request at `xhigh` or `max` effort. This can't
  happen with the defaults, but `--fast-model claude-opus-5 --effort max` would hit it, so
  the combination is refused up front with an explanation rather than failing per call.

### Resuming

If a run is interrupted, re-running the same command picks up where it left off, so you
only pay for what's missing. Progress is tracked in `review/state.json` against a hash of
each section's text, which means:

- **Editing the paper works.** Sections you changed are reviewed again; sections you
  didn't are reused. Inserting or deleting a section doesn't disturb the others.
- **Changing `--strong-model`, `--fast-model`, `--effort`, or `--max-tokens` is refused.**
  The stored reviews were produced under different settings, and presenting them as the
  output of the new ones would be a lie. Use a different `--output` or delete the
  directory.

Before it starts, the run prints how many calls it will make and how many it is reusing,
so you see the cost implication before confirming.

## Output

Everything lands in the output directory (default `<input_dir>/review/`):

| File | Contents |
|---|---|
| `final_report.md` | The final referee report — start here |
| `chunks/NN_<section>.tex` | The exact LaTeX each reviewer saw |
| `reviews/NN_<section>_<reviewer>.json` | Raw JSON from each reviewer per section |
| `all_issues.json` | Every issue behind the current report, as a JSON array |
| `issues.jsonl` | Append-only log of every issue ever produced here, one per line |
| `state.json` | Resume bookkeeping — which reviews are complete, and under what settings |

`all_issues.json` is the authoritative set for the current report. `issues.jsonl` is a
log: if you edit the paper and re-run, it keeps the superseded findings too.

If any reviewer call fails, the run says so, the final report is told to state its own
coverage gaps, and the exit code is `1`. A failing run never silently presents partial
findings as complete.

## Known limitations

- **Sequential.** Reviewers run one at a time, because each pass is shown the issues found
  so far. A ten-section paper is over forty serial API calls, so expect it to be slow.
- **`\section` only.** Documents structured with `\chapter`, or with no sectioning at all,
  produce nothing to review and exit with an error.
- **Reviewers only see one section at a time**, plus a shared global context (title,
  abstract, preamble, first 15 theorems/definitions). Errors that only appear when two
  distant sections are read together are likely to be missed.
- **`\input` is confined to the document's own directory.** A reference pointing outside
  it — an absolute path, or `../` climbing out — is refused with a warning rather than
  inlined, since running this on a paper from someone else would otherwise let the file
  read anything you can read and send it to the API.
- **Prompt caching is not measured.** Repeated context should be served from cache after
  the first call per reviewer, but the saving depends on the cache outliving the gap
  between one reviewer's calls, which nobody here has verified against
  `usage.cache_read_input_tokens`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and papers that break the chunker are
especially welcome — [open an issue](https://github.com/nidrissi/llm-reviewer/issues).

## License

MIT — see [LICENSE](LICENSE).
