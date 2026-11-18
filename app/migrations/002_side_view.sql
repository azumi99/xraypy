-- 002_side_view.sql — second X-ray point-of-view (tampak samping)

ALTER TABLE cv.parcel_identity ADD COLUMN IF NOT EXISTS crop_ref_side TEXT;
