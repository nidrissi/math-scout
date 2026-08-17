# Contributing

Thanks for looking. This is alpha software and the most useful contributions right now are
bug reports — especially papers whose structure breaks the chunker.

## Setup

```bash
git clone https://github.com/nidrissi/llm-reviewer
cd llm-reviewer
uv sync
```

## Before opening a PR

```bash
uv run ruff check
uv run ruff format
uv run pytest
```

CI runs the same checks on Python 3.11 and 3.12, except that it verifies formatting with
`ruff format --check` rather than rewriting files.

## Tests

Tests live in `tests/` and cover the pure, API-free parts of the pipeline plus native
provider request translation: chunking, `\input` resolution, resume behavior, prompt
composition, SDK request shapes, and issue handling. They never hit the network and need
no credentials — please keep it that way. Orchestration tests use a provider-neutral
fake; adapter tests use fake native clients and bind calls to installed SDK signatures.

## Changing prompts

Prompt edits in `src/llm_reviewer/prompts/` are the highest-leverage and least testable
part of this project. There is no automated check that a prompt change is an improvement,
so please run the pipeline on a real paper before and after and say what changed in the
PR — which issues appeared, which disappeared, and whether the new ones are real.

Keep the lanes separate: each reviewer's prompt explicitly tells it what *not* to flag
because another reviewer handles it. Widening one reviewer's scope usually means
duplicate findings rather than better coverage.

## Reporting bugs

Useful reports include the command you ran, what you expected, and what happened. For
chunking or `\input` problems, a minimal `.tex` file that reproduces it is worth more than
anything else — those turn directly into test cases.

Please don't paste unpublished mathematics into a public issue. A reduced fixture with the
real content stripped out is enough.
