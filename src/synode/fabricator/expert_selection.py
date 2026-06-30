from __future__ import annotations

from typing import Any

from synode.fabricator.common import FabricatorError

MAX_OPTIONAL_EXPERTS = 5
MANDATORY_EXPERT_ID = "principal_arbiter"


def select_run_experts(
    *,
    selected_profile: dict[str, Any],
    experts: dict[str, dict[str, Any]],
    expert_override_ids: list[str] | None,
    expert_override_reason: str | None,
) -> dict[str, Any]:
    reason = (expert_override_reason or "").strip()
    if expert_override_ids is None:
        if reason:
            raise FabricatorError("expert_override_reason requires expert override ids")
        optional_experts = _require_string_list(selected_profile, "experts")
        selection_source = "profile"
        stored_reason = None
    else:
        optional_experts = list(expert_override_ids)
        if not optional_experts:
            raise FabricatorError("expert override must include at least one optional expert")
        if not reason:
            raise FabricatorError("expert_override_reason is required when overriding experts")
        if len(optional_experts) > MAX_OPTIONAL_EXPERTS:
            raise FabricatorError(
                f"expert override selects {len(optional_experts)} optional experts, "
                f"above cap {MAX_OPTIONAL_EXPERTS}"
            )
        duplicates = sorted({expert_id for expert_id in optional_experts if optional_experts.count(expert_id) > 1})
        if duplicates:
            raise FabricatorError(f"expert override contains duplicate experts: {', '.join(duplicates)}")
        if MANDATORY_EXPERT_ID in optional_experts:
            raise FabricatorError(f"expert override must not include {MANDATORY_EXPERT_ID}")
        unknown = sorted(set(optional_experts) - set(experts))
        if unknown:
            raise FabricatorError(f"expert override references unknown experts: {', '.join(unknown)}")
        selection_source = "explicit_override"
        stored_reason = reason
    return {
        "selected_experts": [MANDATORY_EXPERT_ID, *optional_experts],
        "optional_experts": optional_experts,
        "expert_selection_source": selection_source,
        "expert_override_reason": stored_reason,
        "max_challenge_experts": min(int(selected_profile["max_challenge_experts"]), len(optional_experts)),
    }


def _require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise FabricatorError(f"{key} must be a non-empty string list")
    return list(value)
