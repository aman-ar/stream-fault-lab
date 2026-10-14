import asyncio
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from aiohttp import ClientPayloadError
from lab.events import Events
from lab.faults import FaultSequence, Faults
from lab.media_packets import answer_metadata, packet_kind, stun_usernames
from lab.media_relay import MediaRelay
from lab.turn import Turn
import test_gateway


def rtp(pt):
    return bytes([128, pt]) + b'\x00' * 10 + b'encrypted'


def stun(username):
    value = username.encode()
    attribute = struct.pack('!HH', 6, len(value)) + value + b'\x00' * (-len(value) % 4)
    return struct.pack('!HHI', 1, len(attribute), 0x2112a442) + b'\x00' * 12 + attribute


class PacketTests(unittest.TestCase):
    def test_per_session_payload_mapping_and_control_demux(self):
        body = b'v=0\r\na=ice-ufrag:edge\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\na=rtpmap:96 H264/90000\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=rtpmap:111 opus/48000/2\r\n'
        fragments, payloads = answer_metadata(body)
        self.assertEqual(fragments, {'edge'})
        self.assertEqual(packet_kind(rtp(96), payloads), 'video')
        self.assertEqual(packet_kind(rtp(111), payloads), 'audio')
        self.assertEqual(packet_kind(bytes([128, 200]) + b'\x00'*10, payloads), 'rtcp')
        self.assertEqual(packet_kind(bytes([22]) + b'\x00'*20, payloads), 'dtls')
        self.assertEqual(stun_usernames(stun('edge:client')), ('edge', 'client'))
        self.assertEqual(stun_usernames(stun('edge:client')[:22]), ())


class ImpairmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = Events(Path(self.directory.name))
        self.relay = MediaRelay(self.events)
        self.relay.udp = True
        self.context = SimpleNamespace(payloads={96:'video', 111:'audio'})

    async def asyncTearDown(self):
        self.relay._cancel_delayed()
        self.events.close()
        self.directory.cleanup()

    async def test_selective_stall_preserves_audio_and_ice_and_reverse_direction(self):
        self.relay.configure(dict(mode='video', direction='downstream'))
        decide = lambda data,direction='downstream': self.relay._decision('udp',data,self.context,direction)[1]
        self.assertTrue(decide(rtp(96)))
        self.assertFalse(decide(rtp(111)))
        self.assertFalse(decide(stun('edge:client')))
        self.assertFalse(decide(rtp(96),'upstream'))
        self.relay.configure(dict(mode='rtp'))
        self.assertTrue(decide(rtp(111)))
        self.assertFalse(decide(stun('edge:client')))

    async def test_restore_discards_delayed_packets_and_forwards_fresh(self):
        sent=[]
        self.relay.configure(dict(mode='quality',latency_ms=150))
        self.relay.send_udp(rtp(96),sent.append,self.context,'downstream')
        self.assertEqual(self.relay.status()['queued_packets'],1)
        self.relay.set_blocked(False)
        self.relay.send_udp(rtp(111),sent.append,self.context,'downstream')
        await asyncio.sleep(.2)
        self.assertEqual(sent,[rtp(111)])
        self.assertEqual(self.relay.status()['queued_bytes'],0)
        self.assertEqual(self.relay.status()['udp_dropped_packets'],1)

    async def test_loss_preserves_control_unless_selected_and_bandwidth_is_bounded(self):
        self.relay.configure(dict(mode='quality',loss_pct=100))
        self.assertTrue(self.relay._decision('tcp',rtp(96),self.context,'downstream')[1])
        self.assertFalse(self.relay._decision('tcp',stun('edge:client'),self.context,'downstream')[1])
        self.relay.configure(dict(mode='quality',bandwidth_kbps=1))
        decisions=[self.relay._decision('udp',rtp(96),self.context,'downstream') for _ in range(100)]
        self.assertTrue(any(d[1] for d in decisions))
        self.assertTrue(all(d[2] <= 2 for d in decisions))


class HttpExpansionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_gateway.GatewayContractTests.asyncSetUp
    asyncTearDown=test_gateway.GatewayContractTests.asyncTearDown

    async def test_status_retry_after_and_lifecycle_scope(self):
        await self.faults.apply('http429','whep-post',10,options={'retry_after':17})
        response=await self.client.post('/live/whep',data='offer')
        self.assertEqual(response.status,429)
        self.assertEqual(response.headers['Retry-After'],'17')
        await self.faults.restore()
        response=await self.client.post('/live/whep',data='offer')
        resource=response.headers['Location']
        await self.faults.apply('http410','whep-lifecycle',10)
        self.assertEqual((await self.client.patch(resource,data='patch')).status,410)
        self.assertEqual((await self.client.post('/live/whep',data='offer')).status,201)

    async def test_redirect_preserves_post_body_and_strips_lab_query(self):
        await self.faults.apply('redirect307','whep-post',10)
        response=await self.client.post('/live/whep',data='offer',allow_redirects=True)
        self.assertEqual(response.status,201)
        self.assertEqual([r.status for r in response.history],[307])
        self.assertEqual(self.seen[-1],('POST','/live/whep','',b'offer'))

    async def test_stale_hls_and_truncated_body_are_real_http_behaviors(self):
        self.runtime.protocol='hls'
        await self.faults.apply('stale-playlist','hls-playlist',10)
        first=await (await self.client.get('/live/stream.m3u8?_HLS_msn=1')).read()
        count=len(self.seen)
        second=await (await self.client.get('/live/stream.m3u8?_HLS_msn=9')).read()
        self.assertEqual(first,second)
        self.assertEqual(len(self.seen),count)
        await self.faults.apply('truncate-media','hls-media',10,options={'cut_percent':50})
        response=await self.client.get('/live/seg.mp4')
        self.assertEqual(response.status,200)
        with self.assertRaises(ClientPayloadError):
            await response.read()
        await self.faults.restore()
        self.assertEqual(await (await self.client.get('/live/seg.mp4')).read(),b'media-data')

    async def test_turn_links_repeat_and_secrets_are_excluded_from_trace(self):
        turn=Turn(Path(self.directory.name),self.events)
        await turn.configure(dict(mode='external',media_host='192.0.2.1',ice_servers=[dict(urls=['turn:turn.example.test:3478?transport=udp','turns:turn.example.test:5349?transport=tcp'],username='user',credential='secret-password')]))
        self.runtime.turn=turn
        response=await self.client.options('/live/whep')
        self.assertEqual(len(response.headers.getall('Link')),2)
        response=await self.client.post('/live/whep',data='offer')
        self.assertEqual(len(response.headers.getall('Link')),2)
        self.assertIn('Link',response.headers['Access-Control-Expose-Headers'])
        self.assertNotIn('secret-password',str(list(self.events.records)))
        self.assertNotIn('credential',str(turn.status()))
        await turn.close()

    async def test_sequence_validation_and_cancel_restores_fault(self):
        sequence=FaultSequence(self.faults,self.events)
        with self.assertRaises(ValueError):
            await sequence.start([dict(duration=float('nan'))])
        await sequence.start([dict(preset='http503',scope='whep-post',duration=20,healthy=1)])
        await asyncio.sleep(.02)
        self.assertEqual(self.faults.status()['preset'],'http503')
        await sequence.stop()
        self.assertIsNone(sequence.status())
        self.assertIsNone(self.faults.status())
