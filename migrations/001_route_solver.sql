ALTER TABLE route_stops ADD COLUMN IF NOT EXISTS vehicle_id uuid;
ALTER TABLE route_stops ADD COLUMN IF NOT EXISTS task_id uuid;
ALTER TABLE route_stops ADD COLUMN IF NOT EXISTS arrival_minute integer;

UPDATE route_stops SET vehicle_id = gen_random_uuid() WHERE vehicle_id IS NULL;
UPDATE route_stops SET task_id = gen_random_uuid() WHERE task_id IS NULL;
UPDATE route_stops SET arrival_minute = 0 WHERE arrival_minute IS NULL;

ALTER TABLE route_stops ALTER COLUMN vehicle_id SET NOT NULL;
ALTER TABLE route_stops ALTER COLUMN task_id SET NOT NULL;
ALTER TABLE route_stops ALTER COLUMN arrival_minute SET NOT NULL;
ALTER TABLE route_stops DROP CONSTRAINT IF EXISTS route_stops_planning_job_id_sequence_number_key;
ALTER TABLE route_stops ADD CONSTRAINT route_stops_job_vehicle_sequence_key
    UNIQUE (planning_job_id, vehicle_id, sequence_number);
ALTER TABLE route_stops ADD CONSTRAINT route_stops_job_task_key
    UNIQUE (planning_job_id, task_id);
ALTER TABLE route_stops ADD CONSTRAINT route_stops_arrival_minute_check
    CHECK (arrival_minute >= 0);
