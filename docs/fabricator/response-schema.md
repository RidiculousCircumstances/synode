# Fabricator Response Schema

Each expert writes a markdown response and a JSON sidecar. The JSON sidecar must include:

- `expert_id`: selected expert id.
- `phase`: `expert`, `challenge`, or `review`.
- `verdict`: `proceed`, `revise`, or `block`.
- `confidence`: `low`, `medium`, or `high`.
- `blockers`: list of blocking findings.
- `advisory_findings`: list of non-blocking findings.
- `required_constraints`: list of constraints the Arbiter should preserve.
- `verification_implications`: list of concrete tests, checks, or evidence.
- `challenged_recommendations`: list of recommendations disputed in challenge or review.
- `decision_impact`: concise explanation of how the Arbiter decision should change.
