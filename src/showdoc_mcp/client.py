"""ShowDoc HTTP API 客户端封装。

接口与认证方式均来自实测（cookie_token + PHPSESSID cookie）：
- /api/item/myList          项目列表
- /api/item/info            项目菜单树（目录 + 页面）
- /api/catalog/catListGroup 目录树（仅目录）
- /api/page/info            页面详情（标题 + markdown 正文）
- /api/catalog/save         目录创建/编辑（新建后需回查 catListGroup 拿 cat_id）
- /api/catalog/delete       删除目录
- /api/page/save            页面创建/更新（is_urlencode=1，正文需先 quote 一次）
- /api/page/delete          删除页面
"""

from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

import httpx2

# ShowDoc 登录态失效时的特征 error_code / 提示文案
_AUTH_ERROR_CODES = {10101, 10102}
_AUTH_ERROR_HINTS = ("no login", "not login", "未登录", "请登录", "登录")


class ShowDocError(Exception):
    """ShowDoc 业务错误，消息面向模型可读。"""


class ShowDocAuthError(ShowDocError):
    """登录态失效（cookie 过期）。"""


class ShowDocClient:
    def __init__(
        self,
        base_url: str,
        cookie_token: str,
        phpsessid: str,
        default_item_id: int = 0,
        timeout: float = 30.0,
        trust_env: bool = False,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_item_id = default_item_id
        self._http = httpx2.Client(
            timeout=timeout,
            # 默认忽略环境里的 http_proxy/all_proxy 等代理变量：内网 ShowDoc 直连更快更稳；
            # 需要走代理时在 MCP env 里设置 SHOWDOC_TRUST_ENV=1。
            trust_env=trust_env,
            cookies={
                "cookie_token": cookie_token,
                "PHPSESSID": phpsessid,
                "think_language": "en-US",
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": self.base_url,
                "Referer": self.base_url + "/web/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
                ),
            },
        )

    # ------------------------------------------------------------------ base

    def post(self, route: str, data: dict[str, Any]) -> Any:
        """POST {base}/server/index.php?s=/api/{route}，error_code!=0 时抛异常。"""
        url = f"{self.base_url}/server/index.php?s=/api/{route}"
        resp = self._http.post(url, data=data)
        if resp.status_code != 200:
            raise ShowDocError(f"{route} HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError:
            raise ShowDocError(f"{route} 返回非 JSON（可能 cookie 失效跳到登录页）: {resp.text[:200]}")
        error_code = body.get("error_code")
        if error_code != 0:
            msg = str(body.get("error_message") or body.get("message") or body)
            if error_code in _AUTH_ERROR_CODES or any(h in msg.lower() for h in _AUTH_ERROR_HINTS):
                raise ShowDocAuthError(
                    f"ShowDoc 登录态已失效（{route}: error_code={error_code}, {msg}）。"
                    "请从浏览器重新抓取 cookie_token 与 PHPSESSID，更新 MCP 配置 env 中的 "
                    "SHOWDOC_COOKIE_TOKEN / SHOWDOC_PHPSESSID 后重启会话。"
                )
            raise ShowDocError(f"{route} 调用失败: error_code={error_code}, {msg}")
        return body.get("data")

    def resolve_item(self, item_id: int | str | None) -> int:
        item_id = int(item_id) if item_id else self.default_item_id
        if not item_id:
            raise ShowDocError(
                "未指定 item_id 且未配置默认项目。请传 item_id 参数，或在 MCP 配置 env 里设置 SHOWDOC_DEFAULT_ITEM_ID。"
            )
        return item_id

    # ------------------------------------------------------------------ read

    def my_items(self) -> list[dict]:
        return self.post("item/myList", {"page": 1, "page_size": 100}) or []

    def item_add(self, item_name: str, item_description: str = "") -> int:
        """创建项目并回查 item_id（scripts/publish_docs.py 等脚本用；不作为 MCP 工具暴露）。
        v2.6.7 的 item/add 成功时返回空 data，因此创建后按名称在 myList 里回查。"""
        for it in self.my_items():
            if it.get("item_name") == item_name:
                return int(it["item_id"])
        self.post("item/add", {
            "item_name": item_name,
            "item_description": item_description,
            "item_type": 1,
        })
        for it in self.my_items():
            if it.get("item_name") == item_name:
                return int(it["item_id"])
        raise ShowDocError(f"项目「{item_name}」创建请求成功但 myList 未找到，请到网页端核对。")

    def item_info(self, item_id: int) -> dict:
        return self.post("item/info", {"item_id": item_id, "keyword": "", "default_page_id": 0}) or {}

    def catalog_tree(self, item_id: int) -> list[dict]:
        return self.post("catalog/catListGroup", {"item_id": item_id}) or []

    def page_info(self, page_id: int) -> dict:
        return self.post("page/info", {"page_id": page_id}) or {}

    def search_pages(self, item_id: int, keyword: str) -> list[dict]:
        """item/info 的 keyword 参数：标题+正文模糊匹配，返回扁平页面列表（v2.6.7 实测）。"""
        data = self.post("item/info", {"item_id": item_id, "keyword": keyword, "default_page_id": 0}) or {}
        return (data.get("menu") or {}).get("pages") or []

    def page_history(self, page_id: int) -> list[dict]:
        """页面版本历史（每次更新存一版，服务端每页最多保留 20 版，含完整正文）。"""
        return self.post("page/history", {"page_id": page_id}) or []

    def recycle_list(self, item_id: int) -> list[dict]:
        return self.post("recycle/getList", {"item_id": item_id}) or []

    def recycle_recover(self, item_id: int, page_id: int) -> Any:
        """从回收站恢复页面。注意：ShowDoc 恢复后页面落在顶级目录（cat_id=0）。"""
        return self.post("recycle/recover", {"item_id": item_id, "page_id": page_id})

    # ------------------------------------------------------------------ write

    def catalog_save(
        self,
        item_id: int,
        cat_name: str,
        cat_id: int | str = "undefined",
        parent_cat_id: int | str | None = "undefined",
        s_number: int | str | None = None,
    ) -> Any:
        """新建（cat_id=undefined）或编辑目录。parent_cat_id 传 None 表示顶级。"""
        data: dict[str, Any] = {"item_id": item_id, "cat_id": cat_id, "cat_name": cat_name}
        data["parent_cat_id"] = "undefined" if parent_cat_id in (None, 0) else parent_cat_id
        if s_number:
            data["s_number"] = s_number
        return self.post("catalog/save", data)

    def catalog_delete(self, item_id: int, cat_id: int) -> Any:
        return self.post("catalog/delete", {"item_id": item_id, "cat_id": cat_id})

    def page_save(
        self,
        item_id: int,
        page_title: str,
        page_content: str,
        cat_id: int | None = 0,
        page_id: int = 0,
        s_number: int | None = None,
    ) -> Any:
        # ShowDoc 约定：is_urlencode=1 时服务端会对 page_content 做一次 urldecode，
        # 因此这里先 quote 一次，httpx 表单编码再编一次。
        data: dict[str, Any] = {
            "page_id": page_id,
            "item_id": item_id,
            "page_title": page_title,
            "page_content": quote(page_content),
            "is_urlencode": 1,
        }
        if cat_id is not None:  # None = 更新时不传该字段，服务端保持页面原目录不动
            data["cat_id"] = cat_id
        if s_number:
            data["s_number"] = s_number
        return self.post("page/save", data)

    def page_delete(self, page_id: int) -> Any:
        return self.post("page/delete", {"page_id": page_id})


# ------------------------------------------------------------------ 树解析工具


def flatten_catalogs(tree: list[dict], depth: int = 1) -> Iterator[tuple[dict, int]]:
    """按任意顺序展开目录树（含子目录）。"""
    for node in tree or []:
        yield node, depth
        yield from flatten_catalogs(node.get("sub") or node.get("catalogs") or [], depth + 1)


def catalog_id_map(tree: list[dict]) -> dict[tuple[str, str], str]:
    """(parent_cat_id, cat_name) -> cat_id，用于按名称定位目录（幂等创建的依据）。"""
    return {
        (str(node.get("parent_cat_id") or "0"), node["cat_name"]): str(node["cat_id"])
        for node, _ in flatten_catalogs(tree)
    }


def find_catalog(tree: list[dict], cat_id: int | str) -> tuple[dict, int] | None:
    for node, depth in flatten_catalogs(tree):
        if str(node["cat_id"]) == str(cat_id):
            return node, depth
    return None
