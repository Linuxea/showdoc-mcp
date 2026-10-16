"""协议层离线自测：不出进程、不打真实 ShowDoc。

用 SDK v2 的内存 Client 直连 MCPServer，分别以两种协议模式验证：
- auto（默认）：2026-07-28 新协议（server/discover 路径）
- legacy：initialize 握手，即 ZCode 等老客户端实际走的路径

验证点：工具清单（前缀命名）、风险注解、缺配置时的错误封装（isError 而非协议错误）。
用法：uv run python scripts/protocol_test.py
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import Client

from showdoc_mcp.tools import mcp

# 清掉宿主机上的真实配置，保证测试完全离线、行为确定
for key in ("SHOWDOC_BASE_URL", "SHOWDOC_COOKIE_TOKEN", "SHOWDOC_PHPSESSID", "SHOWDOC_DEFAULT_ITEM_ID"):
    os.environ.pop(key, None)

EXPECTED_TOOLS = {
    "showdoc_list_items", "showdoc_get_item_tree", "showdoc_get_page",
    "showdoc_create_catalog", "showdoc_update_catalog", "showdoc_delete_catalog",
    "showdoc_save_page", "showdoc_delete_page", "showdoc_upload_markdown_dir",
    "showdoc_clear_item", "showdoc_rename_numbered_catalogs",
}

fails: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  | {detail[:200]}" if not cond else ""))
    if not cond:
        fails.append(name)


async def check_mode(mode: str) -> None:
    async with Client(mcp, mode=mode) as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        check(f"[{mode}] 11 个工具且带 showdoc_ 前缀", names == EXPECTED_TOOLS, f"差集: {names ^ EXPECTED_TOOLS}")

        by_name = {t.name: t for t in tools.tools}
        read_ann = by_name["showdoc_get_page"].annotations
        check(
            f"[{mode}] 读工具注解 readOnly/openWorld=false",
            bool(read_ann and read_ann.read_only_hint and read_ann.open_world_hint is False),
            str(read_ann),
        )
        clear_ann = by_name["showdoc_clear_item"].annotations
        check(
            f"[{mode}] 危险工具注解 destructive",
            bool(clear_ann and clear_ann.destructive_hint),
            str(clear_ann),
        )
        param_desc = by_name["showdoc_save_page"].input_schema.get("properties", {}).get("page_content", {})
        check(
            f"[{mode}] 参数级 description 进入 inputSchema",
            "markdown 正文" in param_desc.get("description", ""),
            str(param_desc)[:120],
        )

        # 缺 env 时应返回干净的 isError（模型可读可自纠），而不是协议错误或堆栈
        result = await client.call_tool("showdoc_list_items", {})
        text = result.content[0].text if result.content else ""
        check(f"[{mode}] 缺配置时 isError 且提示缺失变量", result.is_error and "SHOWDOC_BASE_URL" in text, text[:120])
        print(f"[{mode}] 协商协议版本: {client.protocol_version}")


async def main() -> int:
    await check_mode("auto")
    await check_mode("legacy")
    print(f"\n{'全部通过' if not fails else f'{len(fails)} 项失败'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
