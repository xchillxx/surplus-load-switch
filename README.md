# PV Surplus Manager

Home Assistant custom integration that switches devices on and off based on
your solar surplus — with a priority cascade for multiple devices and
automatic power measurement.

## Why

Most PV surplus automations only handle a single device with a fixed
threshold. This integration is built for households with several
controllable loads (crypto miners, heat pumps, pool pumps, ...) that should
compete for the same surplus by priority, without oscillating every time a
cloud passes over.

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
- **Wallbox exclusion** — mark a device as a wallbox to exclude it from
  switching (e.g. if it's already PV-controlled by your inverter/EMS) while
  still subtracting its power draw from the household load.
- **Hysteresis + stability counters** — every on/off decision must hold for
  several consecutive cycles before acting, to avoid rapid toggling.
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
3. Install "PV Surplus Manager"
4. Restart Home Assistant

### Manual

Copy `custom_components/pv_surplus_manager` into your `config/custom_components/` folder and restart Home Assistant.

## Configuration

1. Settings → Devices & Services → Add Integration → "PV Surplus Manager"
2. Select your solar, load, SOC and battery power sensors, battery capacity, and minimum SOC
3. Open the integration's options to add devices:
   - Name, switch entity, estimated power (kW)
   - Optional: a power sensor for automatic measurement
   - Optional: mark as wallbox (excluded from switching)

Devices are prioritized in the order you add them (first added = highest
priority). Use "Remove device" in the options to delete one.

## How the decision logic works

Every 30 seconds, for each device (highest priority first):

```
remaining_surplus = available_surplus - sum(power already reserved by higher-priority devices)
should_on  = remaining_surplus > device_power + 0.2 kW   OR   battery covers until next solar start
should_off = remaining_surplus < device_power - 0.2 kW   AND  battery does NOT cover until next solar start
```

`device_power` is the measured 7-day average (once ≥20 samples exist) or the
configured estimate. A decision must hold for 2 minutes (ON) or 3 minutes
(OFF) before it's acted on.

## License

MIT
