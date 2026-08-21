# Making the Output Trustworthy

This document is about a gap the security work did not close.

The gates in `SECURITY.md` and `TEST_SANDBOX.md` bound what a bad patch can
*reach*. They say nothing about whether a patch is *correct*. Those are
independent properties, and this repository currently has strong containment
and weak correctness evidence. This document explains why, and what to build.

It is opinionated and it is about work that has not been done yet.

---

## 1. What the system actually knows

When the pipeline reports a candidate as eligible, that verdict decomposes
into exactly five inputs (`orchestrator/app/scoring.py:129-155`):

| Gate | Source of truth |
| --- | --- |
| `patch_valid` | Structural parse of the diff — real, mechanical |
| `review_count >= MINIMUM_CRITIC_REVIEWS` | Count of schema-valid model outputs |
| `security_reviews >= MINIMUM_SECURITY_REVIEWS` | Count of model outputs with a role label |
| `final_score >= MINIMUM_CANDIDATE_SCORE` | Weighted mean of model-emitted integers |
| `not unique_blockers` | Strings a model chose to emit |

One of those five is grounded in something other than a language model's
opinion, and it is the one that only checks the diff's *shape*.

`validate_diff` (`orchestrator/app/patching.py:126`) confirms the patch parses,
touches no sensitive path, crosses no symlink, and stays within bounded change
statistics. That is genuine verification. It is also entirely syntactic — it
cannot distinguish a correct patch from an incorrect one that edits the same
files.

Everything else in the eligibility decision is a model reporting on itself.

Schema validation confirms that `review.correctness` is an integer in range.
It does not confirm the integer means anything. A critic emitting
`correctness: 9` for broken code produces bytes indistinguishable from one
emitting `9` for correct code. No component downstream can tell them apart,
because there is nothing downstream to tell them apart *with*.

This is the whole problem, stated plainly: **the pipeline's correctness signal
is self-report, and self-report from a system that shares its failure modes
with the thing being reported on.**

---

## 2. Three mechanisms that make the signal weaker than it looks

### 2.1 Confidence weighting amplifies confident error

```python
effective_weight = record.weight * (0.5 + 0.5 * review.confidence / 10.0)
```
`orchestrator/app/scoring.py:104`

The reviewer most certain of itself gets up to double the influence of the
most hesitant one. This would be correct if confidence were calibrated —
if a model saying `confidence: 9` were right more often than one saying `5`.

Nothing in this repository has ever tested that. And the dominant failure mode
of language models on code review is *confident wrongness*: the review that
misses a bug is typically not hedging, it is breezily approving. Meanwhile a
critic that noticed something real but could not fully articulate it reports
lower confidence and gets down-weighted for the honesty.

The formula is therefore backwards with respect to the specific error it most
needs to survive. It is not neutral-but-unproven; it actively inverts the
signal in the case that matters.

### 2.2 The reviewers are not independent

`MINIMUM_CRITIC_REVIEWS = 2` reads like a quorum. A quorum is only worth
something if its members fail *differently*. These do not:

- Critics are drawn from the same operator roster as the generator, resolved
  through the same environment expansion (`orchestrator/app/agents.py:120-131`).
- The code already acknowledges two roster entries can resolve to the same
  model — the comment at `agents.py:201-205` exists precisely because a
  memory-constrained host points several roles at one model. On such a host,
  `MINIMUM_CRITIC_REVIEWS = 2` is satisfied by asking one model twice.
- The security reviewer is the same prompt template with a boolean flag
  (`orchestrator/app/prompts.py:140-143`). A different prompt on the same
  weights changes what the model *attends to*, not what it is *capable of
  noticing*.

When the generator misreads a requirement, a critic reading the same task
description tends to misread it the same way. What you get is the visual
form of multi-reviewer consensus carrying roughly one reviewer's worth of
information — and consensus is precisely the property that makes a wrong
patch look safe to merge.

Correlated reviewers do not merely fail to add signal. They add *false*
signal, because agreement is read as corroboration.

### 2.3 Nothing measures the scorer

`MINIMUM_CANDIDATE_SCORE` defaults to `6.5`. There is no record anywhere in
this system of what a patch scored versus whether it survived.

Worse, the record is actively destroyed: `TASK_RETENTION` defaults to `100`
(`orchestrator/app/config.py:202`), and task state is pruned from
`TASK_STORE_DIR` on that bound. The Prometheus counters
(`orchestrator/app/metrics.py`) track gate outcomes in aggregate but never
link a score to a commit, so they cannot answer the question either.

So `6.5` is uncalibrated by construction. It is not a finding, a measurement,
or a tuned parameter — it is a guess that no one is currently in a position
to check. The same is true of every weight in `W_CORRECTNESS`, `W_SECURITY`,
`W_STYLE`, `W_TESTS`.

---

## 3. What to do

Ordered by value per unit of effort. Tier 1 and Tier 2 are the ones that
change the character of the system; the rest are cleanup that becomes
possible once those exist.

### Tier 1 — Make execution the gate, not the tiebreaker

**The problem.** The test run in `orchestrator/app/git_ops.py:731-763`
executes the suite once, against the patched worktree, and checks the return
code. That answers one question: *does the suite pass with this patch
applied?*

It does not answer the question you care about: *did this patch produce
evidence that it does what it claims?*

Three classes of worthless patch pass that check today:

1. A patch whose new test passes on the *unpatched* tree — it tests nothing
   about the change.
2. A patch that breaks nothing and fixes nothing.
3. A test that is tautological, or that mocks out the subject and asserts on
   the mock.

Each of these produces a green run and a plausible score.

**What to build: the revert-the-source protocol.**

The naive fix is a before/after baseline — run the suite pre-patch, run it
post-patch, require that something changed from fail to pass. That is worth
doing and catches class 1 and 2. But there is a stronger and equally cheap
version that catches all three:

1. Apply the full patch to the worktree. Run the suite. **All tests must pass.**
2. Revert *only the non-test files* from the patch, keeping the new tests in
   place. Run the suite again. **At least one new test must now fail.**

Step 2 proves the new test is causally coupled to the source change. A
tautological test passes step 2 and is rejected. A test that mocks its subject
passes step 2 and is rejected. A patch that added tests for behavior it did
not change passes step 2 and is rejected.

This is mutation testing narrowed to the one mutation you already have on
hand — the patch itself — which makes it cheap enough to run on every
candidate.

**Why it is implementable here.** `validate_diff` already returns
`validation.paths` (`patching.py:225-233`). Classifying those into test paths
and source paths is a per-repository predicate (a configurable glob, defaulting
to the conventions of the detected test command). `git apply -R` against the
source subset gives you step 2 directly. The transaction already runs inside
an isolated worktree that is discarded, so there is nowhere for the extra
state to leak.

**Cost.** Roughly 2x test wall-clock per candidate, and it must run inside the
same sandbox as the existing test execution — this is more code running under
`TEST_RUNNER_COMMAND`, not less, so the sandbox stays mandatory.

**Granularity requirement.** A return code is insufficient; you need per-test
results to identify *which* test flipped. Use the structured output the
runners already emit — `pytest --junitxml`, `go test -json`,
`cargo test --format json` — and diff the result sets rather than the exit
statuses.

**Flakiness.** Run the baseline twice. Any test with inconsistent results
across identical runs is excluded from both sides of the comparison and
reported as a warning. Do not let a flaky test either satisfy or block the
evidence requirement.

**Scope.** This requirement makes sense for bugfix and feature tasks. For
pure refactors — where the correct outcome is *no* behavior change — invert
it: require that the existing suite passes unchanged and that no new test was
needed. The task mode is already in the payload (`result["mode"]`), so the
protocol can branch on it.

This single change moves the system from "a model said this was good" to
"the machine demonstrated this patch changes observable behavior in the
direction claimed." That is a categorical difference, not an incremental one.

### Tier 2 — Build the outcome ledger

**Why this is second and not fourth.** Every remaining decision in this
document — the threshold, the weights, whether confidence weighting helps,
whether swapping a critic model was an improvement — is unanswerable without
a record of what happened. Right now you cannot tell whether a change to the
scoring made things better or worse. That makes all tuning superstition.

It is also the cheapest thing on this list.

**What to build.** An append-only ledger, written on every completed task,
stored separately from `TASK_STORE_DIR` and explicitly exempt from
`TASK_RETENTION` pruning:

```
task_id, timestamp, task_mode, final_score, per_critic_raw_scores,
critic_models, patch_paths, patch_line_count, eligibility, eligibility_reasons,
test_evidence (from Tier 1), commit_sha
```

**The outcome half is free.** You do not need human labeling. For every
`commit_sha` in the ledger, git itself answers the question:

- Was it reverted (`git revert` referencing it)?
- Were its lines subsequently rewritten within N days (`git log -L`, or
  `git blame` showing low survival)?
- Did a commit message referencing a fix touch the same paths shortly after?

Line survival at 30 days is a good primary signal and requires nothing but
the repository.

**What it unlocks.** After a few hundred tasks you can compute the things
that are currently guesses: whether score correlates with survival at all,
what the precision is at `6.5`, whether the security reviewer catches
anything the quality reviewers miss, and whether `confidence` predicts
anything whatsoever. Those are the measurements that turn the rest of this
document from opinion into engineering.

### Tier 3 — Break reviewer correlation, and keep the disagreement

Three changes, all small:

**3.1 Enforce distinct model families at roster validation.** Add a `family`
field to roster entries and reject at startup any configuration where the
critic pool spans fewer than `MINIMUM_DISTINCT_CRITIC_FAMILIES` families.
Distinct *model strings* is not enough — `qwen2.5-coder:7b` and
`qwen2.5-coder:14b` share training data and failure modes. The registry
already fails closed on invalid rosters (`agents.py:235-277`), so this fits
the existing pattern exactly.

On a single-model host this will refuse to start. That is the correct
behavior: it makes an unavoidable weakness visible at boot instead of
laundering it into a consensus number at merge time. Provide an explicit
`ALLOW_CORRELATED_CRITICS=true` override, in the same spirit as
`ALLOW_UNSANDBOXED_TESTS`, so the operator opts in knowingly.

**3.2 Stop anchoring the reviewers.** Do not show critics the generator's
own rationale for the patch. A stated justification is an anchor, and it
converts an independent assessment into an evaluation of an argument. Show
the diff, the task, and the repository context — nothing else.

**3.3 Preserve variance; route on it.** `final_score` is a weighted mean
(`scoring.py:126`), and the mean discards the most informative thing in the
review set. Two critics scoring 9 and 3 average to 6. Two critics scoring 6
and 6 average to 6. These are entirely different situations: the first is a
patch that a human needs to look at, the second is a mediocre patch.

Emit the variance alongside the mean, and make high disagreement a routing
condition — a high-variance candidate goes to human review regardless of its
average. Disagreement among genuinely independent reviewers is signal, and
right now it is being averaged into silence.

### Tier 4 — Separate gates from rankings

The current score mixes two incompatible jobs into one float: deciding
whether a patch is *allowed*, and deciding which allowed patch is *best*.

Concretely, `W_SECURITY = 0.25` means a high style score can partially offset
a security concern in the weighted mean — while security is *also* handled as
a hard gate via `REQUIRE_SECURITY_REVIEW`, `MINIMUM_SECURITY_REVIEWS`, and
`BLOCK_ON_CRITIC_BLOCKERS`. The same dimension is both a gate and a
tradeable term. That is incoherent, and the tradeable half undermines the
gate.

The clean structure:

- **Gates are boolean and non-negotiable.** Structural validity. Test
  evidence from Tier 1. Security blockers. Distinct-family review count.
  Patch size ceiling. High-risk path routing. A candidate either clears all
  of them or it is not eligible — no arithmetic.
- **The score ranks only what already cleared the gates**, and should be
  little more than correctness (plus style as a weak tiebreak).

Then delete `W_TESTS` and `W_SECURITY` outright. Once test evidence is a
hard gate, a *weighted opinion about* test quality is noise sitting next to a
measurement. Once security blockers gate, a security number in the mean is
double-counting.

And drop confidence weighting until the Tier 2 ledger shows it predicts
something. Flat weights are less wrong than a formula that rewards bravado.
This is a one-line change with a real expected improvement.

### Tier 5 — Route by risk, not by score alone

`patching.py` already computes `high_risk_paths`, and `DraftScore` already
carries it — but it does not affect eligibility. Make it routing: any patch
touching a high-risk path goes to human review regardless of score.

Add a size ceiling on the same principle. A 5-line patch and a 500-line patch
that both scored 7.0 are not equally trustworthy, because reviewer attention
per line differs by two orders of magnitude between them. Large patches should
be ineligible for automatic handling no matter how well they score — and a
size ceiling also pushes the generator toward smaller, more reviewable
changes, which is independently desirable.

---

## 4. What not to do

**Do not raise `MINIMUM_CANDIDATE_SCORE`.** It is an uncalibrated number on
an uncalibrated scale. Raising it reduces throughput and *feels* safer while
providing no evidence that it improved precision. Do not tune a dial you
cannot read. Build the ledger first, then tune it with data.

**Do not add more critics of the same model.** Adding correlated reviewers
increases apparent consensus without adding information — which makes wrong
patches look *more* trustworthy, not less. This is actively harmful, and it
is the intuitive first move, which is why it is worth naming.

**Do not add an LLM judge over the LLM judges.** It has the same correlation
problem one level up, plus a new failure mode: it will tend to ratify the
majority, which is exactly the thing you needed an independent check on.

**Do not enable `ENABLE_GIT_APPLY` together with `OPEN_PR` until Tiers 1
and 2 exist.** `ENABLE_GIT_APPLY` defaults to `false`
(`orchestrator/app/config.py:139`), and that default is the configuration
telling the truth about the system's current epistemic state. Changing it
before there is execution-grounded evidence converts a patch-suggestion
system into an autonomous merge system without having built the thing that
would justify the promotion.

**Do not treat a green suite as proof of correctness even after Tier 1.**
Tier 1 proves the new test is coupled to the source change. It does not prove
the test encodes the *right* requirement. A model that misunderstands the task
writes a test encoding that misunderstanding, then satisfies the revert
protocol perfectly, and the green checkmark launders the misunderstanding into
evidence. Tests catch regression against known-good behavior; nothing in this
pipeline catches *built the wrong thing*. That remains a human's job, and the
system should be honest that it is routing work to humans rather than
replacing them.

---

## 5. Sequencing

1. **Outcome ledger** (Tier 2). Smallest change, unlocks measurement for
   everything else. Do it first even though it delivers no immediate
   improvement — it is what makes later improvements verifiable.
2. **Revert-the-source protocol** (Tier 1). The substantial engineering. Do
   it second so the ledger captures its effect from the start.
3. **Roster family enforcement and variance routing** (Tier 3). Small,
   independent, real.
4. **Gate/rank separation and weight cleanup** (Tier 4). Do it once the
   ledger has enough data to confirm what the weights were contributing.
5. **Risk and size routing** (Tier 5). Cheap; ordering here is not critical.

---

## 6. The honest summary

What this repository is today: a system that generates candidate patches at
volume, contains their blast radius rigorously, and ranks them with a
plausible-looking number that no one has validated.

The first two of those are real and valuable. The third is the problem —
not because ranking is useless, but because `7.312` reads like a measurement
when it is closer to an impression with three decimal places, and a system
that reports impressions in the format of measurements will eventually be
trusted like one.

The work above does not make the swarm's judgment reliable. Nothing available
would. What it does is replace the weakest link — a model's opinion of its own
output — with something the machine can actually check, and build the record
that would let you find out how well any of it works.

Start with the ledger. You cannot improve what you have never measured, and
right now this system deletes the evidence after a hundred tasks.
