from __future__ import annotations

import re

from synode.domain.runtime.contracts import CODING_PATCH_PROPOSAL_CONTRACT


def invalid_operator_question_text_reason(text: str, *, contract_id: str) -> str | None:
    text = text.strip()
    if not text:
        return "needs_operator requires a concrete operator question"
    if contract_id != CODING_PATCH_PROPOSAL_CONTRACT:
        return None
    lower = text.lower()
    delegation_patterns = [
        r"\bplease\s+(review|inspect|fix|change|update|apply|run|make)\b",
        r"\b(after|then)\s+(making|make|run|apply)\b",
        r"\bmake the necessary changes\b",
        r"\brun (the )?tests\b",
    ]
    if not text.endswith("?") or any(re.search(pattern, lower) for pattern in delegation_patterns):
        return (
            "needs_operator must ask one specific ambiguity question. "
            "Do not delegate implementation, review, patching, or verification back to the operator; "
            "finish with a coding_patch_proposal payload when the context is sufficient."
        )
    return None

