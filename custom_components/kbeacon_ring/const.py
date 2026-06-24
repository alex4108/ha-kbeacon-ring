"""Constants for the KBeacon Ring integration."""

DOMAIN = "kbeacon_ring"

CONF_MAC = "mac"
CONF_NAME = "name"
CONF_PASSWORD = "password"
CONF_RING_MS = "ring_ms"
CONF_RING_TYPE = "ring_type"
CONF_LED_ON = "led_on"
CONF_LED_OFF = "led_off"

DEFAULT_PASSWORD = "0000000000000000"  # Blue Charm / KBeacon factory default
DEFAULT_RING_MS = 5000
DEFAULT_RING_TYPE = 2  # 0=LED, 1=beep, 2=LED+beep
DEFAULT_LED_ON = 200
DEFAULT_LED_OFF = 1800

# KBeacon GATT UUIDs (config service)
SVC_UUID = "0000fea0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fea1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fea2-0000-1000-8000-00805f9b34fb"
# FEA3 = INDICATE characteristic carrying live trigger/button events.
IND_UUID = "0000fea3-0000-1000-8000-00805f9b34fb"
