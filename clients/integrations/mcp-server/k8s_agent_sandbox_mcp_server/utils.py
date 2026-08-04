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

from fastmcp import Context
from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient

from .settings import Settings


TOOL_DEFAULT_TIMEOUT = 60
TOOL_MAX_TIMEOUT = 600

async def ensure_session_owns(
    ctx: Context,
    sandbox_claim_name: str,
    namespace: str,
):
    client: AsyncSandboxClient = ctx.lifespan_context["client"]
    settings: Settings = ctx.lifespan_context["settings"]

    # Read the one claim by name: the GET returns metadata.labels, which is
    # where create_sandbox stamps the session id, so the ownership check
    # needs a single request rather than a LIST of every claim in the
    # namespace on every tool call.
    claim = await client.k8s_helper.get_sandbox_claim(sandbox_claim_name, namespace)

    # Raises when the transport supplies no session id, so an absent label
    # can never be compared equal to an absent session.
    session_id = get_session_id_from_context(ctx)

    # get_sandbox_claim returns None for a missing claim, and a claim with no
    # labels yields None for metadata.labels, so both collapse to {} here and
    # fall through to the same rejection below.
    labels = (claim or {}).get("metadata", {}).get("labels") or {}

    # One comparison covers every failure mode -- claim absent, label absent,
    # or label belonging to another session -- and they all raise the same
    # message. A caller must not be able to tell "someone else owns this"
    # from "this does not exist".
    if labels.get(settings.session_id_label_key) != session_id:
        raise RuntimeError(f"Sandbox claim '{sandbox_claim_name}' is not found in namespace '{namespace}'.")


async def get_sandbox(
    ctx: Context,
    sandbox_claim_name: str,
    namespace: str,
):
    # Making sure that sandbox belongs to this session, otherwise raise error.
    await ensure_session_owns(ctx, sandbox_claim_name, namespace)

    client: AsyncSandboxClient = ctx.lifespan_context["client"]
    sandbox = await client.get_sandbox(sandbox_claim_name, namespace=namespace)

    return sandbox

def get_session_id_from_context(ctx: Context) -> str:

    session_id = getattr(ctx, "session_id", None)

    if session_id is None:
        raise RuntimeError(
            "This server requires a transport that provides a session id (e.g. streamable HTTP); "
            "ctx.session_id is None."
        )

    return session_id


def get_session_label_selector_from_context(ctx: Context) -> str:
    settings: Settings = ctx.lifespan_context["settings"]
    session_id = get_session_id_from_context(ctx)

    label_selector = f"{settings.session_id_label_key}={session_id}"
    return label_selector
