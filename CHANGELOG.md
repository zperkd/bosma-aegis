# Changelog

## 1.0.4 - 2026-11-09

### Door security alerts, configured in the UI

v1.0.4 adds optional security notifications while preserving the native cloud control introduced in v1.0.3.

#### What is new

- **Potential break-in alerts:** notifications for Bosma's reported impact / potential door-breaking events.
- **Door left ajar alerts:** notifications from the lock's own ajar alarm and a Home Assistant open-door timer.
- **Independent ON/OFF switches:** configure each alert type under **Bosma Aegis > Configure > Alerts**.
- **Your choice of delivery:** Home Assistant notifications, an existing phone/notify service, or both.
- **Adjustable timer:** 10 to 3600 seconds, with a default of 120 seconds.

#### How to enable alerts

1. Update to v1.0.4 and restart Home Assistant.
2. Open **Settings > Devices & services > Bosma Aegis > Configure > Alerts**.
3. Turn on the alert types you want.
4. Choose Home Assistant notifications, a phone/notify service, or both.
5. Set the open-door timer and save.

**Both alert types start OFF.** No notifications are enabled automatically by the update.

For break-in warnings, also enable break-in detection in the Bosma app. A reported impact is a potential attempt, not proof of an intrusion. Lock jams and normal unlocking are not treated as break-ins.

The adjustable delay applies to HA's open-door timer. The lock's own door-ajar alarms are delivered when Bosma reports them. Before sending a timer alert, the integration confirms a fresh door contact reading from the cloud.

#### Notification behavior

- Existing alarm history is not replayed at startup.
- Repeated impact notifications are limited to one per lock per minute.
- A continuously open door alerts once until it closes.
- Closing the door clears its Home Assistant door-ajar notification.
- Phone delivery failures do not prevent the HA notification when both destinations are enabled.
- Timers and pending work are cleaned up when the integration unloads or setup fails.

These options do not enable the siren, change Bosma alarm sensitivity, or lock/unlock a door. Existing lock-control settings and entity IDs are preserved.

#### Validation

- **68 automated tests passed:** 16 existing logic tests, 21 native-control regressions and 31 real Home Assistant tests.
- **Official HACS validation and hassfest passed**, with no ignored HACS checks.
- The Alerts screen was checked in a live Home Assistant instance, including the separate ON/OFF controls and notification selector.

[View the release validation](https://github.com/zperkd/bosma-aegis/actions/runs/42756585486).

#### Upgrade and rollback

Install the update through Marketplace or HACS and restart Home Assistant. Then enable the desired alerts and choose their delivery settings.

v1.0.3 and earlier releases remain available for rollback. If upgrading from a read-only version, native lock control is enabled by default; choose **Configure > Lock control > Read only** if you only want monitoring.