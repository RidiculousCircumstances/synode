# LLM Guardrails

Synode is designed for LLM-assisted development and LLM-driven runtime actions.
Mechanical guardrails are required because the system can call tools.

## Change Checklist

- Identify the source of truth for mutable state.
- Confirm the change matches `architecture.yml`.
- Do not add hidden compatibility fallbacks.
- Make failure observable through errors, audit rows, events, or logs.
- Keep tool permissions explicit and role-scoped.
- Keep generated/local files out of versioned source unless they are fixtures.
- Use the smallest verification profile that covers the risk.

## Hard Invariants

- No concrete production model ID is hardcoded in role definitions.
- Native and MCP tools must use the same policy and audit path.
- Mutating filesystem, shell, database, Git, deployment, or destructive MCP
  actions require approval.
- Database write/migration execution is out of scope for MVP runtime agents.
- FastAPI and CLI must call the orchestration service instead of duplicating
  graph behavior.
- Tool failures must be returned as visible tool results; they must not be
  silently replaced by unrelated tools.

## Debt Ratchets

- Files should stay focused by subsystem. Split modules when a file grows into
  unrelated responsibilities.
- Do not add a new dependency unless it supports an MVP invariant or removes a
  real repeated workflow.
- Test fakes must be deterministic.

