# LLM Reviewer

[![CI](https://github.com/nidrissi/llm-reviewer/actions/workflows/ci.yml/badge.svg)](https://github.com/nidrissi/llm-reviewer/actions/workflows/ci.yml)

A multi-agent pipeline that reviews mathematical papers written in LaTeX. It splits a
`.tex` file into sections, runs a set of specialised LLM-based reviewers over them — some
section by section, some over the whole paper — and synthesises a final referee report in
markdown. Anthropic and OpenAI models can be used separately or together.

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

| Agent | Tier | Thinks | Reads | Role |
|---|---|---|---|---|
| `FormalVerifier` | strong | yes | a section | Proof gaps, invalid inferences, missing hypotheses, load-bearing computations |
| `AdversarialSkeptic` | strong | yes | a section | Edge cases, brittle arguments, degenerate examples |
| `ExpositionReferee` | fast | no | a section | Readability, missing intuition, proof strategy clarity |
| `NotationAuditor` | fast | yes | the whole paper | Symbol consistency, convention drift, broken references |
| `ClaimAuditor` | strong | yes | the whole paper | Overclaiming: what the abstract promises against what the theorems prove |

Each reviewer returns structured JSON issues (`title`, `severity`, `type`, `location`,
`quote`, `analysis`, `suggested_fix`, `confidence`). A final referee pass then synthesises
all of them into one report. Non-mathematical sections (References, Bibliography,
Acknowledgments) are dropped from the section passes automatically; the two whole-paper
reviewers still see the raw source, deliberately, since one needs every `\label` and the
other needs the introduction — which the front-matter threshold can otherwise discard.

Scope follows the shape of the question. Whether an inference holds is decidable from the
argument in front of you, so those reviewers work section by section. Whether a symbol
means the same thing on page 4 as on page 19, or whether the abstract promises what
Theorem 1.1 delivers, is not decidable from any one section — asking a section-scoped
reviewer for it only produces guesses the final referee then has to discard. Those two
read the whole source, once, after the section passes, so they also see everything the
section reviewers found.

Every reviewer is sent a shared `review_protocol.md` ahead of its own prompt: one
severity scale, one confidence scale, and one description of what each output field must
contain. Individual prompts hold only their lane and what they must leave to others.

Extended thinking is enabled per reviewer, on the shape of the task rather than the tier.
Deciding whether a proof step actually follows, or building a counterexample, is multi-step
reasoning and benefits from it. Checking that a symbol was defined before use, or that a
`\ref` resolves, is scanning and matching — it gains nothing from thinking and would cost
tokens and latency for it. The final referee thinks too: it weighs and prioritises every
finding across the whole paper, and runs only once per review.

## Requirements

- Python 3.11 or newer
- Credentials with credit for each provider you select (Anthropic and/or OpenAI)

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

Only providers selected by the resolved preset and model flags are initialized or
contacted. Bare model IDs use Anthropic, so the default command still needs only
Anthropic credentials.

For Anthropic, either export a key:

```bash
export ANTHROPIC_API_KEY=...
```

…or sign in once with the [Anthropic CLI](https://platform.claude.com/docs/en/api/sdks/cli)
and the stored profile is picked up automatically:

```bash
ant auth login
```

Don't pass the key inline on the command line — it ends up in your shell history.

For OpenAI, export an API key:

```bash
export OPENAI_API_KEY=...
```

The preflight checks each distinct selected model with its provider's free input-token
count endpoint before creating output or asking you to approve a paid run. Credential,
permission, and missing-model failures name the provider that rejected the check.

## Cost

**Start with `--dry-run`.** A full review makes one API call per section per
section-scoped reviewer, one whole-paper call for each paper-scoped reviewer, and one
final referee call. Every one of them reads at least a section and several read the whole
paper, so on a long paper it adds up to real money.

```bash
llm-reviewer paper.tex --dry-run
```

This asks each selected provider for the exact input-token count of every dry-run request
and prints a cost estimate without sending a generation request. The individual counts
are exact; the run total is still a lower bound because it excludes the accumulated
"known issues" context that only exists after reviewers start producing findings. Cost
estimates are optional metadata: a model with no trustworthy price on file still works
and is shown as `pricing unknown`.

The output figures are the opposite — ceilings nobody reaches, since they assume every
call emits its full `--max-tokens`. Two are printed: one assuming no call truncates, and
one assuming every call truncates and is retried. Real spend lands well below the first.

Before a real run starts, the tool prints the chunks it extracted and asks you to confirm.

## Privacy

Running this **sends the full text of your paper to every API provider selected by your
model flags**. In a mixed run, both Anthropic and OpenAI receive paper content. If the
paper is unpublished, under embargo, covered by a collaboration agreement, or contains
anything you are not free to disclose to a third party, decide deliberately. Consult the
selected providers' current privacy and data-usage terms before running it.

## Usage

```bash
llm-reviewer paper.tex                          # review, with a confirmation prompt
llm-reviewer paper.tex --output /tmp/review     # choose the output directory
llm-reviewer paper.tex --dry-run                # token count + cost estimate only
llm-reviewer paper.tex --yes                    # skip the prompt (needed in CI/scripts)
llm-reviewer paper.tex --preset sol-luna        # OpenAI Sol for strong, Luna for fast
llm-reviewer paper.tex --preset opus-sonnet     # Anthropic Opus for strong, Sonnet for fast
llm-reviewer paper.tex \
  --strong-model openai:gpt-5.6-sol \
  --fast-model anthropic:claude-sonnet-5        # mixed-provider run
```

| Flag | Meaning |
|---|---|
| `--output DIR` | Where to write results (default: `<input_dir>/review/`) |
| `--dry-run` | Count tokens and estimate cost; send no generation requests |
| `-y`, `--yes` | Skip the confirmation prompt. Required when stdin is not a terminal |
| `--preset NAME` | Set both tiers to `sol-luna` or `opus-sonnet` |
| `--strong-model [PROVIDER:]ID` | Model for the three deep reviewers and final referee |
| `--fast-model [PROVIDER:]ID` | Model for the two lighter reviewers |
| `--max-tokens N` | Output token limit per call (default 32000, maximum 64000) |
| `--effort LEVEL` | `low`, `medium`, `high`, `xhigh`, or `max` (default `high`) |
| `--version` | Print the version |

Exit codes: `0` success, or you declined at the confirmation prompt; `1` finished but
some reviewer calls failed; `2` bad configuration — missing provider credentials, an
unknown or unusable provider/model, nothing to review, or no terminal to confirm on
without `--yes`.

### Multi-file papers

`\input{...}` and `\include{...}` are resolved and inlined before chunking, relative to
the main file's directory and then the including file's. Commented-out references are
ignored, cycles are broken, and a reference that can't be found is left alone with a
warning rather than aborting the run.

### Choosing providers, models, and effort

Defaults are `claude-opus-5` for the strong tier and `claude-sonnet-5` for the fast tier.
Bare IDs remain Anthropic for command and `state.json` compatibility. Prefix a model with
`openai:` or `anthropic:` to select its native provider:

```bash
llm-reviewer paper.tex --preset opus-sonnet
llm-reviewer paper.tex --preset sol-luna
llm-reviewer paper.tex --strong-model claude-opus-4-7 --fast-model claude-sonnet-4-6
llm-reviewer paper.tex --strong-model openai:gpt-5.6-sol --fast-model claude-sonnet-5
```

The presets set both tiers together: `opus-sonnet` resolves to `claude-opus-5` and
`claude-sonnet-5`, while `sol-luna` resolves to `openai:gpt-5.6-sol` and
`openai:gpt-5.6-luna`. An explicit `--strong-model` or `--fast-model` overrides only that
tier, so `--preset sol-luna --fast-model claude-sonnet-5` is a concise mixed-provider
configuration. State records the resolved model IDs, not the preset name, so a preset
command and its fully explicit equivalent can resume the same run.

The final referee always uses the strong model, including its provider. Model flags apply
at the existing strong/fast tier boundary; there are no per-reviewer model flags.

Capability validation is provider-specific. Known incompatible combinations are refused
before spending. Reasoning reviewers receive the selected effort. For an OpenAI reviewer
that does not reason, the adapter requests `none` where the model supports it, or omits
the reasoning field for known non-reasoning models.

`--effort` is the main cost and latency lever — it controls how much the models reason and
spend overall. `high` is the default; drop to `medium` or `low` on a long paper or a quick
pass, raise to `xhigh` or `max` on something that warrants it.

Two constraints are worth knowing:

- `--max-tokens` covers thinking *and* response text together. **It is headroom, not a
  budget to spend.** Unused budget costs nothing, but a call that runs out mid-answer is
  billed in full and returns nothing usable — so a cap set too low *causes* cost rather
  than limiting it. Lower it to save money and you will generally spend more. The default
  of 32000 leaves room for a deep reviewer to think its way through a long section; if you
  see truncation warnings, raise it rather than lowering it.
- Some Anthropic models reject a disabled-thinking request at `xhigh` or `max` effort.
  This can't happen with the defaults, but `--fast-model claude-opus-5 --effort max`
  would hit it, so the combination is refused up front with an explanation rather than
  failing per call.

If a call does exhaust its budget, it is retried once at one provider-mapped `--effort`
level down when that produces a different request. A second truncation is a real failure.
An OpenAI non-reasoning call already sent with `effort=none`, for example, is not repeated
under a lower CLI label that would send the same request. Because a truncated call bills
in full, `--dry-run` reports a conservative retry ceiling.

### Resuming

If a run is interrupted, re-running the same command picks up where it left off, so you
only pay for what's missing. Progress is tracked in `review/state.json` against a hash of
each section's text, which means:

- **Editing the paper works.** Sections you changed are reviewed again; sections you
  didn't are reused. Inserting or deleting a section doesn't disturb the others.
- **The two whole-paper passes re-run on any edit.** They are keyed on the whole source,
  so changing one character anywhere invalidates both. That is correct — a notation drift
  or an overclaim can be created by an edit in a section neither pass would otherwise
  revisit — but it means a resumed run is rarely free, and those two are the most
  expensive calls in it. The pre-run call count tells you before you pay.
- **Changing `--strong-model`, `--fast-model`, `--effort`, `--max-tokens`, or a prompt
  file is refused.** The stored reviews were produced under different settings, and
  presenting them as the output of the new ones would be a lie. Use a different
  `--output` or delete the directory.

Before it starts, the run prints how many calls it will make and how many it is reusing,
so you see the cost implication before confirming.

## Output

Everything lands in the output directory (default `<input_dir>/review/`):

| File | Contents |
|---|---|
| `final_report.md` | The final referee report — start here |
| `chunks/NNN_<section>.tex` | The exact LaTeX each reviewer saw, including the whole-paper chunk |
| `reviews/NNN_<section>_<reviewer>.json` | Raw JSON from each reviewer per chunk |
| `all_issues.json` | Every issue behind the current report, as a JSON array |
| `issues.jsonl` | Append-only log of every issue ever produced here, one per line |
| `state.json` | Resume bookkeeping — which reviews are complete, and under what settings |

`all_issues.json` is the authoritative set for the current report. `issues.jsonl` is a
log: if you edit the paper and re-run, it keeps the superseded findings too.

The numbered prefix is the chunk's position in the document, so both directories sort in
reading order. A re-run keeps that true: reviews it reuses are renamed into their new
positions, and files left over from a previous structure are deleted rather than left
sitting next to the current ones. Everything you see under `chunks/` and `reviews/`
belongs to the run that produced the report beside it.

If any reviewer call fails, the run says so, the final report is told to state its own
coverage gaps, and the exit code is `1`. A failing run never silently presents partial
findings as complete.

## Known limitations

- **Sequential.** Reviewers run one at a time, because each pass is shown the issues found
  so far. A ten-section paper is over thirty serial API calls, so expect it to be slow.
- **`\section` only.** Documents structured with `\chapter`, or with no sectioning at all,
  produce nothing to review and exit with an error.
- **Three of the five reviewers see one section at a time**, plus a shared global context
  (title, abstract, preamble, first 15 theorems/definitions). An error that only shows up
  when two distant sections are read together will be caught only if it falls in the
  notation or overclaiming lanes, which are the two that read the whole paper.
- **No access to the literature.** Nothing here can read a reference, so the report never
  says a result is already known. Novelty is assessed only against what the paper itself
  claims and cites; genuine prior-art judgement remains entirely yours.
- **`\input` is confined to the document's own directory.** A reference pointing outside
  it — an absolute path, or `../` climbing out — is refused with a warning rather than
  inlined, since running this on a paper from someone else would otherwise let the file
  read anything you can read and send it to the API.
- **Prompt caching is not measured.** Anthropic receives ephemeral cache controls on the
  stable system blocks. OpenAI GPT-5.6 models receive explicit breakpoints on equivalent
  developer `input_text` blocks, explicit-only cache mode, and a deterministic
  paper-specific `prompt_cache_key`. The providers have different eligibility, lifetime,
  and cache-write pricing rules, so `--dry-run` reports exact token counts without
  claiming a cache saving. Inspect returned usage before quoting one.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and papers that break the chunker are
especially welcome — [open an issue](https://github.com/nidrissi/llm-reviewer/issues).

## License

MIT — see [LICENSE](LICENSE).
