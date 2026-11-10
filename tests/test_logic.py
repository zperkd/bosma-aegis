"""Offline logic tests for the Bosma Aegis coordinator (HA + aiohttp stubbed). No network."""
import asyncio
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "custom_components"))


# --- stub aiohttp ---
aiohttp = types.ModuleType("aiohttp")
class _E(Exception):
    pass
aiohttp.ClientError = _E
aiohttp.ClientResponseError = type("ClientResponseError", (_E,), {"status": 0})
aiohttp.ClientTimeout = lambda **k: None
aiohttp.ClientSession = object
aiohttp.ClientWebSocketResponse = object
aiohttp.WSMsgType = types.SimpleNamespace(TEXT=1, CLOSED=8, ERROR=9)
sys.modules["aiohttp"] = aiohttp

# --- stub homeassistant ---
def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Coord:
    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.data = {}
    def __class_getitem__(cls, item):
        return cls
    def async_set_updated_data(self, data):
        self.data = data
    async def async_shutdown(self):
        pass

_mod("homeassistant")
_mod("homeassistant.config_entries", ConfigEntry=object)
_mod("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_mod("homeassistant.exceptions", ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (Exception,), {}),
     ConfigEntryNotReady=type("ConfigEntryNotReady", (Exception,), {}))
_mod("homeassistant.helpers")
_mod("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: None)
_mod("homeassistant.helpers.update_coordinator",
     DataUpdateCoordinator=_Coord, UpdateFailed=type("UpdateFailed", (Exception,), {}))

from bosma_aegis.coordinator import BosmaCoordinator  # noqa: E402

PID = "0123456789abcdef0123456789abcdef"
DEV = [{"devicePid": PID, "deviceId": "000000ABCDEF", "name": "Garage Door"}]


class FakeBus:
    def __init__(self):
        self.events = []
    def async_fire(self, event, payload):
        self.events.append((event, payload))

class FakeHass:
    def __init__(self):
        self.bus = FakeBus()
    def async_create_task(self, coro):
        coro.close()  # tests await _attribute directly

class FakeApi:
    def __init__(self):
        self.token = "t"
        self.rows = []
    async def async_get_events(self, pid, page_size=5):
        return self.rows

def make():
    c = BosmaCoordinator.__new__(BosmaCoordinator)
    _Coord.__init__(c, FakeHass(), None)
    c.entry = None
    c.api = FakeApi()
    c.devices = DEV
    c._by_pid = {PID: DEV[0]}
    c._revision = {}
    c._seen_events = {}
    c._last_ev_ts = {}
    c._ws_task = None
    c._ws_connected = False
    c.data = {PID: {"status": None, "battery": None, "door": None, "observed_at": None, "activity": None}}
    return c

def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)

async def _nosleep(_s):
    return None
asyncio.sleep = _nosleep  # test-only: don't actually wait during attribution retries

now = time.time()
ok = 0

# 1. a push applies state
c = make()
c._apply(PID, {"lockStatus": "1", "battery": "90"}, "push", now)
assert c.data[PID]["status"] == "locked" and c.data[PID]["battery"] == 90.0, c.data[PID]
ok += 1

# 2. out-of-order poll is rejected (older observation than the push)
c._apply(PID, {"lockStatus": "0"}, "poll", now - 5)
assert c.data[PID]["status"] == "locked", "stale poll must not win"
ok += 1

# 3. unknown status ignored
c._apply(PID, {"lockStatus": "9"}, "push", now + 1)
assert c.data[PID]["status"] == "locked"
ok += 1

# 4. confident human lock -> person True, event fired
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1
c.api.rows = [{"actType": 129, "id": 10, "timestamp": now, "nickName": "Z"}]
run(c._attribute(PID, "locked", 2, now))
# revision mismatch guard: we passed revision=2 but _revision[PID]=1 -> should NOT fire. fix revision:
assert not c.hass.bus.events
c._revision[PID] = 2
run(c._attribute(PID, "locked", 2, now))
ev = c.hass.bus.events[-1][1]
assert ev["person"] is True and ev["correlated"] and ev["act_name"] == "app_lock", ev
ok += 1

# 5. auto_lock within window -> person False
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 5
c.api.rows = [{"actType": 144, "id": 20, "timestamp": now, "nickName": None}]
run(c._attribute(PID, "locked", 5, now))
ev = c.hass.bus.events[-1][1]
assert ev["person"] is False and ev["correlated"], ev
ok += 1

# 6. a STALE row (older than RECENT_WINDOW) is not attributed at all (no event fired)
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 7
c.api.rows = [{"actType": 129, "timestamp": now - 300, "nickName": "Z"}]
run(c._attribute(PID, "locked", 7, now))
assert len(c.hass.bus.events) == 0
ok += 1

# 7. availability freshness
c = make(); c.data[PID]["observed_at"] = now
assert c.available(PID) is True
c.data[PID]["observed_at"] = now - 700
assert c.available(PID) is False
ok += 1

# 8. an unchanged re-report must NOT bump the transition revision (else it supersedes pending attribution)
c = make(); c.data[PID]["status"] = "unlocked"
c._apply(PID, {"lockStatus": "1"}, "push", now)           # real change -> revision bumps
rev = c._revision[PID]
c._apply(PID, {"lockStatus": "1", "battery": "90"}, "push", now + 0.1)  # unchanged re-report
assert c._revision[PID] == rev, "unchanged re-report bumped the revision"
ok += 1

# 9. attribution works with a log row that has NO id (the real REST log has none)
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1
c.api.rows = [{"actType": 133, "timestamp": now, "nickName": "Z"}]  # voice_lock, no 'id'
run(c._attribute(PID, "locked", 1, now))
ev = c.hass.bus.events[-1][1]
assert ev["person"] is True and ev["act_name"] == "voice_lock", ev
ok += 1

# 10. log lags the push: first fetch has no matching row, a retry finds the (correlated) row
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1
_calls = {"n": 0}
async def _lagging(pid, page_size=5):
    _calls["n"] += 1
    return [] if _calls["n"] == 1 else [{"actType": 133, "timestamp": now, "nickName": "Z"}]
c.api.async_get_events = _lagging
run(c._attribute(PID, "locked", 1, now))
ev = c.hass.bus.events[-1][1]
assert ev["act_name"] == "voice_lock" and ev["person"] is True and _calls["n"] >= 2, (ev, _calls)
ok += 1

# 11. an OLD matching row present, the current action's row only appears on a later retry -> correlated to the NEW one
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1
_c2 = {"n": 0}
async def _old_then_new(pid, page_size=5):
    _c2["n"] += 1
    old = {"actType": 129, "timestamp": now - 130, "nickName": "Z"}   # old app_lock (within 180s, outside 20s)
    if _c2["n"] < 3:
        return [old]
    return [{"actType": 133, "timestamp": now, "nickName": "Z"}, old]  # current voice_lock appears
c.api.async_get_events = _old_then_new
run(c._attribute(PID, "locked", 1, now))
ev = c.hass.bus.events[-1][1]
assert ev["act_name"] == "voice_lock" and ev["person"] is True, ev
ok += 1

# 12. a row whose timestamp is AHEAD of the push (Bosma clock skew) is still attributed
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1
c.api.rows = [{"actType": 133, "timestamp": now + 25, "nickName": "Z"}]  # 25s ahead of observed
run(c._attribute(PID, "locked", 1, now))
ev = c.hass.bus.events[-1][1]
assert ev["person"] is True and ev["act_name"] == "voice_lock", ev
ok += 1

# 13. an already-consumed row (ts <= last used) is not re-attributed
c = make(); c.data[PID]["status"] = "unlocked"; c._revision[PID] = 1; c._last_ev_ts[PID] = now
c.api.rows = [{"actType": 129, "timestamp": now, "nickName": "Z"}]  # ts == last used
run(c._attribute(PID, "locked", 1, now))
assert len(c.hass.bus.events) == 0
ok += 1

# 14. IFTTT webhook URL builder (used by the optional lock platform)
from bosma_aegis.const import ifttt_url  # noqa: E402
assert ifttt_url("ABC123", "garage_door_lock") == \
    "https://maker.ifttt.com/trigger/garage_door_lock/with/key/ABC123"
ok += 1

# 15. safe_error never includes the exception text (aiohttp puts the request URL, with the token, in it)
import logging  # noqa: E402
from bosma_aegis.api import safe_error  # noqa: E402
SECRET = "SECRETTOKEN123"
class _HandshakeError(aiohttp.ClientResponseError):  # stands in for aiohttp.WSServerHandshakeError
    def __init__(self, status):
        super().__init__("401, message='Invalid response status', url='wss://x/ws?token=%s'" % SECRET)
        self.status = status
assert safe_error(_HandshakeError(401)) == "_HandshakeError (HTTP 401)", safe_error(_HandshakeError(401))
assert safe_error(aiohttp.ClientError("Cannot connect url=wss://x?token=" + SECRET)) == "_E"
assert safe_error(asyncio.TimeoutError()) == "TimeoutError"
ok += 1

# 16. a rejected websocket handshake (expired token) is logged without the token
import bosma_aegis.coordinator as _coord_mod  # noqa: E402
class _Session:
    def ws_connect(self, url, **kw):
        import base64
        assert SECRET in base64.b64decode(url.split("token=", 1)[1]).decode()  # the token really is in the URL...
        raise _HandshakeError(401)
_coord_mod.async_get_clientsession = lambda hass: _Session()
class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []
    def emit(self, record):
        self.lines.append(record.getMessage())
cap = _Capture()
_coord_mod._LOGGER.addHandler(cap); _coord_mod._LOGGER.setLevel(logging.DEBUG)
c = make(); c.api.token = SECRET
async def _stop_login(*a, **k):
    raise asyncio.CancelledError  # ends the reconnect loop after one failure
c.api.async_login = _stop_login
try:
    run(c._ws_loop())
except asyncio.CancelledError:
    pass
_coord_mod._LOGGER.removeHandler(cap)
assert cap.lines == ["websocket error: _HandshakeError (HTTP 401)"], cap.lines  # ...but never in the log
assert not any(SECRET in line for line in cap.lines)
ok += 1

print("ALL %d LOGIC TESTS PASSED" % ok)
