import os
import re
import sys
import time
import types

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register

from .ledger_client import LedgerClient

def debt_notice(role, debt_usd):
    who = role or "角色"
    return (f"【系统消息】{who}和你通讯的手机欠费啦……\n"
            f"你目前欠了 ￥{debt_usd:.3f} 话费，已经到了欠费上限，暂时收不到{who}的回复了。\n"
            f"私聊任意一位角色发送 bill，打开链接就能充值。充值时会先扣掉欠费，剩下的继续使用。")


@register("astrbot_plugin_ledger", "firejunjun", "话费账本计费", "1.0.0")
class LedgerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        try:
            data_dir = str(StarTools.get_data_dir("astrbot_plugin_ledger"))
        except Exception:
            data_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(data_dir, exist_ok=True)
        exempt = [q.strip() for q in str(config.get("exempt_qq", "")).split(",") if q.strip()]
        self.client = LedgerClient(
            url=config.get("ledger_url", ""),
            secret=config.get("secret", ""),
            bot_name=config.get("bot_name", ""),
            offline_allowance=config.get("offline_allowance", 5),
            queue_path=os.path.join(data_dir, "offline_queue.json"),
            exempt=exempt,
        )
        # 让记忆插件、主动消息插件也能找到同一个账本客户端
        sys.modules["astrbot_ledger"] = types.SimpleNamespace(client=self.client, notify=self._notify_insufficient)
        self.client.start()
        self._last_notice = {}
        logger.info(f"[账本] 已启动，角色={self.client.bot or '未命名'}，账本={self.client.url}")

    # ---------- 工具 ----------
    def _model_name(self, event):
        try:
            p = self.context.get_using_provider(umo=event.unified_msg_origin)
        except TypeError:
            p = self.context.get_using_provider()
        except Exception:
            p = None
        if not p:
            return ""
        for getter in (lambda: p.provider_config.get("id"), lambda: p.get_model()):
            try:
                v = getter()
                if v:
                    return str(v)
            except Exception:
                pass
        return ""

    def _cooldown_ok(self, qq):
        cooldown = int(self.config.get("notice_cooldown_minutes", 30)) * 60
        now = time.time()
        if now - self._last_notice.get(qq, 0) < cooldown:
            return False
        self._last_notice[qq] = now
        return True

    def _wake_prefixes(self, event: AstrMessageEvent):
        try:
            cfg = self.context.get_config(umo=event.unified_msg_origin)
        except TypeError:
            cfg = self.context.get_config()
        except Exception:
            cfg = {}
        wp = (cfg or {}).get("wake_prefix", ["/"])
        if isinstance(wp, str):
            wp = [wp]
        return [p for p in wp if p] or ["/"]

    async def _notify_insufficient(self, event: AstrMessageEvent, info):
        """欠费到上限：私聊直接回系统消息；群聊给那条消息贴表情，再私聊发系统消息"""
        qq = str(event.get_sender_id())
        bal = (info or {}).get("balance", 0) if isinstance(info, dict) else 0
        text = debt_notice(self.client.bot, max(-bal, 0) / 1_000_000)
        group = event.get_group_id()
        if not group:
            await event.send(MessageChain().message(text))
            return
        if event.get_platform_name() != "aiocqhttp":
            return
        api = event.bot.api
        emoji = str(self.config.get("insufficient_emoji_id", "") or "").strip()
        if emoji:
            try:
                await api.call_action("set_msg_emoji_like", message_id=event.message_obj.message_id, emoji_id=emoji)
            except Exception as e:
                logger.debug(f"[账本] 贴表情失败: {e}")
        if self._cooldown_ok(qq):
            try:
                await api.call_action("send_private_msg", user_id=int(qq), group_id=int(group), message=text)
            except Exception as e:
                logger.debug(f"[账本] 私聊系统消息发送失败（可能不是好友）: {e}")

    # ---------- 聊天计费 ----------
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        qq = event.get_sender_id()
        if not qq or event.get_extra("ledger_cid"):
            return
        group = event.get_group_id() or ""
        text = (event.message_str or "").strip()
        for p in self._wake_prefixes(event):
            if text.startswith(p):
                text = text[len(p):]
                break
        kind = "tell" if re.match(r"tell\S", text) else "chat"
        ok, cid, info = await self.client.charge(
            qq=qq, kind=kind, model=self._model_name(event), group_id=group, nickname=event.get_sender_name())
        if not ok:
            event.stop_event()
            await self._notify_insufficient(event, info)
            return
        if cid:
            event.set_extra("ledger_cid", cid)
        if self.config.get("charge_images", True):
            n = sum(1 for c in event.message_obj.message if isinstance(c, Comp.Image))
            for _ in range(n):
                await self.client.charge(qq=qq, kind="image", model=self._model_name(event),
                                         group_id=group, force=True)

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        cid = event.get_extra("ledger_cid")
        if cid:
            await self.client.settle(cid, True)
            event.set_extra("ledger_cid", None)
        # 模型最终失败、没走到这里的挂单，账本会在10分钟后自动退回

    # ---------- 指令 ----------
    async def _bill_text(self, qq, nickname):
        url = await self.client.login_link(qq, nickname)
        line = ""
        try:
            b = await self.client.balance(qq)
            bal = b["balance_usd"]
            line = (f"当前欠费 ￥{-bal:.3f}，充值时会先扣除欠费。\n" if bal < 0
                    else f"当前话费 ￥{bal:.3f}，大约还能聊 {b['chats_left']} 次。\n")
        except Exception:
            pass
        return f"【话费账本】\n{line}查看明细或充值请点链接（15分钟内有效，不要发给别人）：\n{url}"

    @filter.command("bill", alias={"Bill", "BILL"})
    async def bill(self, event: AstrMessageEvent):
        qq = event.get_sender_id()
        try:
            text = await self._bill_text(qq, event.get_sender_name())
        except Exception as e:
            logger.warning(f"[账本] 生成账本链接失败: {e}")
            yield event.plain_result("账本暂时连不上，过一会儿再试试。")
            return
        group = event.get_group_id()
        if not group:
            yield event.plain_result(text)
            return
        # 群里：链接私聊发过去，群里只说一句
        try:
            await event.bot.api.call_action("send_private_msg", user_id=int(qq), group_id=int(group), message=text)
        except Exception as e:
            logger.debug(f"[账本] 群里 bill 私聊发送失败: {e}")
            yield event.plain_result("账本链接要私聊发哦，私聊我发送 bill 就行。")
            return
        yield event.plain_result("【系统提示】话费小账本发给小窗啦！")

    @filter.command("billhelp", alias={"Billhelp", "BILLHELP", "billHelp"})
    async def billhelp(self, event: AstrMessageEvent):
        p = self._wake_prefixes(event)[0]
        price, debt, free = 0.035, 0.35, 0
        try:
            b = await self.client.balance(event.get_sender_id())
            price, debt, free = b.get("price", price), b.get("debt_limit", debt), b.get("free_daily", free)
        except Exception:
            pass
        free_line = f"· 每人每天前 {free} 次调用免费\n" if free else ""
        yield event.plain_result(
            "【系统提示】话费小账本使用说明\n"
            f"群聊里的指令前面要加唤醒词「{p}」，私聊直接发送就行。\n"
            "\n"
            "查话费\n"
            f"· {p}bill：查看话费、领取账本链接（群里发会送到小窗）\n"
            f"· {p}billhelp：查看这份说明\n"
            "\n"
            "怎么计费\n"
            f"· 角色每调用一次模型 ￥{price:.3f}\n"
            "· 群聊一条消息算 1 次；私聊一条消息算 2 次（回复 1 次，判断要不要追发 1 次）\n"
            "· 每发一张图多算 1 次；记忆整理类指令按实际调用次数算\n"
            f"· 让角色互相说话（{p}tell名字 内容、{p}forw名字 内容）由发起的人付 1 次，之后角色之间的对话由公共钱包承担\n"
            f"{free_line}"
            f"· 最多可以欠 ￥{debt:.2f}，欠到上限角色会暂时没法回复，充值时先扣欠费\n"
            "\n"
            "主动消息（每个角色分开设置）\n"
            f"· {p}关闭主动消息 / {p}开启主动消息：空闲时角色会不会主动找你\n"
            f"· {p}主动消息频率 高 / 中 / 低\n"
            f"· {p}关闭回复追发 / {p}开启回复追发：角色回复后会不会过一会儿再追一句，关掉后私聊每条只算 1 次"
        )

    async def terminate(self):
        self.client.stop()
        try:
            await self.client.flush()
        except Exception:
            pass
