from __future__ import annotations

import ast
import re
from typing import Any

from synode.runtime.decisions import PatchProposal


class PythonPatchValidator:
    def validate(
        self,
        proposal: PatchProposal,
        file_context: list[dict[str, Any]],
        patched_content: dict[str, str],
        patch_application_errors: list[str],
    ) -> list[str]:
        errors = list(patch_application_errors)
        truncated_paths = {str(item.get("path")) for item in file_context if bool(item.get("truncated"))}
        patched_python_paths = sorted({patch.path for patch in proposal.patches if patch.path.endswith(".py")})
        for path in patched_python_paths:
            if path in truncated_paths:
                continue
            content = patched_content.get(path)
            if content is None:
                continue
            try:
                ast.parse(content, filename=path)
            except SyntaxError as exc:
                line = exc.lineno if exc.lineno is not None else "?"
                column = exc.offset if exc.offset is not None else "?"
                errors.append(
                    f"patch result is not valid Python syntax in {path}: {exc.msg} "
                    f"at line {line}, column {column}"
                )
        errors.extend(_patched_python_accumulator_errors(proposal, patched_content))
        errors.extend(_patched_python_refund_skip_errors(proposal))
        return errors


def source_function_blocks(file_context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for item in file_context:
        path = str(item.get("path") or "")
        if _looks_like_test_path(path):
            continue
        content = str(item.get("content") or "")
        matches = list(re.finditer(r"(?m)^[ \t]*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            blocks.append(
                {
                    "path": path,
                    "symbol": match.group(1),
                    "start": match.start(),
                    "end": end,
                    "content": content[match.start() : end].rstrip("\n"),
                }
            )
    return blocks


def patch_touched_symbols(proposal: PatchProposal, file_context: list[dict[str, Any]]) -> set[str]:
    blocks = source_function_blocks(file_context)
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    symbols: set[str] = set()
    for patch in proposal.patches:
        symbols.update(re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", patch.old_text))
        symbols.update(re.findall(r"(?m)^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", patch.new_text))
        content = content_by_path.get(patch.path)
        if content is None:
            continue
        start = content.find(patch.old_text)
        if start < 0:
            continue
        end = start + len(patch.old_text)
        for block in blocks:
            if block["path"] != patch.path:
                continue
            block_start = int(block["start"])
            block_end = int(block["end"])
            if start < block_end and end > block_start:
                symbols.add(str(block["symbol"]))
    return symbols


def _patched_python_accumulator_errors(
    proposal: PatchProposal,
    patched_content: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    dict_names_by_path = {
        path: _dict_accumulator_names(content)
        for path, content in patched_content.items()
        if path.endswith(".py")
    }
    for index, patch in enumerate(proposal.patches):
        dict_names = dict_names_by_path.get(patch.path, set())
        if not dict_names:
            continue
        for match in re.finditer(
            r"(?m)\b([A-Za-z_][A-Za-z0-9_]*)\s*\[[^\]\n]+\]\s*([+\-*/%]=)",
            patch.new_text,
        ):
            target_name = match.group(1)
            if target_name not in dict_names:
                continue
            operator = match.group(2)
            errors.append(
                f"patch {index} introduces direct augmented assignment {target_name}[...] {operator} "
                f"in {patch.path}; use {target_name}.get(key, default) or initialize the key before mutation"
            )
    return errors


def _patched_python_refund_skip_errors(proposal: PatchProposal) -> list[str]:
    errors: list[str] = []
    for index, patch in enumerate(proposal.patches):
        if not patch.path.endswith(".py") or "refund" not in patch.new_text:
            continue
        lines = patch.new_text.splitlines()
        for line_index, line in enumerate(lines):
            if not re.search(r"""row\[['"]type['"]\]\s*==\s*['"]refund['"]""", line):
                continue
            indent = len(line) - len(line.lstrip(" \t"))
            body_lines: list[str] = []
            for body_line in lines[line_index + 1 :]:
                if not body_line.strip():
                    body_lines.append(body_line)
                    continue
                body_indent = len(body_line) - len(body_line.lstrip(" \t"))
                if body_indent <= indent:
                    break
                body_lines.append(body_line)
            body = "\n".join(body_lines)
            if not re.search(r"(?m)^\s*continue\b", body):
                continue
            if re.search(r"(-=|=\s*-|\*=\s*-?1|Decimal\([\"']-)", body):
                continue
            errors.append(
                f"patch {index} keeps refund rows as continue without reducing amount in {patch.path}; "
                "make the refund amount negative before updating the accumulator"
            )
    return errors


def _dict_accumulator_names(content: str) -> set[str]:
    return set(
        re.findall(
            r"(?m)^[ \t]*([A-Za-z_][A-Za-z0-9_]*)[ \t]*(?::[^\n=]+)?=[ \t]*\{[ \t]*\}",
            content,
        )
    )


def _looks_like_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path or path.startswith("test_") or "/test_" in path

