"""showdoc MCP 服务器入口（stdio）。日志只写 stderr，stdout 专用于 MCP 协议。"""

from __future__ import annotations

import logging
import sys

from .tools import mcp


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 请求级日志降噪
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
