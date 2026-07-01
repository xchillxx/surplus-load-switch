DOMAIN = "pv_surplus_manager"
PLATFORMS = ["sensor", "number", "switch"]

# Config keys — global
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_LOAD_SENSOR = "load_sensor"
CONF_SOC_SENSOR = "soc_sensor"
CONF_BATT_SENSOR = "batt_sensor"
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_MIN_SOC = "min_soc"
CONF_SOLAR_OFFSETS = "solar_offsets"

# Config keys — per device
CONF_DEVICES = "devices"
CONF_DEVICE_NAME = "name"
CONF_DEVICE_SWITCH = "switch_entity"
CONF_DEVICE_POWER_KW = "avg_power_kw"
CONF_DEVICE_PRIORITY = "priority"
CONF_DEVICE_IS_WALLBOX = "is_wallbox"
CONF_DEVICE_POWER_SENSOR = "power_sensor"

# Logic thresholds
SURPLUS_ON_THRESHOLD = 0.2    # kW: turn on when surplus > this
SURPLUS_OFF_THRESHOLD = -0.2  # kW: turn off when surplus < this
BATT_OK_BUFFER_H = 0.5        # h: extra buffer over h_to_solar

# Stability: how many coordinator cycles (30s each) must condition hold
STABLE_ON_CYCLES = 4   # 4 × 30s = 2 min before turning ON
STABLE_OFF_CYCLES = 6  # 6 × 30s = 3 min before turning OFF

UPDATE_INTERVAL_SECONDS = 30

# Default monthly solar offsets (hours after sunrise until PV is useful)
DEFAULT_SOLAR_OFFSETS = [3.5, 3.0, 2.5, 2.0, 2.0, 2.2, 2.2, 2.0, 2.5, 3.0, 3.5, 4.0]

# --- Power measurement (rolling average while device is ON) ---
STORAGE_VERSION = 1
POWER_HISTORY_WINDOW_DAYS = 7
# Samples are only taken while a device is ON, once per update cycle (30s).
# 7 days of continuous ON time would be 20160 samples; cap generously.
MAX_SAMPLES_PER_DEVICE = 21000
# Minimum samples before trusting the measured average over the configured estimate
# (20 samples × 30s = 10 minutes of runtime)
MIN_SAMPLES_FOR_MEASURED_AVG = 20
# Delay (seconds) before persisting new samples to disk (debounced writes)
POWER_STORE_SAVE_DELAY = 60
