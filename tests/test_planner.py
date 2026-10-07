"""Pure planner + departures checks (no HA needed)."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.surplus_pilot import planner as P
from custom_components.surplus_pilot.departures import all_departures

TZ = ZoneInfo("Europe/Berlin")


def base_inputs(now, **kw):
    d = dict(now=now, pv_kw=0.0, base_kw=0.5, battery_soc=60.0, battery_capacity_kwh=14.0, battery_min_soc=15.0,
             export_kw=0.0, solar_start=now.replace(hour=9) + timedelta(days=1 if now.hour >= 9 else 0),
             solar_start_today=now.replace(hour=9, minute=0), sunset=now.replace(hour=19, minute=0),
             pv_end=now.replace(hour=17, minute=30), night_base_kw=0.45)
    if now.hour >= 19:
        d["sunset"] += timedelta(days=1)
        d["pv_end"] += timedelta(days=1)
    d.update(kw)
    return P.Inputs(**d)


def miner(on=False):
    return P.DeviceInput(id="miner", name="Miner", priority=2, decision_kw=0.15, is_on=on)


def test_night_battery_path():
    now = datetime(2026, 10, 6, 22, 0, tzinfo=TZ)
    plan = P.make_plan(base_inputs(now, battery_soc=80.0, devices=[miner()]))
    assert plan.devices["miner"].on and plan.devices["miner"].reason == "akku_reicht"
    plan = P.make_plan(base_inputs(now, battery_soc=25.0, devices=[miner()]))
    assert not plan.devices["miner"].on and plan.devices["miner"].reason == "akku_reicht_nicht"


def test_departure_obligation_from_grid_late_and_cheap():
    now = datetime(2026, 10, 6, 1, 0, tzinfo=TZ)
    dep = P.Departure(start=now.replace(hour=4, minute=30), target_soc=50.0, name="Frühschicht")
    car = P.CarInput(present=True, soc=35.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    prices = [P.PriceSlot(now + timedelta(minutes=15 * i), now + timedelta(minutes=15 * (i + 1)),
                          0.30 if i < 8 else 0.20) for i in range(16)]
    plan = P.make_plan(base_inputs(now, car=car, departures=[dep], prices=prices))
    assert plan.need_kwh > 10 and not plan.car_grid          # 01:00 is expensive -> wait
    later = now.replace(hour=3, minute=0)
    plan = P.make_plan(base_inputs(later, car=car, departures=[dep], prices=prices))
    assert plan.car_grid and plan.car_kw == 11.04              # 03:00 cheapest slots -> charge


def test_devices_before_car_topup_and_car_first_without_pv_chance():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    car = P.CarInput(present=True, soc=60.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    fc = [P.ForecastHour(end=now.replace(hour=0) + timedelta(hours=h), kwh=8.0 if 10 <= h % 24 <= 16 else 0.0)
          for h in range(48)]
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=100.0, car=car, forecast=fc, devices=[miner()]))
    assert plan.devices["miner"].on and not plan.car_first and plan.car_kw > 3.45
    # tomorrow the car is away all day -> it charges first today
    away = P.Departure(start=(now + timedelta(days=1)).replace(hour=4, minute=30), target_soc=50.0,
                       returns=(now + timedelta(days=1)).replace(hour=18, minute=30))
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=100.0, car=car, forecast=fc, devices=[miner()],
                                   departures=[away]))
    assert plan.car_first


def test_slots_rhythm_and_oneoff():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
    slots = [{"slot": 1, "enabled": True, "name": "Spätschicht", "target_soc": 50, "time": "16:30", "away_h": 14,
              "rhythm_days": 4, "start_date": "2026-10-02"},
             {"slot": 2, "enabled": False, "time": "04:30", "rhythm_days": 1}]
    oneoff = {"enabled": True, "target_soc": 80, "time": "09:00", "start_date": "2026-10-08"}
    deps = all_departures(slots, oneoff, now)
    starts = [d.start.strftime("%m-%d %H:%M") for d in deps]
    assert starts == ["10-06 16:30", "10-08 09:00"]  # every 4 days: next one 10-10, beyond the 4-day horizon
    assert deps[0].returns == deps[0].start + timedelta(hours=14)


def test_departure_battery_mode_only_while_car_can_charge():
    now = datetime(2026, 10, 6, 14, 0, tzinfo=TZ)
    dep = P.Departure(start=now.replace(hour=16, minute=30), target_soc=50.0)
    fc = [P.ForecastHour(end=now.replace(hour=0) + timedelta(hours=h), kwh=6.0 if 10 <= h <= 17 else 0.0)
          for h in range(24)]
    full = P.CarInput(present=True, soc=80.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                      max_kw=11.04)
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=80.0, car=full, departures=[dep], forecast=fc))
    assert plan.battery_mode == "frist"
    half = P.CarInput(**{**full.__dict__, "soc": 60.0})
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, battery_soc=80.0, car=half, departures=[dep], forecast=fc))
    assert plan.battery_mode == "abfahrt"


def _dark_day_inputs(now, night_estimate, price_now=0.20, threshold=None, cheap_target=None, sunny=False):
    dep = P.Departure(start=(now + timedelta(days=1)).replace(hour=4, minute=30), target_soc=50.0, name="Tagschicht")
    car = P.CarInput(present=True, soc=30.0, limit_soc=80.0, capacity_kwh=72.9, efficiency=0.9, min_kw=3.45,
                     max_kw=11.04)
    # today's prices are published (cheap midday 11-14 h), tomorrow's not yet (before 13:00)
    day0 = now.replace(hour=0, minute=0)
    prices = [P.PriceSlot(day0 + timedelta(hours=h), day0 + timedelta(hours=h + 1),
                          0.20 if 11 <= h < 14 else 0.32) for h in range(24)]
    profile = {h: (night_estimate if h < 6 else 0.32) for h in range(24)}
    kwh = 9.0 if sunny else 0.3
    fc = [P.ForecastHour(end=day0 + timedelta(hours=h), kwh=kwh if 10 <= h % 24 <= 16 else 0.0) for h in range(48)]
    return base_inputs(now, pv_kw=0.3, car=car, departures=[dep], prices=prices, price_profile=profile,
                       forecast=fc, price_now=price_now, cheap_threshold=threshold, cheap_target_soc=cheap_target)


def test_day_before_early_shift_charges_at_cheap_midday():
    early = datetime(2026, 11, 3, 11, 0, tzinfo=TZ)
    # 16 kWh need 1.5 h: of the equally cheap 11-14 h slots the LATEST ones are taken
    assert not P.make_plan(_dark_day_inputs(early, night_estimate=0.28)).car_grid
    now = datetime(2026, 11, 3, 12, 45, tzinfo=TZ)
    plan = P.make_plan(_dark_day_inputs(now, night_estimate=0.28))   # night usually dearer -> now
    assert plan.car_grid and plan.car_reason == "netz_pflicht"
    plan = P.make_plan(_dark_day_inputs(now, night_estimate=0.15))   # night usually cheaper -> wait
    assert not plan.car_grid


def test_cheap_topup_only_when_pv_wont_do_it():
    now = datetime(2026, 11, 3, 11, 0, tzinfo=TZ)
    inp = _dark_day_inputs(now, night_estimate=0.15, price_now=0.20, threshold=0.22, cheap_target=80.0)
    inp.car.soc = 55.0  # obligation already met
    plan = P.make_plan(inp)
    assert plan.car_grid and plan.car_reason == "netz_billig"
    inp = _dark_day_inputs(now, night_estimate=0.15, price_now=0.20, threshold=0.22, cheap_target=80.0, sunny=True)
    inp.car.soc = 55.0
    plan = P.make_plan(inp)
    assert not plan.car_grid


def test_ordinary_autumn_tomorrow_keeps_devices_before_car_topup():
    """07.10.2026 live: car 52 % (limit 80), tomorrow ~25 kWh forecast, car
    home all day. The x0.7 forecast made it 'no PV chance tomorrow' and the
    optional top-up pushed the pool pump off."""
    now = datetime(2026, 10, 7, 10, 30, tzinfo=TZ)
    car = P.CarInput(present=True, soc=52.0, limit_soc=80.0, capacity_kwh=72.89, efficiency=0.9, min_kw=4.14,
                     max_kw=11.04)
    tomorrow = now.replace(hour=0, minute=0) + timedelta(days=1)
    kwh = {10: 1.5, 11: 2.8, 12: 3.8, 13: 4.3, 14: 4.3, 15: 3.8, 16: 2.9, 17: 1.6, 18: 0.6}
    fc = [P.ForecastHour(end=tomorrow + timedelta(hours=h), kwh=v) for h, v in kwh.items()]
    pump = P.DeviceInput(id="pump", name="Poolpumpe", priority=3, decision_kw=1.34, is_on=True, soc_reserve=85.0)
    plan = P.make_plan(base_inputs(now, pv_kw=6.0, base_kw=1.0, battery_soc=70.0, battery_capacity_kwh=13.8,
                                   car=car, forecast=fc, devices=[miner(True), pump]))
    assert not plan.car_first
    assert plan.devices["pump"].on


def test_daytime_off_reason_is_missing_surplus_not_reserve():
    now = datetime(2026, 10, 7, 13, 0, tzinfo=TZ)
    boiler = P.DeviceInput(id="boiler", name="Boiler", priority=5, decision_kw=2.0, is_on=True, soc_reserve=95.0)
    plan = P.make_plan(base_inputs(now, pv_kw=2.0, base_kw=1.0, battery_soc=83.0, export_kw=0.0, devices=[boiler]))
    assert plan.devices["boiler"].reason == "kein_ueberschuss"
    night = now.replace(hour=22)
    plan = P.make_plan(base_inputs(night, battery_soc=83.0, devices=[boiler]))
    assert plan.devices["boiler"].reason == "akku_reserve"
