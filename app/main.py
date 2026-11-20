from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, UploadFile, File, Form, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.db import get_connection, open_pool
from app.allocation import solve_fair_allocation
from app.pricing import calculate_pay_quote
from app.schemas import (
    DriverAllocationRequest,
    DriverAssignmentResponse,
    DriverEventCreate,
    OrderCreate,
    ParcelSearch,
    PayQuoteRequest,
    PlanningJobCreate,
    VendorCommitmentCreate,
    VendorCommitmentResponse,
    canonical_order_payload,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_pool = await open_pool()
    try:
        yield
    finally:
        await app.state.db_pool.close()


app = FastAPI(
    title="Fresh Produce Fulfillment API",
    version="0.1.0",
    description=(
        "Operational API for exact farm search, order intake, asynchronous planning, "
        "and delivery events. Route optimization runs outside request handlers."
    ),
    lifespan=lifespan,
)

MAX_PHOTO_BYTES = 8 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}

Connection = Annotated[asyncpg.Connection, Depends(get_connection)]


async def require_api_key(
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    expected = os.environ.get("API_KEY")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY is not configured",
        )
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )


ApiKey = Annotated[None, Depends(require_api_key)]


def json_record(record: asyncpg.Record) -> dict[str, Any]:
    result = dict(record)
    for key, value in result.items():
        if isinstance(value, UUID):
            result[key] = str(value)
        elif hasattr(value, "isoformat"):
            result[key] = value.isoformat()
    return result


@app.get("/health")
async def health(connection: Connection) -> dict[str, str]:
    try:
        await connection.fetchval("SELECT 1")
    except asyncpg.PostgresError as error:
        raise HTTPException(status_code=503, detail="Database unavailable") from error
    return {"status": "ok", "database": "ok"}


@app.get("/v1/products", dependencies=[Depends(require_api_key)])
async def list_products(
    connection: Connection,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    rows = await connection.fetch(
        """
        SELECT id AS product_id, name, category, default_unit, max_transit_minutes
        FROM products
        WHERE status = 'active'
        ORDER BY name, id
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
    )
    return {"items": [json_record(row) for row in rows], "limit": limit, "offset": offset}


@app.get("/v1/farms/search", dependencies=[Depends(require_api_key)])
async def search_farms(
    connection: Connection,
    latitude: Annotated[float, Query(ge=-90, le=90)],
    longitude: Annotated[float, Query(ge=-180, le=180)],
    product_id: UUID,
    quantity: Annotated[float, Query(gt=0)],
    radius_metres: Annotated[int, Query(ge=100, le=100_000)] = 25_000,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    """Return exact-radius matches; geography coordinates are lon/lat in WGS84."""
    rows = await connection.fetch(
        """
        WITH origin AS (
            SELECT ST_SetSRID(ST_MakePoint($1, $2), 4326)::geography AS point
        )
        SELECT f.id AS farm_id, f.name AS farm_name, o.id AS offer_id,
               o.product_id, o.available_quantity, o.unit, o.harvest_date,
               o.quality_grade,
               ST_Distance(f.location, origin.point) AS distance_metres
        FROM farms AS f
        JOIN produce_offers AS o ON o.farm_id = f.id
        CROSS JOIN origin
        WHERE f.status = 'active'
          AND o.status = 'available'
          AND o.product_id = $3
          AND o.available_quantity >= $4
          AND o.harvest_date >= CURRENT_DATE
          AND ST_DWithin(f.location, origin.point, $5)
        ORDER BY distance_metres ASC, f.id ASC
        LIMIT $6 OFFSET $7
        """,
        longitude,
        latitude,
        product_id,
        quantity,
        radius_metres,
        limit,
        offset,
    )
    return {"items": [json_record(row) for row in rows], "limit": limit, "offset": offset}


@app.post("/v1/parcels/search", dependencies=[Depends(require_api_key)])
async def search_parcels(payload: ParcelSearch, connection: Connection) -> dict[str, Any]:
    """Find parcels intersecting a GeoJSON boundary using the GiST index."""
    try:
        rows = await connection.fetch(
            """
            WITH query_shape AS (
                SELECT ST_SetSRID(ST_GeomFromGeoJSON($1), 4326) AS shape
            )
            SELECT p.id AS parcel_id, p.farm_id, p.name,
                   ST_Area(p.shape::geography) AS area_sq_m
            FROM land_parcels AS p
            CROSS JOIN query_shape AS q
            WHERE ST_IsValid(q.shape)
              AND ST_Intersects(p.shape, q.shape)
              AND ($2::double precision IS NULL
                   OR ST_Area(p.shape::geography) >= $2)
            ORDER BY p.id
            LIMIT $3 OFFSET $4
            """,
            json.dumps(payload.geometry),
            payload.minimum_area_sq_m,
            payload.limit,
            payload.offset,
        )
    except asyncpg.PostgresError as error:
        raise HTTPException(status_code=422, detail="Invalid or unsupported GeoJSON geometry") from error
    return {"items": [json_record(row) for row in rows], "limit": payload.limit, "offset": payload.offset}


@app.post("/v1/orders", status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderCreate,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = canonical_order_payload(payload)
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    order_id = uuid4()

    async with connection.transaction():
        inserted = await connection.fetchrow(
            """
            INSERT INTO orders (
                id, customer_id, status, delivery_point, delivery_window,
                idempotency_key, request_hash
            ) VALUES (
                $1, $2, 'received',
                ST_SetSRID(ST_MakePoint($3, $4), 4326)::geography,
                tstzrange($5, $6, '[)'), $7, $8
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, status, created_at
            """,
            order_id,
            payload.customer_id,
            payload.delivery_longitude,
            payload.delivery_latitude,
            payload.delivery_window_start,
            payload.delivery_window_end,
            idempotency_key,
            request_hash,
        )

        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, status, created_at, request_hash FROM orders WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None:
                raise HTTPException(status_code=409, detail="Idempotency conflict; retry request")
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different order payload",
                )
            return {
                "order_id": str(existing["id"]),
                "status": existing["status"],
                "created_at": existing["created_at"].isoformat(),
                "replayed": True,
            }

        for line in payload.lines:
            await connection.execute(
                """
                INSERT INTO order_lines (order_id, product_id, quantity, unit, allow_substitution)
                VALUES ($1, $2, $3, $4, $5)
                """,
                inserted["id"],
                line.product_id,
                line.quantity,
                line.unit,
                line.allow_substitution,
            )

        await connection.execute(
            """
            INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
            VALUES ('order', $1, 'order.received', $2::jsonb)
            """,
            inserted["id"],
            json.dumps({"order_id": str(inserted["id"])}),
        )

    return {
        "order_id": str(inserted["id"]),
        "status": inserted["status"],
        "created_at": inserted["created_at"].isoformat(),
        "replayed": False,
    }


@app.get("/v1/orders/{order_id}", dependencies=[Depends(require_api_key)])
async def get_order(order_id: UUID, connection: Connection) -> dict[str, Any]:
    order = await connection.fetchrow(
        """
        SELECT id, customer_id, status, lower(delivery_window) AS delivery_window_start,
               upper(delivery_window) AS delivery_window_end, created_at
        FROM orders WHERE id = $1
        """,
        order_id,
    )
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    lines = await connection.fetch(
        "SELECT product_id, quantity, unit, allow_substitution FROM order_lines WHERE order_id = $1",
        order_id,
    )
    return {**json_record(order), "lines": [json_record(line) for line in lines]}


@app.post("/v1/operations/vendor-commitments", status_code=status.HTTP_201_CREATED)
async def create_vendor_commitment(
    payload: VendorCommitmentCreate,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    commitment_id = uuid4()
    async with connection.transaction():
        inserted = await connection.fetchrow(
            """
            INSERT INTO vendor_commitments (
                id, farm_id, product_id, delivery_date, requested_quantity,
                unit, ready_by, status, idempotency_key, request_hash
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'requested', $8, $9)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, status, created_at
            """,
            commitment_id,
            payload.farm_id,
            payload.product_id,
            payload.delivery_date,
            payload.requested_quantity,
            payload.unit,
            payload.ready_by,
            idempotency_key,
            request_hash,
        )
        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, status, created_at, request_hash FROM vendor_commitments WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None or existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Commitment idempotency-key conflict")
            inserted = existing
        else:
            await connection.execute(
                """
                INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
                VALUES ('vendor_commitment', $1, 'vendor.commitment_requested', $2::jsonb)
                """,
                inserted["id"],
                json.dumps({"commitment_id": str(inserted["id"])}),
            )
    return {
        "commitment_id": str(inserted["id"]),
        "status": inserted["status"],
        "created_at": inserted["created_at"].isoformat(),
    }


@app.patch("/v1/vendor/commitments/{commitment_id}/response")
async def respond_to_vendor_commitment(
    commitment_id: UUID,
    payload: VendorCommitmentResponse,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = json.dumps(
        {"commitment_id": str(commitment_id), **payload.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    async with connection.transaction():
        existing_event = await connection.fetchrow(
            "SELECT id, aggregate_id, request_hash FROM outbox_events WHERE idempotency_key = $1",
            idempotency_key,
        )
        if existing_event is not None:
            if existing_event["aggregate_id"] != commitment_id or existing_event["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Commitment-response idempotency-key conflict")
            row = await connection.fetchrow(
                "SELECT id, status, confirmed_quantity, confirmed_ready_at AS ready_at FROM vendor_commitments WHERE id = $1",
                commitment_id,
            )
            return json_record(row)

        row = await connection.fetchrow(
            """
            UPDATE vendor_commitments
            SET status = $2, confirmed_quantity = $3, confirmed_ready_at = $4,
                response_note = $5, updated_at = now()
                        WHERE id = $1 AND status = 'requested'
                            AND ($3::numeric IS NULL OR $3 <= requested_quantity)
            RETURNING id, status, confirmed_quantity, confirmed_ready_at AS ready_at
            """,
            commitment_id,
            payload.status.value,
            payload.confirmed_quantity,
            payload.ready_at,
            payload.note,
        )
        if row is None:
            existing_event = await connection.fetchrow(
                "SELECT aggregate_id, request_hash FROM outbox_events WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing_event is not None:
                if (
                    existing_event["aggregate_id"] != commitment_id
                    or existing_event["request_hash"] != request_hash
                ):
                    raise HTTPException(status_code=409, detail="Commitment-response idempotency-key conflict")
                replay = await connection.fetchrow(
                    "SELECT id, status, confirmed_quantity, confirmed_ready_at AS ready_at FROM vendor_commitments WHERE id = $1",
                    commitment_id,
                )
                return json_record(replay)

            existing = await connection.fetchrow(
                "SELECT requested_quantity, status FROM vendor_commitments WHERE id = $1",
                commitment_id,
            )
            if existing is None:
                raise HTTPException(status_code=404, detail="Vendor commitment not found")
            if (
                payload.confirmed_quantity is not None
                and payload.confirmed_quantity > existing["requested_quantity"]
            ):
                raise HTTPException(status_code=422, detail="Confirmed quantity exceeds requested quantity")
            raise HTTPException(status_code=409, detail="Vendor commitment is no longer awaiting response")
        await connection.execute(
            """
            INSERT INTO outbox_events (
                aggregate_type, aggregate_id, event_type, payload, idempotency_key, request_hash
            ) VALUES ('vendor_commitment', $1, 'vendor.commitment_responded', $2::jsonb, $3, $4)
            """,
            commitment_id,
            json.dumps({"commitment_id": str(commitment_id), "status": payload.status.value}),
            idempotency_key,
            request_hash,
        )
    return json_record(row)


@app.post("/v1/operations/planning-jobs", status_code=status.HTTP_202_ACCEPTED)
async def create_planning_job(
    payload: PlanningJobCreate,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    job_id = uuid4()
    job_payload = payload.model_dump(mode="json")
    serialized_payload = json.dumps(job_payload, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()
    async with connection.transaction():
        inserted = await connection.fetchrow(
            """
            INSERT INTO planning_jobs (id, status, idempotency_key, request_hash, payload)
            VALUES ($1, 'queued', $2, $3, $4::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, status, created_at
            """,
            job_id,
            idempotency_key,
            request_hash,
            serialized_payload,
        )
        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, status, created_at, request_hash FROM planning_jobs WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None:
                raise HTTPException(status_code=409, detail="Planning-job idempotency conflict")
            if existing["request_hash"] != request_hash:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key was already used with a different planning request",
                )
            inserted = existing
        else:
            await connection.execute(
                """
                INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
                VALUES ('planning_job', $1, 'planning.requested', $2::jsonb)
                """,
                inserted["id"],
                json.dumps({"planning_job_id": str(inserted["id"])}),
            )
    return {
        "planning_job_id": str(inserted["id"]),
        "status": inserted["status"],
        "created_at": inserted["created_at"].isoformat(),
        "status_url": f"/v1/operations/planning-jobs/{inserted['id']}",
    }


@app.post("/v1/operations/pay-quotes", status_code=status.HTTP_201_CREATED)
async def create_pay_quote(
    payload: PayQuoteRequest,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    quote_id = uuid4()
    quote_result = calculate_pay_quote(payload)
    quote_result["pay_quote_id"] = str(quote_id)

    async with connection.transaction():
        existing = await connection.fetchrow(
            "SELECT id, request_hash, result FROM pay_quotes WHERE idempotency_key = $1",
            idempotency_key,
        )
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Pay-quote Idempotency-Key conflict")
            result = existing["result"]
            if isinstance(result, str):
                result = json.loads(result)
            return {**result, "replayed": True}

        task_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM route_stops WHERE task_id = $1 AND status = 'planned')",
            payload.task_id,
        )
        if not task_exists:
            raise HTTPException(status_code=404, detail="Planned route task not found")

        inserted = await connection.fetchrow(
            """
            INSERT INTO pay_quotes (
                id, task_id, idempotency_key, request_hash, model_version,
                calibration, context, offered_compensation_cents,
                predicted_acceptance_minutes, target_acceptance_minutes,
                target_met, result
            ) VALUES (
                $1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10, $11, $12::jsonb
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            quote_id,
            payload.task_id,
            idempotency_key,
            request_hash,
            payload.calibration.model_version,
            json.dumps(payload.calibration.model_dump(mode="json")),
            json.dumps({
                "density_index": payload.density_index,
                "unattended_share": payload.unattended_share,
                "is_evening": payload.is_evening,
                "target_acceptance_minutes": payload.target_acceptance_minutes,
            }),
            quote_result["offered_compensation_cents"],
            quote_result["predicted_acceptance_minutes"],
            payload.target_acceptance_minutes,
            quote_result["target_met"],
            json.dumps(quote_result),
        )
        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, request_hash, result FROM pay_quotes WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None or existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Pay-quote Idempotency-Key conflict")
            result = existing["result"]
            if isinstance(result, str):
                result = json.loads(result)
            return {**result, "replayed": True}

        await connection.execute(
            """
            INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
            VALUES ('pay_quote', $1, 'driver.pay_quote_created', $2::jsonb)
            """,
            quote_id,
            json.dumps({"pay_quote_id": str(quote_id), "task_id": str(payload.task_id)}),
        )
    return {**quote_result, "replayed": False}


@app.post("/v1/operations/driver-allocations", status_code=status.HTTP_201_CREATED)
async def create_driver_allocation(
    payload: DriverAllocationRequest,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    async with connection.transaction():
        existing_run = await connection.fetchrow(
            "SELECT id, request_hash, result FROM allocation_runs WHERE idempotency_key = $1",
            idempotency_key,
        )
        if existing_run is not None:
            if existing_run["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Allocation Idempotency-Key conflict")
            existing_result = existing_run["result"]
            if isinstance(existing_result, str):
                existing_result = json.loads(existing_result)
            return {**existing_result, "allocation_run_id": str(existing_run["id"]), "replayed": True}

    task_ids = [task.task_id for task in payload.tasks]
    known_tasks = await connection.fetch(
        "SELECT task_id FROM route_stops WHERE task_id = ANY($1::uuid[]) AND status = 'planned'",
        task_ids,
    )
    if {row["task_id"] for row in known_tasks} != set(task_ids):
        raise HTTPException(status_code=409, detail="All allocation tasks must be unassigned planned route stops")

    quote_ids = list({candidate.pay_quote_id for candidate in payload.candidates})
    quote_rows = await connection.fetch(
        """
        SELECT id, task_id, offered_compensation_cents, predicted_acceptance_minutes, target_met
        FROM pay_quotes WHERE id = ANY($1::uuid[])
        """,
        quote_ids,
    )
    quote_by_id = {row["id"]: dict(row) for row in quote_rows}
    if set(quote_by_id) != set(quote_ids):
        raise HTTPException(status_code=422, detail="Every candidate must reference a stored pay quote")
    task_by_id = {task.task_id: task for task in payload.tasks}
    for candidate in payload.candidates:
        quote = quote_by_id[candidate.pay_quote_id]
        if quote["task_id"] != candidate.task_id:
            raise HTTPException(status_code=422, detail="Pay quote does not belong to candidate task")
        if quote["offered_compensation_cents"] > task_by_id[candidate.task_id].compensation_budget_cents:
            raise HTTPException(status_code=422, detail="Pay quote exceeds task compensation budget")
        if not quote["target_met"] and not task_by_id[candidate.task_id].allow_target_miss:
            continue

    history_rows = await connection.fetch(
        """
        SELECT driver_id, count(*)::integer AS accepted_count
        FROM driver_task_assignments
        WHERE driver_id = ANY($1::uuid[])
          AND status = 'accepted'
          AND created_at >= now() - interval '7 days'
        GROUP BY driver_id
        """,
        [driver.driver_id for driver in payload.drivers],
    )
    recent_load = {row["driver_id"]: row["accepted_count"] for row in history_rows}
    result = await asyncio.to_thread(solve_fair_allocation, payload, quote_by_id, recent_load)
    run_id = uuid4()
    result["allocation_run_id"] = str(run_id)
    for assignment in result["assignments"]:
        assignment["assignment_id"] = str(uuid4())
    async with connection.transaction():
        inserted = await connection.fetchrow(
            """
            INSERT INTO allocation_runs (id, idempotency_key, request_hash, status, result)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            run_id,
            idempotency_key,
            request_hash,
            result["status"],
            json.dumps(result),
        )
        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, request_hash, result FROM allocation_runs WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is None or existing["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Allocation Idempotency-Key conflict")
            prior_result = existing["result"]
            if isinstance(prior_result, str):
                prior_result = json.loads(prior_result)
            return {**prior_result, "allocation_run_id": str(existing["id"]), "replayed": True}

        for assignment in result["assignments"]:
            task_id = UUID(assignment["task_id"])
            await connection.execute(
                """
                INSERT INTO driver_task_assignments (
                    id, allocation_run_id, task_id, driver_id, pay_quote_id,
                    compensation_cents, predicted_acceptance_minutes, fit_score
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                UUID(assignment["assignment_id"]), run_id, task_id, UUID(assignment["driver_id"]),
                UUID(assignment["pay_quote_id"]), assignment["compensation_cents"],
                assignment["predicted_acceptance_minutes"], assignment["fit_score"],
            )
            updated = await connection.execute(
                "UPDATE route_stops SET status = 'offered', updated_at = now() WHERE task_id = $1 AND status = 'planned'",
                task_id,
            )
            if updated != "UPDATE 1":
                raise HTTPException(status_code=409, detail="A route task was allocated concurrently; retry with a new key")
        await connection.execute(
            """
            INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
            VALUES ('allocation_run', $1, 'driver.allocation_created', $2::jsonb)
            """,
            run_id,
            json.dumps({
                "allocation_run_id": str(run_id),
                "assigned_count": result["assigned_count"],
                "status": result["status"],
            }),
        )
    return {**result, "replayed": False}


@app.patch("/v1/driver/assignments/{assignment_id}/response")
async def respond_to_driver_assignment(
    assignment_id: UUID,
    payload: DriverAssignmentResponse,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    serialized = json.dumps(
        {"assignment_id": str(assignment_id), **payload.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
    )
    request_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    async with connection.transaction():
        existing_event = await connection.fetchrow(
            "SELECT aggregate_id, request_hash FROM outbox_events WHERE idempotency_key = $1",
            idempotency_key,
        )
        if existing_event is not None:
            if existing_event["aggregate_id"] != assignment_id or existing_event["request_hash"] != request_hash:
                raise HTTPException(status_code=409, detail="Assignment-response Idempotency-Key conflict")
            row = await connection.fetchrow(
                "SELECT id, task_id, driver_id, status FROM driver_task_assignments WHERE id = $1",
                assignment_id,
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Driver assignment not found")
            return {**json_record(row), "replayed": True}

        row = await connection.fetchrow(
            """
            UPDATE driver_task_assignments
            SET status = $2
            WHERE id = $1 AND status = 'offered'
            RETURNING id, task_id, driver_id, status
            """,
            assignment_id,
            payload.status.value,
        )
        if row is None:
            exists = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM driver_task_assignments WHERE id = $1)",
                assignment_id,
            )
            if not exists:
                raise HTTPException(status_code=404, detail="Driver assignment not found")
            raise HTTPException(status_code=409, detail="Assignment has already been answered")
        await connection.execute(
            "UPDATE route_stops SET status = $2, updated_at = now() WHERE task_id = $1 AND status = 'offered'",
            row["task_id"],
            "assigned" if payload.status.value == "accepted" else "planned",
        )
        await connection.execute(
            """
            INSERT INTO outbox_events (
                aggregate_type, aggregate_id, event_type, payload, idempotency_key, request_hash
            ) VALUES ('driver_assignment', $1, $2, $3::jsonb, $4, $5)
            """,
            assignment_id,
            f"driver.assignment_{payload.status.value}",
            json.dumps({
                "assignment_id": str(assignment_id),
                "driver_id": str(row["driver_id"]),
                "status": payload.status.value,
                "note": payload.note,
            }),
            idempotency_key,
            request_hash,
        )
    return {**json_record(row), "replayed": False}


@app.get("/v1/operations/driver-allocations/{allocation_run_id}", dependencies=[Depends(require_api_key)])
async def get_driver_allocation(
    allocation_run_id: UUID,
    connection: Connection,
) -> dict[str, Any]:
    row = await connection.fetchrow(
        "SELECT id, status, result, created_at FROM allocation_runs WHERE id = $1",
        allocation_run_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Allocation run not found")
    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    return {**result, "allocation_run_id": str(row["id"]), "status": row["status"], "created_at": row["created_at"].isoformat()}


@app.get("/v1/operations/planning-jobs/{job_id}", dependencies=[Depends(require_api_key)])
async def get_planning_job(job_id: UUID, connection: Connection) -> dict[str, Any]:
    row = await connection.fetchrow(
        "SELECT id, status, payload, result, error, created_at, updated_at FROM planning_jobs WHERE id = $1",
        job_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Planning job not found")
    result = json_record(row)
    for field in ("payload", "result"):
        if isinstance(result[field], str):
            result[field] = json.loads(result[field])
    return result


@app.post("/v1/driver/stops/{stop_id}/events", status_code=status.HTTP_201_CREATED)
async def create_driver_event(
    stop_id: UUID,
    payload: DriverEventCreate,
    connection: Connection,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
    _: ApiKey,
) -> dict[str, Any]:
    event_id = uuid4()
    event_payload = {
        "route_stop_id": str(stop_id),
        **payload.model_dump(mode="json"),
    }
    serialized_payload = json.dumps(event_payload, sort_keys=True, separators=(",", ":"))
    request_hash = hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()
    async with connection.transaction():
        stop_exists = await connection.fetchval(
            "SELECT EXISTS (SELECT 1 FROM route_stops WHERE id = $1)", stop_id
        )
        if not stop_exists:
            raise HTTPException(status_code=404, detail="Route stop not found")

        inserted = await connection.fetchrow(
            """
            INSERT INTO delivery_events (
                id, route_stop_id, idempotency_key, event_type, occurred_at,
                location, note, request_hash
            ) VALUES (
                $1, $2, $3, $4, $5,
                CASE WHEN $6::double precision IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint($6, $7), 4326)::geography END,
                $8, $9
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id, event_type, occurred_at
            """,
            event_id,
            stop_id,
            idempotency_key,
            payload.event_type.value,
            payload.occurred_at,
            payload.longitude,
            payload.latitude,
            payload.note,
            request_hash,
        )
        if inserted is None:
            existing = await connection.fetchrow(
                "SELECT id, route_stop_id, event_type, occurred_at, request_hash FROM delivery_events WHERE idempotency_key = $1",
                idempotency_key,
            )
            if (
                existing is None
                or existing["route_stop_id"] != stop_id
                or existing["request_hash"] != request_hash
            ):
                raise HTTPException(status_code=409, detail="Idempotency-Key conflict")
            inserted = existing
        else:
            await connection.execute(
                "UPDATE route_stops SET status = $2, updated_at = now() WHERE id = $1",
                stop_id,
                payload.event_type.value,
            )
            if payload.event_type.value == "delivered":
                order_id = await connection.fetchval("SELECT order_id FROM route_stops WHERE id=$1", stop_id)
                if order_id is not None:
                    remaining = await connection.fetchval("SELECT count(*) FROM route_stops WHERE order_id=$1 AND stop_type='delivery' AND status <> 'delivered'", order_id)
                    if remaining == 0:
                        previous = await connection.fetchval("UPDATE orders SET status='delivered', updated_at=now() WHERE id=$1 AND status='dispatched' RETURNING status", order_id)
                        if previous:
                            await connection.execute("INSERT INTO order_status_events(order_id,old_status,new_status,note) VALUES($1,'dispatched','delivered','All delivery stops completed')", order_id)
                            await connection.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,'order.delivered',$2::jsonb)", order_id, json.dumps({"order_id":str(order_id)}))
            await connection.execute(
                """
                INSERT INTO outbox_events (aggregate_type, aggregate_id, event_type, payload)
                VALUES ('route_stop', $1, 'delivery.event_recorded', $2::jsonb)
                """,
                stop_id,
                json.dumps({"event_id": str(inserted["id"]), "event_type": payload.event_type.value}),
            )

    return {
        "event_id": str(inserted["id"]),
        "route_stop_id": str(stop_id),
        "event_type": inserted["event_type"],
        "occurred_at": inserted["occurred_at"].isoformat(),
        "replayed": inserted["id"] != event_id,
    }


class QualityCheckInput(BaseModel):
    grade: str = Field(min_length=1, max_length=40)
    passed: bool
    inspector: str = Field(min_length=1, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)


class OrderStatusInput(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=500)


@app.post("/v1/operations/simulations", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_key)])
async def generate_simulation(connection: Connection,
    farms: Annotated[int, Query(ge=1, le=100)] = 10,
    offers_per_farm: Annotated[int, Query(ge=1, le=20)] = 4,
    seed: Annotated[int, Query(ge=0, le=2_147_483_647)] = 42,
) -> dict[str, Any]:
    """Create repeatable local/demo farms and offers from the active product catalog."""
    async with connection.transaction():
        farm_rows = await connection.fetch("""
            INSERT INTO farms (name, location)
            SELECT 'Demo Farm ' || $1 || '-' || n,
                   ST_SetSRID(ST_MakePoint(77.0 + ((n * 7919 + $2) % 1000) / 10000.0,
                                           28.0 + ((n * 3571 + $2) % 1000) / 10000.0), 4326)::geography
            FROM generate_series(1, $3) AS n RETURNING id
        """, seed, seed, farms)
        farm_ids = [r['id'] for r in farm_rows]
        product_ids = await connection.fetch("SELECT id, default_unit FROM products WHERE status='active' ORDER BY id")
        if not product_ids:
            raise HTTPException(status_code=409, detail="Add active products before generating demo offers")
        await connection.execute("""
            INSERT INTO produce_offers (farm_id, product_id, available_quantity, unit, harvest_date, ready_at, quality_grade)
            SELECT f.id, p.id, 20 + ((g * 37 + $1) % 181), p.default_unit,
                   CURRENT_DATE + ((g * 13 + $1) % 7)::integer,
                   now() + (((g * 17 + $1) % 48)::text || ' hours')::interval,
                   (ARRAY['A','B','A','A','B'])[((g + $1) % 5) + 1]
            FROM unnest($2::uuid[]) WITH ORDINALITY AS f(id, fi)
            CROSS JOIN generate_series(1, $3) AS g
            CROSS JOIN LATERAL (SELECT id, default_unit FROM products WHERE status='active'
                ORDER BY id OFFSET ((f.fi + g + $1) % (SELECT count(*) FROM products WHERE status='active')) LIMIT 1) p
        """, seed, farm_ids, offers_per_farm)
    return {"farm_count": len(farm_ids), "offer_count": len(farm_ids) * offers_per_farm, "seed": seed}


@app.patch("/v1/operations/orders/{order_id}/status", dependencies=[Depends(require_api_key)])
async def transition_order(order_id: UUID, payload: OrderStatusInput, connection: Connection) -> dict[str, Any]:
    allowed = {"received": {"sourcing", "cancelled", "needs_review"},
               "sourcing": {"confirmed", "cancelled", "needs_review"},
               "confirmed": {"planning", "cancelled", "needs_review"},
               "planning": {"dispatched", "cancelled", "needs_review"},
               "dispatched": {"delivered", "needs_review"},
               "needs_review": {"sourcing", "confirmed", "planning", "cancelled"}}
    async with connection.transaction():
        row = await connection.fetchrow("SELECT status FROM orders WHERE id=$1 FOR UPDATE", order_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found")
        if payload.status not in allowed.get(row['status'], set()):
            raise HTTPException(status_code=409, detail=f"Invalid order transition: {row['status']} -> {payload.status}")
        await connection.execute("UPDATE orders SET status=$2, updated_at=now() WHERE id=$1", order_id, payload.status)
        await connection.execute("INSERT INTO order_status_events(order_id, old_status, new_status, note) VALUES($1,$2,$3,$4)", order_id, row['status'], payload.status, payload.note)
        await connection.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('order',$1,$2,$3::jsonb)", order_id, 'order.' + payload.status, json.dumps({"order_id":str(order_id),"from":row['status'],"to":payload.status,"note":payload.note}))
    return {"order_id": str(order_id), "previous_status": row['status'], "status": payload.status}


@app.post("/v1/operations/produce-offers/{offer_id}/quality-checks", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_key)])
async def create_quality_check(offer_id: UUID, payload: QualityCheckInput, connection: Connection) -> dict[str, Any]:
    async with connection.transaction():
        exists = await connection.fetchval("SELECT EXISTS(SELECT 1 FROM produce_offers WHERE id=$1)", offer_id)
        if not exists:
            raise HTTPException(status_code=404, detail="Produce offer not found")
        row = await connection.fetchrow("INSERT INTO quality_checks(offer_id,grade,passed,inspector,notes) VALUES($1,$2,$3,$4,$5) RETURNING id, created_at", offer_id, payload.grade, payload.passed, payload.inspector, payload.notes)
        if not payload.passed:
            await connection.execute("UPDATE produce_offers SET status='unavailable' WHERE id=$1", offer_id)
        await connection.execute("INSERT INTO outbox_events(aggregate_type,aggregate_id,event_type,payload) VALUES('produce_offer',$1,$2,$3::jsonb)", offer_id, 'produce.quality_checked', json.dumps({"offer_id":str(offer_id),"passed":payload.passed,"grade":payload.grade}))
    return {"quality_check_id":str(row['id']), "offer_id":str(offer_id), "passed":payload.passed, "created_at":row['created_at'].isoformat()}


@app.post("/v1/farms/{farm_id}/photos", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_api_key)])
async def upload_farm_photo(farm_id: UUID, connection: Connection, file: UploadFile = File(...), caption: str = Form(default="")) -> dict[str, Any]:
    if file.content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(status_code=415, detail="Upload JPEG, PNG, or WebP")
    content = await file.read(MAX_PHOTO_BYTES + 1)
    if not content or len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(status_code=413, detail="Photo must be between 1 byte and 8 MiB")
    if not await connection.fetchval("SELECT EXISTS(SELECT 1 FROM farms WHERE id=$1)", farm_id):
        raise HTTPException(status_code=404, detail="Farm not found")
    row = await connection.fetchrow("INSERT INTO farm_photos(farm_id,content_type,caption,data) VALUES($1,$2,$3,$4) RETURNING id,created_at", farm_id, file.content_type, caption[:240], content)
    return {"photo_id":str(row['id']),"farm_id":str(farm_id),"content_type":file.content_type,"bytes":len(content),"url":f"/v1/farms/{farm_id}/photos/{row['id']}","created_at":row['created_at'].isoformat()}


@app.get("/v1/farms/{farm_id}/photos/{photo_id}", dependencies=[Depends(require_api_key)])
async def get_farm_photo(farm_id: UUID, photo_id: UUID, connection: Connection) -> Response:
    row = await connection.fetchrow("SELECT content_type,data FROM farm_photos WHERE id=$1 AND farm_id=$2", photo_id, farm_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    return Response(content=bytes(row['data']), media_type=row['content_type'], headers={"Cache-Control":"private, max-age=300"})


@app.get("/v1/operations/insights", dependencies=[Depends(require_api_key)])
async def operations_insights(connection: Connection, days: Annotated[int, Query(ge=7, le=90)] = 28) -> dict[str, Any]:
    """Demand baseline and fulfillment/supply snapshot; forecasts use trailing weekday averages."""
    demand = await connection.fetch("""
        WITH daily AS (SELECT ol.product_id,ol.unit,date_trunc('day',o.created_at)::date day,sum(ol.quantity) qty
              FROM orders o JOIN order_lines ol ON ol.order_id=o.id
              WHERE o.created_at >= now()-($1::text || ' days')::interval AND o.status <> 'cancelled'
              GROUP BY ol.product_id,ol.unit,date_trunc('day',o.created_at)::date),
        pairs AS (SELECT DISTINCT product_id,unit FROM daily),
        calendar AS (SELECT generate_series(CURRENT_DATE-$1::integer+1,CURRENT_DATE,interval '1 day')::date day)
        SELECT p.id product_id,p.name product_name,pair.unit,
               round(avg(coalesce(d.qty,0))::numeric,3) AS forecast_daily_quantity,
               round(stddev_pop(coalesce(d.qty,0))::numeric,3) AS daily_variability,
               count(*)::int AS observed_days
        FROM pairs pair CROSS JOIN calendar c JOIN products p ON p.id=pair.product_id
        LEFT JOIN daily d ON d.product_id=pair.product_id AND d.unit=pair.unit AND d.day=c.day
        GROUP BY p.id,p.name,pair.unit ORDER BY forecast_daily_quantity DESC
    """, days)
    orders = await connection.fetch("SELECT status,count(*)::int count FROM orders WHERE created_at >= now()-($1::text || ' days')::interval GROUP BY status ORDER BY status", days)
    supply = await connection.fetch("SELECT status,count(*)::int count,coalesce(sum(confirmed_quantity),0)::numeric confirmed_quantity FROM vendor_commitments WHERE delivery_date BETWEEN CURRENT_DATE AND CURRENT_DATE+7 GROUP BY status ORDER BY status")
    routes = await connection.fetch("SELECT status,count(*)::int stop_count,coalesce(sum(arrival_minute),0)::int arrival_minute_total FROM route_stops WHERE updated_at >= now()-interval '7 days' GROUP BY status ORDER BY status")
    return {"window_days":days,"forecast_method":"mean daily ordered quantity over trailing window (baseline)","demand_forecast":[json_record(r) for r in demand],"orders_by_status":[json_record(r) for r in orders],"vendor_supply_next_7_days":[json_record(r) for r in supply],"route_execution_last_7_days":[json_record(r) for r in routes]}
