"""Pure, deterministic simulation and emergency P2P clearing for one locality."""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from math import ceil, isfinite
from random import Random
from uuid import NAMESPACE_URL, uuid5


EPSILON = 1e-7
ORDER_QUANTUM = Decimal("0.000001")
ORDER_EPSILON = float(ORDER_QUANTUM) + EPSILON
EFFICIENCY = 0.94
FLOOR = 0.15
CEILING = 0.95


def number(value, label, *, minimum=0):
    value = float(value)
    if not isfinite(value) or value < minimum:
        raise ValueError(f"Invalid {label}")
    return value


def unit(value, label):
    return min(number(value, label), 1.0)


def stable_id(*parts):
    return str(uuid5(NAMESPACE_URL, "gridmind:" + ":".join(map(str, parts))))


def _urgency_rank(value):
    if value in (None, "routine", ""):
        return 0
    if value == "urgent":
        return 1
    return max(0, int(value))


@dataclass
class Resource:
    participant_id: str
    name: str
    demand: float
    essential: float
    generation: float
    rated_storage: float
    storage: float
    power_kwh: float
    island_capable: bool
    urgency: int
    local_served: float = 0.0
    shared_supplied: float = 0.0
    shared_served: float = 0.0
    bought: float = 0.0
    sold: float = 0.0
    charged: float = 0.0
    discharged: float = 0.0
    spilled: float = 0.0
    exported: float = 0.0
    imported: float = 0.0

    @property
    def battery_supply(self):
        if not self.island_capable:
            return 0.0
        maximum = min(self.power_kwh, max(0.0, self.storage - FLOOR * self.rated_storage) * EFFICIENCY)
        return max(0.0, maximum - self.discharged)

    @property
    def solar_wind_surplus(self):
        return max(0.0, self.generation - self.local_served + self.discharged)

    @property
    def deficit(self):
        return max(0.0, self.demand - self.local_served - self.shared_served - self.bought - self.imported)

    @property
    def essential_deficit(self):
        return max(0.0, self.essential - self.local_served - self.shared_served - self.bought - self.imported)

    @property
    def served(self):
        return self.local_served + self.shared_served + self.bought + self.imported


def _load(profile, factor):
    base = number(profile.get("load_kw", 0), "load power")
    factor = number(factor, "demand factor")
    return base * factor


def _renewables(profile, solar_factor, wind_factor):
    solar = number(profile.get("solar_kw", 0), "solar rating") * solar_factor
    wind = number(profile.get("wind_kw", 0), "wind rating") * wind_factor
    return solar, wind


def _scenario(config):
    scenario = config.get("scenario", "historical")
    if scenario == "reference":
        default = "scheduled" if config.get("outage_step", config.get("trip_at")) is not None else "historical"
        scenario = config.get("reference_scenario", default)
    if scenario not in {"historical", "scheduled", "stress"}:
        raise ValueError(f"Unknown simulation scenario {scenario}")
    return scenario


def _with_augmented_demand(config, frame, interval_minutes):
    seed = config.get("seed")
    default = _scenario(config) == "stress" and seed is not None
    factors = frame.get("demand_error_factors") or ()
    if not config.get("augment_demand", default) or not factors:
        return frame
    seed = 0 if seed is None else seed
    h = int(int(frame["step"]) * interval_minutes / 60)
    draw = Random(f"gridmind:demand:{seed}:{h}").choice(factors)
    shift = min(1.10, max(0.90, number(draw, "demand residual multiplier")))
    base = number(frame.get("load_factor", 1.0), "demand factor")
    return {
        **frame, "load_factor": base * shift,
        "forecast_load_factor": frame.get("forecast_load_factor", base),
        "augmentation": {"method": "bounded empirical demand variation", "seed": seed,
                         "factor": shift, "unadjusted_load_factor": base},
    }


def _grid(config, frame, participants, previous, hours):
    """Grid Reliability determines the transition from current published context and policy."""
    scenario = _scenario(config)
    step = int(frame["step"])
    solar_factor = unit(frame.get("solar_factor", 0), "solar factor")
    wind_factor = unit(frame.get("wind_factor", 0), "wind factor")
    demand_kw = sum(_load(profile, frame.get("load_factor", 1.0)) for profile in participants)
    generation_kw = sum(sum(_renewables(p, solar_factor, wind_factor)) for p in participants)
    limit = number(config.get("import_limit_kw", 60), "grid import limit")
    headroom_kw = limit - max(0.0, demand_kw - generation_kw)
    trip_ticks = max(1, int(config.get("trip_ticks", 2)))
    recovery_ticks = max(1, int(config.get("recovery_ticks", 2)))
    trip_streak = int(previous.get("trip_streak", 0)) + 1 if headroom_kw < -EPSILON else 0
    recovery_streak = int(previous.get("recovery_streak", 0)) + 1 if headroom_kw >= -EPSILON else 0
    was_islanded = bool(previous.get("islanded", False))
    upstream = frame.get("outage")

    if scenario == "historical":
        islanded = was_islanded if upstream is None else bool(upstream)
        if upstream is None:
            reason = "Waiting for a documented outage or recovery reading"
        elif islanded:
            reason = "Historical outage signal activated island mode"
        else:
            reason = "Historical grid supply available"
    elif scenario == "scheduled":
        trip_at = config.get("outage_step", config.get("trip_at", 4))
        trip_at = 4 if trip_at is None else int(trip_at)
        duration = max(1, int(config.get("outage_duration", config.get("outage_steps", 4))))
        islanded = trip_at <= step < trip_at + duration
        reason = "Scheduled disconnection" if islanded else "Scheduled grid supply available"
    else:
        if was_islanded:
            islanded = recovery_streak < recovery_ticks
            reason = "Waiting for sustained import headroom" if islanded else "Sustained headroom restored grid supply"
        else:
            islanded = trip_streak >= trip_ticks
            if islanded:
                reason = "Sustained demand above the virtual feeder import limit"
            elif trip_streak:
                reason = "Prospective import exceeds the virtual feeder limit"
            else:
                reason = "Import demand within the virtual feeder limit"

    # Each measured residual represents an alternative next-hour demand level.
    # Hold current renewable availability fixed so user advisories cannot drive dispatch.
    factors = frame.get("demand_error_factors") or []
    horizon_ticks = max(1, ceil(1.0 / hours))
    if factors:
        factors = [number(factor, "demand residual multiplier") for factor in factors]
        if trip_ticks > horizon_ticks + trip_streak:
            risk = 0.0
        else:
            exceeded = sum(demand_kw * factor - generation_kw > limit + EPSILON for factor in factors)
            risk = exceeded / len(factors)
    else:
        risk = None
    market_open = islanded and config.get("scenario") != "reference" and config.get("trading_enabled", True)
    return {
        "status": "islanded" if islanded else "connected",
        "reason": reason,
        "risk_next_hour": risk,
        "headroom_kw": headroom_kw,
        "import_limit_kw": 0.0 if islanded else limit,
        "configured_import_limit_kw": limit,
        "trip_streak": trip_streak,
        "recovery_streak": recovery_streak,
        "upstream_outage": upstream,
        "market_open": market_open,
        "transition": "islanded" if islanded and not was_islanded else "restored" if was_islanded and not islanded else None,
    }


def _resources(config, frame, participants, previous, grid, hours):
    """Renewable & Storage derives available energy from current observations and prior charge."""
    solar_factor = unit(frame.get("solar_factor", 0), "solar factor")
    wind_factor = unit(frame.get("wind_factor", 0), "wind factor")
    islanded = grid["status"] == "islanded"
    storage = previous.get("storage_kwh", {})
    assets = []
    for profile in participants:
        participant_id = str(profile["id"])
        rating = number(profile.get("battery_kwh", 0), "battery capacity")
        initial = unit(profile.get("initial_soc", 0.65), "initial battery state of charge") * rating
        old = number(storage.get(participant_id, initial), "stored energy")
        if old > rating + EPSILON:
            raise ValueError("Stored energy exceeds the battery rating")
        generation = sum(_renewables(profile, solar_factor, wind_factor)) * hours
        assets.append((profile, rating, old, generation))

    def can_form_island(asset):
        profile, rating, old, generation = asset
        backup_available = old > FLOOR * rating + EPSILON and number(profile.get("battery_kw", 0), "battery power") > 0
        grid_forming = profile.get("grid_forming", profile.get("island_capable", True))
        return profile.get("island_capable", True) and grid_forming and (backup_available or generation > EPSILON)

    energized = not islanded or bool(config.get("island_capable", True) and any(map(can_form_island, assets)))
    grid["island_energized"] = energized if islanded else None
    resources = {}
    for profile, rating, old, generation in assets:
        participant_id = str(profile["id"])
        available = not islanded or energized and profile.get("island_capable", True)
        demand = _load(profile, frame.get("load_factor", 1.0)) * hours
        essential = min(demand, number(profile.get("essential_kw", 0), "essential load") * hours)
        resource = Resource(
            participant_id, str(profile.get("name", participant_id)), demand, essential,
            generation if available else 0.0, rating, old,
            number(profile.get("battery_kw", 0), "battery power") * hours,
            bool(islanded and available), _urgency_rank(profile.get("urgency")),
        )
        resource.local_served = min(resource.demand, resource.generation)
        resource.discharged = min(resource.battery_supply, resource.deficit)
        resource.local_served += resource.discharged
        resources[participant_id] = resource
    return resources


def _advisories(frame, participants):
    """Energy outlook is advisory and calculated independently of matching and dispatch."""
    weather = frame.get("weather") or {}
    if isinstance(weather, str):
        condition, valid_at, issued_at = weather, None, None
        outlook = frame.get("renewable_outlook")
    else:
        condition = weather.get("outlook_condition") or weather.get("condition") or "Weather outlook"
        valid_at = weather.get("outlook_valid_at")
        issued_at = weather.get("outlook_issued_at")
        outlook = frame.get("renewable_outlook") if weather.get("outlook_source_kind") != "synthetic" else None
    solar_band = outlook.get("solar_scenario_factor_range") if outlook else frame.get("forecast_solar_range")
    wind_band = outlook.get("wind_scenario_factor_range") if outlook else frame.get("forecast_wind_range")
    solar_forecast = frame.get("forecast_solar_factor")
    wind_forecast = frame.get("forecast_wind_factor")
    current_solar = unit(frame.get("solar_factor", 0), "solar factor")
    current_wind = unit(frame.get("wind_factor", 0), "wind factor")
    advisories = []
    for profile in participants:
        if not profile.get("solar_kw") and not profile.get("wind_kw"):
            continue
        solar = None if solar_forecast is None and profile.get("solar_kw") else 0.0
        wind = None if wind_forecast is None and profile.get("wind_kw") else 0.0
        if solar_forecast is not None:
            solar = _renewables(profile, unit(solar_forecast, "forecast solar factor"), 0)[0]
        if wind_forecast is not None:
            wind = _renewables(profile, 0, unit(wind_forecast, "forecast wind factor"))[1]
        demand = _load(profile, frame.get("forecast_load_factor", frame.get("load_factor", 1.0)))
        generation = None if solar is None or wind is None else solar + wind
        current = sum(_renewables(profile, current_solar, current_wind))
        expected_drop = None if generation is None else max(0.0, current - generation)
        shortfall = None if generation is None else max(0.0, demand - generation)
        lower = upper = None
        solar_bounds = solar_band if profile.get("solar_kw") else (0.0, 0.0)
        wind_bounds = wind_band if profile.get("wind_kw") else (0.0, 0.0)
        outlook_fields = {}
        if generation is not None and solar_bounds is not None and wind_bounds is not None:
            lower = sum(_renewables(profile, unit(solar_bounds[0], "lower solar factor"), unit(wind_bounds[0], "lower wind factor")))
            upper = sum(_renewables(profile, unit(solar_bounds[1], "upper solar factor"), unit(wind_bounds[1], "upper wind factor")))
            if outlook:
                outlook_fields["range_kind"] = "scenario"
        if outlook and generation is not None:
            solar_margin = outlook.get("solar_margin_factor") if profile.get("solar_kw") else 0.0
            wind_margin = outlook.get("wind_margin_factor") if profile.get("wind_kw") else 0.0
            if solar_margin is not None and wind_margin is not None:
                # Add the separate technology margins in kW. No joint coverage is implied.
                outlook_fields["model_error_margin_kw"] = sum(_renewables(
                    profile, unit(solar_margin, "solar margin factor"), unit(wind_margin, "wind margin factor")
                ))
                outlook_fields["model_error_target"] = outlook["target_label"]
        if shortfall is None:
            advisory = "Forecast unavailable"
        elif shortfall > EPSILON:
            advisory = "Expected renewable shortfall. Check backup reserve or arrange additional energy."
        elif lower is not None and lower < demand:
            advisory = ("Output falls below expected load in the weather scenario. Keep backup reserve available."
                        if outlook else "Output may fall below expected load. Keep backup reserve available.")
        elif expected_drop > max(0.25, current * 0.2):
            advisory = "Renewable output is expected to fall. Check planned exports and backup reserve."
        else:
            advisory = "Expected renewable output covers the current load estimate."
        advisories.append({
            "participant_id": str(profile["id"]), "participant_name": profile.get("name", str(profile["id"])),
            "condition": condition, "valid_at": valid_at, "issued_at": issued_at,
            "forecast_solar_kw": solar, "forecast_wind_kw": wind,
            "expected_generation_kw": generation, "current_generation_kw": current,
            "expected_drop_kw": expected_drop, "expected_load_kw": demand, "shortfall_kw": shortfall,
            "range_low_kw": lower, "range_high_kw": upper, "advisory": advisory,
            **outlook_fields,
        })
    return advisories


def _make_orders(config, frame, orders, resources, open_market):
    """Demand & Priority builds bounded requests and offers from the roster and manual input."""
    run_id = config.get("run_id", "preview")
    step = int(frame["step"])
    book = []
    manual_sides = set()
    for order in orders:
        participant_id = str(order["participant_id"])
        side = order["side"]
        if side not in {"buy", "sell"}:
            raise ValueError("Orders require a buy or sell side")
        manual_sides.add((participant_id, side))
        book.append({
            "id": str(order["id"]), "participant_id": participant_id, "side": side,
            "quantity_kwh": number(order["quantity_kwh"], "order quantity", minimum=float(ORDER_QUANTUM)),
            "price": number(order.get("price", 0), "order price"),
            "urgency_rank": _urgency_rank(order.get("urgency")),
            "submitted_at": str(order.get("submitted_at", "")), "manual": True,
        })

    if open_market and config.get("auto_orders", True):
        for participant_id, resource in resources.items():
            available = resource.solar_wind_surplus + resource.battery_supply
            candidates = (
                ("buy", resource.deficit, config.get("default_buy_price", 12.0)),
                ("sell", available, config.get("default_sell_price", 6.0)),
            )
            for side, amount, price in candidates:
                if (participant_id, side) in manual_sides or amount < float(ORDER_QUANTUM):
                    continue
                book.append({
                    "id": stable_id(run_id, step, participant_id, side), "participant_id": participant_id,
                    "side": side, "quantity_kwh": amount, "price": number(price, "default price"),
                    "urgency_rank": resource.urgency, "submitted_at": "", "manual": False,
                })

    if len({o["id"] for o in book}) != len(book):
        raise ValueError("Duplicate order ID in market step")

    for order in book:
        participant_id = order["participant_id"]
        resource = resources.get(participant_id)
        order["participant_name"] = resource.name if resource else participant_id
        order["urgency"] = "urgent" if order["urgency_rank"] else "routine"
        order["filled_kwh"] = 0.0
        order["remaining_kwh"] = order["quantity_kwh"]
        if order["side"] == "sell":
            order["priority"] = "supply"
        elif resource and resource.essential_deficit > EPSILON:
            order["priority"] = "essential"
        else:
            order["priority"] = "standard"
        order["status"] = "unavailable" if resource is None else "unfilled" if open_market else "market_closed"
    return sorted(book, key=lambda order: (
        order["side"] != "buy", order["priority"] != "essential", -order["urgency_rank"],
        not order["manual"], order["submitted_at"], order["id"],
    ))


def _clear_market(run_id, step, resources, book, open_market):
    """Market orders cannot sell the same energy twice or buy beyond remaining load."""
    fills = []
    if not open_market:
        return fills
    sells = sorted((o for o in book if o["side"] == "sell" and o["status"] != "unavailable"),
                   key=lambda order: (order["price"], not order["manual"], order["submitted_at"], order["id"]))
    buys = sorted((o for o in book if o["side"] == "buy" and o["status"] != "unavailable"),
                  key=lambda order: (-order["urgency_rank"], not order["manual"], order["submitted_at"], order["id"]))
    for priority in ("essential", "standard"):
        for buy in buys:
            buyer = resources[buy["participant_id"]]
            for sell in sells:
                if sell["price"] > buy["price"]:
                    break
                wanted = buyer.essential_deficit if priority == "essential" else buyer.deficit - buyer.essential_deficit
                wanted = min(wanted, buy["remaining_kwh"])
                if wanted <= EPSILON:
                    break
                if sell["participant_id"] == buy["participant_id"]:
                    continue
                seller = resources[sell["participant_id"]]
                available = seller.solar_wind_surplus + seller.battery_supply - seller.sold
                limit = min(wanted, sell["remaining_kwh"], available)
                quantity = float(Decimal(str(limit)).quantize(ORDER_QUANTUM, rounding=ROUND_DOWN))
                if quantity < float(ORDER_QUANTUM):
                    continue
                price = (Decimal(str(sell["price"])) + Decimal(str(buy["price"]))) / 2
                buyer.bought += quantity
                seller.sold += quantity
                for order in (buy, sell):
                    order["filled_kwh"] += quantity
                    order["remaining_kwh"] = max(0.0, order["quantity_kwh"] - order["filled_kwh"])
                fills.append({
                    "id": stable_id(run_id, step, buy["id"], sell["id"], priority),
                    "buy_order_id": buy["id"], "sell_order_id": sell["id"],
                    "buyer_id": buyer.participant_id, "seller_id": seller.participant_id,
                    "buyer_name": buyer.name, "seller_name": seller.name, "quantity_kwh": quantity,
                    "price": float(price.quantize(ORDER_QUANTUM, rounding=ROUND_HALF_UP)), "priority": priority,
                })
    for order in book:
        if order["filled_kwh"] <= EPSILON:
            continue
        order["status"] = "filled" if order["remaining_kwh"] <= ORDER_EPSILON else "partially_filled"
    return fills


def _dispatch(resources, grid, hours):
    connected = grid["status"] == "connected"
    unsold = {p.participant_id: max(0.0, p.solar_wind_surplus - p.sold) for p in resources.values()}
    if connected:
        # Behind the common feeder, available PV offsets upstream import. This is not a trade.
        shared = sum(unsold.values())
        supply = shared
        imports = grid["import_limit_kw"] * hours
        consumers = sorted(resources.values(), key=lambda p: (-p.urgency, p.participant_id))
        for essential in (True, False):
            for p in consumers:
                wanted = p.essential_deficit if essential else p.deficit - p.essential_deficit
                served = min(wanted, supply)
                p.shared_served += served
                supply -= served
                imported = min(wanted - served, imports)
                p.imported += imported
                imports -= imported
        used = shared - supply
        for participant_id, remainder in unsold.items():
            drawn = min(used, remainder)
            unsold[participant_id] -= drawn
            resources[participant_id].shared_supplied = drawn
            used -= drawn

    for p in resources.values():
        offered_battery = max(0.0, p.sold - p.solar_wind_surplus)
        p.discharged += offered_battery
        remainder = unsold[p.participant_id]
        if p.discharged <= EPSILON and p.rated_storage > EPSILON:
            charge_limit = max(0.0, CEILING * p.rated_storage - p.storage) / EFFICIENCY
            p.charged = min(remainder, p.power_kwh, charge_limit)
        remainder -= p.charged
        p.exported = remainder if connected else 0.0
        p.spilled = 0.0 if connected else remainder


def _validate(grid, resources, book, fills, hours):
    """Independent validation compares the cleared plan against physical and order limits."""
    traded = defaultdict(float)
    orders = {o["id"]: o for o in book}
    totals = defaultdict(float)
    for fill in fills:
        buy = orders[fill["buy_order_id"]]
        sell = orders[fill["sell_order_id"]]
        amount = fill["quantity_kwh"]
        if (not grid["market_open"] or amount <= EPSILON or buy["side"] != "buy" or sell["side"] != "sell"
                or buy["participant_id"] != fill["buyer_id"] or sell["participant_id"] != fill["seller_id"]
                or buy["participant_id"] == sell["participant_id"]
                or fill["price"] < sell["price"] - 1e-6 or fill["price"] > buy["price"] + 1e-6):
            raise ValueError("Settlement failed independent validation")
        traded[buy["id"]] += amount
        traded[sell["id"]] += amount
    for order in book:
        if (traded[order["id"]] > order["quantity_kwh"] + EPSILON
                or abs(traded[order["id"]] - order["filled_kwh"]) > EPSILON):
            raise ValueError("An order exceeds its settled quantity limit")

    for p in resources.values():
        ending = p.storage + p.charged * EFFICIENCY - p.discharged / EFFICIENCY
        lower = min(p.storage, FLOOR * p.rated_storage)
        upper = max(p.storage, CEILING * p.rated_storage)
        if (ending < lower - EPSILON or ending > upper + EPSILON
                or p.charged > p.power_kwh + EPSILON or p.discharged > p.power_kwh + EPSILON
                or p.charged > EPSILON and p.discharged > EPSILON):
            raise ValueError("Battery dispatch exceeds its constraints")
        if p.served > p.demand + EPSILON or p.discharged > EPSILON and not p.island_capable:
            raise ValueError("Dispatch exceeds the participant's energy limits")
        produced = p.generation + p.discharged
        used = p.local_served + p.sold + p.shared_supplied + p.charged + p.exported + p.spilled
        if abs(produced - used) > EPSILON * max(1.0, produced):
            raise ValueError("Participant dispatch does not conserve energy")
        totals["generation_kwh"] += p.generation
        totals["battery_discharge_kwh"] += p.discharged
        totals["grid_import_kwh"] += p.imported
        totals["served_kwh"] += p.served
        totals["battery_charge_kwh"] += p.charged
        totals["export_kwh"] += p.exported
        totals["spilled_kwh"] += p.spilled
        totals["demand_kwh"] += p.demand
        totals["essential_demand_kwh"] += p.essential
        totals["essential_served_kwh"] += min(p.essential, p.served)
    supplied = totals["generation_kwh"] + totals["battery_discharge_kwh"] + totals["grid_import_kwh"]
    consumed = totals["served_kwh"] + totals["battery_charge_kwh"] + totals["export_kwh"] + totals["spilled_kwh"]
    if abs(supplied - consumed) > EPSILON * max(1.0, supplied):
        raise ValueError("Simulation step does not conserve energy")
    if totals["grid_import_kwh"] > grid["import_limit_kw"] * hours + EPSILON:
        raise ValueError("Import limit exceeded")
    if grid["status"] == "islanded" and totals["export_kwh"] > EPSILON:
        raise ValueError("An islanded microgrid cannot export to the grid")
    totals["unserved_kwh"] = max(0.0, totals["demand_kwh"] - totals["served_kwh"])
    totals["essential_unserved_kwh"] = max(0.0, totals["essential_demand_kwh"] - totals["essential_served_kwh"])
    totals["traded_kwh"] = sum(f["quantity_kwh"] for f in fills)
    totals["settlement_value"] = sum(f["quantity_kwh"] * f["price"] for f in fills)
    return dict(totals)


def tick(config, frame, participants, orders=(), prior=None):
    """Coordinator runs one interval and returns a validated plan ready to persist.

    Inputs are ordinary JSON-like mappings. Inputs remain unchanged so a dry run,
    replay retry, or a paired baseline can safely rerun the identical interval.
    """
    step = int(frame["step"])
    interval = number(config.get("interval_minutes", 15), "interval length", minimum=EPSILON)
    hours = interval / 60.0
    frame = _with_augmented_demand(config, frame, interval)
    previous = prior or {}
    participants = sorted((p for p in participants if int(p.get("active_from_step") or 0) <= step), key=lambda p: str(p["id"]))
    if len({str(p["id"]) for p in participants}) != len(participants):
        raise ValueError("Duplicate participant in active locality")

    grid = _grid(config, frame, participants, previous, hours)
    resources = _resources(config, frame, participants, previous, grid, hours)
    advisories = _advisories(frame, participants)
    market_open = grid["market_open"]
    book = _make_orders(config, frame, orders, resources, market_open)
    supply = {p.participant_id: p.solar_wind_surplus + p.battery_supply for p in resources.values()}
    fills = _clear_market(config.get("run_id", "preview"), step, resources, book, market_open)
    _dispatch(resources, grid, hours)
    totals = _validate(grid, resources, book, fills, hours)
    sellers = []
    for participant_id, available in supply.items():
        if available <= EPSILON:
            continue
        offered = sum(order["quantity_kwh"] for order in book if order["participant_id"] == participant_id and order["side"] == "sell")
        resource = resources[participant_id]
        sellers.append({
            "participant_id": participant_id, "participant_name": resource.name,
            "available_kwh": available, "offered_kwh": min(available, offered),
            "remaining_kwh": max(0.0, available - resource.sold),
        })

    trace = [
        {"agent": "Grid Reliability", "status": "complete", "action": "assess_grid", "reason": grid["reason"],
         "summary": {"status": grid["status"], "headroom_kw": grid["headroom_kw"], "risk_next_hour": grid["risk_next_hour"]}},
        {"agent": "Renewable & Storage", "status": "complete", "action": "evaluate_supply", "reason": "Calculated available renewable and storage energy",
         "summary": {"eligible_sellers": len(sellers), "advisories": len(advisories)}},
        {"agent": "Demand & Priority", "status": "complete", "action": "rank_requests", "reason": "Ordered essential demand ahead of standard demand",
         "summary": {"requests": sum(order["side"] == "buy" for order in book), "essential_kwh": totals.get("essential_demand_kwh", 0.0)}},
        {"agent": "P2P Market", "status": "complete", "action": "clear_orders" if market_open else "hold_market",
         "reason": "Matched by priority and available supply" if market_open else "Emergency market closed",
         "summary": {"fills": len(fills), "traded_kwh": totals["traded_kwh"]}},
        {"agent": "Coordinator", "status": "validated", "action": "validate_step", "reason": "Validated grid limits, order limits, storage and energy balance",
         "summary": {"served_kwh": totals.get("served_kwh", 0.0), "unserved_kwh": totals["unserved_kwh"]}},
    ]
    state = {
        "islanded": grid["status"] == "islanded", "trip_streak": grid["trip_streak"], "recovery_streak": grid["recovery_streak"],
        "storage_kwh": {**previous.get("storage_kwh", {}), **{
            p.participant_id: p.storage + p.charged * EFFICIENCY - p.discharged / EFFICIENCY for p in resources.values()
        }},
    }
    provenance_keys = ("data_kind", "weather", "measurements", "provenance", "augmentation",
                       "forecast_issue_at", "forecast_valid_at")
    source = {key: frame[key] for key in provenance_keys if key in frame}
    return {
        "step": step, "timestamp": frame["timestamp"], "interval_minutes": interval, "source": source,
        "grid": grid, "market": {"open": market_open, "orders": book, "fills": fills, "sellers": sellers},
        "renewables": advisories, "trace": trace, "totals": totals, "state": state,
    }
