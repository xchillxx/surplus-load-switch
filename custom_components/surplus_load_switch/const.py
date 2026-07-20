from datetime import timedelta

DOMAIN = "surplus_load_switch"
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
CONF_DEVICE_IS_CLIMATE = "is_climate"
CONF_DEVICE_CLIMATE_ENTITY = "climate_entity"
CONF_DEVICE_CLIMATE_ON_MODE = "climate_on_mode"  # hvac_mode to set when "turning on" (e.g. "heat")
CONF_DEVICE_POWER_SENSOR = "power_sensor"
CONF_DEVICE_OFF_ONLY = "off_only"  # legacy (v1.1.0) — superseded by window fields below
CONF_DEVICE_WINDOW_START = "window_start"
CONF_DEVICE_WINDOW_END = "window_end"
CONF_DEVICE_SCHEDULE_ENTITY = "schedule_entity"  # schedule.* helper — takes priority over window_start/end
CONF_DEVICE_MIN_DAILY_RUNTIME_H = "min_daily_runtime_h"
CONF_DEVICE_DEPENDS_ON = "depends_on_device_id"  # another device's _id that must be ON first

# Logic thresholds
SURPLUS_ON_THRESHOLD = 0.2    # kW: turn on when surplus > this
SURPLUS_OFF_THRESHOLD = -0.2  # kW: turn off when surplus < this
BATT_OK_BUFFER_H = 0.5        # h: extra buffer over h_to_solar

# Stability: how many coordinator cycles (30s each) must condition hold
STABLE_ON_CYCLES = 4   # 4 × 30s = 2 min before turning ON
STABLE_OFF_CYCLES = 6   # 6 × 30s = 3 min — used when there's no battery margin to spare
STABLE_OFF_CYCLES_MAX = 24  # 24 × 30s = 12 min — used when margin is comfortable

# Priority staggering: when several devices cross their off-threshold in the
# same cycle (e.g. solar drops off a cliff at sunset), they'd otherwise all
# finish their off-hold at the same cycle count and switch off together.
# Each priority rank below the highest gets this many fewer cycles to wait,
# down to OFF_CYCLES_FLOOR — so the lowest-priority device always sheds
# first, even when the underlying trigger fires for everyone at once.
STAGGER_CYCLES_PER_PRIORITY_STEP = 2  # 2 × 30s = 1 min less patience per rank
OFF_CYCLES_FLOOR = 2  # 2 × 30s = 1 min minimum, however low the priority

# "Margin" = h_battery - h_to_solar, i.e. how many hours of battery buffer
# exist beyond what's strictly needed until solar resumes. When margin is
# large, a short deficit is more likely a transient spike (oven, kettle) than
# a real trend, so we can afford to wait longer before reacting. When margin
# is at or below zero, the battery genuinely can't spare it — react fast.
MARGIN_FOR_MAX_PATIENCE_H = 4.0

# h_battery = avail_kwh / discharge_rate amplifies small discharge-rate noise
# into large hour swings (division). A short mean is still dominated by brief
# spikes (a stove running for 10-15 min looks like "this rate for the rest of
# the night" otherwise). Using the MEDIAN over a longer window ignores a spike
# entirely as long as it's under half the window, while still tracking a real,
# sustained change within roughly half the window's length.
DISCHARGE_SMOOTHING_SAMPLES = 40  # 40 × 30s = 20 min rolling median

UPDATE_INTERVAL_SECONDS = 30

# Default monthly solar offsets (hours after sunrise until PV is useful)
DEFAULT_SOLAR_OFFSETS = [3.5, 3.0, 2.5, 2.0, 2.0, 2.2, 2.2, 2.0, 2.5, 3.0, 3.5, 4.0]

# --- Power measurement (rolling average while device is ON) ---
STORAGE_VERSION = 1
# The window is defined by *active* (ON) runtime, not wall-clock days — a
# calendar-day cutoff would empty out during any idle stretch longer than
# the window (e.g. several rainy days with a weather-dependent device like
# a pool heat pump never running), discarding perfectly good historical
# data right before it's needed again once the device runs. 24h of active
# runtime instead adapts to however many calendar days that actually takes
# — samples simply aren't touched while the device is off, since they're
# only appended (and the oldest evicted) while it's on.
POWER_HISTORY_ACTIVE_HOURS = 24
MAX_SAMPLES_PER_DEVICE = int(POWER_HISTORY_ACTIVE_HOURS * 3600 / UPDATE_INTERVAL_SECONDS)  # 2880
# Minimum samples before trusting the measured average over the configured estimate
# (20 samples × 30s = 10 minutes of runtime)
MIN_SAMPLES_FOR_MEASURED_AVG = 20
# Delay (seconds) before persisting new samples to disk (debounced writes)
POWER_STORE_SAVE_DELAY = 60

# --- Minimum daily runtime (catch-up forcing) ---
RUNTIME_STORE_SAVE_DELAY = 60
# Forcing a device on to hit its minimum daily runtime only ever kicks in
# from this local hour onward — never in the morning, so a good-surplus day
# still gets first chance to reach the target for free before we consider
# spending grid power on it.
MIN_RUNTIME_FORCE_AFTER_HOUR = 12

# --- Self-calibrating solar-start offset ---
# Learns DEFAULT_SOLAR_OFFSETS from the system's own historical solar
# production instead of relying on the guessed defaults above, per calendar
# month, once enough good-quality (non-cloudy) days exist for that month.
CALIBRATION_INTERVAL_HOURS = 24  # how often to re-derive offsets from statistics
# If a calibration attempt got no statistics back at all (e.g. the recorder
# hadn't fully finished loading yet right after a restart), retry this soon
# instead of waiting the full normal cadence above.
CALIBRATION_RETRY_INTERVAL = timedelta(hours=1)
CALIBRATION_LOOKBACK_DAYS = 400  # a bit over a year, so multi-year data accumulates
# A day only counts toward calibration if its peak production reaches this
# fraction of the 90th-percentile peak in the surrounding window — filters
# out cloudy/overcast days using only the system's own data, no external
# weather source needed.
CALIBRATION_CLOUD_WINDOW_DAYS = 10
# 0.80 was too strict in practice: an inverter/feed-in power cap means a
# day's visible peak depends partly on how much was being self-consumed at
# that moment, not purely on weather — a handful of high-consumption days
# pushing past the cap inflate the local reference and make equally clear,
# merely-capped days look artificially worse by comparison. 0.70 keeps
# excluding genuinely cloudy days while tolerating that cap-driven noise.
CALIBRATION_CLOUD_GOOD_RATIO = 0.70
# Within a good day, "solar start" is the first hour whose mean production
# reaches this fraction of that day's own peak — relative to the day's own
# peak (not a fixed kW value) so it works the same on any system size.
CALIBRATION_THRESHOLD_RATIO = 0.15
# Minimum good days required before a month's calibrated value is trusted
# over the configured/default estimate.
CALIBRATION_MIN_GOOD_DAYS = 5
# A month without its own calibration may borrow from a calibrated
# neighbour up to this many months away (circularly) — solar offset moves
# gradually across the year, so a nearby measured month is a better guess
# than the static default, but a gap wider than this isn't trusted since
# the seasonal relationship isn't necessarily linear over that distance.
CALIBRATION_MAX_INTERP_MONTHS = 2
