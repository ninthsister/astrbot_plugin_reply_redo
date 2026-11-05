import copy
import json
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from .rollback import Ticket, DelayGate, validate_tail


@register('astrbot_plugin_reply_redo', 'wtx', '重答上一轮并取消待写入的 AngelMemory 记忆', '0.1.1')
class ReplyRedo(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        seconds = max(30, min(1800, int(config.get('undo_seconds', 300))))
        self.gate = DelayGate(seconds)
        self.latest = {}
        self.bound = None
        self.original = None
        self.wrapper = None
        self.closing = False

    def bind_angel(self):
        meta = self.context.get_registered_star('astrbot_plugin_angel_memory')
        angel = getattr(meta, 'star_cls', None)
        manager = getattr(angel, 'plugin_manager', None)
        if manager is None:
            return False
        if self.bound is manager and manager.handle_memory_consolidation is self.wrapper:
            return True
        # Fail closed after another plugin or hot reload replaces our integration.
        if self.bound is not None:
            return False
        original = getattr(manager, 'handle_memory_consolidation', None)
        if not callable(original):
            return False

        async def wrapped(event, event_plugin_context=None):
            ticket = getattr(event, '_reply_redo_ticket', None)
            if self.closing or ticket is None:
                return await original(event, event_plugin_context)
            if ticket.status == 'waiting':
                async def commit():
                    return await original(event, event_plugin_context)
                await self.gate.enqueue(ticket, commit)
                def finished(task):
                    if not task.cancelled():
                        exc = task.exception()
                        if exc:
                            logger.error(f'[ReplyRedo] 延迟记忆提交失败：{type(exc).__name__}')
                    if self.latest.get(ticket.session) is ticket and ticket.status != 'pending':
                        self.latest.pop(ticket.session, None)
                ticket.task.add_done_callback(finished)
            return {'status': 'skipped', 'message': 'ReplyRedo owns deferred consolidation'}

        manager.handle_memory_consolidation = wrapped
        self.bound, self.original, self.wrapper = manager, original, wrapped
        logger.info('[ReplyRedo] AngelMemory 自动整理已接入延迟提交')
        return True

    @filter.on_astrbot_loaded()
    async def ready(self):
        self.bind_angel()

    @filter.on_llm_request(priority=10000)
    async def capture(self, event: AstrMessageEvent, req: ProviderRequest):
        # Administrator check comes from AstrBot's active configuration.
        if self.closing or not event.is_admin() or not self.bind_angel():
            return
        if getattr(event, '_reply_redo_ticket', None) is not None:
            return
        # First release protects text conversations only. Media refs may expire.
        if (event.get_group_id() or getattr(event, 'angelheart_context', None)
                or not req.conversation or req.image_urls or getattr(req, 'audio_urls', [])):
            return
        before = json.loads(req.conversation.history or '[]')
        ticket = Ticket(event, event.unified_msg_origin, req.conversation.cid,
                        copy.deepcopy(before), req.prompt or '', str(event.get_sender_id()),
                        copy.deepcopy(getattr(event, 'angelheart_context', None)))
        event._reply_redo_ticket = ticket
        self.latest[ticket.session] = ticket

    @filter.on_llm_request(priority=-10000)
    async def protect_writes(self, event: AstrMessageEvent, req: ProviderRequest):
        if getattr(event, '_reply_redo_ticket', None) is None or req.func_tool is None:
            return
        # Clone the per-request container, never mutate the globally registered tools.
        tools = copy.copy(req.func_tool)
        tools.tools = [t for t in req.func_tool.tools
                       if t.name not in {'angel_remember', 'angel_note_create'}]
        req.func_tool = tools

    @filter.on_llm_response(priority=-10000)
    async def response(self, event: AstrMessageEvent, response):
        ticket = getattr(event, '_reply_redo_ticket', None)
        if ticket:
            ticket.response = getattr(response, 'completion_text', '') or ''

    @filter.command('重答')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def redo(self, event: AstrMessageEvent):
        async for result in self._rollback(event, regenerate=True):
            yield result

    @filter.command('撤销')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def undo(self, event: AstrMessageEvent):
        async for result in self._rollback(event, regenerate=False):
            yield result

    async def _rollback(self, event: AstrMessageEvent, regenerate: bool):
        event.should_call_llm(False)
        if not event.is_admin():
            yield event.plain_result('仅 AstrBot 管理员可使用。')
            return
        ticket = self.latest.get(event.unified_msg_origin)
        if not ticket or ticket.owner != str(event.get_sender_id()):
            yield event.plain_result('没有可撤销的本人上一轮私聊文本回复。需安装后产生、AngelMemory 已启用，且不使用 AngelHeart。')
            return
        cm = self.context.conversation_manager
        try:
            if not self.bind_angel():
                raise ValueError('AngelMemory 已重载或卸载，请重启本插件后再使用。')
            cid = await cm.get_curr_conversation_id(ticket.session)
            if cid != ticket.cid:
                raise ValueError('当前对话已切换，不能撤销旧对话。')
            conv = await cm.get_conversation(ticket.session, cid)
            if conv is None:
                raise ValueError('对话不存在。')
            validate_tail(ticket.before, json.loads(conv.history or '[]'), ticket.response)
            # No await between pending-state check and cancellation.
            self.gate.cancel(ticket)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            yield event.plain_result(str(exc))
            return
        try:
            await cm.update_conversation(ticket.session, ticket.cid,
                                         history=copy.deepcopy(ticket.before))
        except Exception:
            # Restore memory scheduling if history update did not succeed.
            ticket.status = 'waiting'
            await self.gate.enqueue(ticket, ticket.commit)
            logger.exception('[ReplyRedo] 对话回滚失败')
            yield event.plain_result('对话回滚失败，未发起重答；记忆恢复为待提交。')
            return
        self.latest.pop(ticket.session, None)
        if not regenerate:
            yield event.plain_result('已移除上一轮用户输入和助手回复，并取消对应待提交记忆；不重新生成。')
            return
        # Re-run the ordinary agent path with original text/persona/history.
        conv.history = json.dumps(ticket.before, ensure_ascii=False)
        event.message_str = ticket.prompt
        # AngelMemory reads get_message_outline(), not only message_str.
        event.message_obj = copy.copy(event.message_obj)
        event.message_obj.message = [Plain(ticket.prompt)]
        event.message_obj.message_str = ticket.prompt
        if ticket.heart is not None:
            event.angelheart_context = copy.deepcopy(ticket.heart)
        # A retry itself becomes a fresh protected round in capture().
        yield event.request_llm(prompt=ticket.prompt, conversation=conv)

    @filter.command('重答状态')
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def status(self, event: AstrMessageEvent):
        event.should_call_llm(False)
        ticket = self.latest.get(event.unified_msg_origin)
        bound = self.bind_angel()
        yield event.plain_result(
            f'AngelMemory 接入：{bound}；重答窗口：{self.gate.seconds}秒；'
            f'上一轮状态：{ticket.status if ticket else "无"}。'
            '仅 pending 状态支持撤销记忆；不撤回 QQ 中已发送的消息。')

    async def terminate(self):
        self.closing = True
        results = await self.gate.close()
        for result in results:
            if isinstance(result, BaseException):
                logger.error(f'[ReplyRedo] 停用时记忆提交失败：{type(result).__name__}')
        if self.bound and self.bound.handle_memory_consolidation is self.wrapper:
            self.bound.handle_memory_consolidation = self.original
