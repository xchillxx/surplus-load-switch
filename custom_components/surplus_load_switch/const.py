from datetime import timedelta

DOMAIN = "surplus_load_switch"
PLATFORMS = ["sensor", "number", "switch", "select", "binary_sensor"]

# Defined early: every "N coordinator cycles" constant below is derived from
# this via minutes_to_cycles(), so changing this doesn't silently change any
# of the wall-clock durations they were tuned to (a fixed cycle count alone
# would double every hold/smoothing time if this doubled, without anyone
# intending that).
UPDATE_INTERVAL_SECONDS = 60


def _minutes_to_cycles(minutes: float) -> int:
    return max(int(round(minutes * 60 / UPDATE_INTERVAL_SECONDS)), 1)

CLIMATE_HVAC_MODE_OPTIONS = ["heat", "cool", "auto", "heat_cool", "dry", "fan_only"]
# Sentinel shown in select entities for an optional field with no value
# selected — never itself written to config; async_select_option maps it
# back to None/removing the key.
SELECT_NONE = "— keine/keiner —"

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
# Optional fallback for a device with neither a schedule nor a window: the
# overnight battery projection otherwise has to assume it might run all the
# way to solar start (no known stopping point), which can make even a high-
# priority device fail the battery check on a device that's actually only
# expected to run a few more hours. A plain on/off flag rather than a
# configurable number — asking "does this device stop overnight?" is a much
# more natural question than picking a specific number of hours, and one
# sensible default (DEFAULT_MAX_ASSUMED_RUNTIME_H below) covers it. When
# set, this caps the assumed worst case at a rolling "at most N hours from
# right now" instead of a fixed clock time — re-derived every cycle, so it
# keeps sliding forward while conditions stay good, rather than a one-shot
# commitment. See coordinator._effective_cutoff.
CONF_DEVICE_STOPS_OVERNIGHT = "stops_overnight"
DEFAULT_MAX_ASSUMED_RUNTIME_H = 2.0
CONF_DEVICE_MIN_DAILY_RUNTIME_H = "min_daily_runtime_h"
CONF_DEVICE_DEPENDS_ON = "depends_on_device_id"  # another device's _id that must be ON first
# Wallbox-only, optional: a wallbox is never itself switched by the
# cascade (it runs its own PV-surplus charging logic), so another device
# depending on one uses this instead of "is it on" — see
# PVSurplusCoordinator._wallbox_satisfied. Once its own power draw reaches
# this, a dependent device may run too (the car's getting plenty, no need
# to keep holding back for it). Entirely optional; the "idle" side of
# _wallbox_satisfied (car not charging at all, sustained) needs no config
# beyond the wallbox's own required power_sensor.
CONF_WALLBOX_SATISFIED_KW = "wallbox_satisfied_kw"
# A wallbox is never itself ranked/switched by the cascade, but on a
# detected weak day (see WEAK_DAY_* below) it still needs an effective
# priority to compare other devices against: any candidate device whose
# own priority is this number or worse (higher) gets held off entirely
# until the battery's nearly full — the wallbox takes over that priority
# slot for the day, the car gets first claim on a scarce day and
# everything from there on down waits. 0/unset disables the feature for
# this wallbox.
CONF_WALLBOX_WEAK_DAY_PRIORITY = "wallbox_weak_day_priority"
# Per-device "enabled" toggle — exposed as a live switch entity (switch.py),
# not a config-flow field, since it's meant for a quick vacation-style
# on/off rather than something you configure once at setup. Absent/True
# means enabled; only ever written as False by that switch entity.
CONF_DEVICE_ENABLED = "enabled"

# Logic thresholds
SURPLUS_ON_THRESHOLD = 0.2    # kW: turn on when surplus > this
SURPLUS_OFF_THRESHOLD = -0.2  # kW: turn off when surplus < this
BATT_OK_BUFFER_H = 0.5        # h: extra buffer over h_to_solar
# Below this, a wallbox counts as "not really charging" for
# _wallbox_satisfied's idle-release check — low enough that a genuinely
# charging car is never mistaken for an idle one.
WALLBOX_IDLE_THRESHOLD_KW = 0.3

# --- Weak-day detection ---
# Solar power (kW) above which today counts as "producing" for the purpose
# of capturing the battery's baseline SOC at solar start — an absolute
# value, not a fraction of today's own peak (unlike CALIBRATION_THRESHOLD_
# RATIO above), since live tracking can't know today's peak in advance the
# way the historical offset calibration can.
SOLAR_START_MIN_KW = 0.3
# Today counts as "weak" once the battery's SOC gain since solar start
# drops below this fraction of the calibrated reference gain for this
# calendar month (see SolarOffsetCalibrator.reference_soc_gain) —
# comparing against a learned normal for the time of year, not a fixed kW
# or %-SOC value. SOC gain (not raw solar power) is used because charging
# naturally integrates production over time, so a brief sun break through
# passing clouds barely moves it, and because it already reflects
# whatever the house consumed along the way — both of which a simple
# instantaneous solar-power peak or reading would misjudge.
WEAK_DAY_RATIO_THRESHOLD = 0.6
# Don't judge a day "weak" before this local hour — the morning peak may
# simply not have happened yet, which would otherwise look identical to a
# genuinely overcast day for the first few hours of every single morning.
WEAK_DAY_EARLIEST_CHECK_HOUR = 11
# Battery SOC at/above which a weak day's extra caution no longer applies
# — once the battery's essentially full, there's no reason to keep low-
# priority devices held back purely because production is weak; the
# battery doesn't need the surplus either way.
WEAK_DAY_BATTERY_FULL_SOC = 95.0

# h_to_solar ("hours until solar_start") is the time until the *next*
# calibrated morning threshold — once today's has already passed, that's
# tomorrow's, roughly a full day away, even at high noon with a cloud
# passing overhead. Using that as the battery-projection horizon made a
# brief daytime dip look like "no more sun for 23 hours", badly
# overweighting a small, temporary deficit and forcing devices off (and
# with a short patience window too, since _required_off_cycles' margin
# calculation is poisoned by the same inflated h_to_solar). Whenever the
# sun is still above the horizon, the projection horizon and the
# should-conserve-battery threshold use this short, fixed value instead
# of h_to_solar — long enough to ride out a passing cloud, nowhere near
# "assume no sun until tomorrow morning". h_to_solar itself is untouched
# for display (the "Bis Solar-Start" sensor still honestly answers "how
# long until tomorrow's threshold" once today's has passed).
DAYTIME_PROJECTION_HORIZON_H = 1.0

# Stability: how many coordinator cycles must the condition hold, expressed
# as wall-clock minutes (via _minutes_to_cycles) rather than a fixed cycle
# count — a fixed count would silently double every hold time below if
# UPDATE_INTERVAL_SECONDS ever changes, which is not what changing the
# update interval is supposed to do. No switching action fires off a brief
# few-minute fluctuation — every on/off decision needs at least 10 minutes
# of the condition holding true, however low the priority or however tight
# the battery margin looks. (The sensor-staleness correction elsewhere is a
# separate, additional safeguard specifically for the cloud-polling-lag
# scenario, not a substitute for this general floor.)
STABLE_ON_CYCLES = _minutes_to_cycles(10)   # 10 min before turning ON
STABLE_OFF_CYCLES = _minutes_to_cycles(10)  # 10 min minimum — used when there's no battery margin to spare
STABLE_OFF_CYCLES_MAX = _minutes_to_cycles(20)  # 20 min — used when margin is comfortable

# Priority staggering: when several devices cross their off-threshold in the
# same cycle (e.g. solar drops off a cliff at sunset), they'd otherwise all
# finish their off-hold at the same cycle count and switch off together.
# Each priority rank below the highest gets this many fewer cycles to wait,
# down to OFF_CYCLES_FLOOR — so the lowest-priority device sheds first, even
# when the underlying trigger fires for everyone at once. OFF_CYCLES_FLOOR
# must stay strictly below STABLE_OFF_CYCLES for this to do anything: the
# base wait time (_required_off_cycles) already collapses to exactly
# STABLE_OFF_CYCLES whenever there's no margin to spare — precisely the
# "cliff" scenario this exists for — and if the floor matched that same
# value, subtracting a rank's stagger would immediately get clamped straight
# back up to it, cancelling the stagger for every device at once right when
# it matters most. Confirmed happening in practice: every device reaching
# the same 10-minute floor simultaneously during a real margin cliff, with
# no observable staggering at all.
STAGGER_CYCLES_PER_PRIORITY_STEP = _minutes_to_cycles(1)  # 1 min less patience per rank
OFF_CYCLES_FLOOR = _minutes_to_cycles(5)  # strictly below STABLE_OFF_CYCLES — see above

# Battery-optimal set selection (see coordinator._select_battery_optimal_set):
# exhaustive over 2^n subsets of devices competing for overnight battery
# budget, so it's fast for any realistic device count but grows
# exponentially — this caps it, falling back to a simpler independent check
# above the cap rather than blocking the coordinator's own cycle.
MAX_BATTERY_OPTIMIZATION_DEVICES = 16

# How long to keep using the pre-transition managed-power figure for
# base_load AND battery-discharge attribution after a managed device's
# on/off state changes, at most — covers cloud-polled sensors (e.g.
# FusionSolarPlus, observed ~5 min lag) whose readings don't reflect the
# change immediately. This is a safety cap, not the primary release
# condition: normally the freeze releases as soon as both the load and
# discharge sensors have each produced STALENESS_MIN_REFRESHES genuinely
# new readings (real evidence they've caught up — see
# _evaluate_devices), since a fixed timer alone was observed releasing
# right as a sensor was mid-refresh, before its value had actually
# settled. This cap only matters if a sensor stalls and never reaches
# that count.
LOAD_SENSOR_STALENESS_GRACE = timedelta(minutes=10)
# How many genuinely new readings the load and discharge sensors must
# each produce after a managed device's composition changes before the
# staleness correction trusts them again.
STALENESS_MIN_REFRESHES = 2

# How long to keep computing off each core sensor's (solar/load/soc/
# battery) last known good reading once it goes unavailable/unknown,
# before actually freezing the coordinator (see _get_core_float) — a
# *different* mechanism from LOAD_SENSOR_STALENESS_GRACE above, which
# only covers a lagging-but-present reading right after a device's
# composition changes. This one covers the sensor itself going away
# entirely. Sized from what's actually been observed on a real
# installation: most FusionSolarPlus blips clear within ~10-25 minutes,
# so a short outage no longer skips a cycle at all, while a genuinely
# extended one (observed once: ~5 hours) still correctly freezes rather
# than running forever on an increasingly stale number.
CORE_SENSOR_GRACE_PERIOD = timedelta(minutes=20)

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
DISCHARGE_SMOOTHING_SAMPLES = _minutes_to_cycles(20)  # 20 min rolling median

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
MAX_SAMPLES_PER_DEVICE = int(POWER_HISTORY_ACTIVE_HOURS * 3600 / UPDATE_INTERVAL_SECONDS)
# Minimum samples before trusting the measured average over the configured
# estimate — 10 minutes of active runtime.
MIN_SAMPLES_FOR_MEASURED_AVG = _minutes_to_cycles(10)
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

# --- Self-calibrating base-load floor ---
# base_load's floor: a low percentile of the raw house-load sensor's own
# hourly minimums over this many recent days, instead of a hard 0.0 — see
# base_load_floor.py for why.
BASE_LOAD_FLOOR_LOOKBACK_DAYS = 3
# Percentile (not the outright minimum) so a single glitched hour can't
# poison the floor — see base_load_floor.py's module docstring.
BASE_LOAD_FLOOR_PERCENTILE = 5
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
# How many recent complete calendar days to use for weak-day detection's
# simple fallback reference SOC gain, whenever the current month doesn't
# have its own calibrated one yet (see SolarOffsetCalibrator.
# effective_reference_soc_gain) — short enough to reflect current
# conditions quickly, long enough to smooth out a single cloudy day.
RECENT_SOC_GAIN_WINDOW_DAYS = 14
