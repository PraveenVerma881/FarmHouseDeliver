from __future__ import annotations

import argparse
import asyncio
import json
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from app.db import open_pool
from app.optimizer import solve_routing_problem
from app.schemas import PlanningJobCreate

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOGGER = logging.getLogger("fresh_produce_planner")


async def claim_job(pool: asyncpg.Pool) -> tuple[UUID, dict[str, Any]] | None:
    async with pool.acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                UPDATE planning_jobs
                SET status = 'queued', updated_at = now()
                WHERE status = 'running' AND updated_at < now() - interval '10 minutes'
                """
            )
            job = await connection.fetchrow(
                """
                SELECT id, payload
                FROM planning_jobs
                WHERE status = 'queued'
                ORDER BY created_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            if job is None:
                return None
            await connection.execute(
                "UPDATE planning_jobs SET status = 'running', error = NULL, updated_at = now() WHERE id = $1",
                job["id"],
            )
    payload = job["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return job["id"], payload


async def persist_result(
    pool: asyncpg.Pool,
    job_id: UUID,
    request: PlanningJobCreate,
    result: dict[str, Any],
) -> None:
    async with pool.acquire() as connection:
        async with connection.transaction():
            if result["status"] == "succeeded":
                await connection.execute("DELETE FROM route_stops WHERE planning_job_id = $1", job_id)
                stops_by_id = {str(stop.stop_id): stop for stop in request.problem.stops}
                locations = request.problem.locations
                for route in result["routes"]:
                    vehicle_id = UUID(route["vehicle_id"])
                    for stop_result in route["stops"]:
                        stop = stops_by_id[stop_result["stop_id"]]
                        location = locations[stop.location_index]
                        await connection.execute(
                            """
                            INSERT INTO route_stops (
                                planning_job_id, order_id, vehicle_id, task_id,
                                stop_type, sequence_number, status, location,
                                arrival_minute, updated_at
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, 'planned',
                                ST_SetSRID(ST_MakePoint($7, $8), 4326)::geography,
                                $9, now()
                            )
                            """,
                            job_id,
                            stop.order_id,
                            vehicle_id,
                            stop.stop_id,
                            stop.stop_type.value,
                            stop_result["sequence_number"],
                            location.longitude,
                            location.latitude,
                            stop_result["arrival_minute"],
                        )

                # Every route is assigned to the driver selected as its vehicle.
                # A transparent rate card provides the first pay offer; operators
                # can calibrate these environment rates for their market.
                automatic_assignments = []
                for route in result["routes"]:
                    route_stops = route.get("stops", [])
                    if not route_stops:
                        continue
                    driver_id = UUID(route["vehicle_id"])
                    driver_active = await connection.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM platform_users WHERE id=$1 AND role='driver' AND is_active)",
                        driver_id,
                    )
                    if not driver_active:
                        continue
                    distance_km = int(route.get("distance_metres", 0)) / 1000
                    minutes = int(route.get("drive_minutes", 0)) + int(route.get("service_minutes", 0))
                    compensation = min(100_000, int(os.getenv("DRIVER_PAY_BASE_CENTS", "3000"))
                                       + int(distance_km * int(os.getenv("DRIVER_PAY_PER_KM_CENTS", "1000")))
                                       + minutes * int(os.getenv("DRIVER_PAY_PER_MINUTE_CENTS", "150")))
                    task_id = UUID(route_stops[0]["stop_id"])
                    quote_id = uuid4()
                    assignment_id = uuid4()
                    quote = {
                        "task_id": str(task_id), "model_version": "automatic-rate-card-v1",
                        "offered_compensation_cents": compensation,
                        "offered_compensation_currency": round(compensation / 100, 2),
                        "predicted_acceptance_minutes": 15, "target_acceptance_minutes": 30,
                        "target_met": True, "warning": "Compensation is generated from the configured route rate card; response time is a placeholder until locally calibrated.",
                    }
                    quote_input = json.dumps({"job_id": str(job_id), "task_id": str(task_id), "driver_id": str(driver_id), "compensation": compensation}, sort_keys=True)
                    quote_key = f"auto-pay:{job_id}:{driver_id}"
                    if not automatic_assignments:
                        await connection.execute(
                            "INSERT INTO allocation_runs(id,idempotency_key,request_hash,status,result) VALUES($1,$2,$3,'optimal',$4::jsonb)",
                            job_id, f"auto-allocation:{job_id}", hashlib.sha256(str(job_id).encode()).hexdigest(), json.dumps({"status":"optimal","assignments":[]} ),
                        )
                    await connection.execute(
                        """INSERT INTO pay_quotes(id,task_id,idempotency_key,request_hash,model_version,calibration,context,
                           offered_compensation_cents,predicted_acceptance_minutes,target_acceptance_minutes,target_met,result)
                           VALUES($1,$2,$3,$4,'automatic-rate-card-v1',$5::jsonb,$6::jsonb,$7,15,30,true,$8::jsonb)""",
                        quote_id, task_id, quote_key, hashlib.sha256(quote_input.encode()).hexdigest(),
                        json.dumps({"basis": "route rate card", "currency": "INR"}),
                        json.dumps({"distance_metres": route.get("distance_metres", 0), "drive_minutes": route.get("drive_minutes", 0), "service_minutes": route.get("service_minutes", 0)}),
                        compensation, json.dumps(quote),
                    )
                    automatic_assignments.append({
                        "assignment_id": str(assignment_id), "task_id": str(task_id),
                        "driver_id": str(driver_id), "pay_quote_id": str(quote_id),
                        "compensation_cents": compensation,
                    })
                    await connection.execute(
                        """INSERT INTO driver_task_assignments(id,allocation_run_id,task_id,driver_id,pay_quote_id,
                           compensation_cents,predicted_acceptance_minutes,fit_score)
                           VALUES($1,$2,$3,$4,$5,$6,15,100)""",
                        assignment_id, job_id, task_id, driver_id, quote_id, compensation,
                    )
                    await connection.execute(
                        "UPDATE route_stops SET status='offered',updated_at=now() WHERE planning_job_id=$1 AND vehicle_id=$2",
                        job_id, driver_id,
                    )
                    route_orders={UUID(stop['order_id']) for stop in route_stops if stop.get('order_id')}
                    wait_minutes=max(0,int((request.problem.planning_start_at-datetime.now(timezone.utc)).total_seconds()/60))
                    eta=wait_minutes+int(route.get('drive_minutes',0))+int(route.get('service_minutes',0))
                    for order_id in route_orders:
                        item_quote=await connection.fetchrow("""SELECT coalesce(sum(vc.confirmed_quantity*o.unit_price_paise),0)::bigint item_total,
                            coalesce(bool_and(o.unit_price_paise>0),false) all_priced
                            FROM vendor_commitments vc JOIN produce_offers o ON o.id=vc.offer_id
                            WHERE vc.order_id=$1 AND vc.status='confirmed'""",order_id)
                        distance=int(route.get('distance_metres',0))
                        delivery_fee=int(os.getenv('DELIVERY_BASE_FEE_PAISE','3000'))+((distance*int(os.getenv('DELIVERY_PER_KM_PAISE','500'))+999)//1000)
                        total=int(item_quote['item_total'])+delivery_fee if item_quote['all_priced'] else None
                        await connection.execute("UPDATE orders SET estimated_total_paise=$4,estimated_delivery_minutes=$2,estimated_route_distance_m=$3,updated_at=now() WHERE id=$1",order_id,eta,distance,total)
                if automatic_assignments:
                    allocation_payload = {"allocation_run_id": str(job_id), "status": "optimal", "assignments": automatic_assignments}
                    await connection.execute(
                        "UPDATE allocation_runs SET result=$2::jsonb WHERE id=$1",
                        job_id, json.dumps(allocation_payload),
                    )
                order_ids = {stop.order_id for stop in request.problem.stops if stop.order_id is not None}
                if not automatic_assignments:
                    for order_id in order_ids:
                        old=await connection.fetchval("UPDATE orders SET status='needs_review',updated_at=now() WHERE id=$1 AND status='planning' RETURNING status", order_id)
                        if old:
                            await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'planning','needs_review','No active driver could be assigned to the route')",order_id)
                elif order_ids:
                    for order_id in order_ids:
                        await connection.execute(
                            "INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.driver_offers_created',$2::jsonb)",
                            order_id, json.dumps({"order_id": str(order_id), "assignment_count": len(automatic_assignments)}),
                        )
            else:
                order_ids = {stop.order_id for stop in request.problem.stops if stop.order_id is not None}
                for order_id in order_ids:
                    old=await connection.fetchval("UPDATE orders SET status='needs_review',updated_at=now() WHERE id=$1 AND status='planning' RETURNING status", order_id)
                    if old:
                        await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'planning','needs_review',$2)",order_id,f"Automatic route planning {result['status']}: {result.get('message','')[:400]}")

            await connection.execute(
                """
                UPDATE planning_jobs
                SET status = $2, result = $3::jsonb, error = $4, updated_at = now()
                WHERE id = $1
                """,
                job_id,
                result["status"],
                json.dumps(result),
                result.get("message"),
            )
            await connection.execute(
                """
                INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
                VALUES ('planning_job', $1, $2, $3::jsonb)
                """,
                job_id,
                f"planning.{result['status']}",
                json.dumps({"planning_job_id": str(job_id), "status": result["status"]}),
            )


async def process_one_job(pool: asyncpg.Pool) -> bool:
    claimed = await claim_job(pool)
    if claimed is None:
        return False
    job_id, payload = claimed
    try:
        request = PlanningJobCreate.model_validate(payload)
        result = await asyncio.to_thread(
            solve_routing_problem,
            request.problem,
            request.solver_time_limit_seconds,
        )
        await persist_result(pool, job_id, request, result)
        LOGGER.info("Planning job %s finished with status %s", job_id, result["status"])
    except Exception as error:
        LOGGER.exception("Planning job %s failed", job_id)
        async with pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "UPDATE planning_jobs SET status = 'failed', error = $2, updated_at = now() WHERE id = $1",
                    job_id,
                    str(error)[:1000],
                )
                order_ids=payload.get("order_ids", []) if isinstance(payload,dict) else []
                for raw_order_id in order_ids:
                    try: order_id=UUID(str(raw_order_id))
                    except (TypeError,ValueError): continue
                    old=await connection.fetchval("UPDATE orders SET status='needs_review',updated_at=now() WHERE id=$1 AND status='planning' RETURNING status",order_id)
                    if old:
                        await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'planning','needs_review',$2)",order_id,f"Automatic route planning failed: {str(error)[:400]}")
                await connection.execute(
                    """
                    INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
                    VALUES ('planning_job', $1, 'planning.failed', $2::jsonb)
                    """,
                    job_id,
                    json.dumps({"planning_job_id": str(job_id), "status": "failed"}),
                )
    return True


async def run_worker(once: bool, poll_interval_seconds: float) -> None:
    pool = await open_pool()
    try:
        while True:
            processed = await process_one_job(pool)
            if once:
                return
            if not processed:
                await asyncio.sleep(poll_interval_seconds)
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Process queued fresh-produce route planning jobs")
    parser.add_argument("--once", action="store_true", help="process at most one queued job")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    asyncio.run(run_worker(args.once, max(0.1, args.poll_seconds)))


if __name__ == "__main__":
    main()
