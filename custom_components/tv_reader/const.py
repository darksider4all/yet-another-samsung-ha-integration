"""Constants for the Samsung TV Reader integration."""

DOMAIN = "tv_reader"

CONF_HOST = "host"
CONF_NAME = "name"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_POWER_DELEGATE = "power_delegate_entity"

DEFAULT_NAME = "Living Room TV"
DEFAULT_SCAN_INTERVAL = 2

# Control actions forwarded verbatim to the reader's POST /v1/control.
CONTROL_ACTIONS = ("launch_app", "close_app", "send_key")