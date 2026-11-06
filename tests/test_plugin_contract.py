"""AstrBot-shaped fakes: these are contract checks, not a live server test."""
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import sys
import types
import unittest

ROOT=Path(__file__).resolve().parents[1]
for name in ['astrbot','astrbot.api','astrbot.api.event','astrbot.api.provider','astrbot.api.star','astrbot.api.message_components']:
    sys.modules[name]=types.ModuleType(name)
sys.modules['astrbot.api.message_components'].Plain=lambda text:types.SimpleNamespace(text=text)
sys.modules['astrbot.api'].AstrBotConfig=dict
sys.modules['astrbot.api'].logger=logging.getLogger('test')
class Filters:
    PermissionType=types.SimpleNamespace(ADMIN=1)
    def __getattr__(self,key): return lambda *a,**kw: lambda f:f
sys.modules['astrbot.api.event'].filter=Filters()
sys.modules['astrbot.api.event'].AstrMessageEvent=object
sys.modules['astrbot.api.provider'].ProviderRequest=object
class Star:
    def __init__(self,context): self.context=context
sys.modules['astrbot.api.star'].Star=Star
sys.modules['astrbot.api.star'].Context=object
sys.modules['astrbot.api.star'].register=lambda *a:lambda c:c
package=types.ModuleType('redo_test_package'); package.__path__=[str(ROOT)]
sys.modules['redo_test_package']=package
spec=importlib.util.spec_from_file_location('redo_test_package.main',ROOT/'main.py')
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

class Event:
    def __init__(self): self.message_obj=types.SimpleNamespace(message=[],message_str='重答')
    unified_msg_origin='qq:FriendMessage:123'; message_str='hi'
    def is_admin(self): return True
    def get_group_id(self): return ''
    def get_sender_id(self): return '123'
    def should_call_llm(self,x): pass
    def request_llm(self,**kw): return kw
    def plain_result(self,x): return {'error':x}

class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.commits=[]
        async def consolidate(event,ctx=None): self.commits.append(event); return {'status':'success'}
        self.original=consolidate
        self.manager=types.SimpleNamespace(handle_memory_consolidation=consolidate)
        self.conv=types.SimpleNamespace(cid='cid',history='[]')
        self.saved=None
        async def current(umo): return 'cid'
        async def get(umo,cid): return self.conv
        async def update(umo,cid,history): self.saved=history
        cm=types.SimpleNamespace(get_curr_conversation_id=current,get_conversation=get,update_conversation=update)
        self.ctx=types.SimpleNamespace(get_registered_star=lambda name:types.SimpleNamespace(star_cls=types.SimpleNamespace(plugin_manager=self.manager)),conversation_manager=cm)
        self.plugin=module.ReplyRedo(self.ctx,{'undo_seconds':30})
    async def asyncTearDown(self): await self.plugin.terminate()
    async def test_end_to_end_deferral_and_retry_request(self):
        event=Event()
        req=types.SimpleNamespace(conversation=self.conv,image_urls=[],audio_urls=[],prompt='original question',func_tool=None)
        await self.plugin.capture(event,req)
        await self.plugin.response(event,types.SimpleNamespace(completion_text='bad answer'))
        await self.manager.handle_memory_consolidation(event)
        self.assertEqual(self.commits,[])
        self.conv.history=json.dumps([{'role':'user','content':'original question'},{'role':'assistant','content':'bad answer'}])
        retry=Event()
        out=[x async for x in self.plugin.redo(retry)]
        self.assertEqual(retry.message_obj.message[0].text,'original question')
        self.assertEqual(self.saved,[])
        self.assertEqual(out[0]['prompt'],'original question')
        self.assertEqual(event._reply_redo_ticket.status,'cancelled')
        self.assertEqual(self.commits,[])
    async def test_undo_removes_turn_without_llm_request(self):
        event=Event()
        req=types.SimpleNamespace(conversation=self.conv,image_urls=[],audio_urls=[],prompt='original question',func_tool=None)
        await self.plugin.capture(event,req)
        await self.plugin.response(event,types.SimpleNamespace(completion_text='bad answer'))
        await self.manager.handle_memory_consolidation(event)
        self.conv.history=json.dumps([{'role':'user','content':'original question'},{'role':'assistant','content':'bad answer'}])
        command=Event()
        def forbidden(**kw): raise AssertionError('undo must never request LLM')
        command.request_llm=forbidden
        out=[x async for x in self.plugin.undo(command)]
        self.assertEqual(self.saved,[])
        self.assertIn('不重新生成',out[0]['error'])
        self.assertEqual(event._reply_redo_ticket.status,'cancelled')
        self.assertNotIn(event.unified_msg_origin,self.plugin.latest)
        self.assertEqual(self.commits,[])
        self.assertEqual(command.message_str,'hi')

    async def test_tools_not_removed_globally(self):
        event=Event(); event._reply_redo_ticket=object()
        tools=types.SimpleNamespace(tools=[types.SimpleNamespace(name=n) for n in ['angel_remember','angel_note_create','angel_recall']])
        req=types.SimpleNamespace(func_tool=tools)
        await self.plugin.protect_writes(event,req)
        self.assertEqual([t.name for t in req.func_tool.tools],['angel_recall'])
        self.assertEqual(len(tools.tools),3)
    async def test_unmanaged_memory_immediate(self):
        self.plugin.bind_angel()
        e=Event()
        await self.manager.handle_memory_consolidation(e)
        self.assertEqual(self.commits,[e])
    async def test_unsupported_group_not_captured(self):
        e=Event(); e.get_group_id=lambda:'group'
        req=types.SimpleNamespace(conversation=self.conv,image_urls=[],audio_urls=[],prompt='x')
        await self.plugin.capture(e,req)
        self.assertFalse(hasattr(e,'_reply_redo_ticket'))

    async def protected_round(self):
        event=Event()
        req=types.SimpleNamespace(conversation=self.conv,image_urls=[],audio_urls=[],prompt='question',func_tool=None)
        await self.plugin.capture(event,req)
        await self.plugin.response(event,types.SimpleNamespace(completion_text='answer'))
        await self.manager.handle_memory_consolidation(event)
        self.conv.history=json.dumps([{'role':'user','content':'question'},{'role':'assistant','content':'answer'}])
        return event

    async def test_manager_reload_recovers_new_round_without_restart(self):
        self.plugin.bind_angel()
        old_manager=self.manager
        self.manager=types.SimpleNamespace(handle_memory_consolidation=self.original)
        event=await self.protected_round()
        self.assertIs(old_manager.handle_memory_consolidation,self.original)
        self.assertIs(self.plugin.bound,self.manager)
        self.assertEqual(event._reply_redo_ticket.status,'pending')
        out=[x async for x in self.plugin.undo(Event())]
        self.assertIn('不重新生成',out[0]['error'])
        self.assertEqual(self.commits,[])

    async def test_old_round_rejected_but_next_round_recovers(self):
        old=await self.protected_round()
        self.manager=types.SimpleNamespace(handle_memory_consolidation=self.original)
        out=[x async for x in self.plugin.undo(Event())]
        self.assertIn('无需重启',out[0]['error'])
        self.assertIsNone(self.saved)
        self.assertEqual(old._reply_redo_ticket.status,'pending')
        self.conv.history='[]'
        new=await self.protected_round()
        out=[x async for x in self.plugin.redo(Event())]
        self.assertEqual(out[0]['prompt'],'question')
        self.assertEqual(new._reply_redo_ticket.status,'cancelled')
        self.assertEqual(self.commits,[])

    async def test_foreign_wrapper_chain_commits_once_and_is_preserved(self):
        self.plugin.bind_angel()
        previous=self.manager.handle_memory_consolidation
        calls=[]
        async def other(event,ctx=None):
            calls.append(event)
            return await previous(event,ctx)
        self.manager.handle_memory_consolidation=other
        event=await self.protected_round()
        self.assertEqual(self.commits,[])
        self.assertEqual(calls,[])
        await self.plugin.gate.commit(event._reply_redo_ticket)
        self.assertEqual(self.commits,[event])
        self.assertEqual(calls,[event])
        await self.plugin.terminate()
        self.assertIs(self.manager.handle_memory_consolidation,other)

    async def test_disabled_or_missing_plugin_recovers_automatically(self):
        self.plugin.bind_angel()
        getter=self.ctx.get_registered_star
        self.ctx.get_registered_star=lambda name:types.SimpleNamespace(activated=False,star_cls=types.SimpleNamespace(plugin_manager=self.manager))
        self.assertFalse(self.plugin.bind_angel())
        self.assertIn('未启用',self.plugin.binding_error)
        self.ctx.get_registered_star=lambda name:None
        self.assertFalse(self.plugin.bind_angel())
        self.ctx.get_registered_star=getter
        event=await self.protected_round()
        self.assertEqual(event._reply_redo_ticket.status,'pending')

    async def test_disabled_interval_invalidates_previous_round(self):
        event=await self.protected_round()
        getter=self.ctx.get_registered_star
        self.ctx.get_registered_star=lambda name:None
        self.assertFalse(self.plugin.bind_angel())
        self.ctx.get_registered_star=getter
        out=[x async for x in self.plugin.undo(Event())]
        self.assertIn('接入变化之前',out[0]['error'])
        self.assertIsNone(self.saved)
        self.assertEqual(event._reply_redo_ticket.status,'pending')

    async def test_reload_during_history_read_does_not_rollback(self):
        event=await self.protected_round()
        async def reloading_get(umo,cid):
            self.manager=types.SimpleNamespace(handle_memory_consolidation=self.original)
            return self.conv
        self.ctx.conversation_manager.get_conversation=reloading_get
        out=[x async for x in self.plugin.undo(Event())]
        self.assertIn('接入变化之前',out[0]['error'])
        self.assertIsNone(self.saved)
        self.assertEqual(event._reply_redo_ticket.status,'pending')

    async def test_foreign_ticket_is_not_swallowed(self):
        self.plugin.bind_angel()
        event=Event()
        event._reply_redo_ticket=types.SimpleNamespace(status='waiting')
        await self.manager.handle_memory_consolidation(event)
        self.assertEqual(self.commits,[event])

    async def test_initializing_plugin_does_not_prevent_later_binding(self):
        getter=self.ctx.get_registered_star
        self.ctx.get_registered_star=lambda name:types.SimpleNamespace(star_cls=None)
        self.assertFalse(self.plugin.bind_angel())
        self.assertIn('初始化',self.plugin.binding_error)
        self.ctx.get_registered_star=getter
        self.assertTrue(self.plugin.bind_angel())

if __name__=='__main__': unittest.main()
