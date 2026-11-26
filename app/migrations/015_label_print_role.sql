ALTER TABLE cv.auth_users DROP CONSTRAINT auth_users_role_check;
ALTER TABLE cv.auth_users ADD CONSTRAINT auth_users_role_check
CHECK (role IN ('operator', 'bc', 'label_print'));
