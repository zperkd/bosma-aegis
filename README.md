# Bosma Aegis for Home Assistant

A Home Assistant integration that brings your **Bosma Aegis smart lock(s)** into HA with **instant updates** and
**rich activity** â€” lock/unlock state, battery, door contact, jam, and *how* each change happened (keypad, app,
fingerprint, autoâ€‘lock, by hand) with *who* did it.

It runs **entirely inside Home Assistant** â€” no separate bridge, service, or addâ€‘on â€” and talks to the Bosma cloud the
same way the Bosma app does (a persistent websocket for instant pushes, plus a periodic REST reconcile).

**Native lock control is included in v1.0.3.** Lock and unlock through Bosma's authenticated cloud API,
with encrypted challenge-response and confirmation from the actual reported lock state. No IFTTT subscription,
phone, Bluetooth adapter or separate bridge is needed. The Bosma gateway and internet are required.
Choose **Read only** under Configure â†’ Lock control to disable commands. Existing IFTTT settings remain
available under the explicit **IFTTT (legacy)** option; native failures never trigger an IFTTT command.

> **Native control is enabled by default, including on upgrade from a read-only version.** Each lock gets a
> `lock.<name>` entity that can physically lock and unlock the door. Home Assistant users, integrations and
> automations with permission to operate that entity can send these commands. For state and activity only,
> choose **Configure â†’ Lock control â†’ Read only** after setup.

---

## What you get

Per lock, a Home Assistant **device** with:

| Entity | Example | Notes |
| --- | --- | --- |
| `sensor.<lock>_status` | `locked` / `unlocked` / `jammed` | enum sensor |
| `sensor.<lock>_battery` | `100 %` | battery % |
| `sensor.<lock>_last_activity` | `keypad_password_lock` | attributes: `who`, `act_type`, `person`, `correlated`, `attribution` |
| `binary_sensor.<lock>_jammed` | `off` | problem class |
| `binary_sensor.<lock>_door` | `closed` | only for locks with a door sensor |
| `lock.<lock>` | `locked` / `unlocked` | native Bosma cloud control by default |

Plus a Home Assistant **event** on every live, correlated change:

```yaml
event_type: bosma_aegis_activity
data:
  device_pid: "0123â€¦"
  name: "Garage Door"
  status: "locked"          # or "unlocked" / "jammed"
  locked: true
  act_name: "keypad_password_lock"
  act_type: 135
  who: "Alex"
  person: true              # True only on a CONFIDENT human action; None if unconfirmed; False if automatic
  correlated: true
  event_id: "0123â€¦:1794187601"
```

Use the event to drive automations that need to know a **person** acted â€” e.g. "turn the porch light off when someone
locks the front door, but ignore the autoâ€‘lock." See the examples in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#using-the-activity-event).

---

## Installation

### 1. Add the repository to the Marketplace

Home Assistant **2026.11+** ships the builtâ€‘in **Marketplace** (Settings â†’ **Marketplace**; it replaced HACS).

1. **Settings â†’ Marketplace**.
2. Topâ€‘right menu (â‹®) â†’ **Custom repositories**.
3. Add `https://github.com/zperkd/bosma-aegis` with category **Integration**.
4. Find **Bosma Aegis** in the list and **Download**.
5. **Restart Home Assistant** (Settings â†’ System â†’ Restart).

> On older setups still using HACS, the same steps work (HACS â†’ â‹® â†’ Custom repositories â†’ Integration).

*Manual install (no Marketplace):* copy `custom_components/bosma_aegis/` into your HA `config/custom_components/`
folder and restart.

### 2. Create a dedicated Bosma account (recommended)

**Bosma allows only one device signed into an account at a time.** If Home Assistant signs into the same account as
your phone (or another integration/bridge), they'll fight over the session. So:

1. In the Bosma app, create a **second Bosma account** for Home Assistant.
2. **Share your lock(s)** from your main account to that new account (Bosma app â†’ the lock â†’ Share).
3. Use that dedicated account in the step below.

(You *can* use your main account, but then the Bosma app on your phone will keep getting logged out.)

### 3. Add the integration

1. **Settings â†’ Devices & services â†’ Add integration â†’ "Bosma Aegis"**.
2. Sign in with the **Bosma account** email + password.
3. **Newâ€‘device verification:** the first time a given account signs in from Home Assistant, Bosma emails a oneâ€‘time
   **verification code** (its "signing in from another device" security). The integration shows a **code entry**
   step â€” enter the code from the email to finish. (Subsequent restarts don't ask again.)
4. Your locks are discovered automatically; their entities appear under a device each.

That's it â€” the lock states update **instantly** when anything changes, and reconcile every 60 s as a safety net.

**Options** (Settings â†’ the integration â†’ Configure):
- **Polling** â€” the reconciliation **poll interval** (30â€“900 s; default 60).
- **Lock control** â€” choose native Bosma cloud (default), Read only, or IFTTT (legacy). Existing entity IDs are preserved.

---

## Door security alerts (v1.0.4)

Open **Configure → Alerts** to enable **Potential break-in** and/or **Door left ajar** alerts. Both are off by default.
Choose persistent Home Assistant notifications, an existing phone/notify service, or both. The door-open delay is
configurable from 10 to 3600 seconds (default 120). A door-ajar notification clears in HA when the door closes.

Break-in warnings use Bosma's impact / potential door-breaking event (activity code 64), not normal unlocking or a
jam. Enable break-in detection in the Bosma app for the lock to produce these events. A reported impact is a
potential attempt, not proof of intrusion. Door-ajar alerts use Bosma's event 68 and the contact-open timer; the
integration confirms a fresh door contact reading before sending a timer alert. Historical alarms are not replayed
on startup; repeated impacts are limited to one notification per lock per minute, and an open door alerts once per
continuous opening. Pushes prompt alarm checks, with the normal reconciliation poll as a fallback.

These options do not enable the lock's siren, change Bosma alarm sensitivity, or lock/unlock a door. They require
cloud connectivity and a lock/gateway that reports the corresponding alarms or door contact. The
`bosma_aegis_alert` event can also drive your own automations (`alert_type`: `break_in` or `door_ajar`).

---

## How it works (short version)

- On setup it logs into the Bosma cloud and **discovers your locks**.
- A background **websocket** subscribes to your locks and receives `DEVICE_SHADOW` pushes the instant anything
  changes (subâ€‘second).
- A periodic **REST poll** reconciles state as a backstop, with **observationâ€‘time ordering** so a slow poll can
  never overwrite a newer push.
- On a live change it reads the **activity log** and attributes *how/who* â€” and marks `person: true` **only** when it
  can *confidently* correlate a human action to that exact transition (a tight time window); otherwise the activity is
  shown as history with `person: null`, so an ambiguous row never triggers a personâ€‘gated automation.
- Entities go **unavailable** if neither the websocket nor the poll has seen a lock for ~11 minutes (a dead cloud
  link surfaces instead of showing stale state).

The full design â€” ordering, attribution confidence, reconnection, availability â€” is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Troubleshooting

- **"No Bosma locks were found"** â€” the account signed in, but discovery returned no lock devices. Make sure the
  account owns/was shared the lock(s). Discovery is the most accountâ€‘specific part; open an issue with debug logs
  (`logger:` â†’ `custom_components.bosma_aegis: debug`) and the deviceâ€‘list shape can be adjusted.
- **Entities "unavailable"** â€” the cloud link is down (check internet / Bosma outage). It recovers automatically.
- **Reâ€‘authentication prompt** â€” your Bosma password changed; reâ€‘enter it when HA asks.

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.bosma_aegis: debug
```

---

## Notes & disclaimer

- **Region: United States / Canada.** The integration talks to Bosma's North American cloud
  (`smarthome.bosmaliving.com`), so it needs a Bosma account registered there. Accounts on other regional Bosma
  servers aren't supported yet. If you're elsewhere and want to help add your region, please open an issue.

- **Unofficial.** Not affiliated with, endorsed by, or supported by Bosma. "Bosma" and "Aegis" are trademarks of their
  owner. This integration uses the same cloud endpoints the Bosma app uses, for your own account and devices.
- **Native cloud control** uses the account's authorized encrypted lock key and a fresh server challenge.
  Keys are decoded in memory and are never logged or stored in entity attributes. HTTP success means accepted;
  `locking`/`unlocking` clears only after a fresh state confirmation or a reported timeout. Commands are serialized
  per lock, and uncertain network failures never cause an automatic command retry or IFTTT fallback.
- Credentials are stored by Home Assistant in its config entry (as with every cloud integration) and are only sent to
  Bosma's servers.

## Upgrading from v1.0.2

Install v1.0.3 and restart Home Assistant. Lock entities appear automatically and existing lock entity IDs remain
unchanged. The integration refreshes gateway metadata during setup; no re-pairing or phone is required.

**Headsâ€‘up:** native control becomes the default, so if you were running readâ€‘only, upgrading gives every lock a
`lock.<name>` entity that can **unlock the door** â€” with no extra optâ€‘in. If that's not what you want, set
**Configure â†’ Lock control â†’ Read only** after the upgrade. To keep using IFTTT, select **IFTTT (legacy)** there.

## License

MIT â€” see [LICENSE](LICENSE).
