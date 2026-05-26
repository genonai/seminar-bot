"""Agent tool permission metadata."""
from __future__ import annotations

from src import agent


def test_every_agent_tool_has_required_role() -> None:
    names = {tool["function"]["name"] for tool in agent.TOOLS}
    assert names == set(agent.TOOL_REQUIRED_ROLE)


def test_role_hierarchy_for_tools() -> None:
    assert agent._has_tool_role("bystander", "answer_schedule_question")
    assert agent._has_tool_role("member", "set_topic")
    assert agent._has_tool_role("admin", "manage_admin")
    assert agent._has_tool_role("admin", "dm_user")

    assert not agent._has_tool_role("bystander", "add_memo")
    assert not agent._has_tool_role("member", "manage_admin")
    assert not agent._has_tool_role("member", "dm_user")
    assert not agent._has_tool_role("bystander", "inspect_db_state")
