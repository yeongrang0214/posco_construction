PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source_filename TEXT NOT NULL,
  source_object_key TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'reviewing',
  warning TEXT NOT NULL DEFAULT '',
  kcs_snapshot TEXT NOT NULL DEFAULT '',
  kcs_revision TEXT NOT NULL DEFAULT '',
  kcs_scope TEXT NOT NULL DEFAULT 'all',
  archived_at TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_uploaded_at ON projects(uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status, archived_at);

CREATE TABLE IF NOT EXISTS clauses (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source_order INTEGER NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL,
  outline_level INTEGER,
  content TEXT NOT NULL,
  edited_content TEXT NOT NULL,
  decision TEXT,
  decision_reason TEXT NOT NULL DEFAULT '',
  coverage_confirmed INTEGER NOT NULL DEFAULT 0,
  selected_candidate_id TEXT,
  review_note TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_clauses_project_order ON clauses(project_id, source_order);
CREATE INDEX IF NOT EXISTS idx_clauses_project_decision ON clauses(project_id, decision);

CREATE TABLE IF NOT EXISTS kcs_documents (
  id TEXT PRIMARY KEY,
  kcs_code TEXT NOT NULL,
  full_code TEXT NOT NULL DEFAULT '',
  document_name TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  update_date TEXT NOT NULL DEFAULT '',
  parent_names TEXT NOT NULL DEFAULT '',
  synced_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_kcs_documents_code ON kcs_documents(kcs_code);

CREATE TABLE IF NOT EXISTS kcs_sections (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES kcs_documents(id) ON DELETE CASCADE,
  kcs_code TEXT NOT NULL,
  section_order INTEGER NOT NULL,
  kcs_clause TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  search_text TEXT NOT NULL,
  version TEXT NOT NULL DEFAULT '',
  update_date TEXT NOT NULL DEFAULT '',
  document_name TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_kcs_sections_code ON kcs_sections(kcs_code);
CREATE INDEX IF NOT EXISTS idx_kcs_sections_document ON kcs_sections(document_id, section_order);

CREATE TABLE IF NOT EXISTS project_candidates (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  clause_id TEXT NOT NULL REFERENCES clauses(id) ON DELETE CASCADE,
  section_id TEXT NOT NULL REFERENCES kcs_sections(id) ON DELETE CASCADE,
  rank INTEGER NOT NULL,
  score REAL NOT NULL,
  reasons_json TEXT NOT NULL DEFAULT '[]',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  excluded INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_clause ON project_candidates(clause_id, excluded, rank);

CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
