ALTER TABLE cv.auth_users ADD COLUMN pjt_code TEXT;
ALTER TABLE cv.auth_users DROP CONSTRAINT auth_users_role_check;
ALTER TABLE cv.auth_users ADD CONSTRAINT auth_users_role_check
CHECK (
    (role IN ('operator', 'bc', 'label_print', 'admin') AND pjt_code IS NULL)
    OR (role = 'pjt_label_print' AND pjt_code IS NOT NULL)
);
ALTER TABLE cv.label_print_cas ADD COLUMN pjt_code TEXT;
ALTER TABLE cv.label_print_bgd ADD COLUMN pjt_code TEXT;

CREATE INDEX label_print_cas_pjt_idx ON cv.label_print_cas (pjt_code, created_at DESC, id DESC);
CREATE INDEX label_print_bgd_pjt_idx ON cv.label_print_bgd (pjt_code, created_at DESC, id DESC);
