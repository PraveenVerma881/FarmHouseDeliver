CREATE TABLE IF NOT EXISTS order_status_events (
    id bigserial PRIMARY KEY,
    order_id uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    old_status text NOT NULL,
    new_status text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS order_status_events_order_idx ON order_status_events(order_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quality_checks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    offer_id uuid NOT NULL REFERENCES produce_offers(id) ON DELETE CASCADE,
    grade text NOT NULL,
    passed boolean NOT NULL,
    inspector text NOT NULL,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quality_checks_offer_idx ON quality_checks(offer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS farm_photos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    farm_id uuid NOT NULL REFERENCES farms(id) ON DELETE CASCADE,
    content_type text NOT NULL CHECK(content_type IN ('image/jpeg','image/png','image/webp')),
    caption varchar(240) NOT NULL DEFAULT '',
    data bytea NOT NULL CHECK(octet_length(data) BETWEEN 1 AND 8388608),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS farm_photos_farm_idx ON farm_photos(farm_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orders_status_created_idx ON orders(status, created_at DESC);
CREATE INDEX IF NOT EXISTS vendor_commitments_delivery_status_idx ON vendor_commitments(delivery_date, status);
