# Bosma Aegis integration — how it works

This document explains the full design: the Bosma protocol it speaks, how the integration turns that into Home
Assistant entities and events, and the engineering decisions (ordering, attribution confidence, availability,
reconnection) that make it reliable.

- [1. The Bosma cloud protocol](#1-the-bosma-cloud-protocol)
- [2. Integration structure](#2-integration-structure)
- [3. State: ordered observations](#3-state-ordered-observations)
- [4. Attribution: who and how (with confidence)](#4-attribution-who-and-how-with-confidence)
- [5. Availability](#5-availability)
- [6. Authentication & reconnection](#6-authentication--reconnection)
- [7. Entities and the event](#7-entities-and-the-event)
- [8. Using the activity event](#8-using-the-activity-event)
- [9. Native Bosma cloud control](#9-native-bosma-cloud-control-v103)
- [10. Limitations & honest caveats](#10-limitations--honest-caveats)

---

## 1. The Bosma cloud protocol

The integration uses the same HTTPS + websocket endpoints the Bosma mobile app uses. All of them need only the
account **login token** (the lock‑reading endpoints are not in the signed `/sdk/` family), so reading is reproducible
with just your account.

| Purpose | Endpoint | Returns |
| --- | --- | --- |
| Login | `POST /bosma-smart-global/api/global/user/login` | `data.token` (password sent as MD5) |
| Discover locks | `GET /bosma-smart-frontend/api/family/getFamilyDeviceList` (fallback `…/api/userdevice/queryAllByType`) | device list incl. `devicePid`, `deviceId`, name, `modelCode` (Aegis = `0106…`) |
| Lock state (shadow) | `GET /bosma-smart-frontend/api/deviceShadow/getDeviceInfo?deviceId=…&fields=lockStatus,doorStatus,battery` | `{lockStatus, doorStatus, battery}` — `lockStatus` 1 = locked, 0 = unlocked |
| Activity log | `GET /bosma-smart-frontend/api/log/lock/list?devicePids=…&pageSize=5&pageCurrent=1` | newest‑first rows `{actType, nickName, timestamp(s), id, …}` |

**Instant pushes (websocket):**

```
wss://smarthome.bosmaliving.com/bosma-smart-websocket/ws?token=<B64>
    where <B64> = base64( {"lang":"en_US","token":"<token>","client":"APP"} )
```

After connecting the client sends a subscribe frame for its lock PIDs:

```json
{"action":"notification/subscribe","reqData":{"devicePid":"<pid1>,<pid2>"}}
```

and then receives, the instant anything changes:

```json
{"action":"notification/data","respData":{"data":{
  "devicePid":"…","modelCode":"0106…","notifyType":"DEVICE_SHADOW",
  "notifyData":{"lockStatus":"0","doorStatus":"1","battery":"100","jammed":"0"}}}}
```

Keep‑alive is an app‑level text `"ping"` every ~30 s; the server answers `"pong"` (and may send `"ping"`, which we
answer with `"pong"`).

**`actType` — the "how".** Each activity row carries an `actType` code. The important distinction for automations is
**human vs. automatic**:

- *Human:* `app_lock` (129), `keypad_password_lock` (135), `fingerprint` (2), `onetouch_lock` (137), `manual` (255), … 
- *Automatic / not a person:* `auto_lock` (144), `auto_unlock` (16), and the alarm/door‑event codes.

The full map is in [`const.py`](../custom_components/bosma_aegis/const.py) (`ACTTYPE`, `NON_PERSON`, `LOCK_ACTS`,
`UNLOCK_ACTS`).

---

## 2. Integration structure

```
custom_components/bosma_aegis/
  api.py           Async Bosma reader: login, discovery, shadow, activity log. Read‑only.
  coordinator.py   The brain: websocket push + REST reconcile, ordering, attribution, availability.
  __init__.py      Entry setup/teardown; starts the websocket task.
  config_flow.py   UI setup (email/password → discover locks), re‑auth, options (poll interval).
  sensor.py        status / battery / last_activity sensors.
  binary_sensor.py jammed / door binary sensors.
  entity.py        Shared base: one HA device per lock; availability from observation freshness.
  const.py         Endpoints, headers, actType maps, timing windows.
```

The `BosmaCoordinator` (a `DataUpdateCoordinator`) owns all state. Entities are thin `CoordinatorEntity`s that read
`coordinator.data[devicePid]`.

Two inputs feed the coordinator:

1. a **background websocket task** (`_ws_loop`) that applies `DEVICE_SHADOW` pushes the instant they arrive, and
2. the coordinator's **periodic `_async_update_data`** that polls each lock's shadow every *N* seconds (default 60)
   as a reconciliation backstop.

Both call the same `_apply(...)`. Because everything runs in Home Assistant's single event loop, there are no
threads or locks — ordering is handled by observation timestamps (below).

---

## 3. State: ordered observations

Every observation (push or poll) is stamped with an **observation time** (`observed`). For a poll, that's the moment
*before* the REST request starts; for a push, the moment it arrives.

`_apply` rejects any observation **older** than the one already applied for that lock:

```python
if observed < state["observed_at"]:
    return  # a newer observation already won
```

This is the whole ordering story: a slow poll whose request was in flight while a newer push landed will have an
older `observed` and is dropped, so **a stale read can never overwrite newer state**. Unknown `lockStatus` values
(anything other than `0`/`1`) are ignored rather than guessed, and far‑future timestamps are rejected.

A real change (`push` only) also schedules **attribution**.

---

## 4. Attribution: who and how (with confidence)

When a live push changes a lock, the coordinator reads the **activity log** and finds the row that best matches the
transition:

1. **Direction** must match — a `locked` transition only accepts a lock `actType`, an `unlock` only an unlock one.
2. The row must be **recent** relative to the transition (within `FRESH_WINDOW` = 180 s) and carry a usable `id` +
   `timestamp`.
3. Among matches, the **newest** row is chosen.

The chosen row is always shown as **historical** activity (the `last_activity` sensor + its `who`/`act_type`). But the
`person` flag — the one thing an automation should act on — is set **only when the match is *confident***:

```python
correlated = (not already_consumed) and (observed - CORRELATION_WINDOW <= row_ts <= observed + CLOCK_SKEW)
person     = (actType is a human code) if correlated else None
```

- `CORRELATION_WINDOW` = **20 s**: the row's own timestamp must sit right around the transition. A human lock from a
  minute ago that happens to be the newest row is **not** treated as *this* action (`person: null`).
- `person` is `True` only for a **human** actType inside that window; **`False`** for `auto_lock`/`auto_unlock`/alarm
  codes; **`None`** when it can't be confidently correlated.

**This is a heuristic, not proof.** A single newest REST row can't be *cryptographically* tied to a specific
transition, and rapid intervening events aren't observed. The 20‑second window makes a false "a person did it" far
less likely than a naive "newest row wins," but it does not eliminate it — so the integration keeps `person` out of
anything by default and leaves the decision to *your* automation. A fired event is deduplicated by `event_id` and only
emitted for genuinely live transitions.

---

## 5. Availability

Each lock is **available** while *either* source observed it within `FRESH_AVAILABILITY` = **11 minutes**:

```python
def available(pid):
    obs = data[pid]["observed_at"]
    return obs is not None and (now - obs) < 660
```

The websocket updates `observed_at` on every push; the poll updates it every 60 s. So a healthy lock is always fresh.
If the websocket dies *and* polls fail (cloud outage, token death), `observed_at` goes stale and the entities become
**`unavailable`** — surfacing the loss of live data instead of showing a stale "locked" forever. Availability is
deliberately independent of the coordinator's last poll result, so a single failed poll with a live websocket does
**not** flap the entities.

---

## 6. Authentication & reconnection

- **Login** sends the password as MD5 and stores the token with a **generation** counter.
- Every authenticated GET retries **once** on an auth failure — covering both a JSON `code` of `401`/`507`/"expired"
  *and* a real HTTP `401` (which raises before any JSON body). The retry is **single‑flight and generation‑aware**: if
  another coroutine already refreshed the token, we reuse it instead of logging in again, so concurrent 401s don't
  invalidate each other's sessions.
- The **websocket** reconnects on any drop with **exponential backoff** (2 s → 60 s), refreshing the token before each
  reconnect, and re‑subscribes on connect. A per‑entry stable device id (`imei`) is generated at setup so repeated
  logins don't trip Bosma's "device changed" protection.

---

## 7. Entities and the event

Per lock (one HA **device**, grouped by `devicePid`):

| Entity | State | Attributes |
| --- | --- | --- |
| `sensor.<lock>_status` | `locked`/`unlocked`/`jammed` | — |
| `sensor.<lock>_battery` | battery % | diagnostic |
| `sensor.<lock>_last_activity` | action name | `who`, `act_type`, `person`, `correlated`, `attribution` |
| `binary_sensor.<lock>_jammed` | on = jammed | — |
| `binary_sensor.<lock>_door` | on = open | only if the lock has a door sensor |
| `lock.<lock>` | `locked`/`unlocked`/`locking`/`unlocking`/`jammed` | native cloud control by default |

And the **`bosma_aegis_activity`** event (see the README for its schema) fires on each live, correlated change.

The `lock` platform creates one lock entity per device by default. State comes from the coordinator;
commands use the authenticated REST challenge-response flow described in §9. Read-only mode omits control
entities. Legacy IFTTT mode uses the existing configured webhooks. Entity unique IDs are preserved.

---

## 8. Using the activity event

**Porch light off when a person locks the front door (ignore the auto‑lock):**

```yaml
automation:
  - alias: Porch light off when someone locks the front door
    triggers:
      - trigger: event
        event_type: bosma_aegis_activity
        event_data:
          name: Front Door
    conditions:
      - "{{ trigger.event.data.locked and trigger.event.data.person }}"
    actions:
      - action: light.turn_off
        target: { entity_id: light.front_porch }
```

`trigger.event.data.person` is truthy only on a **confident human lock**, so the auto‑lock (which fires `person: false`)
and any uncorrelated activity (`person: null`) won't turn the light off.

**Notify who unlocked:**

```yaml
      - trigger: event
        event_type: bosma_aegis_activity
        event_data: { name: Front Door }
    conditions:
      - "{{ not trigger.event.data.locked }}"
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >-
            Front door unlocked
            {{ '' if trigger.event.data.person is none else 'by ' ~ (trigger.event.data.who or 'someone') }}
            ({{ trigger.event.data.act_name }})
```

---

## 9. Native Bosma cloud control (v1.0.3)

The Android app exposes a working REST control flow, verified on an Aegis lock and gateway:

1. Login provides an account-specific `macKey`. `POST api/lock/getOfflineKey` with the authorized device identifiers
   returns the encrypted `offlineKey`.
2. Decode the base64 ciphertext and decrypt with AES-CBC/PKCS7, using the app's shared session suffix and IV.
   Account and device key material stays in memory.
3. `POST api/lock/remoteUnlockBefore` yields a fresh random challenge. Encrypt its hex-decoded bytes with the
   decoded lock key (AES-CBC/PKCS7), and uppercase-hex encode the response.
4. `POST api/lock/remoteUnlock` submits `devicePid`, `dbPid`, `rndEn`, `isUnlock` and the token. `dbPid` follows the
   app's `hubMac`, falling back to `lockPid`; `gatewayPid` is a different identifier and must not replace it.
   `isUnlock=1` unlocks and `isUnlock=0` locks.
5. Accepted is not completed: confirm a fresh observed lock state via websocket or reconciliation for up to 90 s.

Serialize commands per lock. Auth recovery is permitted for key retrieval; the challenge and physical command
must remain in one session generation. An ambiguous POST failure is never retried automatically. Native mode
never falls back to IFTTT. Legacy webhook configuration is retained only as an explicitly selected alternative.
No TUTK transport or BLE implementation is needed for this cloud path. See `crypto.py` and `api.py`.

### Protocol constant verification

The IV is shared by the inspected Bosma Android app, rather than provisioned per lock. Static analysis of
version 2.5.2 build005's `libbosmaencrypt.so` confirms that
`Java_com_bosma_encrypt_EncryptUtil_n_1getAegisLockKeyIV` loads the fixed literal `5beba2290a7fc515`
from the library and returns it through JNI. The companion session-padding function similarly returns the
fixed suffix `61622324`. Neither function takes a lock identifier or account key.

Use the IV's **UTF-8 bytes**, matching the app; it is not hex-decoded. Account-specific `macKey` and the
authorized per-lock key remain distinct from these public app constants. The synthetic Android reference
vector in `tests/protocol_vector.json` verifies Python challenge-encryption parity. This establishes the
constants for the inspected app version; other app/firmware variants have not been independently tested.

### Startup metadata

Setup refreshes device discovery to obtain current gateway routing metadata, including when upgrading an
older entry that lacks it. An empty discovery result falls back to the entry's cached device list. A discovery
transport error schedules setup retry, and an authentication failure starts reauthentication. Fresh cloud
state is still required by the first coordinator refresh before entities become available. Cached discovery
metadata is not used as a substitute for a fresh observation of lock state.


---

## 10. Limitations & honest caveats

- **Unofficial / reverse‑engineered.** Bosma could change its cloud API at any time; discovery (the device‑list shape)
  is the most likely thing to need a tweak.
- **Attribution is a heuristic**, not proof — see §4. `person: true` is a confident best guess, never a guarantee.
- **Cloud‑dependent.** No internet / Bosma outage → entities go `unavailable` (by design). There is no local path.
- **One account per entry.** Add the integration again for a second Bosma account.
