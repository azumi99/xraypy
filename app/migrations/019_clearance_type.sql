CREATE TABLE cv.clearance_type (
    warehouse TEXT NOT NULL CHECK (warehouse IN ('cas', 'bgd')),
    mawb TEXT NOT NULL,
    hawb TEXT NOT NULL,
    clearance_type TEXT NOT NULL,
    PRIMARY KEY (warehouse, mawb, hawb)
);
