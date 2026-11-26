"""Issue a one-hour read-only MCP credential; current Web authorization is rechecked on use."""

import argparse
from uuid import UUID

from compass_agent.context_ops.mcp_server import issue_token
from compass_agent.schemas import TenantScope, TrustedContext
from compass_agent.settings import get_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--workspace", type=UUID, required=True)
    parser.add_argument("--administrator", action="store_true")
    args = parser.parse_args()
    context = TrustedContext(
        actor_user_id=args.actor,
        scope=TenantScope(kind="tenant", tenant_id=args.tenant, workspace_id=args.workspace),
        purpose="contextops_admin" if args.administrator else "contextops_read",
    )
    print(issue_token(get_settings(), context))


if __name__ == "__main__":
    main()
