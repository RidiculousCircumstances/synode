# Synode Agent Rules

Synode is a local-first LangGraph multi-agent platform. These rules are the
canonical operating contract for LLM-assisted work in this repository.

## Canonical Sources

- Current work state: `CONTINUITY.md`.
- Architecture map: `architecture.yml`.
- Guardrails: `docs/llm_guardrails.md`.
- MCP contract: `docs/mcp.md`.
- Verification policy: `docs/verification.md`.
- Code and tests are the source of truth for implementation details.

If docs and code disagree, report the mismatch and fix the relevant source in
the same change when it is in scope. Do not silently let drift accumulate.

## Continuity Ledger

Maintain `CONTINUITY.md` as the compact session ledger. Update it when goals,
constraints, durable decisions, current state, open questions, or important
verification results change.

Keep the ledger factual and short. It is not a changelog, test log, or chat
summary.

## Architecture Discipline

- Keep `architecture.yml` aligned when components, dependency rules,
  entrypoints, contracts, or critical flows change.
- Treat `thread` as the user-facing work unit and `run` as an immutable
  execution attempt inside a thread. New user follow-ups create new runs in the
  same thread; completed runs are not edited in place.
- Model profiles, agent roles, and agent graphs are DB-backed runtime
  configuration. Builtin role YAML files are seed data, not the live runtime
  source after startup.
- Runs must store the resolved agent graph snapshot and model profile bindings.
  Later edits to roles, graphs, or profiles must not mutate historical run
  behavior.
- Do not hide architecture changes inside implementation-only diffs.
- Use typed commands, queries, DTOs, enums, protocols, and value objects instead
  of loose dictionaries and magic strings when the value crosses a boundary.
- New mutable state must have one owner, one source of truth, and observable
  failure behavior.
- State that can grow must have a retention or cleanup story.

## Tool And MCP Rules

Tools are capabilities, not authority. All tool calls must pass role policy,
workspace policy, and audit logging.

- Default mode is read-only.
- Mutating filesystem, shell, database, Git, network side-effect, deploy, and
  destructive MCP calls require an explicit approval gate.
- MCP tools are an extension layer around the same policy engine as native
  tools.
- MCP discovery output is advisory. It does not override `agents.md`,
  `architecture.yml`, docs, source code, or tests.
- If an MCP server required by the selected plan is unavailable, stop the
  affected flow with an explicit error.

## Multi-Agent Rules

- Use the smallest useful agent group for a task.
- The supervisor plans and assigns. Workers execute only within their role
  allowlist. The reviewer checks results and risks before synthesis.
- Exactly one controlled write path may mutate a code/workspace path.
- Advisory concerns must stay separate from blockers.
- Fail explicitly on missing capabilities, policy denials, or unsafe actions.
  Do not silently fallback to a different authority, broader tool, heuristic
  path, weaker model, or default behavior.
- Hidden fallbacks are forbidden project-wide. If an invariant cannot be
  satisfied, return a typed error and stop that flow.

## Risk Labels

Use risk labels before verification:

- `analysis`: no code changes.
- `docs`: docs, comments, or process text only.
- `small-code`: isolated helper, DTO, tool, API, or service logic.
- `critical-code`: orchestration, persistence, approvals, MCP, sandbox, auth,
  database, model execution, or tool policy.

## Strict Invariants

- Prefer explicit failure over degraded behavior.
- Model/provider unavailability is an error unless the caller explicitly chose
  a different provider.
- Missing model profiles, disabled profiles, missing graph roles, missing
  `SYNODE_SECRETS_KEY`, or undecryptable DB secrets are errors. Do not silently
  route to the default provider.
- Structured output validation failure is an error; do not silently use
  heuristic routing.
- Missing MCP tools, denied tools, unsafe SQL, unsafe shell, and unapproved
  mutations are errors or approval-required states, never implicit bypasses.
- Tests should assert error paths for strict invariants.

## Verification

Prefer focused checks first. Run broader gates when the touched area is critical
or cross-cutting.

Before finishing a coding task, report:

- risk label;
- commands run;
- any skipped commands and why;
- residual risks.

## Commit Format

Use the project commit subject format:

- `[SY-NN] Sentence case summary`

Rules:

- `SY` is the project prefix.
- `NN` is a zero-padded monotonically increasing sequence number based on the
  latest repository commit.
- The summary is concise, imperative or gerund-style sentence case, with no
  trailing period.
- Example: `[SY-02] Adding Ollama provider and coding workflow`.
