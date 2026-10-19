r"""Hub-eszköz hívása parancssorból (fejlesztéshez, hibakereséshez).

    .venv\Scripts\python.exe hubcall.py hub_list
    .venv\Scripts\python.exe hubcall.py hub_test "{\"plugin\": \"look\"}"
"""
import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(tool: str, args: dict) -> None:
    async with streamablehttp_client("http://127.0.0.1:8765/mcp") as (r, w, _), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool(tool, args)
        print(res.content[0].text if res.content else res)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}))
