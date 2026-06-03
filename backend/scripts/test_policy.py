"""Quick script to test policy engine filtering via MCP protocol."""
import asyncio

from mcp.client.streamable_http import streamablehttp_client
from mcp.client.session import ClientSession


async def test_user(user_id: str) -> None:
    headers = {"X-MCPolis-User": user_id}
    async with streamablehttp_client(
        "http://localhost:8000/mcp/", headers=headers
    ) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            tool_names = [t.name for t in result.tools]
            print(f"\n{user_id}: {len(tool_names)} tools")
            for name in tool_names:
                print(f"  - {name}")


async def main() -> None:
    for user in ["admin@test.com", "reader@test.com", "nobody@test.com", "anonymous"]:
        await test_user(user)


if __name__ == "__main__":
    asyncio.run(main())
