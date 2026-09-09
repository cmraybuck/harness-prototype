# Issue workflow

Use [GitHub issue #22](https://github.com/cmraybuck/harness-prototype/issues/22)
as the single source of truth for implementation order. Issue titles describe
the work and remain stable when priorities change. Reorder only the roadmap
checklist when inserting, removing, or reprioritizing work.

## Before creating an issue

Create an issue only when the work has one independently verifiable outcome.
Search open and closed issues for overlap first. Split work when it has separate
safety boundaries, can be delivered independently, or needs distinct acceptance
tests. Do not create issues for speculative framework changes that do not advance
the current roadmap.

Use one of these title prefixes:

- `(Tool):` for a model-callable capability.
- `(Feat):` for user-visible behavior outside a single tool.
- `(Optimization):` for reliability, consistency, or maintainability work.
- `(Test):` for evaluation or test infrastructure.
- `(Docs):` for documentation-only work.
- `(Roadmap):` for planning issues; normally there should be only one open roadmap.

Priority numbers, phase names, and dependency status do not belong in titles.

## Issue body template

```markdown
# Summary

State the observable outcome in one or two sentences.

## Why now

Explain how this advances the current roadmap phase.

## Dependencies

- Blocked by #123
- Enables #456

Write `None` when there are no known hard dependencies.

## Requirements

- Describe required behavior, boundaries, and failure handling.

## Acceptance criteria

- Describe outcomes that a reviewer can verify.
- Include relevant tests and documentation updates.

## Out of scope

- Name adjacent work intentionally deferred from this issue.
```

Requirements say what must be built. Acceptance criteria say how completion is
recognized. Include success, expected failure, limits or truncation, and recovery
paths when the issue changes a tool or the conversation loop.

## Add an issue to the roadmap

1. Create the issue using the template above.
2. Place its checklist entry in the appropriate phase of issue #22.
3. Add or update explicit dependency references in affected issue bodies.
4. Change a phase exit condition only when the new work changes what that phase
   must prove.

Place an issue at the earliest point where all of its dependencies will be
complete. If it can proceed in parallel, place it beside related work and state
that in its **Dependencies** section instead of inventing an arbitrary order.

## Complete or change work

- Check the roadmap item only after its acceptance criteria and required
  verification are complete.
- If an issue is closed as duplicate or not planned, remove it from the active
  checklist and leave a short note in the roadmap.
- When priorities change, edit issue #22 rather than renaming every affected
  issue.
- Keep historical discussion in the original issue; do not rewrite it to make a
  later decision appear to have been the original plan.

