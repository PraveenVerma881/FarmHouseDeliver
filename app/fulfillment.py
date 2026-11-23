"""Automatic order sourcing and route-job creation for the server-rendered UI."""
from __future__ import annotations

import hashlib
import json
import math
import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg
import httpx

from app.schemas import PlanningJobCreate

log=logging.getLogger("fresh_produce_fulfillment")


def haversine_metres(a: tuple[float, float], b: tuple[float, float]) -> int:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return int(2 * 6_371_000 * math.asin(math.sqrt(h)))


def estimate_delivery(distance_metres: int) -> int:
    """Conservative city-speed estimate when no road matrix server is configured."""
    road_metres = distance_metres * 1.3
    return max(15, int(road_metres / (25_000 / 60)) + 15)


async def quote_basket(connection: asyncpg.Connection, cart: dict[str, str], latitude: float | None, longitude: float | None, *, lock: bool = False) -> dict:
    """Select nearby offer slices and calculate an indicative basket and ETA."""
    plans: list[dict] = []
    total_paise = 0
    all_priced = True
    path: list[tuple[float, float]] = []
    missing: list[str] = []
    for product_raw, quantity_raw in cart.items():
        product_id = UUID(product_raw)
        remaining = Decimal(quantity_raw)
        if not remaining.is_finite() or remaining <= 0:
            continue
        sql = """SELECT o.id offer_id,o.farm_id,o.product_id,o.available_quantity,o.unit,o.unit_price_paise,
                         f.name farm_name,ST_Y(f.location::geometry) farm_lat,ST_X(f.location::geometry) farm_lon
                  FROM produce_offers o JOIN farms f ON f.id=o.farm_id JOIN products p ON p.id=o.product_id
                  WHERE o.product_id=$1 AND o.unit=p.default_unit AND o.status='available' AND o.harvest_date>=CURRENT_DATE AND o.available_quantity>0
                    AND EXISTS(SELECT 1 FROM platform_users u WHERE u.farm_id=o.farm_id AND u.role IN ('farmer','vendor') AND u.is_active)
                  ORDER BY CASE WHEN $2::double precision IS NULL THEN 0 ELSE ST_Distance(f.location,ST_SetSRID(ST_MakePoint($3,$2),4326)::geography) END,
                           o.unit_price_paise,o.harvest_date,o.created_at"""
        if lock:
            sql += " FOR UPDATE OF o SKIP LOCKED"
        offers = await connection.fetch(sql, product_id, latitude, longitude)
        selected=[]
        for offer in offers:
            if remaining <= 0: break
            take=min(remaining,Decimal(offer['available_quantity']))
            if take<=0: continue
            selected.append({"offer":offer,"quantity":take})
            remaining-=take
            total_paise+=int(take*int(offer['unit_price_paise']))
            if int(offer['unit_price_paise'])<=0: all_priced=False
            path.append((float(offer['farm_lat']),float(offer['farm_lon'])))
        if remaining>0:
            product_name=await connection.fetchval("SELECT name FROM products WHERE id=$1",product_id)
            missing.append(f"{product_name or product_id}: {remaining} not currently offered")
        plans.append({"product_id":product_id,"quantity":Decimal(quantity_raw),"selected":selected,"missing":remaining})
    distance=0
    if latitude is not None and longitude is not None and path:
        customer=(latitude,longitude)
        # Use nearest pickup as the dispatch origin, then estimate the sequence
        path.sort(key=lambda point:haversine_metres(point,customer))
        distance=sum(haversine_metres(a,b) for a,b in zip(path,path[1:]))+haversine_metres(path[-1],customer)
    base_fee=int(os.getenv("DELIVERY_BASE_FEE_PAISE","3000"))
    per_km=int(os.getenv("DELIVERY_PER_KM_PAISE","500"))
    delivery_fee=base_fee+((distance*per_km+999)//1000 if distance else 0)
    return {"plans":plans,"items_paise":total_paise,"delivery_fee_paise":delivery_fee,
            "total_paise":total_paise+delivery_fee if all_priced and not missing else None,
            "distance_metres":distance,"eta_minutes":estimate_delivery(distance)+60 if distance else None,
            "all_priced":all_priced,"missing":missing,
            "provider":"OSRM road estimate" if os.getenv("ROUTING_BASE_URL") else "Development ETA estimate (1.3x straight-line distance at 25 km/h)"}


async def road_matrices(locations: list[tuple[float, float]]) -> tuple[list[list[int]], list[list[int]], str]:
    """Use an operator-configured OSRM-compatible server, otherwise a labelled estimate."""
    base = os.getenv("ROUTING_BASE_URL", "").rstrip("/")
    fallback_provider="straight-line development estimate (1.3x distance, 25 km/h)"
    if base:
        try:
            coordinates = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in locations)
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get(
                    f"{base}/table/v1/driving/{coordinates}",
                    params={"annotations": "duration,distance"},
                )
                response.raise_for_status()
                result = response.json()
            if result.get("code") != "Ok" or not result.get("durations") or not result.get("distances"):
                raise ValueError("Routing service did not return a complete duration and distance matrix")
            minutes = [[max(0, int((v + 59) // 60)) for v in row] for row in result["durations"]]
            metres = [[max(0, int(v)) for v in row] for row in result["distances"]]
            return minutes, metres, "OSRM road routing"
        except Exception:
            log.exception("Road routing provider failed; using a clearly marked development estimate")
            fallback_provider="development estimate (routing provider unavailable)"
    else:
        fallback_provider="straight-line development estimate (1.3x distance, 25 km/h)"

    metres = [[0 if i == j else int(haversine_metres(a, b) * 1.3) for j, b in enumerate(locations)] for i, a in enumerate(locations)]
    minutes = [[0 if i == j else max(1, int(d / (25_000 / 60))) for d in row] for i, row in enumerate(metres)]
    return minutes, metres, fallback_provider


async def queue_order_route(connection: asyncpg.Connection, order_id: UUID) -> UUID | None:
    """Queue one route once every requested vendor commitment is confirmed."""
    order = await connection.fetchrow(
        "SELECT id,status,delivery_point,planning_job_id FROM orders WHERE id=$1 FOR UPDATE", order_id
    )
    if not order or order["planning_job_id"] or order["status"] not in {"sourcing", "confirmed"}:
        return order["planning_job_id"] if order else None
    commitments = await connection.fetch(
        """SELECT vc.id,vc.farm_id,vc.requested_quantity,vc.confirmed_quantity,vc.unit,
                  ST_Y(f.location::geometry) farm_lat,ST_X(f.location::geometry) farm_lon,p.max_transit_minutes
           FROM vendor_commitments vc JOIN farms f ON f.id=vc.farm_id
           JOIN products p ON p.id=vc.product_id
           WHERE vc.order_id=$1 AND vc.status='confirmed'
             AND vc.confirmed_quantity >= vc.requested_quantity
           ORDER BY vc.created_at,vc.id""",
        order_id,
    )
    commitment_count = await connection.fetchval("SELECT count(*) FROM vendor_commitments WHERE order_id=$1", order_id)
    pending = await connection.fetchval("SELECT count(*) FROM vendor_commitments WHERE order_id=$1 AND status='requested'", order_id)
    rejected = await connection.fetchval("SELECT count(*) FROM vendor_commitments WHERE order_id=$1 AND status IN ('shortfall','rejected')", order_id)
    if pending:
        return None
    if rejected or not commitments or len(commitments) != commitment_count:
        old = order["status"]
        await connection.execute("UPDATE orders SET status='needs_review',updated_at=now() WHERE id=$1 AND status<>'needs_review'", order_id)
        if old != "needs_review":
            await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,$2,'needs_review','Vendor reported a shortfall or rejection')", order_id, old)
        return None
    unmet_line = await connection.fetchval(
        """SELECT EXISTS(
             SELECT 1 FROM order_lines ol WHERE ol.order_id=$1 AND ol.quantity > coalesce(
               (SELECT sum(vc.confirmed_quantity) FROM vendor_commitments vc
                WHERE vc.order_line_id=ol.id AND vc.status='confirmed'),0))""",
        order_id,
    )
    if unmet_line:
        old=order["status"]
        await connection.execute("UPDATE orders SET status='needs_review',updated_at=now() WHERE id=$1",order_id)
        if old!='needs_review':
            await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,$2,'needs_review','Confirmed vendor quantities do not cover every order line')",order_id,old)
        return None
    if order["status"] == "sourcing":
        await connection.execute("UPDATE orders SET status='confirmed',updated_at=now() WHERE id=$1", order_id)
        await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'sourcing','confirmed','All vendor supply confirmed')", order_id)
    if order["delivery_point"] is None:
        return None
    drivers = await connection.fetch("SELECT id FROM platform_users WHERE role='driver' AND is_active ORDER BY created_at,id LIMIT 50")
    if not drivers:
        return None

    destination = await connection.fetchrow(
        "SELECT ST_Y(delivery_point::geometry) latitude,ST_X(delivery_point::geometry) longitude FROM orders WHERE id=$1",
        order_id,
    )
    start = (float(commitments[0]["farm_lat"]), float(commitments[0]["farm_lon"]))
    locations: list[tuple[float, float]] = [start]
    stop_payload: list[dict] = []
    from app.schemas import LocationInput, RoutingProblem, RoutingStopInput, RoutingStopType, VehicleInput

    customer = (float(destination["latitude"]), float(destination["longitude"]))
    for item in commitments:
        farm_point = (float(item["farm_lat"]), float(item["farm_lon"]))
        pickup_index = len(locations); locations.append(farm_point)
        delivery_index = len(locations); locations.append(customer)
        pair_id = uuid4()
        qty = Decimal(item["confirmed_quantity"])
        kg = qty if item["unit"].lower() in {"kg", "kilogram", "kilograms"} else qty
        stop_payload.extend([
            RoutingStopInput(stop_id=uuid4(), order_id=order_id, location_index=pickup_index,
                stop_type=RoutingStopType.PICKUP, pair_id=pair_id, quantity_kg=kg,
                service_minutes=8, window_start_minute=0, window_end_minute=1440),
            RoutingStopInput(stop_id=uuid4(), order_id=order_id, location_index=delivery_index,
                stop_type=RoutingStopType.DELIVERY, pair_id=pair_id, quantity_kg=kg,
                service_minutes=5, window_start_minute=0, window_end_minute=1440,
                max_transit_minutes=min(1440,item["max_transit_minutes"] or 720)),
        ])
    durations, distances, provider = await road_matrices(locations)
    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    vehicles = [VehicleInput(
        vehicle_id=row["id"], start_location_index=0, end_location_index=0,
        capacity_kg=Decimal(os.getenv("DRIVER_VEHICLE_CAPACITY_KG", "500")),
        shift_start_minute=0, shift_end_minute=1440,
        fixed_cost_cents=int(os.getenv("DRIVER_FIXED_COST_CENTS", "0")),
        cost_per_km_cents=int(os.getenv("DRIVER_COST_PER_KM_CENTS", "0")),
        cost_per_minute_cents=int(os.getenv("DRIVER_COST_PER_MINUTE_CENTS", "0")),
    ) for row in drivers]
    problem = RoutingProblem(
        planning_start_at=now, travel_matrix_provider=provider, travel_matrix_generated_at=datetime.now(timezone.utc),
        locations=[LocationInput(latitude=lat, longitude=lon) for lat, lon in locations],
        stops=stop_payload, vehicles=vehicles,
        travel_time_matrix_minutes=durations, distance_matrix_metres=distances,
    )
    payload = PlanningJobCreate(
        delivery_date=date.today(), zone_id=uuid4(), order_ids=[order_id],
        solver_time_limit_seconds=5, problem=problem,
    ).model_dump(mode="json")
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    job_id = uuid4(); key = f"automatic-order-{order_id}"
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    await connection.execute(
        "INSERT INTO planning_jobs(id,status,idempotency_key,request_hash,payload) VALUES($1,'queued',$2,$3,$4::jsonb)",
        job_id, key, digest, serialized,
    )
    await connection.execute("UPDATE orders SET status='planning',planning_job_id=$2,updated_at=now() WHERE id=$1", order_id, job_id)
    await connection.execute(
        "INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,$2,'planning','Vendor supply confirmed; route queued automatically')",
        order_id, order["status"],
    )
    await connection.execute(
        "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('planning_job',$1,'planning.requested',$2::jsonb)",
        job_id, json.dumps({"planning_job_id": str(job_id), "order_id": str(order_id)}),
    )
    return job_id


async def release_order_offers(connection: asyncpg.Connection, order_id: UUID) -> None:
    rows = await connection.fetch("SELECT offer_id,requested_quantity FROM vendor_commitments WHERE order_id=$1 AND offer_id IS NOT NULL AND status IN ('requested','confirmed')", order_id)
    for row in rows:
        await connection.execute(
            "UPDATE produce_offers SET available_quantity=available_quantity+$2,status='available' WHERE id=$1",
            row["offer_id"], row["requested_quantity"],
        )


async def offer_route_to_next_driver(connection: asyncpg.Connection, planning_job_id: UUID, old_vehicle_id: UUID) -> UUID | None:
    """On decline, offer the persisted route to the next active, untried driver."""
    anchor = await connection.fetchrow(
        "SELECT task_id FROM route_stops WHERE planning_job_id=$1 AND vehicle_id=$2 ORDER BY sequence_number LIMIT 1",
        planning_job_id, old_vehicle_id,
    )
    if not anchor:
        return None
    candidates = await connection.fetch(
        """SELECT u.id FROM platform_users u
           WHERE u.role='driver' AND u.is_active
             AND NOT EXISTS(SELECT 1 FROM driver_task_assignments a WHERE a.task_id=$1 AND a.driver_id=u.id)
           ORDER BY (SELECT count(*) FROM driver_task_assignments a2 WHERE a2.driver_id=u.id AND a2.status='accepted'),u.created_at,u.id
           LIMIT 1""",
        anchor['task_id'],
    )
    if not candidates:
        return None
    new_driver=candidates[0]['id']
    job=await connection.fetchrow("SELECT result FROM planning_jobs WHERE id=$1",planning_job_id)
    result=job['result'] if job else None
    if isinstance(result,str): result=json.loads(result)
    route=next((r for r in (result or {}).get('routes',[]) if any(s.get('stop_id')==str(anchor['task_id']) for s in r.get('stops',[]))),None)
    if not route:
        return None
    distance_km=int(route.get('distance_metres',0))/1000
    minutes=int(route.get('drive_minutes',0))+int(route.get('service_minutes',0))
    compensation=min(100_000,int(os.getenv('DRIVER_PAY_BASE_CENTS','3000'))+
                     int(distance_km*int(os.getenv('DRIVER_PAY_PER_KM_CENTS','1000')))+
                     minutes*int(os.getenv('DRIVER_PAY_PER_MINUTE_CENTS','150')))
    quote_id=uuid4(); assignment_id=uuid4(); run_id=uuid4()
    key=f'auto-pay:{planning_job_id}:{new_driver}'
    quote={"task_id":str(anchor['task_id']),"model_version":"automatic-rate-card-v1",
           "offered_compensation_cents":compensation,"offered_compensation_currency":round(compensation/100,2),
           "predicted_acceptance_minutes":15,"target_acceptance_minutes":30,"target_met":True,
           "warning":"Compensation is generated from the configured route rate card; response time is a placeholder until locally calibrated."}
    request_hash=hashlib.sha256(f'{planning_job_id}:{anchor["task_id"]}:{new_driver}:{compensation}'.encode()).hexdigest()
    await connection.execute("""INSERT INTO pay_quotes(id,task_id,idempotency_key,request_hash,model_version,calibration,context,
        offered_compensation_cents,predicted_acceptance_minutes,target_acceptance_minutes,target_met,result)
        VALUES($1,$2,$3,$4,'automatic-rate-card-v1',$5::jsonb,$6::jsonb,$7,15,30,true,$8::jsonb)""",
        quote_id,anchor['task_id'],key,request_hash,json.dumps({"basis":"route rate card","currency":"INR"}),
        json.dumps({"distance_metres":route.get('distance_metres',0),"drive_minutes":route.get('drive_minutes',0)}),compensation,json.dumps(quote))
    allocation={"allocation_run_id":str(run_id),"status":"optimal","assignments":[{"task_id":str(anchor['task_id']),"driver_id":str(new_driver),"pay_quote_id":str(quote_id),"compensation_cents":compensation}]}
    await connection.execute("INSERT INTO allocation_runs(id,idempotency_key,request_hash,status,result) VALUES($1,$2,$3,'optimal',$4::jsonb)",run_id,f'auto-reoffer:{planning_job_id}:{new_driver}',hashlib.sha256(json.dumps(allocation,sort_keys=True).encode()).hexdigest(),json.dumps(allocation))
    await connection.execute("INSERT INTO driver_task_assignments(id,allocation_run_id,task_id,driver_id,pay_quote_id,compensation_cents,predicted_acceptance_minutes,fit_score) VALUES($1,$2,$3,$4,$5,$6,15,100)",assignment_id,run_id,anchor['task_id'],new_driver,quote_id,compensation)
    await connection.execute("UPDATE route_stops SET status='offered',updated_at=now() WHERE planning_job_id=$1 AND vehicle_id=$2",planning_job_id,old_vehicle_id)
    await connection.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('driver_assignment',$1,'driver.assignment_offered',$2::jsonb)",assignment_id,json.dumps({"assignment_id":str(assignment_id),"driver_id":str(new_driver),"planning_job_id":str(planning_job_id)}))
    return assignment_id
