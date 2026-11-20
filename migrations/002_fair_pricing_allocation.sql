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
