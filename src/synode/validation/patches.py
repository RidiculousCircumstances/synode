from __future__ import annotations

import json
import re
from typing import Any

from synode.domain.runtime.commands import is_safe_command
from synode.domain.runtime.contracts import CODING_PATCH_PROPOSAL_CONTRACT
from synode.domain.runtime.decisions import FilePatch, PatchProposal
from synode.validation.operator import invalid_operator_question_text_reason
from synode.validation.python import (
    PythonPatchValidator,
    patch_touched_symbols,
    source_function_blocks,
)

_PYTHON_VALIDATOR = PythonPatchValidator()


def validate_patch_proposal(
    proposal: PatchProposal,
    file_context: list[dict[str, Any]],
    *,
    allowed_verification_commands: list[list[str]] | None = None,
    required_patch_symbols: list[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    sha_by_path = {str(item["path"]): str(item["sha256"]) for item in file_context}
    if proposal.action in {"no_change", "needs_operator"} and proposal.patches:
        errors.append(f"{proposal.action} action must not include patches")
    if proposal.action == "needs_operator":
        operator_error = invalid_operator_question_text_reason(
            str(proposal.operator_question or proposal.summary),
            contract_id=CODING_PATCH_PROPOSAL_CONTRACT,
        )
        if operator_error:
            errors.append(operator_error)
    for index, patch in enumerate(proposal.patches):
        content = content_by_path.get(patch.path)
        if content is None:
            errors.append(f"patch {index} targets a file outside provided context: {patch.path}")
            continue
        if patch.expected_sha256 != sha_by_path[patch.path]:
            errors.append(f"patch {index} checksum does not match provided file context: {patch.path}")
        if not patch.old_text:
            errors.append(f"patch {index} old_text is empty: {patch.path}")
            continue
        occurrences = content.count(patch.old_text)
        if occurrences != 1:
            errors.append(f"patch {index} old_text must occur exactly once in {patch.path}; occurrences={occurrences}")
        if patch.old_text == patch.new_text:
            errors.append(f"patch {index} new_text is identical to old_text: {patch.path}")
    if proposal.action == "patch":
        patched_content, patch_application_errors = patched_content_by_path(proposal, file_context)
        errors.extend(_PYTHON_VALIDATOR.validate(proposal, file_context, patched_content, patch_application_errors))
    if proposal.action == "patch" and required_patch_symbols:
        touched_text = "\n".join(
            f"{patch.path}\n{patch.old_text}\n{patch.new_text}" for patch in proposal.patches
        )
        touched_symbols = patch_touched_symbols(proposal, file_context)
        missing_symbols = [
            symbol
            for symbol in required_patch_symbols
            if symbol not in touched_symbols and not re.search(rf"\b{re.escape(symbol)}\b", touched_text)
        ]
        if missing_symbols:
            errors.append(
                "patch does not address source symbols named in failing assertions: "
                + ", ".join(missing_symbols)
            )
    allowed_keys = {
        _command_key([str(part) for part in command])
        for command in (allowed_verification_commands or [])
    }
    for index, command in enumerate(proposal.verification_commands):
        argv = [str(part) for part in command]
        if not argv:
            errors.append(f"verification command {index} is empty")
        elif not is_safe_command(argv):
            errors.append(f"verification command {index} is unsafe: {argv}")
        elif allowed_keys and _command_key(argv) not in allowed_keys:
            errors.append(f"verification command {index} is not in allowed command catalog: {argv}")
    return errors


def patched_content_by_path(
    proposal: PatchProposal,
    file_context: list[dict[str, Any]],
) -> tuple[dict[str, str], list[str]]:
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    patched_content = dict(content_by_path)
    errors: list[str] = []
    for index, patch in enumerate(proposal.patches):
        content = patched_content.get(patch.path)
        if content is None or not patch.old_text:
            continue
        occurrences = content.count(patch.old_text)
        if occurrences != 1:
            errors.append(
                f"patch {index} old_text must occur exactly once after earlier patches in "
                f"{patch.path}; occurrences={occurrences}"
            )
            continue
        patched_content[patch.path] = content.replace(patch.old_text, patch.new_text, 1)
    return patched_content, errors


def categorize_patch_validation_failure(validation_errors: list[str]) -> str:
    joined = "\n".join(validation_errors)
    if "native loop exceeded" in joined or "contract" in joined:
        return "contract_invalid"
    if "verification command" in joined and ("unsafe" in joined or "not in allowed" in joined):
        return "verification_unsafe"
    if "operator" in joined:
        return "needs_operator"
    if validation_errors:
        return "patch_invalid"
    return "contract_invalid"


def normalize_patch_proposal(
    proposal: PatchProposal,
    file_context: list[dict[str, Any]],
    *,
    required_patch_symbols: list[str] | None = None,
) -> PatchProposal:
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    sha_by_path = {str(item["path"]): str(item["sha256"]) for item in file_context}
    patches: list[FilePatch] = []
    for patch in proposal.patches:
        content = content_by_path.get(patch.path)
        old_text = patch.old_text
        new_text = patch.new_text
        if content is not None and content.count(old_text) != 1:
            expanded = _expand_patch_to_required_function(
                patch.path,
                content,
                old_text,
                new_text,
                file_context,
                required_patch_symbols or [],
            )
            if expanded is not None:
                old_text, new_text = expanded
        if content is not None and content.count(old_text) != 1:
            old_text = _align_old_text_to_content(content, old_text)
        patches.append(
            FilePatch(
                path=patch.path,
                expected_sha256=sha_by_path.get(patch.path, patch.expected_sha256),
                old_text=old_text,
                new_text=_align_new_text_indentation(old_text, new_text),
            )
        )
    return proposal.model_copy(update={"patches": patches})


def dedupe_file_patches(patches: list[FilePatch]) -> list[FilePatch]:
    seen: set[str] = set()
    result: list[FilePatch] = []
    for patch in patches:
        key = json.dumps(patch.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        result.append(patch)
    return result


def extract_required_patch_symbols(context_packet: dict[str, Any], file_context: list[dict[str, Any]]) -> list[str]:
    source_symbols = _source_function_symbols(file_context)
    if not source_symbols:
        return []
    evidence = _failure_assertion_evidence(context_packet)
    if not evidence:
        return []
    return sorted(
        {
            symbol
            for symbol in source_symbols
            if re.search(rf"\b{re.escape(symbol)}\b", evidence)
        }
    )


def required_patch_targets(required_patch_symbols: list[str], file_context: list[dict[str, Any]]) -> list[dict[str, str]]:
    required = set(required_patch_symbols)
    return [
        {
            "symbol": str(block["symbol"]),
            "path": str(block["path"]),
            "content": str(block["content"]),
        }
        for block in source_function_blocks(file_context)
        if str(block["symbol"]) in required
    ]


def _expand_patch_to_required_function(
    path: str,
    content: str,
    old_text: str,
    new_text: str,
    file_context: list[dict[str, Any]],
    required_patch_symbols: list[str],
) -> tuple[str, str] | None:
    if not required_patch_symbols:
        return None
    required = set(required_patch_symbols)
    target_blocks = [
        block
        for block in source_function_blocks(file_context)
        if block["path"] == path and str(block["symbol"]) in required
    ]
    expansion_candidates: list[tuple[str, str]] = []
    for block in target_blocks:
        block_text = str(block["content"])
        block_old_text = old_text
        if block_text.count(block_old_text) != 1:
            block_old_text = _align_old_text_to_content(block_text, old_text)
        if not block_old_text or block_text.count(block_old_text) != 1:
            continue
        aligned_new_text = _align_new_text_indentation(block_old_text, new_text)
        expanded_new_text = block_text.replace(block_old_text, aligned_new_text, 1)
        if content.count(block_text) == 1 and expanded_new_text != block_text:
            expansion_candidates.append((block_text, expanded_new_text))
    if len(expansion_candidates) == 1:
        return expansion_candidates[0]
    return None


def _source_function_symbols(file_context: list[dict[str, Any]]) -> set[str]:
    return {str(block["symbol"]) for block in source_function_blocks(file_context)}


def _failure_assertion_evidence(context_packet: dict[str, Any]) -> str:
    failed_verification = _collect_failure_evidence(context_packet.get("failed_verification", {}))
    if failed_verification:
        return "\n".join(_dedupe(failed_verification))
    inspection = context_packet.get("inspection", {})
    observed = inspection.get("observed_failures", []) if isinstance(inspection, dict) else []
    observed_evidence = _collect_failure_evidence(observed)
    return "\n".join(_dedupe(observed_evidence))


def _collect_failure_evidence(value: Any) -> list[str]:
    lines: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            lines.extend(_collect_failure_evidence(item))
        return lines
    if isinstance(value, list):
        for item in value:
            lines.extend(_collect_failure_evidence(item))
        return lines
    if isinstance(value, tuple):
        for item in value:
            lines.extend(_collect_failure_evidence(item))
        return lines
    if isinstance(value, str):
        for line in value.replace("\\n", "\n").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(">") or stripped.startswith("FAILED "):
                lines.append(stripped)
    return lines


def _align_new_text_indentation(old_text: str, new_text: str) -> str:
    old_indent = _first_non_empty_indent(old_text)
    if not old_indent:
        return new_text
    new_indent = _first_non_empty_indent(new_text)
    if new_indent.startswith(old_indent):
        return new_text
    lines = new_text.splitlines()
    if not lines:
        return new_text
    aligned = [old_indent + line if line.strip() else line for line in lines]
    suffix = "\n" if new_text.endswith("\n") else ""
    return "\n".join(aligned) + suffix


def _first_non_empty_indent(text: str) -> str:
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^[ \t]*", line)
        return match.group(0) if match else ""
    return ""


def _align_old_text_to_content(content: str, old_text: str) -> str:
    simple_candidates = [
        old_text.strip("\n"),
        old_text.strip(),
        old_text.replace("\r\n", "\n"),
        old_text.replace("\r\n", "\n").strip("\n"),
        old_text.replace("\r\n", "\n").strip(),
    ]
    for candidate in simple_candidates:
        if candidate and content.count(candidate) == 1:
            return candidate

    stripped_lines = [line.strip() for line in old_text.strip().splitlines()]
    if not stripped_lines or any(not line for line in stripped_lines):
        return old_text
    content_lines = content.splitlines(keepends=True)
    matches: list[str] = []
    width = len(stripped_lines)
    for start in range(0, len(content_lines) - width + 1):
        window = content_lines[start : start + width]
        if [line.strip() for line in window] != stripped_lines:
            continue
        candidate = "".join(window)
        if candidate:
            matches.append(candidate)
        trimmed = candidate.rstrip("\n")
        if trimmed and trimmed != candidate:
            matches.append(trimmed)
    exact_matches = list(dict.fromkeys(candidate for candidate in matches if content.count(candidate) == 1))
    if len(exact_matches) == 1:
        return exact_matches[0]
    equivalent_matches = {candidate.rstrip("\n") for candidate in exact_matches}
    if len(equivalent_matches) == 1:
        return next(iter(equivalent_matches))
    return old_text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _command_key(command: list[str]) -> tuple[str, ...]:
    return tuple(str(part) for part in command)
