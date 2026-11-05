"""Small transaction guard. No dependency on AstrBot or AngelMemory."""
import asyncio
from dataclasses import dataclass, field


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(p.get('text', '') for p in value if isinstance(p, dict) and p.get('type') == 'text')
    return ''


def validate_tail(before, current, response):
    if current[:len(before)] != before:
        raise ValueError('对话历史已压缩或修改，不能准确定位上一轮。')
    tail = current[len(before):]
    visible = [m for m in tail if m.get('role') not in {'checkpoint', '_checkpoint'}]
    if [m.get('role') for m in visible] != ['user', 'assistant']:
        raise ValueError('上一轮含工具调用、并发消息或额外轮次，拒绝自动回滚。')
    if any(m.get('tool_calls') for m in tail):
        raise ValueError('上一轮含工具调用，不能撤销工具产生的外部影响。')
    if not response or text_content(visible[-1].get('content')) != response:
        raise ValueError('最后一条回复已变化，拒绝回滚其他内容。')


@dataclass
class Ticket:
    event: object
    session: str
    cid: str
    before: list
    prompt: str
    owner: str
    heart: object = None
    response: str = ''
    status: str = 'waiting'
    task: object = None
    commit: object = None


class DelayGate:
    def __init__(self, seconds):
        self.seconds = seconds
        self.tickets = {}

    async def enqueue(self, ticket, commit):
        if ticket.status != 'waiting':
            return
        ticket.status, ticket.commit = 'pending', commit
        self.tickets[id(ticket.event)] = ticket
        ticket.task = asyncio.create_task(self._later(ticket))

    async def _later(self, ticket):
        try:
            await asyncio.sleep(self.seconds)
            await self.commit(ticket)
        finally:
            self.tickets.pop(id(ticket.event), None)

    async def commit(self, ticket):
        if ticket.status != 'pending':
            return
        ticket.status = 'committing'
        try:
            result = await ticket.commit()
            if isinstance(result, dict) and result.get('status') not in {'success', 'skipped'}:
                raise RuntimeError('AngelMemory did not report successful consolidation')
            ticket.status = 'committed'
        except BaseException:
            ticket.status = 'failed'
            raise

    def cancel(self, ticket):
        if ticket.status != 'pending':
            raise ValueError('记忆尚未进入待提交状态，或已经开始整理；无法保证完整撤销。')
        ticket.status = 'cancelled'
        if ticket.task:
            ticket.task.cancel()

    async def close(self):
        tickets = list(self.tickets.values())
        for ticket in tickets:
            if ticket.task:
                ticket.task.cancel()
        await asyncio.gather(*(t.task for t in tickets if t.task), return_exceptions=True)
        results = await asyncio.gather(*(self.commit(t) for t in tickets), return_exceptions=True)
        return results
