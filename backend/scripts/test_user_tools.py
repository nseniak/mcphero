"""Test which tools a user sees through the gateway.

Usage:
    poetry run python scripts/test_user_tools.py [user_id]

Examples:
    poetry run python scripts/test_user_tools.py           # anonymous user
    poetry run python scripts/test_user_tools.py alice      # alice
"""
import asyncio
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(user_id: str = "anonymous") -> None:
    headers = {"X-MCPolis-User": user_id}
    async with streamablehttp_client(
        "http://localhost:8000/mcp", headers=headers
    ) as (read_stream, write_stream, _):
        session = ClientSession(read_stream, write_stream)
        async with session:
            await session.initialize()
            result = await session.list_tools()
            if not result.tools:
                print(f"User '{user_id}' sees 0 tools.")
            else:
                print(f"User '{user_id}' sees {len(result.tools)} tools:")
                for tool in result.tools:
                    print(f"  - {tool.name}: {tool.description}")


if __name__ == "__main__":
    user = sys.argv[1] if len(sys.argv) > 1 else "anonymous"
    asyncio.run(main(user))
