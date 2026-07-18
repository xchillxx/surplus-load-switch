<img src="branding/logo.png" alt="Surplus Load Switch" width="420">

Home Assistant custom integration that switches devices on and off based on
your solar surplus — with a priority cascade for multiple devices, automatic
power measurement, and a battery-aware overnight mode. Works with any PV/battery
system that exposes the right sensors to Home Assistant, not tied to a
specific inverter brand.

## Why

Most PV surplus automations only handle a single device with a fixed
threshold. This integration is built for households with several
controllable loads (crypto miners, heat pumps, pool pumps, ...) that should
compete for the same surplus by priority, without oscillating every time a
cloud passes over or another appliance briefly kicks in.

## Features

- **Priority cascade** — configure devices in priority order; the highest
  priority device gets first claim on available surplus, the next only sees
  what's left over.
- **Automatic power measurement** — optionally link a power sensor per
  device. The integration learns its real average consumption over a
  rolling 7-day window (while the device is on) and uses that instead of a
  static estimate once enough data exists.
- **Battery-aware overnight logic** — devices stay on overnight if the
  battery has enough charge to last until solar production resumes the next
  morning (based on sunrise + a configurable monthly offset, since raw
  sunrise isn't when solar actually becomes useful).
- **Spike-resistant** — the battery-margin projection uses a 20-minute
  rolling median of the discharge rate, so a stove or kettle running for a
  few minutes doesn't get projected forward as if it continued all night.
  How long the system waits before switching a device off also scales with
  how much battery margin is currently available.
- **Wallbox support** — wallboxes with their own PV-surplus charging logic
  are added as a separate device type: never switched, only their power is
  subtracted from the household load so they don't distort the surplus
  calculation for other devices.
- **Time-windowed devices** — restrict a device to a daily window (e.g. a
  pool pump); outside it, it's forced off immediately. Inside the window
  it's a normal cascade device — still only switched on when there's
  surplus (or battery margin) to cover it, so it won't cycle on/off just
  because the window is open. Point it at an existing `schedule.*` helper
  entity for multiple blocks per day / per-weekday schedules, or set a
  simple start/end time directly if you don't need that.
- **Priority-graduated shedding** — when there isn't enough surplus or
  battery margin for everything, the lowest-priority device is shed first
  instead of every device switching off together. Each device's own
  "would the battery still last?" check accounts for every higher-priority
  device already committed ahead of it, so a lower-priority device drops off
  battery power sooner than a higher-priority one, rather than all of them
  sharing one global yes/no flag.
- **Minimum daily runtime** — set an optional target (e.g. a pool pump that
  needs to filter for 4h/day for hygiene). It's never denied its normal
  chance to reach that for free on surplus/battery power earlier in the day;
  only from the afternoon onward, if it's still short, does it get forced on
  (potentially on grid power) to catch up before the day is over.
- **Climate-controlled devices** — some devices (e.g. a pool heat pump) have
  no on/off switch at all, only a thermostat-style mode selector
  (off/heat/cool/auto). Add these as a climate-controlled device: pick the
  `climate.*` entity and which mode counts as "on" (e.g. `heat`) — the
  cascade otherwise treats it exactly like a switch-controlled device
  (priority, power measurement, time windows, minimum runtime all apply).
- **Device dependencies** — some devices physically can't do anything unless
  another one is already running (e.g. a heat pump with a flow switch that
  only lets the compressor start while its pool pump is circulating water).
  Mark a device as depending on another; it's only ever turned on while the
  prerequisite is also on, so it never wastes a cascade reservation or
  dilutes its own power measurement with idle-but-"on" time.
- Fully configurable through the Home Assistant UI (no YAML required).

## Requirements

Your PV/battery system needs to expose, as Home Assistant entities:

- Solar production power (kW)
- House load power (kW)
- Battery state of charge (%)
- Battery charge/discharge power (kW) — **negative = discharging**

## Installation

### Via HACS (recommended)

1. HACS → Integrations → ⋮ → Custom repositories
2. Add this repository URL, category "Integration"
3. Install "Surplus Load Switch"
4. Restart Home Assistant

### Manual

Copy `custom_components/surplus_load_switch` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

1. Settings → Devices & Services → Add Integration → "Surplus Load Switch"
2. Select your solar, load, SOC and battery power sensors, battery capacity, and minimum SOC
3. Right after setup (or later via the integration's Configure menu), add devices:
   - **Switchable device** (e.g. a miner): name, switch entity, priority, estimated power, optional power sensor for automatic measurement
   - **Climate-controlled device** (e.g. a pool heat pump with no on/off switch): name, climate entity, which hvac_mode counts as "on", same priority/power/window/runtime options as a switchable device
   - **Wallbox**: name and power sensor only — never switched, just subtracted from the load

Priority determines serving order (1 = highest). Use "Edit device" to change
priority or other values later, and "Remove device" to delete one.

## How the decision logic works

Every 30 seconds, for each switchable device (highest priority first):

```
remaining_surplus = available_surplus - sum(power already reserved by higher-priority devices)
battery_would_last = would the battery still cover the time until next solar start
                      if this device — plus every higher-priority device already
                      committed — draws its predicted power, with whatever isn't
                      covered by surplus coming from the battery?
should_on  = remaining_surplus > device_power + 0.2 kW   OR   battery_would_last
should_off = remaining_surplus < device_power - 0.2 kW   AND  NOT battery_would_last
```

`device_power` is the measured 7-day average (once ≥20 samples exist) or the
configured estimate. An ON decision must hold for 2 minutes before it's acted
on. An OFF decision's required hold time scales from 3 minutes (no battery
margin to spare — react fast) up to 12 minutes (4h+ margin — likely a
transient spike, safe to wait it out).

`battery_would_last` is evaluated per device, projecting forward instead of
reading the *current* discharge trend — turning a device on doesn't make it
appear to break its own battery budget a few minutes later once it actually
starts drawing power, and a lower-priority device sheds before a
higher-priority one when there isn't enough margin for both, rather than
every device sharing one global "is the battery discharging right now" flag.

## License

MIT
