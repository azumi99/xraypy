DO $$
DECLARE
    item record;
BEGIN
    FOR item IN
        SELECT conrelid::regclass AS label_table, conname
        FROM pg_constraint
        WHERE conrelid IN ('cv.label_print_cas'::regclass, 'cv.label_print_bgd'::regclass)
          AND contype = 'u'
          AND pg_get_constraintdef(oid) = 'UNIQUE (mawb, hawb, part)'
    LOOP
        EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', item.label_table, item.conname);
    END LOOP;
END $$;

CREATE UNIQUE INDEX label_print_cas_pjt_unique
    ON cv.label_print_cas (mawb, hawb, part, COALESCE(pjt_code, ''));
CREATE UNIQUE INDEX label_print_bgd_pjt_unique
    ON cv.label_print_bgd (mawb, hawb, part, COALESCE(pjt_code, ''));
