import asyncio
import unittest
from rollback import DelayGate, Ticket, validate_tail

class HistoryTests(unittest.TestCase):
    def test_normal_and_checkpoint(self):
        validate_tail([], [{'role':'user','content':'hi'}, {'role':'assistant','content':[{'type':'text','text':'ok'}]}, {'role':'_checkpoint'}], 'ok')
    def test_refuse_other_turn(self):
        with self.assertRaises(ValueError):
            validate_tail([], [{'role':'user'},{'role':'assistant','content':'a'},{'role':'user'}], 'a')
    def test_refuse_tool_side_effect(self):
        with self.assertRaises(ValueError):
            validate_tail([], [{'role':'user'}, {'role':'assistant','content':'a','tool_calls':[{}]}], 'a')
    def test_refuse_changed_prefix_and_response(self):
        with self.assertRaises(ValueError):
            validate_tail([{'role':'user','content':'old'}], [], 'a')
        with self.assertRaises(ValueError):
            validate_tail([], [{'role':'user'}, {'role':'assistant','content':'different'}], 'a')

class GateTests(unittest.IsolatedAsyncioTestCase):
    def ticket(self):
        return Ticket(object(), 'test', 'cid', [], 'hi', 'owner')
    async def test_cancel_prevents_all_writes(self):
        writes=[]
        gate=DelayGate(0.02)
        t=self.ticket()
        async def commit(): writes.append('write')
        await gate.enqueue(t,commit)
        gate.cancel(t)
        await asyncio.sleep(0.04)
        self.assertEqual(writes, [])
        self.assertEqual(t.status, 'cancelled')
        await gate.close()
    async def test_timeout_commits_once(self):
        writes=[]
        gate=DelayGate(0.01)
        t=self.ticket()
        async def commit(): writes.append('write'); return {'status':'success'}
        await gate.enqueue(t,commit)
        await asyncio.sleep(0.03)
        await gate.commit(t)
        self.assertEqual(writes, ['write'])
        with self.assertRaises(ValueError): gate.cancel(t)
    async def test_independent_sessions(self):
        writes=[]
        gate=DelayGate(0.01)
        a,b=self.ticket(),self.ticket()
        async def ca(): writes.append('a')
        async def cb(): writes.append('b')
        await gate.enqueue(a,ca)
        await gate.enqueue(b,cb)
        gate.cancel(a)
        await asyncio.sleep(0.03)
        self.assertEqual(writes,['b'])
    async def test_unload_flush(self):
        writes=[]
        gate=DelayGate(100)
        t=self.ticket()
        async def commit(): writes.append('write')
        await gate.enqueue(t,commit)
        await gate.close()
        self.assertEqual(writes,['write'])
    async def test_inflight_cannot_cancel(self):
        gate=DelayGate(100)
        t=self.ticket()
        started=asyncio.Event(); release=asyncio.Event()
        async def commit(): started.set(); await release.wait()
        await gate.enqueue(t,commit)
        task=asyncio.create_task(gate.commit(t))
        await started.wait()
        with self.assertRaises(ValueError): gate.cancel(t)
        release.set(); await task
        await gate.close()

if __name__ == '__main__': unittest.main()
