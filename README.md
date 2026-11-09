# Bosma Aegis for Home Assistant

A Home Assistant integration that brings your **Bosma Aegis smart lock(s)** into HA with **instant updates** and
**rich activity** — lock/unlock state, battery, door contact, jam, and *how* each change happened (keypad, app,
fingerprint, auto‑lock, by hand) with *who* did it.

It runs **entirely inside Home Assistant** — no separate bridge, service, or add‑on — and talks to the Bosma cloud the
same way the Bosma app does (a persistent websocket for instant pushes, plus a periodic REST reconcile).

> **Read by default; control is optional.** The integration can't send Bosma's *own* lock/unlock command (it's an
> encrypted, gateway‑relayed BLE challenge/response that can't be reproduced from the cloud API). But Bosma exposes
> lock/unlock as an **IFTTT action**, so you can turn on real `lock.*` control by pasting your IFTTT Webhooks key in
> the integration's options — it fires the webhook and **confirms** the result from its own instant state. No YAML.
> See **[docs/CONTROL_VIA_IFTTT.md](docs/CONTROL_VIA_IFTTT.md)**. Leave the key blank to stay fully read‑only.

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
| `lock.<lock>` | `locked` / `unlocked` | **optional** — appears only after you configure IFTTT control (below) |

Plus a Home Assistant **event** on every live, correlated change:

```yaml
event_type: bosma_aegis_activity
data:
  device_pid: "0123…"
  name: "Garage Door"
  status: "locked"          # or "unlocked" / "jammed"
  locked: true
  act_name: "keypad_password_lock"
  act_type: 135
  who: "Alex"
  person: true              # True only on a CONFIDENT human action; None if unconfirmed; False if automatic
  correlated: true
  event_id: "0123…:1794187601"
```

Use the event to drive automations that need to know a **person** acted — e.g. "turn the porch light off when someone
locks the front door, but ignore the auto‑lock." See the examples in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#using-the-activity-event).

---

## Installation

### 1. Add the repository to the Marketplace

Home Assistant **2026.11+** ships the built‑in **Marketplace** (Settings → **Marketplace**; it replaced HACS).

1. **Settings → Marketplace**.
2. Top‑right menu (⋮) → **Custom repositories**.
3. Add `https://github.com/zperkd/bosma-aegis` with category **Integration**.
4. Find **Bosma Aegis** in the list and **Download**.
5. **Restart Home Assistant** (Settings → System → Restart).

> On older setups still using HACS, the same steps work (HACS → ⋮ → Custom repositories → Integration).

*Manual install (no Marketplace):* copy `custom_components/bosma_aegis/` into your HA `config/custom_components/`
folder and restart.

### 2. Create a dedicated Bosma account (recommended)

**Bosma allows only one device signed into an account at a time.** If Home Assistant signs into the same account as
your phone (or another integration/bridge), they'll fight over the session. So:

1. In the Bosma app, create a **second Bosma account** for Home Assistant.
2. **Share your lock(s)** from your main account to that new account (Bosma app → the lock → Share).
3. Use that dedicated account in the step below.

(You *can* use your main account, but then the Bosma app on your phone will keep getting logged out.)

### 3. Add the integration

1. **Settings → Devices & services → Add integration → "Bosma Aegis"**.
2. Sign in with the **Bosma account** email + password.
3. **New‑device verification:** the first time a given account signs in from Home Assistant, Bosma emails a one‑time
   **verification code** (its "signing in from another device" security). The integration shows a **code entry**
   step — enter the code from the email to finish. (Subsequent restarts don't ask again.)
4. Your locks are discovered automatically; their entities appear under a device each.

That's it — the lock states update **instantly** when anything changes, and reconcile every 60 s as a safety net.

**Options** (Settings → the integration → Configure):
- **Polling** — the reconciliation **poll interval** (30–900 s; default 60).
- **Lock control (IFTTT)** — optionally add real `lock.*` control by pasting your IFTTT Webhooks key. See
  [docs/CONTROL_VIA_IFTTT.md](docs/CONTROL_VIA_IFTTT.md).

---

## How it works (short version)

- On setup it logs into the Bosma cloud and **discovers your locks**.
- A background **websocket** subscribes to your locks and receives `DEVICE_SHADOW` pushes the instant anything
  changes (sub‑second).
- A periodic **REST poll** reconciles state as a backstop, with **observation‑time ordering** so a slow poll can
  never overwrite a newer push.
- On a live change it reads the **activity log** and attributes *how/who* — and marks `person: true` **only** when it
  can *confidently* correlate a human action to that exact transition (a tight time window); otherwise the activity is
  shown as history with `person: null`, so an ambiguous row never triggers a person‑gated automation.
- Entities go **unavailable** if neither the websocket nor the poll has seen a lock for ~11 minutes (a dead cloud
  link surfaces instead of showing stale state).

The full design — ordering, attribution confidence, reconnection, availability — is in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

---

## Troubleshooting

- **"No Bosma locks were found"** — the account signed in, but discovery returned no lock devices. Make sure the
  account owns/was shared the lock(s). Discovery is the most account‑specific part; open an issue with debug logs
  (`logger:` → `custom_components.bosma_aegis: debug`) and the device‑list shape can be adjusted.
- **Entities "unavailable"** — the cloud link is down (check internet / Bosma outage). It recovers automatically.
- **Re‑authentication prompt** — your Bosma password changed; re‑enter it when HA asks.

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.bosma_aegis: debug
```

---

## Notes & disclaimer

- **Unofficial.** Not affiliated with, endorsed by, or supported by Bosma. "Bosma" and "Aegis" are trademarks of their
  owner. This integration uses the same cloud endpoints the Bosma app uses, for your own account and devices.
- **Control is via IFTTT, not Bosma's API.** The integration never sends Bosma's own lock command; optional control
  works by firing *your* IFTTT webhook (which runs Bosma's IFTTT action) and confirming the result from the read side.
  See [docs/CONTROL_VIA_IFTTT.md](docs/CONTROL_VIA_IFTTT.md). The Webhooks key is stored with the config entry.
- Credentials are stored by Home Assistant in its config entry (as with every cloud integration) and are only sent to
  Bosma's servers.

## License

MIT — see [LICENSE](LICENSE).
