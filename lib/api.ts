export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL || 'http://127.0.0.1:8000';

export type Decision = 'keep' | 'delete' | 'hold' | null;
export type ProjectReviewStatus = 'reviewing' | 'submitted' | 'changes_requested' | 'approved';
export type ReviewSubmissionStatus = 'submitted' | 'changes_requested' | 'approved' | 'superseded';

export interface ReviewSubmission {
  id: string;
  project_id?: string;
  revision_no: number;
  status: ReviewSubmissionStatus;
  author_name: string;
  author_note: string;
  submitted_at: string;
  decided_by: string;
  decision_note: string;
  decided_at: string | null;
  kcs_snapshot: string;
  kcs_revision: string;
  snapshot_schema_version?: number;
  review_snapshot_sha256?: string;
}

export interface ReviewWorkflowPayload {
  project: Project;
  workflow?: {
    project_id: string;
    status: ProjectReviewStatus;
    latest_review_submission: ReviewSubmission | null;
    submissions: ReviewSubmission[];
    events: Record<string, unknown>[];
  };
}
export type DecisionReason =
  | 'posco_specific'
  | 'posco_stricter'
  | 'partial_overlap_residual'
  | 'no_kcs_match'
  | 'fully_covered_by_kcs'
  | 'needs_expert_review'
  | 'candidate_uncertain'
  | 'kcs_conflict';

export const DECISION_REASON_OPTIONS: Record<Exclude<Decision, null>, { value: DecisionReason; label: string }[]> = {
  keep: [
    { value: 'posco_specific', label: '포스코 고유기준' },
    { value: 'posco_stricter', label: '포스코 강화기준' },
    { value: 'partial_overlap_residual', label: '중복 제거 후 잔여기준 유지' },
    { value: 'no_kcs_match', label: '대응 KCS 없음' },
  ],
  delete: [{ value: 'fully_covered_by_kcs', label: 'KCS가 전체 요구사항 포함' }],
  hold: [
    { value: 'needs_expert_review', label: '전문가 검토 필요' },
    { value: 'candidate_uncertain', label: '후보 불확실' },
    { value: 'kcs_conflict', label: 'KCS와 충돌' },
  ],
};

export function isDecisionReason(decision: Decision, reason: string): reason is DecisionReason {
  return decision !== null && DECISION_REASON_OPTIONS[decision].some((option) => option.value === reason);
}

export interface DecisionSavePayload {
  decision: Decision;
  edited_content: string;
  review_note: string;
  decision_reason: string;
  coverage_confirmed: boolean;
  selected_candidate_id: string | null;
  expected_kcs_revision?: string;
  impact_run_id?: string;
  acknowledge_kcs_impact?: boolean;
}
export type QualityVerdict =
  | 'candidate_selected'
  | 'all_candidates_incorrect'
  | 'no_candidate_correct'
  | 'kcs_missing'
  | null;

export interface KcsConfig {
  kcs_available: boolean;
  kcs_snapshot?: string;
  kcs_revision?: string;
  kcs_document_count?: number;
  kcs_usable_document_count?: number;
  kcs_unavailable_document_count?: number;
  latest_only?: boolean;
  openai_available?: boolean;
  openai_embeddings_available?: boolean;
  openai_embedding_model?: string;
  openai_embedding_dimensions?: number;
  openai_rerank_model?: string;
  error?: string;
}

export interface SystemBackup {
  id: string;
  created_at: string;
  reason: 'manual' | 'pre_restore' | 'unknown';
  database_schema_version: number;
  project_count: number;
  upload_count: number;
  size_bytes: number;
  sha256: string;
  restorable: boolean;
  error: string;
}

export interface RestoreSystemBackupResult {
  restored_backup: SystemBackup;
  safety_backup: SystemBackup;
  restart_required: boolean;
}

export type KcsRematchStatus = 'pending' | 'running' | 'completed' | 'failed' | 'superseded';

export interface KcsRematchRun {
  id: string;
  from_revision: string;
  target_revision: string;
  status: KcsRematchStatus;
  total_clauses: number;
  matched_count: number;
  material_change_count: number;
  review_required_count: number;
  unacknowledged_count: number;
  error: string;
  queued_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  matcher_signature?: Record<string, unknown>;
}

export interface ClauseKcsImpact {
  run_id: string;
  clause_id: string;
  target_revision: string;
  impact_type: 'material_change' | 'metadata_change';
  review_required: boolean;
  acknowledged_at: string | null;
  reason: string;
  reasons: string[];
  run_status: KcsRematchStatus;
  queued_at: string;
  finished_at: string | null;
}

export type ClauseKcsImpactRecord = ClauseKcsImpact;

export type AiRelationType =
  | 'equivalent'
  | 'kcs_covers'
  | 'partial_overlap'
  | 'posco_specific'
  | 'conflict'
  | 'unrelated';

export interface CandidateAiAnalysis {
  relation_type: AiRelationType;
  confidence: number;
  rationale: string;
  simplified_content: string;
  model: string;
  analyzed_at?: string;
}

export type CoverageStatus =
  | 'fully_covered'
  | 'partially_covered'
  | 'posco_specific'
  | 'conflict'
  | 'uncertain';

export type RequirementCoverageStatus = 'covered' | 'not_covered' | 'conflict' | 'uncertain';

export interface CoverageRequirement {
  requirement: string;
  source_segment_ids: [string];
  status: RequirementCoverageStatus;
  evidence_candidate_ids: string[];
  evidence: string;
}

export interface ClauseCoverageAnalysis {
  coverage_status: CoverageStatus;
  confidence: number;
  requirements: CoverageRequirement[];
  residual_content: string;
  rationale: string;
  deletion_safe: boolean;
  candidate_ids: string[];
  evidence_candidate_ids: string[];
  model: string;
  analyzed_at: string;
}

export interface Project {
  id: string;
  title: string;
  source_filename: string;
  uploaded_at: string;
  kcs_snapshot: string;
  kcs_revision: string;
  kcs_stale: boolean;
  requires_source_reupload?: boolean;
  kcs_scope: string;
  status: ProjectReviewStatus;
  warning: string;
  total_clauses: number;
  reviewed_clauses: number;
  keep_count: number;
  delete_count: number;
  hold_count: number;
  candidate_clauses: number;
  high_match_clauses: number;
  table_clauses: number;
  reviewable_clauses: number;
  unreviewed_clauses: number;
  invalid_decision_count?: number;
  unsafe_delete_count: number;
  final_export_ready: boolean;
  final_export_blockers: string[];
  review_submission_ready: boolean;
  review_submission_blockers: string[];
  review_locked: boolean;
  latest_review_submission: ReviewSubmission | null;
  kcs_rematch?: KcsRematchRun | null;
  unacknowledged_kcs_impact_count?: number;
  archived_at?: string | null;
  duplicate_title_count?: number;
  is_latest_for_title?: boolean;
}

export interface ProjectCatalogPage {
  projects: Project[];
  total: number;
  limit: number;
  has_more: boolean;
  next_cursor: string | null;
}

export interface ClauseSummary {
  id: string;
  source_order: number;
  label: string;
  title: string;
  source_type: 'paragraph' | 'table' | 'heading';
  decision: Decision;
  decision_reason: string;
  coverage_confirmed: boolean;
  selected_candidate_id: string | null;
  reviewed_at: string | null;
  candidate_count: number;
  top_score: number | null;
  in_quality_sample?: number | boolean;
  quality_verdict?: QualityVerdict;
  kcs_impact?: ClauseKcsImpact | null;
}

export interface Candidate {
  id: string;
  rank: number;
  kcs_code: string;
  document_name: string;
  version: string;
  update_date: string;
  kcs_clause: string;
  title: string;
  content: string;
  score: number;
  classification: string;
  reasons: string[];
  warnings: string[];
  ai_analysis?: CandidateAiAnalysis | null;
}

export interface ClauseDetail extends ClauseSummary {
  project_id: string;
  content: string;
  outline_level: number | null;
  edited_content: string;
  review_note: string;
  candidates: Candidate[];
  excluded_candidates?: Candidate[];
  coverage_analysis?: ClauseCoverageAnalysis | null;
  quality_evaluation?: QualityItem | null;
}

export interface QualityCounts {
  candidate_selected: number;
  all_candidates_incorrect: number;
  no_candidate_correct: number;
  kcs_missing: number;
}

export interface QualityMetrics {
  sample_size: number;
  evaluated_count: number;
  remaining_count: number;
  complete: boolean;
  counts: QualityCounts;
  rank_hits: Record<string, number>;
  recall_at_3: number | null;
  mrr: number | null;
  accuracy: number | null;
}

export interface QualityItem {
  sample_order: number;
  cohort: 'high' | 'review' | 'no_candidate';
  verdict: QualityVerdict;
  relevant_candidate_id: string | null;
  expected_kcs_code: string;
  expected_kcs_clause: string;
  quality_note: string;
  evaluated_at: string | null;
  updated_at: string | null;
  clause_id: string;
  source_order: number;
  label: string;
  title: string;
  source_type: 'paragraph' | 'table' | 'heading';
  candidate_count: number;
  top_score: number | null;
  relevant_rank: number | null;
}

export interface QualityInsightRow {
  cohort?: string | null;
  band?: string | null;
  label?: string | null;
  sample_count?: number | null;
  evaluated_count?: number | null;
  verdict_counts?: Partial<QualityCounts> | null;
  correct_count?: number | null;
  sample_accuracy?: number | null;
  total?: number | null;
  evaluated?: number | null;
  correct?: number | null;
  accuracy?: number | null;
}

export interface QualityInsights {
  status?: 'collecting' | 'partial' | 'complete' | null;
  message?: string | null;
  scope_notice?: string | null;
  evaluated_count?: number | null;
  cohorts?: Record<string, QualityInsightRow> | QualityInsightRow[] | null;
  score_bands?: Record<string, QualityInsightRow> | QualityInsightRow[] | null;
  cohort_rows?: QualityInsightRow[] | null;
  score_band_rows?: QualityInsightRow[] | null;
  error_signals?: {
    kcs_missing?: number | null;
    all_candidates_incorrect?: number | null;
    rank_2_or_3_selected?: number | null;
    candidate_selected_rank_2_or_3?: number | null;
    total?: number | null;
    rank_2_or_3_hits?: number | null;
  } | null;
}

export interface QualityEvaluation {
  id: string;
  project_id: string;
  target_sample_size: number;
  population_size: number;
  sampling_version: string;
  reviewer_name: string;
  created_at: string;
  completed_at: string | null;
  actual_sample_size: number;
  evaluated_count: number;
  items: QualityItem[];
  metrics: QualityMetrics;
  insights?: QualityInsights | null;
}

export interface QualityUpdatePayload {
  verdict: QualityVerdict;
  relevant_candidate_id: string | null;
  expected_kcs_code: string;
  expected_kcs_clause: string;
  quality_note: string;
}

export interface ProjectPayload {
  project: Project;
  clauses: ClauseSummary[];
}

export type UploadJobStatus = 'queued' | 'running' | 'completed' | 'failed';
export type UploadJobPhase = 'queued' | 'preparing' | 'converting' | 'parsing' | 'matching' | 'saving' | 'completed' | 'failed';

export interface UploadJobItem {
  id: string;
  job_id: string;
  position: number;
  filename: string;
  source_size: number;
  project_id: string | null;
  status: UploadJobStatus;
  progress: number;
  phase: UploadJobPhase;
  error: string;
  retryable: boolean;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface UploadJob {
  id: string;
  status: UploadJobStatus;
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  progress: number;
  error: string;
  kcs_snapshot: string;
  kcs_revision: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  items: UploadJobItem[];
}

export interface ClauseStructurePayload extends ProjectPayload {
  active_clause_id: string;
}

export type BulkReviewStatus = 'all' | 'kcs_impact' | 'unreviewed' | 'keep' | 'delete' | 'hold';

export interface SourceContextItem {
  source_order: number;
  label: string;
  title: string;
  content: string;
  source_type: 'paragraph' | 'table' | 'heading';
}

export interface SourceContext {
  path: string;
  previous: SourceContextItem | null;
  next: SourceContextItem | null;
}

export interface BulkReviewItem {
  id: string;
  source_order: number;
  label: string;
  title: string;
  content: string;
  source_type: 'paragraph' | 'table' | 'heading';
  outline_level: number | null;
  decision: Decision;
  edited_content: string;
  review_note: string;
  decision_reason: string;
  coverage_confirmed: boolean;
  selected_candidate_id: string | null;
  reviewed_at: string | null;
  excluded_candidate_count: number;
  candidates: Candidate[];
  source_context: SourceContext;
  kcs_impact?: ClauseKcsImpact | null;
}

export interface BulkReviewPayload {
  items: BulkReviewItem[];
  total: number;
  offset: number;
  limit: number;
  has_more: boolean;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let message = `요청을 처리하지 못했습니다. (${response.status})`;
    try {
      const payload = (await response.json()) as {
        detail?: string | { msg?: string; loc?: (string | number)[] }[];
      };
      if (typeof payload.detail === 'string') message = payload.detail;
      else if (Array.isArray(payload.detail)) {
        message = payload.detail.map((item) => item.msg || '입력값을 확인해 주세요.').join(' ');
      }
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export const api = {
  config: () => request<KcsConfig>('/api/config'),
  systemBackups: () => request<{ backups: SystemBackup[] }>('/api/system/backups'),
  createSystemBackup: () => request<{ backup: SystemBackup }>('/api/system/backups', { method: 'POST' }),
  inspectUploadedSystemBackup: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<{ backup: SystemBackup }>('/api/system/backups/inspect-upload', {
      method: 'POST',
      body: form,
    });
  },
  restoreSystemBackup: (backupId: string) =>
    request<RestoreSystemBackupResult>(
      `/api/system/backups/${encodeURIComponent(backupId)}/restore`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmation: '복구' }),
      },
    ),
  restoreUploadedSystemBackup: (file: File) => {
    const form = new FormData();
    form.append('file', file);
    form.append('confirmation', '복구');
    return request<RestoreSystemBackupResult>('/api/system/backups/restore-upload', {
      method: 'POST',
      body: form,
    });
  },
  systemBackupUrl: (backupId: string) =>
    `${API_BASE}/api/system/backups/${encodeURIComponent(backupId)}/download`,
  syncKcs: () => request<{ kcs_snapshot: string; kcs_revision: string; kcs_document_count: number; changed_count: number; usable_document_count: number; unavailable_document_count: number; queued_project_count: number; rematch_run_ids: string[] }>('/api/kcs/sync', { method: 'POST' }),
  projects: (options: {
    search?: string;
    parserStatus?: 'current' | 'legacy';
    archiveStatus?: 'active' | 'archived';
    kcsImpactOnly?: boolean;
    reviewStatus?: ProjectReviewStatus;
    limit?: number;
    cursor?: string;
  } = {}) => {
    const params = new URLSearchParams();
    if (options.search?.trim()) params.set('search', options.search.trim());
    if (options.parserStatus) params.set('parser_status', options.parserStatus);
    if (options.archiveStatus) params.set('archive_status', options.archiveStatus);
    if (options.kcsImpactOnly) params.set('kcs_impact_only', 'true');
    if (options.reviewStatus) params.set('review_status', options.reviewStatus);
    if (options.limit !== undefined) params.set('limit', String(options.limit));
    if (options.cursor) params.set('cursor', options.cursor);
    const query = params.toString();
    return request<ProjectCatalogPage>(`/api/projects${query ? `?${query}` : ''}`);
  },
  project: (projectId: string) => request<ProjectPayload>(`/api/projects/${projectId}`),
  clause: (projectId: string, clauseId: string) =>
    request<{ clause: ClauseDetail }>(`/api/projects/${projectId}/clauses/${clauseId}`),
  quickReviewClause: (
    projectId: string,
    clauseId: string,
    payload: {
      decision?: Decision;
      decision_reason?: string;
      coverage_confirmed?: boolean;
      selected_candidate_id?: string | null;
      expected_kcs_revision?: string;
      impact_run_id?: string;
      acknowledge_kcs_impact?: boolean;
    },
  ) =>
    request<{ clause: ClauseDetail; project: Project }>(
      `/api/projects/${projectId}/clauses/${clauseId}/quick-review`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    ),
  bulkReview: (
    projectId: string,
    options: { offset?: number; limit?: number; status?: BulkReviewStatus; q?: string } = {},
  ) => {
    const params = new URLSearchParams({
      offset: String(options.offset ?? 0),
      limit: String(options.limit ?? 60),
      status: options.status || 'all',
    });
    if (options.q?.trim()) params.set('q', options.q.trim());
    return request<BulkReviewPayload>(`/api/projects/${projectId}/bulk-review?${params.toString()}`);
  },
  kcsImpact: (projectId: string) =>
    request<{ run: KcsRematchRun | null; impacts: ClauseKcsImpactRecord[] }>(
      `/api/projects/${projectId}/kcs-impact`,
    ),
  startKcsRematch: (projectId: string) =>
    request<{ run: KcsRematchRun }>(`/api/projects/${projectId}/kcs-rematch`, { method: 'POST' }),
  analyzeCandidate: (projectId: string, clauseId: string, candidateId: string, refresh = false) =>
    request<{
      analysis: CandidateAiAnalysis;
      excluded: boolean;
      clause: ClauseDetail;
      project: Project;
    }>(
      `/api/projects/${projectId}/clauses/${clauseId}/candidates/${candidateId}/ai-analysis`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh }),
      },
    ),
  analyzeCoverage: (projectId: string, clauseId: string, refresh = false) =>
    request<{
      analysis: ClauseCoverageAnalysis;
      clause: ClauseDetail;
      project: Project;
    }>(
      `/api/projects/${projectId}/clauses/${clauseId}/coverage-analysis`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh }),
      },
    ),
  restoreCandidate: (projectId: string, clauseId: string, candidateId: string) =>
    request<{ clause: ClauseDetail; project: Project }>(
      `/api/projects/${projectId}/clauses/${clauseId}/candidates/${candidateId}/ai-analysis`,
      { method: 'DELETE' },
    ),
  qualityEvaluation: (projectId: string) =>
    request<{ evaluation: QualityEvaluation }>(`/api/projects/${projectId}/quality-evaluation`),
  ensureQualityEvaluation: (projectId: string) =>
    request<{ evaluation: QualityEvaluation }>(`/api/projects/${projectId}/quality-evaluation`, { method: 'POST' }),
  saveQualityItem: (projectId: string, clauseId: string, payload: QualityUpdatePayload) =>
    request<{ evaluation: QualityEvaluation }>(
      `/api/projects/${projectId}/quality-evaluation/items/${clauseId}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    ),
  upload: async (file: File) => {
    const form = new FormData();
    form.append('file', file);
    return request<ProjectPayload>('/api/projects/upload', { method: 'POST', body: form });
  },
  startUploadJob: async (files: File[]) => {
    const form = new FormData();
    files.forEach((file) => form.append('files', file));
    return request<{ job: UploadJob }>('/api/upload-jobs', { method: 'POST', body: form });
  },
  uploadJob: (jobId: string) =>
    request<{ job: UploadJob }>(`/api/upload-jobs/${encodeURIComponent(jobId)}`),
  uploadJobs: (limit = 20) => {
    const params = new URLSearchParams({ limit: String(limit) });
    return request<{ jobs: UploadJob[] }>(`/api/upload-jobs?${params.toString()}`);
  },
  retryUploadJobItem: (jobId: string, itemId: string) =>
    request<{ job: UploadJob }>(
      `/api/upload-jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/retry`,
      { method: 'POST' },
    ),
  archiveProject: (projectId: string, archived: boolean) =>
    request<{ project: Project }>(`/api/projects/${projectId}/archive`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ archived }),
    }),
  submitReviewWorkflow: (projectId: string, payload: { author_name: string; note: string }) =>
    request<ReviewWorkflowPayload>(`/api/projects/${projectId}/review-workflow/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }),
  approveReviewWorkflow: (
    projectId: string,
    submissionId: string,
    payload: { approver_name: string; note: string },
  ) =>
    request<ReviewWorkflowPayload>(
      `/api/projects/${projectId}/review-workflow/${encodeURIComponent(submissionId)}/approve`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    ),
  requestReviewChanges: (
    projectId: string,
    submissionId: string,
    payload: { approver_name: string; reason: string },
  ) =>
    request<ReviewWorkflowPayload>(
      `/api/projects/${projectId}/review-workflow/${encodeURIComponent(submissionId)}/request-changes`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    ),
  mergeClauses: (projectId: string, clauseIds: [string, string]) =>
    request<ClauseStructurePayload>(`/api/projects/${projectId}/clauses/merge`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clause_ids: clauseIds }),
    }),
  splitClause: (projectId: string, clauseId: string, parts: [string, string]) =>
    request<ClauseStructurePayload>(`/api/projects/${projectId}/clauses/${clauseId}/split`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parts }),
    }),
  saveClause: (
    projectId: string,
    clauseId: string,
    payload: DecisionSavePayload,
  ) =>
    request<{ clause: ClauseDetail; project: Project }>(
      `/api/projects/${projectId}/clauses/${clauseId}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    ),
  exportUrl: (projectId: string, kind: 'review' | 'final') =>
    `${API_BASE}/api/projects/${projectId}/export/${kind}`,
  auditUrl: (projectId: string) => `${API_BASE}/api/projects/${projectId}/audit.xlsx`,
  qualityReportUrl: (projectId: string) => `${API_BASE}/api/projects/${projectId}/quality-evaluation.xlsx`,
};
