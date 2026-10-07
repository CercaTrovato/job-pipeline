CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  region TEXT NOT NULL CHECK (region IN ('CN','HK')),
  platform_id TEXT,
  company TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT,
  url TEXT,
  jd_text TEXT NOT NULL,
  jd_lang TEXT,
  salary_raw TEXT,
  posted_at TEXT,
  fetched_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  raw_json TEXT,
  fingerprint TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'fetched',
  prescore REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_sources (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  source TEXT NOT NULL,
  platform_id TEXT,
  url TEXT,
  PRIMARY KEY (job_id, source, platform_id)
);

CREATE TABLE IF NOT EXISTS analyses (
  job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
  input_hash TEXT NOT NULL,
  payload TEXT NOT NULL,          -- analyze output.json 原文
  llm_backend TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matches (
  job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
  input_hash TEXT NOT NULL,
  hard_pass INTEGER NOT NULL,
  hard_fail_reasons TEXT NOT NULL, -- JSON 数组
  payload TEXT NOT NULL,           -- match output.json 原文
  soft_score REAL NOT NULL,
  fixable INTEGER NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  decision TEXT NOT NULL CHECK (decision IN ('apply','skip','later')),
  note TEXT,
  decided_at TEXT NOT NULL,
  later_until TEXT
);

CREATE TABLE IF NOT EXISTS duplicate_reviews (
  review_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  related_job_id TEXT NOT NULL REFERENCES jobs(job_id),
  action TEXT NOT NULL CHECK (action IN ('keep_both','mark_duplicate')),
  note TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_duplicate_reviews_pair ON duplicate_reviews(job_id, related_job_id);

CREATE TABLE IF NOT EXISTS applications (
  app_id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(job_id),
  channel TEXT NOT NULL,
  resume_variant TEXT,
  resume_pdf_paths TEXT,           -- JSON 数组
  intro_text TEXT,
  status TEXT NOT NULL DEFAULT 'submitted',
  submitted_at TEXT,
  last_event_at TEXT,
  next_followup_at TEXT,
  contact TEXT,
  jd_snapshot TEXT,
  notion_page_id TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  command TEXT NOT NULL,
  source TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  pages_done INTEGER DEFAULT 0,
  pages_total INTEGER DEFAULT 0,
  jobs_new INTEGER DEFAULT 0,
  jobs_seen INTEGER DEFAULT 0,
  packets_done INTEGER DEFAULT 0,
  packets_total INTEGER DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'running',
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS cooldowns (
  source TEXT PRIMARY KEY,         -- boss / linkedin / jobsdb / nowcoder / watchlist
  until TEXT NOT NULL,             -- ISO 本地时间，到期前 fetch 拒绝该来源
  reason TEXT,
  created_at TEXT NOT NULL
);
