"""End-to-end: set up Surplus Pilot in a test Home Assistant and watch it act."""
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.surplus_pilot.const import DOMAIN

DATA = {
    "pv_sensor": "sensor.pv", "load_sensor": "sensor.load", "grid_export_sensor": "sensor.export",
    "battery_soc_sensor": "sensor.bat_soc", "battery_capacity_kwh": 14.0, "battery_min_soc": 15,
    "pv_end_before_sunset_h": 1.5, "stale_minutes": 20, "price_source": "none",
    "car": {"name": "Model 3", "charge_switch": "switch.car_charge", "current_entity": "number.car_amps",
            "soc_sensor": "sensor.car_soc", "limit_default": 80, "plugged_sensor": "binary_sensor.car_plugged",
            "power_sensor": "sensor.car_power", "capacity_kwh": 72.9, "efficiency": 0.9, "min_amps": 5,
            "max_amps": 16, "phases": 3, "voltage": 230, "allow_grid": True, "max_commands_per_day": 0},
    "devices": [
        {"id": "miner", "kind": "switch", "name": "Miner", "entity": "switch.miner", "priority": 2, "power_kw": 0.15},
        {"id": "pump", "kind": "switch", "name": "Pumpe", "entity": "switch.pump", "priority": 3, "power_kw": 1.3,
         "window_start": "00:00", "window_end": "23:59", "soc_reserve": 85},
        {"id": "wp", "kind": "climate", "name": "Pool-WP", "entity": "climate.pool", "climate_on_mode": "heat",
         "priority": 4, "power_kw": 0.86, "depends_on": "pump", "soc_reserve": 90},
    ],
}


def set_world(hass, pv, load, soc, car_soc=50, plugged="on", car_kw=0.0, export=0.0):
    now = dt_util.now()
    hass.states.async_set("sensor.pv", pv, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.load", load, {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.export", export * 1000, {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.bat_soc", soc, {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.car_soc", car_soc)
    hass.states.async_set("binary_sensor.car_plugged", plugged)
    hass.states.async_set("sensor.car_power", car_kw, {"unit_of_measurement": "kW"})
    hass.states.async_set("number.car_amps", 6, {"min": 5, "max": 16})
    hass.states.async_set("sun.sun", "above_horizon", {
        "next_rising": (now.replace(hour=7, minute=0) + timedelta(days=1)).isoformat(),
        "next_setting": (now + timedelta(hours=5)).isoformat()})


async def test_setup_and_decisions(hass: HomeAssistant, freezer):
    hass.config.language = "de"
    set_world(hass, pv=9.0, load=0.6, soc=96, car_soc=50, export=2.0)
    hass.states.async_set("switch.car_charge", "off")
    hass.states.async_set("switch.miner", "off")
    hass.states.async_set("switch.pump", "off")
    hass.states.async_set("climate.pool", "off")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = []

    async def fake(call):
        calls.append((call.domain, call.service, dict(call.data)))
        ent = call.data.get("entity_id")
        if call.domain == "switch":
            hass.states.async_set(ent, "on" if call.service == "turn_on" else "off")
        elif call.domain == "climate":
            hass.states.async_set(ent, call.data["hvac_mode"])
        elif call.domain == "number":
            hass.states.async_set(ent, call.data["value"], {"min": 5, "max": 16})

    for dom, srv in (("switch", "turn_on"), ("switch", "turn_off"), ("number", "set_value"),
                     ("climate", "set_hvac_mode")):
        hass.services.async_register(dom, srv, fake)
    co = hass.data[DOMAIN][entry.entry_id]

    from homeassistant.helpers import entity_registry as er
    reg = er.async_get(hass)
    ids = sorted(e.entity_id for e in reg.entities.values() if e.platform == DOMAIN)
    print(len(ids), ids)
    st = hass.states.get("sensor.surplus_pilot_status")
    assert st is not None
    print("STATUS:", st.state)
    print("ERKL:", st.attributes.get("erklaerung"))

    # run 15 simulated minutes so the car decision (needs 10 min of samples) and device debounce happen
    for i in range(26):
        freezer.tick(timedelta(minutes=1))
        set_world(hass, pv=9.0, load=0.6 + (0.15 if hass.states.get("switch.miner").state == "on" else 0),
                  soc=96, car_soc=50, export=2.0)
        async_fire_time_changed(hass, dt_util.utcnow())
        await hass.async_block_till_done()
    print("CAR:", co.car.decision, co.car.action)
    print("DEV PLAN:", {k: (v.on, v.reason) for k, v in co.device_plan.devices.items()})
    print("CALLS:", calls)
    st = hass.states.get("sensor.surplus_pilot_status")
    print("STATUS2:", st.state)
    print("ERKL2:", st.attributes.get("erklaerung"))
    print("CARSENSOR:", hass.states.get("sensor.model_3_ladestatus").state,
          hass.states.get("sensor.model_3_ladestatus").attributes)
    assert ("switch", "turn_on", {"entity_id": "switch.miner"}) in calls
    assert any(c[0] == "climate" for c in calls)
    print("LOG:", list(co.log))
    assert co.device_plan.devices["miner"].on
    assert co.car.decision is not None


async def test_config_flow(hass: HomeAssistant):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert r["type"] == "form"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {
        "pv_sensor": "sensor.pv", "load_sensor": "sensor.load", "battery_capacity_kwh": 10,
        "battery_min_soc": 15, "pv_end_before_sunset_h": 1.5, "stale_minutes": 20, "price_source": "none"})
    assert r["type"] == "create_entry", r
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    r = await hass.config_entries.options.async_init(entry.entry_id)
    assert r["type"] == "menu"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"next_step_id": "device_add"})
    r = await hass.config_entries.options.async_configure(r["flow_id"], {"kind": "switch"})
    assert r["type"] == "form" and r["step_id"] == "device"
    r = await hass.config_entries.options.async_configure(r["flow_id"], {
        "name": "Boiler", "entity": "switch.boiler", "priority": 5, "power_kw": 1.0})
    assert r["type"] == "create_entry", r
    await hass.async_block_till_done()
    assert entry.data["devices"][0]["name"] == "Boiler"


async def test_unknown_plug_state_means_not_present(hass: HomeAssistant, freezer):
    set_world(hass, pv=9.0, load=0.6, soc=96, car_soc=50)
    hass.states.async_set("binary_sensor.car_plugged", "unavailable")
    hass.states.async_set("sensor.car_soc", "unavailable")
    hass.states.async_set("sensor.car_soc_fleet", 55)
    hass.states.async_set("sensor.car_cable", "complete")
    for e in ("switch.car_charge", "switch.miner", "switch.pump", "climate.pool"):
        hass.states.async_set(e, "off")
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    co = hass.data[DOMAIN][entry.entry_id]
    assert co.status["auto_da"] is False          # plug state unknown -> no commands
    car = {**DATA["car"], "soc_fallback_sensor": "sensor.car_soc_fleet", "plugged_fallback_sensor": "sensor.car_cable"}
    hass.config_entries.async_update_entry(entry, data={**DATA, "car": car})
    await hass.async_block_till_done()
    co = hass.data[DOMAIN][entry.entry_id]
    await co.async_refresh()
    assert co.status["auto_da"] is True and co.status["auto_soc"] == 55.0
    hass.states.async_set("sensor.car_cable", "disconnected")
    await co.async_refresh()
    assert co.status["auto_da"] is False
