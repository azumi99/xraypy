CREATE TABLE cv.xray_device (
    warehouse TEXT PRIMARY KEY CHECK (warehouse IN ('cas', 'bgd')),
    config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object')
);
