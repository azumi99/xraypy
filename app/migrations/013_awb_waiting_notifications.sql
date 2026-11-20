CREATE OR REPLACE FUNCTION cv.notify_awb_waiting_changed()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status = 'WAITING' THEN
            PERFORM pg_notify('cv_awb_waiting_changed', TG_ARGV[0]);
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.status = 'WAITING' THEN
            PERFORM pg_notify('cv_awb_waiting_changed', TG_ARGV[0]);
        END IF;
    ELSIF OLD.status = 'WAITING' OR NEW.status = 'WAITING' THEN
        PERFORM pg_notify('cv_awb_waiting_changed', TG_ARGV[0]);
    END IF;
    RETURN NULL;
END;
$$;

CREATE TRIGGER awb_waiting_changed
AFTER INSERT OR UPDATE OR DELETE ON cv.awb_bgd
FOR EACH ROW EXECUTE FUNCTION cv.notify_awb_waiting_changed('bgd');

CREATE TRIGGER awb_waiting_changed
AFTER INSERT OR UPDATE OR DELETE ON cv.awb_cas
FOR EACH ROW EXECUTE FUNCTION cv.notify_awb_waiting_changed('cas');
