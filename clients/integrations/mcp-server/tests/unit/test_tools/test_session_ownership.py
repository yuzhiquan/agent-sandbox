# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Session-ownership checks shared by every claim-scoped tool.

The session label is the only isolation boundary between MCP sessions, so
these cover the boundary itself rather than any one tool: what is accepted,
what is rejected, that rejections are indistinguishable from one another,
and that a rejection never reaches the sandbox.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError


# Every tool that resolves a claim through the ownership check. delete_sandbox
# calls ensure_session_owns directly; the rest go through get_sandbox.
CLAIM_SCOPED_TOOLS = [
    ("delete_sandbox", {}),
    ("execute_command", {"command": "echo hi"}),
    ("upload_file", {"path": "some/path", "content": "data"}),
    ("download_file", {"path": "some/path"}),
]

NOT_FOUND = "claim 'my-claim' is not found"


def call_args(tool_extra):
    return {
        "sandbox_claim_name": "my-claim",
        "namespace": "my-namespace",
        **tool_extra,
    }


def stub_sandbox_io(mock_sandbox):
    """Give the sandbox's I/O calls real return values.

    A bare AsyncMock hands back coroutines, which the tools' output schemas
    reject. Only needed by the accepted-path tests; a rejected call never
    reaches these.
    """
    mock_sandbox.files.read.return_value = b"some content"
    mock_sandbox.files.write.return_value = None
    mock_sandbox.commands.run.return_value = SimpleNamespace(
        exit_code=0, stdout="out", stderr=""
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("mocked_servers_sandbox_client_class")
@pytest.mark.parametrize("tool_name,tool_extra", CLAIM_SCOPED_TOOLS)
async def test_owned_claim_is_accepted(
    mcp_client,
    mock_sandbox_client,
    mock_sandbox,
    tool_name,
    tool_extra,
):
    stub_sandbox_io(mock_sandbox)

    # The default fixture labels the claim for whichever session is asking.
    result = await mcp_client.call_tool(tool_name, call_args(tool_extra))

    assert result.is_error is False
    mock_sandbox_client.k8s_helper.get_sandbox_claim.assert_awaited_once_with(
        "my-claim", "my-namespace"
    )


@pytest.mark.anyio
@pytest.mark.usefixtures("mocked_servers_sandbox_client_class")
@pytest.mark.parametrize("tool_name,tool_extra", CLAIM_SCOPED_TOOLS)
@pytest.mark.parametrize(
    "claim,case",
    [
        (None, "claim does not exist (404)"),
        ({"metadata": {}}, "claim has no labels at all"),
        ({"metadata": {"labels": None}}, "labels present but null"),
        ({"metadata": {"labels": {}}}, "labels present but empty"),
        (
            {"metadata": {"labels": {"unrelated": "value"}}},
            "labels present without the session key",
        ),
        (
            {"metadata": {"labels": {"mcp.k8s-agent-sandbox/session-id": "another-session"}}},
            "claim owned by a different session",
        ),
    ],
)
async def test_unowned_claim_is_rejected(
    mcp_client,
    mock_sandbox_client,
    mock_sandbox,
    tool_name,
    tool_extra,
    claim,
    case,
):
    mock_sandbox_client.k8s_helper.get_sandbox_claim = AsyncMock(return_value=claim)

    # Every case raises the *same* message: a caller must not be able to tell
    # "someone else owns this" from "this does not exist".
    with pytest.raises(ToolError, match=NOT_FOUND):
        await mcp_client.call_tool(tool_name, call_args(tool_extra))

    # Fail closed: a rejected call must never reach the sandbox or resolve a
    # handle to it.
    mock_sandbox_client.get_sandbox.assert_not_awaited()
    mock_sandbox.files.read.assert_not_called()
    mock_sandbox.files.write.assert_not_called()
    mock_sandbox.commands.run.assert_not_called()


@pytest.mark.anyio
@pytest.mark.usefixtures("mocked_servers_sandbox_client_class")
@pytest.mark.parametrize("tool_name,tool_extra", CLAIM_SCOPED_TOOLS)
async def test_ownership_check_does_not_list_claims(
    mcp_client,
    mock_sandbox_client,
    mock_sandbox,
    tool_name,
    tool_extra,
):
    """The regression guard for the fix: one GET, and no LIST.

    The check used to list every claim in the namespace on each tool call.
    Asserting the call counts is what stops that being reintroduced.
    """
    stub_sandbox_io(mock_sandbox)

    await mcp_client.call_tool(tool_name, call_args(tool_extra))

    assert mock_sandbox_client.k8s_helper.get_sandbox_claim.await_count == 1
    mock_sandbox_client.list_all_sandboxes.assert_not_awaited()
