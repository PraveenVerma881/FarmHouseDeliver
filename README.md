# Fresh Produce Fulfillment API

FastAPI MVP for the two uploaded design briefs. PostgreSQL/PostGIS is the
transactional source of truth; request handlers stay short and enqueue durable
events for background work.

## Included

- Exact nearby-farm and produce-offer search using `ST_DWithin` on indexed
  `geography(Point, 4326)` values. Request coordinates are latitude/longitude;
  PostGIS point construction deliberately uses longitude first.
- Parcel intersection search using GeoJSON polygons and the parcel GiST index.
- Operations-created vendor commitments and idempotent vendor confirmations,
  including shortfalls and readiness times.
- Idempotent order intake. Order, lines, and `order.received` outbox event are
  committed in one database transaction. Availability is not decremented at
  checkout; vendor commitments belong to the sourcing workflow.
- Asynchronous planning-job submission and status lookup. The API persists a
  validated routing problem and `planning.requested` outbox event; the separate
  OR-Tools worker solves without blocking the HTTP request.
- Context-aware pay quotes for already planned route tasks, using an explicitly
  supplied locally calibrated acceptance-response model.
- CP-SAT driver-to-task matching that first maximizes covered tasks, then trades
  driver fit, quoted compensation, predicted response time, and recent accepted
  workload balance.
- Idempotent driver accept/decline responses, persisted so accepted work informs
  future allocation fairness.
- Idempotent driver events with an event history and current stop status.
- A shared API key for local/prototype access. Replace it with customer, vendor,
  driver, and operations identity/role authorization before public deployment.

## Run locally

1. Create a PostgreSQL database with PostGIS installed.
2. Apply the schema: `psql "$DATABASE_URL" -f schema.sql`.
3. Create and activate a virtual environment, then install
  `pip install -r requirements.txt`.
4. Set `DATABASE_URL` and `API_KEY` from `.env.example` in the shell.
5. Apply `migrations/001_route_solver.sql` if upgrading a database created from
  an earlier version of this MVP.
6. Apply `migrations/002_fair_pricing_allocation.sql` to add pay-quote and
  driver-allocation audit tables.
7. Apply `migrations/003_platform_operations.sql` for quality, farm photos, and
   order lifecycle history.
8. From this directory run `uvicorn app.main:app --reload`.
9. In a separate process, run `python -m app.worker` to process planning jobs.
10. Open `/docs` for the interactive API contract; `/health` checks the database.

## API outline

- `GET /v1/farms/search`: exact radius query filtered by product and quantity.
- `GET /v1/products`: paginated active product catalog.
- `POST /v1/parcels/search`: exact parcel intersection search by GeoJSON boundary.
- `POST /v1/orders`: accepts `Idempotency-Key`, writes an order, and returns
  `received`; repeated identical requests replay the original order, while key
  reuse with a different body returns `409`.
- `GET /v1/orders/{order_id}`: retrieve status and order lines.
- `PATCH /v1/operations/orders/{order_id}/status`: validate and audit operational
  lifecycle changes; delivered driver stops complete their order automatically.
- `POST /v1/operations/simulations`: generate bounded, seeded demo farms and
  produce offers from the active product catalog.
- `POST /v1/operations/produce-offers/{offer_id}/quality-checks`: record a
  quality inspection; failed offers are removed from availability.
- `POST /v1/farms/{farm_id}/photos` and `GET /v1/farms/{farm_id}/photos/{photo_id}`:
  upload and retrieve JPEG, PNG, or WebP farm photographs (8 MiB maximum).
- `GET /v1/operations/insights`: trailing-window demand baseline and variability,
  order status, upcoming vendor commitments, and recent route execution counts.
- `POST /v1/operations/planning-jobs`: accepts a bounded dispatch batch and
  returns `202` plus a status URL. The request includes stops, pickup-delivery
  pairs, vehicle shifts/capacities/cost weights, and provider-produced road
  travel-time/distance matrices.
- `GET /v1/operations/planning-jobs/{job_id}`: poll job state/result.
- `POST /v1/operations/pay-quotes`: calculate and persist a task compensation
  quote using a caller-supplied calibrated ART model and operational context.
- `POST /v1/operations/driver-allocations`: match planned tasks to available
  driver candidates using stored pay quotes and recent accepted-task history.
- `GET /v1/operations/driver-allocations/{allocation_run_id}`: retrieve the
  persisted allocation result.
- `PATCH /v1/driver/assignments/{assignment_id}/response`: accept or decline an
  offered assignment; acceptance is counted in the following seven-day fairness
  history.
- `POST /v1/operations/vendor-commitments`: send a requested quantity and ready-by time.
- `PATCH /v1/vendor/commitments/{commitment_id}/response`: confirm quantity/readiness or report shortfall/rejection.
- `POST /v1/driver/stops/{stop_id}/events`: append an idempotent driver event.

All endpoints except `/health` require `X-API-Key`. Pagination and result limits
are enforced on spatial search.

The insights demand number is a trailing daily mean with zero-order days included;
it is a baseline for evaluation, not a seasonal or promotion-aware forecast. Photo
bytes are stored in PostgreSQL for this MVP. For larger production photo volumes,
move image bytes to object storage and keep metadata in PostgreSQL. Redis is not
introduced: order/supply reads must remain authoritative and these operational
summaries are not yet shown to be hot enough to justify invalidation complexity.

## Research-paper design choices

- Manu: use the transactional outbox pattern to publish order, vendor, and
  planning events after database commits. This borrows a useful decoupling idea;
  it does not implement Manu's vector database system.
- FINGER and Fast-Convergent Proximity Graphs: these optimize approximate
  nearest-neighbor search over embedding vectors. They do not preserve the exact
  radius, polygon, inventory, and delivery constraints this workflow requires,
  so they are not used in the operational API.
- HARMONY: its distributed vector-search partitioning is not needed for this
  relational/geospatial workload. Revisit only if a measured embedding-search
  feature becomes a real requirement.

For route construction, use a vehicle-routing solver with pickup/delivery,
capacity, time-window, readiness, shift, and freshness constraints. The
ANN research papers do not solve that routing problem.

## Fair pricing and driver allocation

`POST /v1/operations/pay-quotes` takes a planned route task, a compensation
range, target acceptance-response time (ART), normalized task context, and a
versioned model calibration. The predictor uses the study-inspired form
`log(ART) = intercept + pay + pay^2 + context + pay x context + pay^2 x context`
for density, unattended-share, and evening context. It tests bounded pay steps
and selects the lowest tested offer whose predicted ART meets the target. If
none does, it returns the configured maximum with `target_met: false`. That
quote is never treated as a guaranteed driver response.

The paper reports empirical relationships and example effects; its coefficients
are not universal. The API therefore requires locally trained/calibrated
coefficients and their model version, region, and training date instead of
shipping invented defaults. Density must use the exact normalization used to
train that calibration. For production, persist model registry records and
restrict who can publish a calibration.

`POST /v1/operations/driver-allocations` takes tasks and compensation budgets,
drivers and per-round capacity, and eligible task-driver edges with suitability
scores and pay-quote IDs. The service verifies that tasks are still planned,
quotes belong to those tasks, and quotes fit budgets. CP-SAT uses a bounded
single-worker solve. Its objective lexicographically prioritizes assigning the
maximum number of feasible tasks, then considers suitability, pay cost,
predicted ART, and a convex penalty on recent accepted-task load. The history is
queried from the database for the previous seven days; clients cannot choose
their own fairness history. An unmet ART target is not allocated by default;
the task must explicitly set `allow_target_miss` to opt in.

The resulting `allocation_run`, assignment rows, route-stop `offered` statuses,
and outbox event are committed together. Drivers answer with the response
endpoint: accepted assignments change the route stop to `assigned`; declined
ones return it to `planned` for another round. Only `accepted` records count in
the future workload-balancing history. Each assignment response includes its
`assignment_id`, required by the driver's response endpoint. The fairness score is an operational
opportunity-balancing proxy, not a formal envy-free guarantee from the
unit-demand market paper.

The allocation request runs synchronously but has a 1--10 second CP-SAT limit
(default 3 seconds); `optimal` means CP-SAT proved the optimum for this encoded
objective, while a time-limited feasible result is labeled
`feasible_time_limited`. Database commands still use the 8-second asyncpg
timeout. Historical accepted counts are a proxy: if the product later needs
fair opportunity independent of acceptance, track offered exposure too.

Parcel boundaries are stored as validated WGS84 multipolygons in this MVP;
area is calculated with PostGIS `geography` in square metres. If local editing
or topology operations require a projected CRS, select it for the operating
region and transform at the database boundary rather than guessing an SRID.

## Planning worker and optimization model

The worker claims `queued` jobs with `FOR UPDATE SKIP LOCKED`, runs OR-Tools
guided local search with a bounded time limit, then persists route stops and the
objective breakdown. It enforces vehicle capacity, pickup-before-delivery,
vehicle shifts, stop windows, and each delivery pair's maximum transit/freshness
limit. All submitted stops are mandatory in this version; an infeasible batch is
reported as `infeasible` for operations to split, re-source, or review.
Jobs left in `running` for over ten minutes are returned to the queue after a
worker crash; the configured solve limit is at most two minutes.

The request must include square travel-time (minutes) and distance (metres)
matrices for its indexed locations, plus a provider name and timezone-aware
matrix-generation/planning timestamps. Generate matrices from the selected road
routing provider; this worker never substitutes straight-line distance for road
cost. Minute offsets are relative to `planning_start_at`. Cost is configured
per vehicle as fixed cents, cents per kilometre, and cents per paid minute,
including time-window waiting; the
response labels the objective in cents. Guided local search returns the best
feasible solution found before the time limit, not a proof of global optimality.
Saving route stops, updating job status, and writing `planning.succeeded`,
`planning.infeasible`, or `planning.failed` to the outbox happen transactionally.

The API/worker boundary is usable now, but this MVP expects the operations
planner to assemble confirmed orders, vendor pickups, vehicle data, and road
matrices into the planning-job payload. The automated sourcing-to-route data
assembler and maps-provider adapter remain integration work.

Farm-coordinate lookup is exact geospatial filtering, not vector ANN. The
vehicle-routing solver addresses the actual route problem; the FINGER and
proximity-graph papers do not optimize delivery routing.

## Before production

Add OIDC role-based authorization, migrations, rate limits, request tracing,
outbox publisher/worker processes, audit policy, retention controls, provider
timeouts/circuit breakers, backups/restore tests, and operational dashboards.
Tune indexes from representative `EXPLAIN (ANALYZE, BUFFERS)` results.
