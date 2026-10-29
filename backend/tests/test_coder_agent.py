"""Exercise the official harness with a synthetic model and a mock workspace, never a paid model."""

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from app.agents import coder
from app.integrations.workspace import PROVIDER
from test_workspace_backend import StubbedBackend


async def test_official_coder_exposes_direct_shell_with_trusted_split_result(monkeypatch):
    backend = StubbedBackend()
    monkeypatch.setattr(coder, "SandboxWorkspaceBackend", lambda **_: backend)
    seen = []
    def model(messages, info):
        names = {tool.name for tool in info.function_tools}
        assert "run_command" in names and "shell" not in names and "delegate_task" not in names
        if not seen:
            seen.append("requested")
            return ModelResponse(parts=[ToolCallPart("run_command", {"command": "printf passed; exit 7"}, "call1")])
        results = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        assert results and "exit code: 7" in str(results[-1].content)
        assert "error" in str(results[-1].content) and "passed" in str(results[-1].content)
        return ModelResponse(parts=[TextPart("已运行，退出码7；需要继续修复。")])
    operation_id = "12345678-1234-1234-1234-123456789012"
    with coder.CODER_AGENT.override(model=FunctionModel(model)):
        result = await coder.CODER_AGENT.run("解释并运行提交", deps={"operation": {"id": operation_id},
            "coding_run_id": "run", "workspace_ref": {"provider": PROVIDER,
                "id": "coding_" + operation_id.replace("-", "")}})
    assert "退出码7" in result.output
    assert len([method for method, _ in backend.calls if method == "StartCommand"]) == 1


def test_coder_workspace_rejects_foreign_ref_without_any_io():
    from types import SimpleNamespace
    from pydantic_ai.workspaces import WorkspaceRef
    capability = coder.CodingWorkspace()
    context = SimpleNamespace(deps={"operation": {"id": "abc"},
        "workspace_ref": {"provider": PROVIDER, "id": "coding_abc"}})
    with pytest.raises(ValueError):
        capability.get_workspace(context, ref=WorkspaceRef(provider=PROVIDER, id="coding_other"))
