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

FORWARD_NAME = "话费小账本"


def debt_notice(role, debt_usd):
    who = role or "角色"
    return (f"【系统消息】{who}和你通讯的手机欠费啦……\n"
            f"你目前欠了 ￥{debt_usd:.3f} 话费，已经到了欠费上限，暂时收不到{who}的回复了。\n"
            f"私聊任意一位角色发送 bill，打开链接就能充值。充值时会先扣掉欠费，剩下的继续使用。")


@register("astrbot_plugin_ledger", "firejunjun", "话费账本计费", "4.0.0")
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
        sys.modules["astrbot_ledger"] = types.SimpleNamespace(
            client=self.client, notify=self._notify_insufficient,
            forward_result=self.forward_result, send_forward=self.send_forward,
            billed_generate=self.billed_generate, billed_text_chat=self.billed_text_chat)
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

    # ---------- 折叠转发：系统消息和角色说话区分开 ----------
    def _node_chain(self, event: AstrMessageEvent, texts):
        uin = str(event.get_self_id() or "0")
        nodes = [Comp.Node(uin=uin, name=FORWARD_NAME, content=[Comp.Plain(t)]) for t in texts]
        try:
            return [Comp.Nodes(nodes=nodes)]
        except Exception:
            return nodes

    def forward_result(self, event: AstrMessageEvent, *texts):
        return event.chain_result(self._node_chain(event, texts))

    async def send_forward(self, event: AstrMessageEvent, *texts):
        try:
            await event.send(MessageChain(chain=self._node_chain(event, texts)))
        except Exception as e:
            logger.debug(f"[账本] 折叠消息发送失败，改发普通消息: {e}")
            await event.send(MessageChain().message("\n\n".join(texts)))

    async def send_private_forward(self, event: AstrMessageEvent, qq, *texts):
        """私聊对方，用折叠转发；对方不是好友时退回临时会话普通消息"""
        api = event.bot.api
        uin = str(event.get_self_id() or "0")
        msgs = [{"type": "node", "data": {"name": FORWARD_NAME, "uin": uin,
                                          "content": [{"type": "text", "data": {"text": t}}]}} for t in texts]
        try:
            await api.call_action("send_private_forward_msg", user_id=int(qq), messages=msgs)
            return True
        except Exception as e:
            logger.debug(f"[账本] 私聊折叠消息失败，改发普通消息: {e}")
        kwargs = {"user_id": int(qq), "message": "\n\n".join(texts)}
        if event.get_group_id():
            kwargs["group_id"] = int(event.get_group_id())
        try:
            await api.call_action("send_private_msg", **kwargs)
            return True
        except Exception as e:
            logger.debug(f"[账本] 私聊发送失败（可能不是好友）: {e}")
            return False

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
            await self.send_forward(event, text)
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
            await self.send_private_forward(event, qq, text)

    # ---------- 给其他插件用：塔罗、六爻、戳一戳等直接调用模型的地方 ----------
    async def _billed(self, event: AstrMessageEvent, kind, model, call, silent=False):
        qq = str(event.get_sender_id() or "")
        if not qq:
            return await call()
        ok, cid, info = await self.client.charge(
            qq=qq, kind=kind, model=str(model or ""), group_id=event.get_group_id() or "",
            nickname=event.get_sender_name())
        if not ok:
            if not silent:
                await self._notify_insufficient(event, info)
            return None
        try:
            resp = await call()
        except Exception:
            await self.client.settle(cid, False)
            raise
        await self.client.settle(cid, True)
        return resp

    async def billed_generate(self, context, event, kind, silent=False, **kwargs):
        return await self._billed(event, kind, kwargs.get("chat_provider_id", ""),
                                  lambda: context.llm_generate(**kwargs), silent)

    async def billed_text_chat(self, provider, event, kind, silent=False, **kwargs):
        model = ""
        try:
            model = provider.provider_config.get("id") or provider.get_model()
        except Exception:
            pass
        return await self._billed(event, kind, model, lambda: provider.text_chat(**kwargs), silent)

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
        event.set_extra("ledger_llm", True)  # 标记：这条消息真的调用了模型（主动消息插件据此判断要不要做回复后决策）
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
    async def _bill_texts(self, qq, nickname):
        url = await self.client.login_link(qq, nickname)
        line = ""
        try:
            b = await self.client.balance(qq)
            bal = b["balance_usd"]
            line = (f"当前欠费 ￥{-bal:.3f}，充值时会先扣除欠费。\n" if bal < 0
                    else f"当前话费 ￥{bal:.3f}，大约还能聊 {b['chats_left']} 次。\n")
        except Exception:
            pass
        intro = ("【系统提示-话费账本】\n" + line +
                 "\n↓ 复制下面的链接，粘贴到手机自带的浏览器（Safari、Chrome 等）里打开，"
                 "就能查看明细和充值。不要用QQ直接打开哦。\n"
                 "链接24小时内有效，可以重复打开，不要发给别人。")
        return intro, url

    @filter.command("bill", alias={"Bill", "BILL"})
    async def bill(self, event: AstrMessageEvent):
        qq = event.get_sender_id()
        try:
            intro, url = await self._bill_texts(qq, event.get_sender_name())
        except Exception as e:
            logger.warning(f"[账本] 生成账本链接失败: {e}")
            yield self.forward_result(event, "【系统提示】账本暂时连不上，过一会儿再试试。")
            return
        if not event.get_group_id():
            yield self.forward_result(event, intro, url)
            return
        # 群里：链接发到小窗，群里只留一条折叠提示
        if await self.send_private_forward(event, qq, intro, url):
            yield self.forward_result(event, "【系统提示】话费小账本发给小窗啦！")
        else:
            yield self.forward_result(event, "【系统提示】账本链接要私聊发哦，私聊发送 bill 就行。")

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
        status_block = ""
        am = sys.modules.get("astrbot_activemessage")
        if am and getattr(am, "status", None):
            try:
                st = am.status(event.get_sender_id())
                who = self.client.bot or "这个角色"
                if not st.get("followup_enabled", True):
                    fu = "未启用（管理员关闭了这个功能）"
                else:
                    fu = "开启" if st["followup_on"] else "关闭"
                status_block = (
                    "\n\n"
                    f"你在{who}这里的当前设置（私聊）\n"
                    f"· 主动消息：{'开启' if st['idle_on'] else '关闭'}，频率 {st['freq']}\n"
                    f"· 回复追发：{fu}"
                )
            except Exception as e:
                logger.debug(f"[账本] 读取主动消息设置失败: {e}")
        yield self.forward_result(event,
            "【系统提示】话费小账本使用说明\n"
            f"群聊里的指令前面要加唤醒词「{p}」，私聊直接发送bill或billhelp。\n"
            "\n"
            "查话费\n"
            f"· {p}bill：查看话费、领取账本链接（群里发会送到小窗）\n"
            f"· {p}billhelp：查看这份说明\n"
            "\n"
            "怎么计费\n"
            f"· 角色每调用一次模型 ￥{price:.3f}\n"
            "· 群聊一条消息算 1 次；私聊一条消息算 1 次（私聊开启回复追发时，私聊一条消息算2次，回复消息1次+判断是否追发消息1次）\n"
            "· 每发一张图多算 1 次（识别图片）；记忆整理类指令按实际调用次数算，聊天到特定轮数自动总结或手动生成记忆都会计费。\n"
            f"· 让角色互相说话（{p}tell名字 内容、{p}forw名字 内容）由发起的人付 1 次，之后角色之间的对话由公共钱包承担\n"
            "· 塔罗、雷诺曼、六爻的解读和追问，戳一戳的回复，妙手空空的AI物品，各算 1 次；角色回复时挑小表情也算 1 次（话费不足时自动改成随机表情，不扣钱）\n"
            f"{free_line}"
            f"· 最多可以欠 ￥{debt:.2f}，欠到上限角色会暂时没法回复，充值时先扣欠费\n"
            "\n"
            "主动消息（每个角色分开设置）\n"
            f"· {p}关闭主动消息 / {p}开启主动消息：空闲时角色会不会主动找你\n"
            f"· {p}主动消息频率 高 / 中 / 低：调整主动消息的频率，默认中（注意指令的高/中/低前有空格）。\n"
            f"· {p}关闭回复追发 / {p}开启回复追发：角色回复后会不会过一会儿再追一句，开启时私聊每条消息算2次，默认关闭。"
            f"{status_block}"
        )

    async def terminate(self):
        self.client.stop()
        try:
            await self.client.flush()
        except Exception:
            pass
