from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from typing import Any
from uuid import UUID

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