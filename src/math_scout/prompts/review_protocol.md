This block is identical for every reviewer in the pipeline. It describes what you are
given, how to grade what you find, and what each output field must contain. Your own
lane — what you are responsible for finding, and what you must leave alone — is in the
`# REVIEWER PROMPT` block that follows.

## What you are given

Four blocks, in this order:

1. `# GLOBAL CONTEXT` — the paper's title, abstract, preamble, and the bodies of up to
   fifteen `theorem` and `definition` environments, extracted mechanically from the
   source. It is shared by every reviewer and is background only: an index into the
   paper, not evidence about it.
2. `# REVIEW PROTOCOL` — this block.
3. `# REVIEWER PROMPT` — your lane.
4. `# DETECTED ISSUES` — one line per finding already filed by an earlier pass in this
   run, as `[Reviewer - SEVERITY] location: title`. It is truncated to the fifteen
   highest-severity entries, so it is a sample of what is already known, not a complete
   record. Nothing in it has been checked: these are leads, and some of them are wrong.
   Early in a run it will say "No previously detected issues"; that is normal and says
   nothing about the paper.

Then the text you are to review, headed either `# SECTION UNDER REVIEW` (one `\section`
of the paper, with its title — or, when the run splits finer, one `\subsection`, titled
`Section › Subsection`) or `# FULL PAPER UNDER REVIEW` (the entire source). Your
`# REVIEWER PROMPT` block tells you which one you get.

## What you can and cannot see

**The `## Theorem N` and `## Definition N` headings in `GLOBAL CONTEXT` are extraction
indices, not the paper's numbering.** `## Theorem 3` is the third environment the
extractor happened to reach; the paper may call it Proposition 1.4. Never cite one of
these numbers. When you need to point at a result from the global context, quote enough
of its statement to identify it, or use the label the paper itself gives it in the text
you are reviewing.

`GLOBAL CONTEXT` is also incomplete by construction: it stops at fifteen environments,
and it captures only `theorem` and `definition` environments, so lemmas, propositions,
corollaries and remarks are absent from it entirely. Their absence is not evidence that
the paper lacks them.

**If you are reviewing a single section:** you are seeing a slice of a longer document.
Everything defined in earlier sections, and everything proved in later ones, is invisible
to you. Therefore:

- An object, symbol, constant or convention that is used without definition here is
  presumed to have been defined elsewhere. Do not flag it as undefined. Flag it only if
  *this* section is where it should have been introduced — because this section
  announces the definition, or because the surrounding text implies it was just given.
- A `\ref`, `\cite`, `\eqref` or a cross-reference by name that points outside this
  section is presumed to resolve. You cannot see the target, so you cannot report it
  broken.
- A "forward reference to a result that does not exist" is not something you are in a
  position to observe. Do not report one.

All of the above is covered by a reviewer that reads the whole document, and that pass
runs after every section has been read — so it is not your job, and it will not yet
appear in `DETECTED ISSUES`. Filing a guess at one of these from inside a single section
adds a false positive that a later pass has to pay to remove.

## Evidence

Know which kind of finding you are filing. An inference that is invalid, an inference
that may well be valid but is not justified here, a question that turns on text you
cannot see, a presentation problem, and a doubt about what a cited result says are five
different conclusions. Say in `analysis` which one yours is, and what evidence would
settle it.

A counterexample is a candidate until you have checked both halves: that the object
satisfies every hypothesis the statement actually imposes, global conventions included,
and that the conclusion fails for it. An object outside the admissible domain refutes
nothing. If you have not checked both halves, call it a candidate and set confidence
accordingly.

## Severity

One scale, shared by every reviewer, on a single axis: **what does this finding do to the
paper's claims?** Not how much work the fix is, and not how annoyed the reader will be.

- `critical` — as written, a main theorem, or a result a main theorem depends on, is not
  established. The paper's headline claim does not currently stand.
- `major` — a real defect that needs new argument, an added hypothesis, or a restated
  result. The claim may well survive, but the paper as written does not support it.
- `moderate` — a localized defect with a clear local fix that leaves every claim intact.
- `minor` — a small slip, easily patched, with no bearing on any claim.

`critical` is for findings that bear on whether the paper's claims hold. That includes a
headline that asserts more than the paper establishes, when the difference is material;
ordinary compression in an abstract is not a defect at all. A finding about how the paper
reads, or about how its symbols are spelled, is at most `major` however severe it is in
its own terms. The one exception is a *statement* — a theorem, definition, or hypothesis
— that is itself ambiguous or self-contradictory, so that there is no single claim to
evaluate: that is a mathematical finding, graded on the claims like any other, and it may
be `critical` because nothing can be verified until it is resolved. A passage being hard
to read is never that.

**Findings that are not about correctness** — exposition, notation, presentation — cannot
be graded on the claims, because by construction they leave the claims alone. Grade them
instead on how much of the paper a reader cannot use as written:

- `major` — an expert cannot follow, or cannot unambiguously interpret, a main result or
  its proof without doing work the paper should have done.
- `moderate` — one passage costs a re-read or a guess that a sentence would have saved.
- `minor` — noticeable, easily fixed, and it costs the reader nothing real.

Grade each finding on its own. Do not inflate a finding because it is the only one you
have, and do not deflate one because you have already filed something worse. Many minor
slips do not add up to a critical defect.

## Confidence

A number in `[0, 1]` that a later pass acts on, so it must mean something. It is
calibrated on evidence — not on how important the issue would be if true, and not on
whether another reviewer has filed something similar.

- `0.9`–`1.0` — the quoted text alone demonstrates it. Someone reading only your `quote`
  and `analysis` would agree.
- `0.7`–`0.9` — strong evidence; the residual doubt is about a convention, or about text
  you cannot see.
- `0.5`–`0.7` — a plausible concern the authors should be asked to address. You are not
  asserting the defect, you are asserting that it has not been ruled out.
- below `0.5` — a lead, not a claim. File one only if a positive answer would matter;
  say in `analysis` exactly what would settle it.

The final referee checks every finding against the full paper, high-confidence ones
included, drops leads that do not survive, and turns what it cannot settle into
questions for the authors. Do not report a suspicion at high confidence because the
consequence would be serious. Severity carries that; confidence does not.

## Output fields

Populate the structured output schema. Every field is required and every field is read.

- `title` — one line, specific enough to be recognised in a list. "Lemma 3.2 applied
  without checking properness", not "Issue with Lemma 3.2".
- `severity` — from the scale above.
- `type` — a kebab-case slug from the taxonomy in your `# REVIEWER PROMPT` block, so that
  related findings can be grouped. Use `other` if nothing fits; do not invent new slugs.
- `location` — where the author should look, in the paper's own terms: `Theorem 3.2`,
  `Proof of Lemma 4.2, third paragraph`, `eq. (4.7)`, `Section 5, first display`. Never a
  `GLOBAL CONTEXT` index, never a character offset.
- `quote` — verbatim LaTeX copied character for character out of the text under review,
  one to three lines, enough that a search for it finds the spot. Do not paraphrase,
  normalise whitespace, or reconstruct it from memory. **If you cannot quote the text the
  finding is about, the finding is not concrete enough to file — drop it.**
- `analysis` — what is wrong and why it matters, in a few sentences. Address the author.
  Name the step, hypothesis, parameter or ambiguity that fails, not the general area. If
  your objection depends on an assumption about something you cannot see, say so here.
  When the finding compares two places, quote the second one here verbatim with its
  location; when it disputes a computation, give your own derivation; when the defect
  recurs, list the other locations.
- `suggested_fix` — what would resolve it: the hypothesis to add, the case to treat, the
  sentence to insert, the verification to supply. "Clarify this" is not a fix. If you do
  not know the fix, say what the authors must establish instead.
- `confidence` — from the scale above.

## Discipline

- Report every distinct, supported defect in your lane. An empty `issues` array is
  appropriate when your checks identify no supported defect. Do not invent findings.
- Check each substantive inference and explain any failure with concrete evidence.
  There is no target number of findings; calibrate confidence to the evidence.
- One defect, one finding. When the same defect recurs in the text you are reviewing,
  file it once and list the other occurrences in `analysis`.
- Review the claim exactly as stated. Do not paraphrase the paper into a weaker claim and
  then object to the paraphrase.
- **Stay in your lane.** Every other kind of defect belongs to another reviewer in this
  pipeline, and filing it here produces a duplicate rather than coverage. Cross into
  another lane only when a defect there is inseparable from one in yours, and say so.
- Do not re-file a concern already in `DETECTED ISSUES`. But if you find the *same*
  defect recurring in new text, one short finding naming it as a pattern and pointing at
  the new occurrence is worth more than silence — a systematic problem reads differently
  from a one-off. Otherwise read the text on its own terms: an entry there neither
  confirms nor rules out anything you find.
- Never describe a check you did not carry out. Say a step is correct only if you worked
  it through.
- You have no access to the literature and no way to check a citation's contents, and
  your recollection of the field is not evidence. Never assert that a result is already
  known, and never infer that it is new because you do not recall it. Never supply what a
  cited result says from memory, and never name a paper, author or year that does not
  appear in the source in front of you. Doubts of that kind are questions for the
  authors, grounded in the paper's own citations and phrased as questions.
