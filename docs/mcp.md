# MCP Contract

Synode supports MCP as a pluggable tool layer. MCP servers are DB-backed runtime
configuration managed through the Settings UI and `/mcp/servers` API.

## Rules

- MCP tools are named as `mcp.<server>.<tool>`.
- MCP discovery is read-only and happens on demand through Synode.
- MCP calls must pass role allowlists, risk classification, approval checks,
  and audit logging.
- Native nodes use the Synode tool gateway in-process. External runtimes use
  only scoped Synode HTTP MCP proxy sessions.
- No node or external runtime should call configured MCP servers directly.
- MCP server output is advisory unless a specific tool contract says otherwise.
- Missing MCP packages or unavailable servers must produce explicit errors.

## Supported MVP Transports

- `stdio` server definitions through `langchain-mcp-adapters`.
- `sse` and `streamable_http` server definitions when supported by the
  installed adapter.
- Synode exposes an HTTP MCP proxy at `/mcp/proxy/{session_id}` for scoped
  external runtime tool access.

## Configuration Shape

`POST /mcp/servers` accepts a server name, transport, enabled flag, and the
adapter config object. For `stdio`, the config must include `command` and may
include `args` and `env`.

```json
{
  "name": "example",
  "transport": "stdio",
  "enabled": true,
  "config": {
    "command": "python3",
    "args": ["tools/example_mcp.py"],
    "env": {}
  }
}
```

Discovery stores the tool names on the server record. Startup registers only
enabled DB-backed MCP servers with discovered tools.
