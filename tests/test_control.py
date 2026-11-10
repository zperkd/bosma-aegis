"""Offline native-control regressions, including an independent Android crypto vector."""
import asyncio
import base64
import importlib
import json
import sys
import types
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
package = types.ModuleType('bosma_aegis')
package.__path__ = [str(ROOT / 'custom_components/bosma_aegis')]
sys.modules['bosma_aegis'] = package
from bosma_aegis.api import BosmaApi, BosmaAuthError, BosmaConnectionError
from bosma_aegis.crypto import LOCK_IV, SESSION_PADDING, decode_offline_key, encrypt_challenge

MAC = '00112233445566778899aabb'
KEY = bytes(range(16))
DEVICE = {'devicePid':'0123456789abcdef0123456789abcdef','deviceId':'000000ABCDEF',
          'name':'Example Lock','remotePid':'example-remote','gatewayPid':'wrong-gateway'}

def wrapped_key():
    padder = padding.PKCS7(128).padder()
    data = padder.update(KEY) + padder.finalize()
    enc = Cipher(algorithms.AES(bytes.fromhex(MAC + SESSION_PADDING)), modes.CBC(LOCK_IV)).encryptor()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()

class Response:
    def __init__(self, body, status=200):self.body,self.status = body,status
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    def raise_for_status(self):pass
    async def json(self,**kwargs):return self.body

class Session:
    def __init__(self,responses):self.responses=list(responses);self.calls=[]
    def post(self,url,**kw):
        self.calls.append((url,kw))
        value=self.responses.pop(0)
        if isinstance(value,BaseException):raise value
        return value if isinstance(value,Response) else Response(value)

class CryptoTests(unittest.TestCase):
    def test_android_reference_vector(self):
        vector=json.loads((ROOT/'tests/protocol_vector.json').read_text())
        self.assertEqual(encrypt_challenge(vector['synthetic_challenge'],bytes.fromhex(vector['synthetic_key'])),
                         vector['synthetic_response'])
    def test_decode_authorized_key(self):self.assertEqual(decode_offline_key(wrapped_key(),MAC),KEY)
    def test_reject_bad_key_material_without_echo(self):
        for encoded,mac in [('SECRET_notbase64',MAC),(wrapped_key(),'SECRET_bad'),('',MAC)]:
            with self.assertRaisesRegex(ValueError,'Invalid Bosma offline-key response') as caught:
                decode_offline_key(encoded,mac)
            self.assertNotIn('SECRET',str(caught.exception))
    def test_invalid_challenge(self):
        for value,key in [('SECRET',KEY),('',KEY),('00',b'bad')]:
            with self.assertRaises(ValueError):encrypt_challenge(value,key)

class ControlTests(unittest.IsolatedAsyncioTestCase):
    def make(self,responses):
        session=Session(responses);api=BosmaApi(session,'test@example.invalid','synthetic','test-imei')
        api._token='synthetic-token';api._mac_key=MAC
        return api,session
    async def test_native_flow_and_direction(self):
        for unlock in (True,False):
            api,session=self.make([{'code':0,'data':{'offlineKey':wrapped_key()}},
                                   {'code':0,'data':{'rnd':'00112233445566778899aabbccddeeff'}},
                                   {'code':0,'data':{'msgId':'synthetic'}}])
            await api.async_control(DEVICE,unlock=unlock)
            self.assertEqual([url.rsplit('/',1)[-1] for url,_ in session.calls],
                             ['getOfflineKey','remoteUnlockBefore','remoteUnlock'])
            data=session.calls[-1][1]['data']
            self.assertEqual(data['dbPid'],'example-remote')
            self.assertEqual(data['isUnlock'],'1' if unlock else '0')
            self.assertEqual(data['rndEn'],encrypt_challenge('00112233445566778899aabbccddeeff',KEY))
            self.assertTrue(all('maker.ifttt' not in url for url,_ in session.calls))
    async def test_bad_key_sends_no_challenge_or_command(self):
        api,s=self.make([{'code':0,'data':{'offlineKey':'SECRET'}}])
        with self.assertRaises(BosmaConnectionError):await api.async_control(DEVICE,unlock=True)
        self.assertEqual(len(s.calls),1)
    async def test_challenge_rejection_sends_no_command(self):
        api,s=self.make([{'code':0,'data':{'offlineKey':wrapped_key()}},{'code':551,'msg':'SECRET'}])
        with self.assertRaises(BosmaConnectionError) as caught:await api.async_control(DEVICE,unlock=True)
        self.assertNotIn('SECRET',str(caught.exception));self.assertEqual(len(s.calls),2)
    async def test_submit_timeout_is_never_retried(self):
        api,s=self.make([{'code':0,'data':{'offlineKey':wrapped_key()}},
                         {'code':0,'data':{'rnd':'00112233'}},asyncio.TimeoutError('SECRET')])
        with self.assertRaises(BosmaConnectionError) as caught:await api.async_control(DEVICE,unlock=True)
        self.assertEqual(len(s.calls),3);self.assertNotIn('SECRET',str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
    async def test_submit_auth_rejection_never_resubmits(self):
        api,s=self.make([{'code':0,'data':{'offlineKey':wrapped_key()}},
                         {'code':0,'data':{'rnd':'00112233'}},{'code':507}])
        with self.assertRaises(BosmaAuthError):await api.async_control(DEVICE,unlock=False)
        self.assertEqual(len(s.calls),3)
    async def test_auth_refresh_at_key_retrieval(self):
        api,s=self.make([Response({},401),{'code':0,'data':{'token':'new-token','macKey':MAC}},
                         {'code':0,'data':{'offlineKey':wrapped_key()}},
                         {'code':0,'data':{'rnd':'00112233'}},{'code':0}])
        await api.async_control(DEVICE,unlock=False)
        self.assertEqual(s.calls[-1][1]['data']['token'],'new-token')
        self.assertEqual(sum(url.endswith('/remoteUnlock') for url,_ in s.calls),1)
    async def test_malformed_response_does_not_echo_secrets(self):
        api,s=self.make([{'code':'SECRET','msg':'SECRET'}])
        with self.assertRaises(BosmaConnectionError) as caught:await api.async_control(DEVICE,unlock=True)
        self.assertNotIn('SECRET',str(caught.exception))
    async def test_device_discovery_keeps_app_remote_identifier(self):
        api,s=self.make([])
        async def get(base,path,params):
            if path.endswith('/getList'):return {'data':[{'familyId':'example-family'}]}
            return {'data':[dict(DEVICE,pid=DEVICE['devicePid'],modelCode='010601',hubMac='app-remote',gatewayPid='different')]}
        api._get=get
        devices=await api.async_discover_locks()
        self.assertEqual(devices[0]['remotePid'],'app-remote')
    async def test_device_commands_are_serialized(self):
        api,s=self.make([]);active=0;max_active=0
        async def post(path,params,**kwargs):
            nonlocal active,max_active
            if path.endswith('/getOfflineKey'):
                active+=1;max_active=max(active,max_active);await asyncio.sleep(.01)
                return {'code':0,'data':{'offlineKey':wrapped_key()}}
            if path.endswith('/remoteUnlockBefore'):return {'code':0,'data':{'rnd':'00112233'}}
            active-=1;return {'code':0}
        api._post=post
        await asyncio.gather(api.async_control(DEVICE,unlock=True),api.async_control(DEVICE,unlock=False))
        self.assertEqual(max_active,1)


# Focused HA platform stubs: verify native dispatch and fresh-state confirmation without HA/network.
def module(name, **attrs):
    value=types.ModuleType(name)
    for key,item in attrs.items():setattr(value,key,item)
    sys.modules[name]=value
    return value

class EntityBase:
    def __init__(self,coordinator,device):self.coordinator=coordinator;self._pid=device['devicePid']
    @property
    def _obs(self):return self.coordinator.data[self._pid]
    def async_write_ha_state(self):pass
module('homeassistant');module('homeassistant.components');module('homeassistant.components.lock',LockEntity=type('LockEntity',(),{}))
module('homeassistant.config_entries',ConfigEntry=object)
module('homeassistant.core',HomeAssistant=object)
module('homeassistant.helpers');module('homeassistant.helpers.aiohttp_client',async_get_clientsession=lambda h:None)
module('homeassistant.helpers.entity_platform',AddEntitiesCallback=object)
module('bosma_aegis.coordinator',BosmaCoordinator=object)
module('bosma_aegis.entity',BosmaEntity=EntityBase)
from bosma_aegis.lock import BosmaLock, async_setup_entry
from bosma_aegis.const import DOMAIN

class LockTests(unittest.IsolatedAsyncioTestCase):
    def make(self):
        from unittest.mock import AsyncMock,Mock
        loop=asyncio.get_running_loop()
        coordinator=types.SimpleNamespace(
            data={DEVICE['devicePid']:{'status':'locked','observed_at':0}},
            api=types.SimpleNamespace(async_control=AsyncMock()),
            entry=types.SimpleNamespace(async_start_reauth=Mock()),
            async_request_refresh=AsyncMock())
        lock=BosmaLock(coordinator,DEVICE,None,'','','')
        lock.hass=types.SimpleNamespace(loop=loop)
        lock._op_seq=1;lock._pending='locked'
        lock._fire=AsyncMock(return_value=True)
        return lock,coordinator
    async def test_native_does_not_fire_ifttt_and_rejects_stale_confirmation(self):
        import time
        lock,c=self.make()
        async def refresh():c.data[DEVICE['devicePid']]['observed_at']=time.time()
        c.async_request_refresh.side_effect=refresh
        await lock._run(1,'locked','legacy_event')
        c.api.async_control.assert_awaited_once_with(DEVICE,unlock=False)
        c.async_request_refresh.assert_awaited_once()
        lock._fire.assert_not_awaited();self.assertIsNone(lock._pending)
    async def test_rejected_native_command_has_no_ifttt_fallback(self):
        lock,c=self.make();c.api.async_control.side_effect=BosmaConnectionError('synthetic')
        await lock._run(1,'unlocked','legacy_event')
        lock._fire.assert_not_awaited();self.assertIsNone(lock._pending)
    async def test_expired_auth_starts_reauth(self):
        lock,c=self.make();c.api.async_control.side_effect=BosmaAuthError()
        await lock._run(1,'locked','')
        c.entry.async_start_reauth.assert_called_once_with(lock.hass)
        lock._fire.assert_not_awaited()
    async def test_superseded_command_is_not_sent(self):
        lock,c=self.make();lock._op_seq=2
        await lock._run(1,'unlocked','')
        c.api.async_control.assert_not_awaited()
        self.assertEqual(lock._pending,'locked')
    async def test_read_only_creates_no_control_entities(self):
        from unittest.mock import Mock
        added=Mock()
        await async_setup_entry(None,types.SimpleNamespace(options={'control_mode':'read_only'}),added)
        added.assert_not_called()
    async def test_native_entities_do_not_require_ifttt_config(self):
        from unittest.mock import Mock
        lock,c=self.make();c.devices=[DEVICE]
        hass=types.SimpleNamespace(data={DOMAIN:{'example-entry':c}})
        added=Mock()
        await async_setup_entry(hass,types.SimpleNamespace(entry_id='example-entry',options={}),added)
        entities=added.call_args.args[0]
        self.assertEqual(len(entities),1);self.assertEqual(entities[0]._attr_unique_id,DEVICE['devicePid']+'_lock')

if __name__=='__main__':unittest.main()
