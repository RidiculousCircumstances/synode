from __future__ import annotations

import pathlib

import pytest

from synode.schemas import (
    AgentGraphCreateRequest,
    ModelProfileCreateRequest,
    ModelProviderType,
    RunStatus,
    SecretCreateRequest,
)


async def test_custom_graph_and_model_profile_drive_run(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n2026-06-02,20\n", encoding="utf-8")
    profile = await service.create_model_profile(
        ModelProfileCreateRequest(
            name="fake test profile",
            provider_type=ModelProviderType.FAKE,
            model="fake",
        )
    )
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        AgentGraphCreateRequest(
            name="analysis-only test graph",
            role_ids=[
                roles["supervisor"].id,
                roles["data_analyst"].id,
                roles["reviewer"].id,
            ],
            edges=[
                {"from_role": roles["supervisor"].id, "to_role": roles["data_analyst"].id},
                {"from_role": roles["data_analyst"].id, "to_role": roles["reviewer"].id},
            ],
            default_model_profile_id=profile.id,
        )
    )

    result = await service.run_task(
        "Analyze sample data and summarize findings",
        workspace=str(tmp_path),
        default_model_profile_id=profile.id,
        agent_graph_id=graph.id,
    )

    assert result.status == RunStatus.COMPLETED
    assert result.model_provider == ModelProviderType.FAKE.value
    assert result.default_model_profile_id == profile.id
    assert result.agent_graph_id == graph.id
    assert result.agent_graph_snapshot["name"] == graph.name
    assert "data_analyst" in (result.final_answer or "")


async def test_secret_creation_requires_configured_key(service) -> None:
    with pytest.raises(RuntimeError, match="SYNODE_SECRETS_KEY"):
        await service.create_secret(SecretCreateRequest(name="test", value="secret"))
