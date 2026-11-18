-- Existing records belong to BGD. Identity-related CAS tables are separate
-- so searches, review records and batch/track uniqueness cannot cross sites.
ALTER TABLE cv.awb RENAME TO awb_bgd;
ALTER TABLE cv.label_print RENAME TO label_print_bgd;

CREATE TABLE cv.parcel_identity_cas (LIKE cv.parcel_identity INCLUDING ALL);
ALTER TABLE cv.parcel_identity_cas ADD FOREIGN KEY (duplicate_of) REFERENCES cv.parcel_identity_cas(id);
CREATE TABLE cv.parcel_embedding_cas (LIKE cv.parcel_embedding INCLUDING ALL);
ALTER TABLE cv.parcel_embedding_cas ADD FOREIGN KEY (identity_id) REFERENCES cv.parcel_identity_cas(id) ON DELETE CASCADE;
CREATE TABLE cv.review_audit_cas (LIKE cv.review_audit INCLUDING ALL);
ALTER TABLE cv.review_audit_cas ADD FOREIGN KEY (identity_id) REFERENCES cv.parcel_identity_cas(id);
CREATE TABLE cv.session_cas (LIKE cv.session INCLUDING ALL);
CREATE TABLE cv.awb_cas (LIKE cv.awb_bgd INCLUDING ALL);
ALTER TABLE cv.awb_cas ADD FOREIGN KEY (parcel_id) REFERENCES cv.parcel_identity_cas(id);
CREATE TABLE cv.label_print_cas (LIKE cv.label_print_bgd INCLUDING ALL);
