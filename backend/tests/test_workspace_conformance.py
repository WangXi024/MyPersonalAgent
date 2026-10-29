"""Official POSIX conformance against a real node; opt in with SANDBOX_WORKSPACE_TEST_ADDRESS.

No fake filesystem/shell and no provider escape fixtures. Each test's environments are destroyed.
This opt-in suite does not call an LLM.
"""

import os

import pytest
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

from app.integrations.settings import RuntimeSettings
from app.integrations import workspace


@pytest.mark.skipif(not os.environ.get("SANDBOX_WORKSPACE_TEST_ADDRESS"), reason="real WorkspaceService not configured")
class TestRemoteWorkspace(WorkspaceBackendSuite):
    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @pytest.fixture(autouse=True)
    def address(self, monkeypatch):
        settings = RuntimeSettings(sandbox_address=os.environ.get("SANDBOX_WORKSPACE_TEST_ADDRESS", ""))
        monkeypatch.setattr(workspace, "runtime_settings", lambda: settings)

    @pytest.fixture
    async def fresh_backend(self):
        instances = []
        def create():
            backend = workspace.SandboxWorkspaceBackend()
            instances.append(backend)
            return backend
        try:
            yield create
        finally:
            for backend in instances:
                if backend.ref:
                    await backend.close()

    @pytest.fixture
    async def backend(self):
        backend = workspace.SandboxWorkspaceBackend()
        try:
            yield backend
        finally:
            if backend.ref:
                await backend.close()

    @pytest.fixture
    def attach_backend(self):
        return lambda ref: workspace.SandboxWorkspaceBackend(ref=ref)

    @pytest.fixture
    def destroy_environment(self):
        return lambda backend: backend.close()
