from __future__ import annotations

from copy import deepcopy
from typing import Any

JSONSchema = dict[str, Any]


_UNKNOWN_TOOL_SCHEMA: JSONSchema = {
    "type": "object",
    "additionalProperties": True,
}


TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "native.fs_list": {
        "description": "List files under the current run workspace. Use this before reading files.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "glob": {
                    "type": "string",
                    "description": "File glob relative to the workspace, for example '*.py' or 'tests/**/*.py'.",
                    "default": "*",
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum number of files to return.",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 200,
                },
            },
        },
        "examples": [{"glob": "*.py", "max_matches": 50}],
        "notes": [
            "The workspace root is implicit; do not pass root, cwd, or path.",
            "Use native.fs_search when you need to search file contents.",
        ],
    },
    "native.fs_search": {
        "description": "Search file contents using a regular expression.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Regular expression for text contents, for example 'refund|sale'. This is not a file glob.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional file glob relative to the workspace, for example '*.py'.",
                    "default": "*",
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum number of matching files/lines to return.",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 50,
                },
            },
        },
        "examples": [{"pattern": "refund|sale", "glob": "*.py", "max_matches": 20}],
        "notes": [
            "Use native.fs_list to list files; do not call native.fs_search with only a glob.",
            "Put file patterns like '*.py' in glob, not pattern.",
            "The workspace root is implicit; do not pass root, cwd, or path.",
        ],
    },
    "native.fs_read": {
        "description": "Read one file from the current run workspace.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path relative to the workspace, for example 'src/app.py'.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": "Maximum bytes to read.",
                    "minimum": 1,
                    "maximum": 200000,
                    "default": 12000,
                },
            },
        },
        "examples": [{"path": "ledger.py", "max_bytes": 12000}],
        "notes": ["Do not pass an absolute workspace root; use a relative path."],
    },
    "native.fs_write": {
        "description": "Write one file in the workspace through the sandbox and approval flow.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "description": "Path relative to the workspace."},
                "content": {"type": "string", "description": "Complete file content to write."},
            },
        },
        "examples": [{"path": "notes.txt", "content": "updated content\n"}],
        "notes": ["Requires approval and sandbox availability."],
    },
    "native.git_status": {
        "description": "Return concise git status for the workspace.",
        "input_schema": {"type": "object", "additionalProperties": False, "properties": {}},
        "examples": [{}],
    },
    "native.git_diff": {
        "description": "Return git diff for the workspace.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "cached": {"type": "boolean", "default": False},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 200000, "default": 20000},
            },
        },
        "examples": [{"max_bytes": 20000}],
    },
    "native.patch_apply": {
        "description": "Apply a unified diff patch through the sandbox and approval flow.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["patch"],
            "properties": {"patch": {"type": "string", "description": "Unified diff patch."}},
        },
        "examples": [{"patch": "--- a/file.py\n+++ b/file.py\n@@ ..."}],
        "notes": ["Requires approval and sandbox availability."],
    },
    "native.verify": {
        "description": "Run a verification command from the current workspace through the sandbox.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Command argv, for example ['pytest', '-q'].",
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
            },
        },
        "examples": [{"command": ["pytest", "-q"], "timeout_seconds": 120}],
    },
    "native.shell": {
        "description": "Run a shell command through sandbox governance.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command"],
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
            },
        },
        "examples": [{"command": "python -m pytest -q", "timeout_seconds": 120}],
        "notes": ["Prefer native.verify for test commands when available."],
    },
    "native.data_profile": {
        "description": "Profile a local CSV or JSON file.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["path"],
            "properties": {"path": {"type": "string", "description": "Data file path relative to workspace."}},
        },
        "examples": [{"path": "data.csv"}],
    },
    "native.python_sandbox": {
        "description": "Run Python code in the sandbox for local data analysis.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["code"],
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600, "default": 120},
            },
        },
        "examples": [{"code": "print('ok')"}],
    },
    "native.db_readonly": {
        "description": "Execute a read-only database query.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string", "description": "Read-only SQL query."}},
        },
        "examples": [{"query": "select count(*) from items"}],
    },
    "native.web_search": {
        "description": "Search the web through the configured local search backend.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
        },
        "examples": [{"query": "project documentation", "limit": 5}],
    },
    "native.web_fetch": {
        "description": "Fetch one URL through the configured web backend.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url"],
            "properties": {"url": {"type": "string"}},
        },
        "examples": [{"url": "https://example.com"}],
    },
}


def tool_input_schema(name: str) -> JSONSchema:
    entry = TOOL_CATALOG.get(name)
    if entry is None:
        if name.startswith("mcp."):
            return deepcopy(_UNKNOWN_TOOL_SCHEMA)
        return deepcopy(_UNKNOWN_TOOL_SCHEMA)
    return deepcopy(entry["input_schema"])


def tool_catalog_entry(name: str) -> dict[str, Any]:
    entry = TOOL_CATALOG.get(name)
    if entry is None:
        return {
            "name": name,
            "description": f"Synode governed tool {name}",
            "input_schema": tool_input_schema(name),
            "examples": [],
            "notes": [],
        }
    item = deepcopy(entry)
    item["name"] = name
    return item


def tool_catalog_for(names: list[str]) -> list[dict[str, Any]]:
    return [tool_catalog_entry(name) for name in sorted(names)]
