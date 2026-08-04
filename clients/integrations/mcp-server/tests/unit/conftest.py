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

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.client import Client
from fastmcp.server.dependencies import get_context

from k8s_agent_sandbox_mcp_server.settings import (
    Settings,
    DirectConnectionConfig,
)
from k8s_agent_sandbox_mcp_server.server import create_mcp_server
from k8s_agent_sandbox_mcp_server.utils import get_session_id_from_context


# Sentinel for "label this claim for whichever session is asking", so the
# default fixture exercises the allowed path without knowing the session id
# that fastmcp assigns per connection.
OWNED_BY_CALLER = object()

@pytest.fixture
def mcp_server_settings():
    settings = Settings(
        connection=DirectConnectionConfig(api_url="http://some-url")
    )

    return settings


@pytest.fixture
def mcp_server(mcp_server_settings):
    return create_mcp_server(settings=mcp_server_settings)


@pytest.fixture
async def mcp_client(mcp_server):
    async with Client(transport=mcp_server) as mcp_client:
        yield mcp_client


@pytest.fixture
def mock_sandbox():
    sandbox = AsyncMock()
    sandbox.claim_name = "my-claim"
    return sandbox

@pytest.fixture
def mock_sandbox_client(mock_sandbox, mcp_server_settings):
    client = AsyncMock()
    client.create_sandbox.return_value = mock_sandbox
    client.get_sandbox.return_value = mock_sandbox
    client.list_all_sandboxes.return_value = [mock_sandbox.claim_name]

    # The ownership check reads the claim by name and compares its session
    # label with the caller's session id. fastmcp assigns that id per
    # connection, so the default claim is labelled from the live request
    # context -- i.e. the calling session owns it. Tests that need a
    # rejection replace get_sandbox_claim with a fixed claim or None
    # (see test_session_ownership.py).
    client.k8s_helper = MagicMock()
    client.claim_labels = OWNED_BY_CALLER

    async def get_sandbox_claim(name, namespace):
        labels = client.claim_labels
        if labels is OWNED_BY_CALLER:
            labels = {
                mcp_server_settings.session_id_label_key: get_session_id_from_context(
                    get_context()
                )
            }
        return {"metadata": {"name": name, "labels": labels}}

    client.k8s_helper.get_sandbox_claim = AsyncMock(side_effect=get_sandbox_claim)
    return client
    
@pytest.fixture
def mocked_servers_sandbox_client_class(mock_sandbox_client):
    with patch("k8s_agent_sandbox_mcp_server.server.AsyncSandboxClient") as m:
        m.return_value = mock_sandbox_client
        yield

