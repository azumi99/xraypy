ALTER TABLE cv.auth_users DROP CONSTRAINT auth_users_role_check;
ALTER TABLE cv.auth_users ADD CONSTRAINT auth_users_role_check
CHECK (role IN ('operator', 'bc', 'label_print', 'admin'));
ALTER TABLE cv.auth_users ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE cv.auth_users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;
