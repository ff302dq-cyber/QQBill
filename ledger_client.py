import asyncio
import json
import os
import time
import uuid

import httpx
from astrbot.api import logger


class LedgerClient:
    """和话费账本通信。连不上账本时先记在本地小本本，恢复后补交。"""

    def __init__(self, url, secret, bot_name, offline_allowance, queue_path, exempt=None):
        self.exempt = set(exempt or [])
        self.url = (url or "").rstrip("/")
        self.secret = secret or ""
        self.bot = bot_name or ""
        self.offline_allowance = int(offline_allowance or 0)
        self.queue_path = queue_path
        self.queue = self._load_queue()
        self._lock = asyncio.Lock()
        self._task = None

    # ---------- 本地小本本 ----------
    def _load_queue(self):
        try:
            with open(self.queue_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def _save_queue(self):
        try:
            tmp = self.queue_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.queue, f, ensure_ascii=False)
            os.replace(tmp, self.queue_path)
        except Exception as e:
            logger.warning(f"[账本] 本地小本本保存失败: {e}")

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._flush_loop())

    def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(60)
            try:
                await self.flush()
            except Exception as e:
                logger.debug(f"[账本] 补交失败，稍后再试: {e}")

    async def flush(self):
        async with self._lock:
            if not self.queue:
                return
            left = []
            async with httpx.AsyncClient(timeout=10) as cli:
                for item in self.queue:
                    try:
                        r = await cli.post(f"{self.url}/api/charge", json={**item, "force": True},
                                           headers=self._headers())
                        if r.status_code != 200:
                            left.append(item)
                    except Exception:
                        left.append(item)
            done = len(self.queue) - len(left)
            self.queue = left
            self._save_queue()
            if done:
                logger.info(f"[账本] 断线期间的 {done} 笔记录已补交")

    # ---------- 对外接口 ----------
    def _headers(self):
        return {"X-Ledger-Secret": self.secret}

    async def _post(self, path, body, timeout=3):
        async with httpx.AsyncClient(timeout=timeout) as cli:
            return await cli.post(f"{self.url}{path}", json=body, headers=self._headers())

    async def charge(self, qq, kind="chat", model="", group_id="", nickname=None, final=False, force=False):
        """返回 (是否放行, charge_id 或 None, 详情)"""
        if str(qq) in self.exempt:
            return True, None, {"exempt": True}
        body = {"qq": str(qq), "kind": kind, "model": model, "bot": self.bot,
                "group_id": str(group_id or ""), "nickname": nickname,
                "client_ref": uuid.uuid4().hex, "final": final, "force": force}
        try:
            r = await self._post("/api/charge", body)
            if r.status_code == 402:
                return False, None, r.json()
            if r.status_code == 200:
                j = r.json()
                return True, None if (final or force) else j.get("charge_id"), j
            raise RuntimeError(f"账本返回 {r.status_code}")
        except Exception as e:
            # 连不上账本：在额度内先放行，记到本地小本本
            mine = sum(1 for i in self.queue if i["qq"] == str(qq))
            if mine >= self.offline_allowance:
                logger.warning(f"[账本] 连不上账本且 {qq} 已用完断线额度: {e}")
                return False, None, {"reason": "offline_limit"}
            body["created_offline_at"] = int(time.time())
            self.queue.append(body)
            self._save_queue()
            logger.warning(f"[账本] 连不上账本，先记在本地: {e}")
            return True, None, {"offline": True}

    async def settle(self, charge_id, ok=True):
        if not charge_id:
            return
        try:
            await self._post(f"/api/charge/{charge_id}/settle", {"ok": bool(ok)})
        except Exception as e:
            # 结算失败也没关系：账本10分钟后会自动退回没结算的挂单
            logger.debug(f"[账本] 结算失败: {e}")

    async def balance(self, qq):
        async with httpx.AsyncClient(timeout=5) as cli:
            r = await cli.get(f"{self.url}/api/balance/{qq}", headers=self._headers())
            return r.json()

    async def login_link(self, qq, nickname=None):
        r = await self._post("/api/login-link", {"qq": str(qq), "nickname": nickname}, timeout=5)
        return r.json()["url"]
