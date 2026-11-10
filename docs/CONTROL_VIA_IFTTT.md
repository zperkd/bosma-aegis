> **Legacy option (v1.0.3):** Native Bosma cloud control is now the default and does not require IFTTT.
> To use the setup below, explicitly select **IFTTT (legacy)** in Configure → Lock control.

# Controlling your Bosma locks from Home Assistant (via IFTTT)

The [Bosma Aegis integration](../README.md) supports native Bosma cloud control from v1.0.3 onward.
This guide documents the optional legacy IFTTT path for users who want to keep existing applets.
It fires an IFTTT webhook and confirms the result from the integration's observed state.

There are two ways to wire this up:

- **[Built‑in control (recommended)](#built-in-control-recommended)** — enter your IFTTT Webhooks key in the
  integration's options and you get real `lock.*` entities. No YAML.
- **[Manual template lock (advanced)](#manual-template-lock-advanced)** — build the lock yourself in YAML. Useful if
  you want to customise the confirmation/notification behaviour.

Either way you end up with a normal `lock.front_door` / `lock.garage_door` you can use anywhere (Lovelace lock card,
voice, automations), backed by the integration's trustworthy state.

> **Latency & reliability.** IFTTT webhooks typically act in a few seconds, occasionally longer, and can rarely drop.
> That's why control **waits for the integration to confirm** the new state — never assume "webhook sent" means the
> deadbolt moved. The built‑in lock shows a *locking…/unlocking…* state until the integration confirms, then logs a
> warning if it never does.

---

## Prerequisites

- The **Bosma Aegis integration** installed and working (you see `sensor.<lock>_status` etc.).
- An **IFTTT** account with the **Bosma** service connected (the same Bosma account), and the **Webhooks** service
  enabled.

## Step 1 — Create the IFTTT applets

Create **one applet per action** (so two per lock: lock and unlock). For each:

1. **If This** → **Webhooks** → *Receive a web request*. **Event Name**: use the integration's default convention
   so the options form is pre‑filled for you — it's the lock's name slugified plus `_lock` / `_unlock`:
   - *Front Door* → `front_door_lock`, `front_door_unlock`
   - *Garage Door* → `garage_door_lock`, `garage_door_unlock`

   (Any names work — you can edit them in the options — but matching this convention means you won't have to.)
2. **Then That** → **Bosma** → choose **Lock** (or **Unlock**) and select the matching lock.
3. Create the applet. Repeat for every lock × {lock, unlock}.

## Step 2 — Get your Webhooks key

Go to **[ifttt.com/maker_webhooks](https://ifttt.com/maker_webhooks)** → **Documentation**. Copy your key (the URL is
`https://maker.ifttt.com/trigger/{event}/with/key/{YOUR_KEY}`). Treat the key like a password.

---

## Built‑in control (recommended)

1. In Home Assistant go to **Settings → Devices & services → Bosma Aegis → Configure**.
2. Choose **Lock control (IFTTT)**.
3. Paste your **Webhooks key**. The **lock/unlock event names** for each lock are pre‑filled with the convention from
   Step 1 — leave them if you followed it, or edit to match your applet names.
4. **Submit.** The integration reloads and a `lock.<name>` entity appears on each lock's device.

That's it. The new lock entities:

- show state from the read‑only side (instant, trustworthy),
- control by firing your IFTTT webhook,
- display *locking…/unlocking…* until the integration confirms the deadbolt actually moved (up to 90 s), and
- are available exactly when the lock's `sensor.<lock>_status` is.

To **turn control back off**, open the same step and clear the Webhooks key — the `lock.*` entities disappear and the
legacy lock entities are omitted. Select Read only in the control-method selector to disable all control.

> **Security.** The Webhooks key grants control of your locks. It's stored with the config entry (like any other
> integration credential) and shown masked. Don't share screenshots of the options form with the key visible.

---

## Manual template lock (advanced)

Prefer to own the YAML (custom confirmation window, notifications, etc.)? Skip the built‑in step above and build the
lock yourself. Add your key to `secrets.yaml`:

```yaml
# secrets.yaml
ifttt_webhook_key: YOUR_IFTTT_WEBHOOKS_KEY
```

A generic webhook command (in `configuration.yaml` or a package):

```yaml
rest_command:
  ifttt_webhook:
    url: "https://maker.ifttt.com/trigger/{{ event }}/with/key/{{ key }}"
    method: POST
```

A template `lock.` with confirmation (adjust names, the `sensor.*_status` ids, and the event names to match yours):

```yaml
template:
  - lock:
      - name: Front Door
        unique_id: bosma_front_door_lock_manual
        state: "{{ states('sensor.front_door_status') }}"
        availability: "{{ has_value('sensor.front_door_status') }}"
        lock:
          - action: rest_command.ifttt_webhook
            data: { event: front_door_lock, key: !secret ifttt_webhook_key }
          - wait_template: "{{ is_state('sensor.front_door_status', 'locked') }}"
            timeout: "00:01:30"
            continue_on_timeout: true
          - if: "{{ not is_state('sensor.front_door_status', 'locked') }}"
            then:
              - action: persistent_notification.create
                data:
                  title: Front door lock not confirmed
                  message: >-
                    Sent the lock command but the lock still reports
                    {{ states('sensor.front_door_status') }} after 90s.
        unlock:
          - action: rest_command.ifttt_webhook
            data: { event: front_door_unlock, key: !secret ifttt_webhook_key }
          - wait_template: "{{ is_state('sensor.front_door_status', 'unlocked') }}"
            timeout: "00:01:30"
            continue_on_timeout: true
```

> Use a different `unique_id` than the built‑in lock if you ever have both configured, so they don't collide.

---

## Using it

Add the `lock.` entities to a dashboard, expose them to voice assistants, or drive them from automations. Because the
integration confirms state, your automations can trust `lock.front_door` and react to real lock/unlock events — or use
the richer `bosma_aegis_activity` event for *who/how* (see [ARCHITECTURE.md §8](ARCHITECTURE.md#8-using-the-activity-event)).

## Notes

- **Jam/failure:** if the lock jams, `sensor.<lock>_status` reports `jammed` and the confirmation never arrives → the
  built‑in lock logs a warning (the manual version raises the notification). Handle `jammed` explicitly in automations.
- **Alternatives to IFTTT:** any method that ends in Bosma's own lock/unlock (an Alexa routine, a Google Home routine,
  SmartThings, etc.) works the same way — the only Bosma‑specific part is the webhook URL. The built‑in control is
  IFTTT‑specific; for other services use the manual template lock and swap the `rest_command`.
