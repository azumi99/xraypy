ALTER TABLE cv.auth_users
ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'operator'
CHECK (role IN ('operator', 'bc'));
