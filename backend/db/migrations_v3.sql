CREATE TABLE IF NOT EXISTS device_readings (
  reading_id VARCHAR(64) PRIMARY KEY,
  device_id    VARCHAR(64) NOT NULL,
  elder_id     VARCHAR(64) NOT NULL,
  device_type  VARCHAR(50) NOT NULL,
  metric_name  VARCHAR(50) NOT NULL,
  reading_value VARCHAR(50) NOT NULL,
  reading_unit  VARCHAR(20) DEFAULT '',
  is_abnormal   BOOLEAN DEFAULT FALSE,
  recorded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (device_id) REFERENCES devices(device_id),
  FOREIGN KEY (elder_id) REFERENCES elders(elder_id)
);
CREATE INDEX IF NOT EXISTS idx_dr_elder_time ON device_readings(elder_id, recorded_at DESC);
