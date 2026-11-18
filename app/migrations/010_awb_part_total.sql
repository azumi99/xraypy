-- Legacy rows deliberately remain NULL: their piece numbers are unknown.
ALTER TABLE cv.awb_bgd ADD COLUMN IF NOT EXISTS part INTEGER;
ALTER TABLE cv.awb_bgd ADD COLUMN IF NOT EXISTS total INTEGER;
ALTER TABLE cv.awb_cas ADD COLUMN IF NOT EXISTS part INTEGER;
ALTER TABLE cv.awb_cas ADD COLUMN IF NOT EXISTS total INTEGER;
