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
