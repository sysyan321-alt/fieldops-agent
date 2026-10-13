CREATE TABLE IF NOT EXISTS device_readings (
  reading_id VARCHAR(64) PRIMARY KEY,
  device_id    VARCHAR(64) NOT NULL,
  site_id     VARCHAR(64) NOT NULL,
  device_type  VARCHAR(50) NOT NULL,
  metric_name  VARCHAR(50) NOT NULL,
  reading_value VARCHAR(50) NOT NULL,
  reading_unit  VARCHAR(20) DEFAULT '',
  is_abnormal   BOOLEAN DEFAULT FALSE,
  recorded_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (device_id) REFERENCES devices(device_id),
  FOREIGN KEY (site_id) REFERENCES sites(site_id)
);
CREATE INDEX IF NOT EXISTS idx_dr_site_time ON device_readings(site_id, recorded_at DESC);
