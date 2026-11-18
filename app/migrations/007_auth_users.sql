CREATE TABLE IF NOT EXISTS cv.auth_users (
    email TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    access TEXT NOT NULL CHECK (access IN ('cas', 'bgd'))
);
