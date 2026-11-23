CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS farms (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    address text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'closed')),
    location geography(Point, 4326) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS farms_location_gist ON farms USING gist (location);
CREATE INDEX IF NOT EXISTS farms_status_idx ON farms (status);

CREATE TABLE IF NOT EXISTS products (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE,
    category text NOT NULL,
    default_unit text NOT NULL,
    max_transit_minutes integer CHECK (max_transit_minutes > 0),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'inactive')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS products_status_name_idx ON products (status, name);

CREATE TABLE IF NOT EXISTS produce_offers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id uuid NOT NULL REFERENCES farms(id),
    product_id uuid NOT NULL REFERENCES products(id),
    available_quantity numeric(12, 3) NOT NULL CHECK (available_quantity >= 0),
    unit text NOT NULL,
    harvest_date date NOT NULL,
    ready_at timestamptz,
    unit_price_paise integer NOT NULL DEFAULT 0 CHECK (unit_price_paise >= 0),
    quality_grade text,
    status text NOT NULL DEFAULT 'available'
        CHECK (status IN ('available', 'committed', 'unavailable')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS produce_offers_product_date_idx
    ON produce_offers (product_id, harvest_date, status);
CREATE INDEX IF NOT EXISTS produce_offers_farm_idx ON produce_offers (farm_id);

CREATE TABLE IF NOT EXISTS vendor_commitments (
    id uuid PRIMARY KEY,
    farm_id uuid NOT NULL REFERENCES farms(id),
    product_id uuid NOT NULL REFERENCES products(id),
    delivery_date date NOT NULL,
    requested_quantity numeric(12, 3) NOT NULL CHECK (requested_quantity > 0),
    confirmed_quantity numeric(12, 3) CHECK (confirmed_quantity >= 0),
    unit text NOT NULL,
    ready_by timestamptz NOT NULL,
    confirmed_ready_at timestamptz,
    response_note text,
    status text NOT NULL CHECK (status IN ('requested', 'confirmed', 'shortfall', 'rejected', 'cancelled')),
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (confirmed_quantity IS NULL OR confirmed_quantity <= requested_quantity)
);
CREATE INDEX IF NOT EXISTS vendor_commitments_farm_status_idx
    ON vendor_commitments (farm_id, status, delivery_date);

CREATE TABLE IF NOT EXISTS land_parcels (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id uuid NOT NULL REFERENCES farms(id),
    name text NOT NULL,
    shape geometry(MultiPolygon, 4326) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (ST_IsValid(shape))
);
CREATE INDEX IF NOT EXISTS land_parcels_shape_gist ON land_parcels USING gist (shape);
CREATE INDEX IF NOT EXISTS land_parcels_farm_idx ON land_parcels (farm_id);

CREATE TABLE IF NOT EXISTS orders (
    id uuid PRIMARY KEY,
    customer_id uuid NOT NULL,
    status text NOT NULL CHECK (status IN (
        'received', 'sourcing', 'confirmed', 'planning', 'dispatched',
        'delivered', 'cancelled', 'needs_review'
    )),
    delivery_point geography(Point, 4326),
    delivery_address text NOT NULL DEFAULT '',
    estimated_total_paise bigint,
    estimated_delivery_minutes integer,
    estimated_route_distance_m integer,
    delivery_window tstzrange,
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orders_customer_created_idx ON orders (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orders_status_created_idx ON orders (status, created_at);
CREATE INDEX IF NOT EXISTS orders_delivery_window_gist ON orders USING gist (delivery_window);

CREATE TABLE IF NOT EXISTS order_lines (
    id bigserial PRIMARY KEY,
    order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id uuid NOT NULL REFERENCES products(id),
    quantity numeric(12, 3) NOT NULL CHECK (quantity > 0),
    unit text NOT NULL,
    allow_substitution boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS order_lines_order_idx ON order_lines (order_id);
ALTER TABLE vendor_commitments ADD COLUMN IF NOT EXISTS order_id uuid REFERENCES orders(id) ON DELETE SET NULL;
ALTER TABLE vendor_commitments ADD COLUMN IF NOT EXISTS order_line_id bigint REFERENCES order_lines(id) ON DELETE SET NULL;
ALTER TABLE vendor_commitments ADD COLUMN IF NOT EXISTS offer_id uuid REFERENCES produce_offers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS vendor_commitments_order_idx ON vendor_commitments(order_id, status);

CREATE TABLE IF NOT EXISTS planning_jobs (
    id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'infeasible', 'failed', 'cancelled'
    )),
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    payload jsonb NOT NULL,
    result jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS planning_jobs_queue_idx ON planning_jobs (created_at)
    WHERE status = 'queued';
ALTER TABLE orders ADD COLUMN IF NOT EXISTS planning_job_id uuid REFERENCES planning_jobs(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS route_stops (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    planning_job_id uuid NOT NULL REFERENCES planning_jobs(id),
    order_id uuid REFERENCES orders(id),
    vehicle_id uuid NOT NULL,
    task_id uuid NOT NULL,
    stop_type text NOT NULL CHECK (stop_type IN ('pickup', 'delivery', 'depot')),
    sequence_number integer NOT NULL CHECK (sequence_number >= 0),
    status text NOT NULL DEFAULT 'planned',
    location geography(Point, 4326) NOT NULL,
    service_window tstzrange,
    arrival_minute integer NOT NULL CHECK (arrival_minute >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (planning_job_id, vehicle_id, sequence_number),
    UNIQUE (planning_job_id, task_id)
);

CREATE TABLE IF NOT EXISTS delivery_events (
    id uuid PRIMARY KEY,
    route_stop_id uuid NOT NULL REFERENCES route_stops(id),
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'arrived', 'picked_up', 'delivered', 'failed', 'quality_rejected'
    )),
    occurred_at timestamptz NOT NULL,
    location geography(Point, 4326),
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS delivery_events_stop_idx ON delivery_events (route_stop_id, occurred_at);

CREATE TABLE IF NOT EXISTS outbox_events (
    id bigserial PRIMARY KEY,
    aggregate_type text NOT NULL,
    aggregate_id uuid NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    idempotency_key text UNIQUE,
    request_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS outbox_unpublished_idx ON outbox_events (id)
    WHERE published_at IS NULL;

CREATE TABLE IF NOT EXISTS pay_quotes (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    model_version text NOT NULL,
    calibration jsonb NOT NULL,
    context jsonb NOT NULL,
    offered_compensation_cents integer NOT NULL CHECK (offered_compensation_cents >= 0),
    predicted_acceptance_minutes double precision NOT NULL CHECK (predicted_acceptance_minutes > 0),
    target_acceptance_minutes double precision NOT NULL CHECK (target_acceptance_minutes > 0),
    target_met boolean NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pay_quotes_task_created_idx ON pay_quotes (task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS allocation_runs (
    id uuid PRIMARY KEY,
    idempotency_key text NOT NULL UNIQUE,
    request_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('optimal', 'feasible_time_limited', 'failed')),
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS driver_task_assignments (
    id uuid PRIMARY KEY,
    allocation_run_id uuid NOT NULL REFERENCES allocation_runs(id),
    task_id uuid NOT NULL,
    driver_id uuid NOT NULL,
    pay_quote_id uuid NOT NULL REFERENCES pay_quotes(id),
    compensation_cents integer NOT NULL CHECK (compensation_cents >= 0),
    predicted_acceptance_minutes double precision NOT NULL CHECK (predicted_acceptance_minutes > 0),
    fit_score integer NOT NULL CHECK (fit_score BETWEEN 0 AND 100),
    status text NOT NULL DEFAULT 'offered' CHECK (status IN ('offered', 'accepted', 'declined', 'expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (allocation_run_id, task_id)
);
CREATE INDEX IF NOT EXISTS driver_task_assignments_driver_idx
    ON driver_task_assignments (driver_id, created_at DESC);
