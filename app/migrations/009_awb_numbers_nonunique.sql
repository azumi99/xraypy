DO $$
DECLARE item record;
BEGIN
    FOR item IN
        SELECT t.relname AS table_name, c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1]
        WHERE n.nspname = 'cv' AND t.relname IN ('awb_bgd', 'awb_cas')
          AND c.contype = 'u' AND cardinality(c.conkey) = 1
          AND a.attname IN ('awb_number', 'hawb_number')
    LOOP
        EXECUTE format('ALTER TABLE cv.%I DROP CONSTRAINT %I', item.table_name, item.conname);
    END LOOP;
    FOR item IN
        SELECT ns.nspname, idx.relname
        FROM pg_index i
        JOIN pg_class t ON t.oid = i.indrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_namespace ns ON ns.oid = idx.relnamespace
        JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0]
        WHERE n.nspname = 'cv' AND t.relname IN ('awb_bgd', 'awb_cas')
          AND i.indisunique AND NOT i.indisprimary AND i.indnkeyatts = 1
          AND a.attname IN ('awb_number', 'hawb_number')
          AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
    LOOP
        EXECUTE format('DROP INDEX %I.%I', item.nspname, item.relname);
    END LOOP;
END $$;
