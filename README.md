# Bosma Aegis for Home Assistant

Control your Bosma Aegis locks from Home Assistant, with live lock status, battery, door contact and activity details.

Native cloud control runs inside Home Assistant. No IFTTT subscription, phone, Bluetooth adapter or separate bridge is required. Remote control requires a Bosma gateway and internet access.

> **Lock control is ON by default**, including when upgrading from a read-only version. Users and automations with permission to operate the lock entity can lock and unlock the door. For monitoring only, choose **Configure > Lock control > Read only** after setup.

## New in v1.0.4

Optional door security alerts, configured entirely in the UI:

- Separate ON/OFF switches for **Potential break-in** and **Door left ajar**.
- Home Assistant notifications, phone notifications, or both.
- An adjustable open-door timer, with fresh contact confirmation before an alert.
- Duplicate suppression and automatic clearing of HA door-ajar notifications when the door closes.

**Alerts start OFF.** Enable the types and destinations you want under **Configure > Alerts**.

[Read the v1.0.4 update notes](CHANGELOG.md) or [view the release](https://github.com/zperkd/bosma-aegis/releases/tag/v1.0.4).

## Features

- **Lock and unlock** through Bosma's native cloud API.
- **Live status** with periodic cloud reconciliation.
- **Battery, door contact and jam status**, when reported by the lock.
- **Activity details:** how the lock was operated and who operated it, when available.
- **Optional security alerts** for reported impacts and doors left ajar.
- **Automation events** for activity and alerts.

## Before you install

- Home Assistant **2024.4 or newer**. Tested with **2026.11.1**.
- A Bosma Aegis lock and gateway for remote operation.
- An account on Bosma's **North American cloud** (US/Canada). Other regional servers are not supported; Canada has not been independently tested.
- A dedicated Bosma account is recommended because signing in from another device can replace an existing session.

## Installation

### 1. Prepare a dedicated Bosma account

1. Create a second account in the Bosma app for Home Assistant.
2. From your main account, share the locks with that account.
3. Keep your phone signed into your main account.

Using a separate account avoids Home Assistant and the phone app competing for the same session.

### 2. Install the integration

**Home Assistant 2026.11 and newer:** open **Settings > Marketplace**, search for **Bosma Aegis**, and follow the installation prompts.

**Older Home Assistant versions:** open **HACS**, search for **Bosma Aegis**, and download the integration. Restart Home Assistant when prompted.

If it has not appeared in your catalog yet, add this repository as a custom integration repository:

[github.com/zperkd/bosma-aegis](https://github.com/zperkd/bosma-aegis)

For a manual installation, copy the `custom_components/bosma_aegis` folder into Home Assistant's `config/custom_components` folder, then restart.

### 3. Sign in

Marketplace may open setup automatically. Otherwise, open **Settings > Devices & services > Add integration** and choose **Bosma Aegis**.

1. Sign in with the dedicated account.
2. If Bosma emails a new-device verification code, enter it when prompted.
3. Your locks and their entities are discovered automatically.

## Configuration

Open **Settings > Devices & services > Bosma Aegis > Configure**.

- **Polling:** set the reconciliation interval from 30 to 900 seconds. Default: 60 seconds.
- **Lock control:** choose native Bosma cloud control, Read only, or IFTTT (legacy).
- **Alerts:** enable each alert type and choose how it is delivered.

Native control never falls back to IFTTT automatically. Existing IFTTT setups remain available through the explicit legacy option; see the [IFTTT guide](docs/CONTROL_VIA_IFTTT.md).

## Door security alerts

### Potential break-in

Turn on **Potential break-in alerts** to receive Bosma's impact / potential door-breaking warnings.

**Break-in detection must also be enabled in the Bosma app.** An impact warning indicates a potential attempt, not proof of an intrusion. Normal unlocking and lock jams do not trigger this alert.

### Door left ajar

Turn on **Door left ajar alerts** for device-reported ajar alarms and the Home Assistant open-door timer.

Set **Door-open delay** from **10 to 3600 seconds**. Default: **120 seconds**. This delay applies to the Home Assistant timer; Bosma's own ajar alarms are delivered when reported.

Before sending a timer alert, the integration requests a fresh door contact reading. It alerts once per continuous opening and clears the Home Assistant door-ajar notification when the door closes.

### Notification delivery

Choose either destination, or both:

- **Home Assistant notifications:** enable the toggle for notifications in the HA frontend.
- **Phone / notify service:** select an existing notification service, such as your phone's Companion App service.

To use phone notifications only, select a phone service and turn off Home Assistant notifications. At least one destination is required when alerts are enabled.

Old alarm history is not replayed at startup. Repeated impact notifications are limited to one per lock per minute. These options do not change the lock's siren or sensitivity and do not operate the lock.

## Entities and automations

Each discovered lock has entities for:

- Lock control and state.
- Battery level.
- Last activity, including who/how details when available.
- Jam status.
- Door contact, if the device reports one.

For automations, the integration provides:

- `bosma_aegis_activity` for a correlated lock-state change.
- `bosma_aegis_alert` for an enabled security alert.

Activity attribution is a best-effort interpretation of Bosma's event log. Use the `person` and `correlated` fields when an automation must distinguish a person from auto-locking.

[See the architecture and automation examples](docs/ARCHITECTURE.md#8-using-the-activity-event).

## Updating

Install the latest release through Marketplace or HACS, then restart Home Assistant.

**Upgrading from v1.0.3:** native control and existing entity IDs are preserved. The new alerts remain OFF until you enable them.

**Upgrading from v1.0.2 or a read-only setup:** native control is enabled by default. Choose **Configure > Lock control > Read only** if you want monitoring only, or **IFTTT (legacy)** to retain that control method.

[Previous releases remain available for rollback](https://github.com/zperkd/bosma-aegis/releases).

## Troubleshooting

**No locks found:** check that the dedicated account owns the locks or has been granted access to them.

**Entities unavailable:** check internet access, gateway connectivity and the Bosma cloud. The integration requires cloud access and has no local-only control path.

**Sign-in or verification problems:** check the account credentials and emailed code. Use a dedicated account so the phone app does not replace HA's session.

**No break-in alerts:** enable that alert in the integration and break-in detection in the Bosma app. Confirm that a notification destination is selected.

**No phone notifications:** select an available notification service in **Configure > Alerts** and check that it works in Home Assistant.

<details>
<summary>Enable debug logging</summary>

Add this to your Home Assistant configuration and reload the logging settings:

```yaml
logger:
  logs:
    custom_components.bosma_aegis: debug
```

Review logs for personal information before sharing them in an issue. The integration redacts request error details that could expose tokens or webhook keys.

</details>

## Privacy and project status

Credentials are stored in Home Assistant's config entry and sent to Bosma's cloud for authentication. Account and lock keys used for native control are handled in memory and are not logged or exposed as entity attributes.

This is an unofficial integration, not affiliated with or endorsed by Bosma. "Bosma" and "Aegis" belong to their respective owners.

## License

[MIT](LICENSE).