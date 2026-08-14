"""Minimal async 115 transfer client, adapted to the existing plugin workflow."""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx


class P115Error(RuntimeError):
    pass


class P115Client:
    def __init__(self, cookie: str, timeout: int = 30):
        self.cookie, self.timeout = cookie.strip(), timeout
        self.headers = {"Cookie": self.cookie, "User-Agent": "Mozilla/5.0", "Accept": "application/json, text/plain, */*"}

    @staticmethod
    def share_parts(url: str) -> tuple[str, str]:
        match = re.search(r"115\.com/s/([A-Za-z0-9]+)", url)
        if not match:
            raise P115Error("不是有效的115分享链接")
        query = parse_qs(urlparse(url).query)
        return match.group(1), str((query.get("password") or query.get("code") or [""])[0])

    async def user_id(self, client: httpx.AsyncClient) -> str:
        response = await client.get("https://my.115.com/?ct=ajax&ac=get_user_aq", headers=self.headers)
        data = response.json()
        uid = str((data.get("data") or {}).get("uid") or "")
        if not uid:
            raise P115Error("115 Cookie 已失效或无法获取账号 UID")
        return uid

    async def transfer(self, share_url: str, target_cid: str = "") -> dict[str, Any]:
        if not self.cookie:
            raise P115Error("115 Cookie 未配置")
        share_code, receive_code = self.share_parts(share_url)
        file_ids: list[str] = []
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            offset = 0
            while True:
                response = await client.get("https://webapi.115.com/share/snap", params={"share_code": share_code, "receive_code": receive_code, "limit": 100, "offset": offset, "cid": ""}, headers=self.headers)
                payload = response.json()
                if payload.get("state") is not True:
                    error = str(payload.get("error") or payload.get("message") or "获取分享信息失败")
                    if "无需重复接收" in error:
                        return {"ok": True, "duplicate": True, "message": "文件已存在，已跳过"}
                    raise P115Error(error)
                data = payload.get("data") or {}
                items = data.get("list") or []
                file_ids.extend(str(item.get("fid") or item.get("cid") or item.get("file_id")) for item in items if item.get("fid") or item.get("cid") or item.get("file_id"))
                offset += 100
                if not items or offset >= int(data.get("count") or 0):
                    break
            if not file_ids:
                raise P115Error("分享链接中没有可转存文件")
            uid = await self.user_id(client)
            response = await client.post("https://webapi.115.com/share/receive", data={"user_id": uid, "share_code": share_code, "receive_code": receive_code, "file_id": ",".join(file_ids), "cid": target_cid if str(target_cid).isdigit() else ""}, headers=self.headers)
            payload = response.json()
        if payload.get("state") is True:
            return {"ok": True, "count": len(file_ids), "message": f"转存成功：{len(file_ids)} 个文件"}
        error = str(payload.get("error") or payload.get("message") or "115转存失败")
        if "无需重复接收" in error:
            return {"ok": True, "duplicate": True, "message": "文件已存在，已跳过"}
        raise P115Error(error)

    async def offline(self, task_url: str, target_cid: str = "") -> dict[str, Any]:
        if not re.match(r"^(?:magnet:\?|ed2k://)", task_url, re.I):
            raise P115Error("仅支持 magnet 或 ed2k 链接")
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            uid = await self.user_id(client)
            signed = (await client.get("https://115.com/?ct=offline&ac=space", headers=self.headers)).json()
            data = signed.get("data") or {}
            sign, sign_time = signed.get("sign") or data.get("sign"), signed.get("time") or data.get("time")
            if not sign or not sign_time:
                raise P115Error(str(signed.get("error") or "无法获取115离线下载签名"))
            response = await client.post("https://115.com/web/lixian/?ct=lixian&ac=add_task_url", data={"url": task_url, "savepath": "", "wp_path_id": target_cid if str(target_cid).isdigit() else "", "uid": uid, "sign": sign, "time": sign_time}, headers={**self.headers, "Origin": "https://115.com", "Referer": "https://115.com/?tab=offline&mode=wangpan"})
            payload = response.json()
        if payload.get("state") is True:
            data = payload.get("data") or {}
            return {"ok": True, "task_id": str(data.get("info_hash") or data.get("task_id") or data.get("id") or ""), "status": "submitted", "message": "115离线下载任务已提交"}
        raise P115Error(str(payload.get("error_msg") or payload.get("error") or "离线任务提交失败"))

    async def offline_tasks(self) -> list[dict[str, Any]]:
        """Read current 115 offline tasks for progress polling."""
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            await self.user_id(client)
            response = await client.get("https://115.com/web/lixian/", params={"ct": "lixian", "ac": "task_lists", "page": 1}, headers={**self.headers, "Referer": "https://115.com/?tab=offline&mode=wangpan"})
        payload = response.json()
        if payload.get("state") is False:
            raise P115Error(str(payload.get("error_msg") or payload.get("error") or "离线任务状态读取失败"))
        data = payload.get("data") or payload
        items = data.get("tasks") or data.get("list") or data.get("items") or []
        return items if isinstance(items, list) else []
