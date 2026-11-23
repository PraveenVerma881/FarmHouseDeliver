ALTER TABLE platform_users DROP CONSTRAINT IF EXISTS platform_users_role_check;
ALTER TABLE platform_users ADD CONSTRAINT platform_users_role_check
    CHECK (role IN ('customer','farmer','vendor','driver','operations'));
