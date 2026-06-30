# MCP Contract

Synode supports MCP as a pluggable tool layer. MCP servers are loaded from
`.mcp.json` or `SYNODE_MCP_CONFIG_PATH`.

## Rules

- MCP tools are named as `mcp.<server>.<tool>`.
- MCP discovery is read-only and may happen at startup or on demand.
- MCP calls must pass role allowlists, risk classification, approval checks,
  and audit logging.
- MCP server output is advisory unless a specific tool contract says otherwise.
- Missing MCP packages or unavailable servers must produce explicit errors.

## Supported MVP Transports

- `stdio` server definitions through `langchain-mcp-adapters`.
- HTTP/streamable server definitions when supported by the installed adapter.

## Configuration Shape

```json
{
  "mcpServers": {
    "example": {
      "command": "python3",
      "args": ["tools/example_mcp.py"],
      "env": {}
    }
  }
}
```

