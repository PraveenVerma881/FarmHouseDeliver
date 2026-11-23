CREATE TABLE IF NOT EXISTS platform_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name text NOT NULL,
    email text NOT NULL UNIQUE,
    phone text NOT NULL UNIQUE,
    role text NOT NULL CHECK (role IN ('customer','farmer','vendor','driver','operations')),
    farm_id uuid REFERENCES farms(id) ON DELETE SET NULL,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS login_challenges (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
    code_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_challenges_user_created_idx ON login_challenges(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS platform_users_role_idx ON platform_users(role, is_active);
ALTER TABLE platform_users DROP CONSTRAINT IF EXISTS platform_users_role_check;
ALTER TABLE platform_users ADD CONSTRAINT platform_users_role_check
    CHECK (role IN ('customer','farmer','vendor','driver','operations'));
