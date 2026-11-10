"""Constants for the Bosma Aegis integration."""

from __future__ import annotations

DOMAIN = "bosma_aegis"
PLATFORMS = ["sensor", "binary_sensor", "lock"]

# Config / option keys
CONF_ACCOUNT = "account"
CONF_PASSWORD = "password"
CONF_IMEI = "imei"
CONF_SCAN_INTERVAL = "scan_interval"
# Optional lock control via IFTTT (opt-in, configured in the options flow):
CONF_CONTROL_MODE = "control_mode"
CONTROL_NATIVE = "native"
CONTROL_IFTTT = "ifttt"
CONTROL_READ_ONLY = "read_only"
CONF_IFTTT_KEY = "ifttt_key"          # the user's IFTTT Webhooks key (enables control when set)
CONF_CONTROL_EVENTS = "control_events"  # resolved {devicePid: {"lock": <event>, "unlock": <event>}}

# Opt-in security notifications (no change to lock settings or siren).
CONF_ALERT_BREAK_IN = "alert_break_in"
CONF_ALERT_DOOR_AJAR = "alert_door_ajar"
CONF_ALERT_AJAR_DELAY = "alert_ajar_delay"
CONF_ALERT_PERSISTENT = "alert_persistent"
CONF_ALERT_NOTIFY_SERVICE = "alert_notify_service"
DEFAULT_AJAR_DELAY = 120
ALERT_COOLDOWN = 60
EVENT_ALERT = "bosma_aegis_alert"

DEFAULT_SCAN_INTERVAL = 60  # seconds between REST reconciliation polls (the websocket gives instant pushes)

# IFTTT Maker webhook. Firing <event> runs the user's applet, whose "Then" is Bosma's own Lock/Unlock action.
IFTTT_WEBHOOK_URL = "https://maker.ifttt.com/trigger/{event}/with/key/{key}"
CONTROL_CONFIRM_TIMEOUT = 90  # seconds to wait for the integration to confirm the deadbolt actually moved


def ifttt_url(key: str, event: str) -> str:
    """Build the IFTTT Maker webhook URL for an event (pure helper, unit-tested)."""
    return IFTTT_WEBHOOK_URL.format(event=event, key=key)

# Bosma cloud (the same endpoints the mobile app uses)
GLOBAL_BASE = "https://smarthome.bosmaliving.com/bosma-smart-global"
FRONTEND_BASE = "https://smarthome.bosmaliving.com/bosma-smart-frontend"
WS_URL = "wss://smarthome.bosmaliving.com/bosma-smart-websocket/ws?token="
APP_HEADERS = {
    "lang": "en_US",
    "appversion": "2.5.2 build005",
    "apppackage": "com.bosma.smarthome",
    "corp": "Bosma",
    "platform": "2",
    "appid": "smart",
    "platformversion": "17",
    "user-agent": "okhttp/4.9.2",
}

# Event fired on a live, correlated activity. `person` is True ONLY on a confident human lock/unlock (see
# CORRELATION_WINDOW); None when attribution can't be positively correlated; False for automatic/alarm activity.
EVENT_ACTIVITY = "bosma_aegis_activity"

# actType -> human-readable "how it happened" (from the app's WsService constants)
ACTTYPE = {
    0: "local", 1: "app", 2: "fingerprint", 3: "guest", 4: "guest_temp", 5: "voice", 6: "scene",
    7: "keypad_password", 8: "keypad_temp_password", 16: "auto_unlock", 64: "hit", 65: "block",
    66: "axis", 67: "flash", 68: "door_ajar", 73: "left_unlock", 100: "door_open", 101: "door_close",
    128: "local_lock", 129: "app_lock", 131: "guest_lock", 132: "tmp_guest_lock", 133: "voice_lock",
    134: "scene_lock", 135: "keypad_password_lock", 136: "keypad_temp_password_lock",
    137: "onetouch_lock", 144: "auto_lock", 254: "hand_close", 255: "manual",
}
# actTypes that are NOT a human operating the lock (automatic re-lock/unlock + pure alarm/door events)
NON_PERSON = {16, 144, 64, 65, 66, 67, 68, 100, 101}
# Direction of an actType (used to confirm a log row belongs to a lock vs an unlock transition)
LOCK_ACTS = {128, 129, 131, 132, 133, 134, 135, 136, 137, 144, 254, 255}
UNLOCK_ACTS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 16, 73}

# Attribution windows (seconds)
CLOCK_SKEW = 5           # tolerated future skew when ordering observations
# An activity-log row is attributed to the just-taken action when it is UNCONSUMED (newer than the last used for
# this lock) and within RECENT_WINDOW of now. Bosma's log clock can run tens of seconds off ours, so this absolute
# window (tolerating skew both ways) is used instead of comparing the row timestamp to the push time.
RECENT_WINDOW = 120

# Availability: a device is "available" while its last valid observation is within this many seconds.
FRESH_AVAILABILITY = 660  # 11 minutes (>= 2 missed 5-min reconciliations / a dead websocket)
