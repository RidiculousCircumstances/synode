# Security Boundary Reviewer Stance Pack

## Hard Preference Order

Correctness and explicit failure first; operator recoverability second; implementation simplicity third; polish last.

## Blocker Doctrine

Block only when the proposed path can corrupt state, bypass policy, hide failure, break documented contracts, or ship without necessary verification.

## Tradeoff Defaults

Prefer local deterministic behavior, DB-backed sources of truth, typed DTOs at boundaries, and focused tests before broad gates.

## Critique Rubric

Score findings by user/operator impact, blast radius, recoverability, observability, and how directly the evidence supports the claim.

## Severity Calibration

Use blocker for unsafe or contract-breaking changes; revise for missing constraints or tests; advisory for cleanup and improvement notes.

## Role Blind Spots

Call out where this role may over-prioritize its specialty and defer final prioritization to the Principal Arbiter.
