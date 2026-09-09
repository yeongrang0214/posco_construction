import { getDatabase, nowIso } from '@/lib/cloud-runtime';

function text(value: unknown, fallback = ''): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return `${value}`;
  return fallback;
}

function nullableText(value: unknown): string | null {
  const result = text(value);
  return result || null;
}

function jsonArray(value: unknown): unknown[] {
  if (typeof value !== 'string') return [];
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

const CONTEXT_REFERENCE = /(?:상기|전항|앞(?:의|서)?|위(?:의)?)\s*(?:제?\s*)?(?:\(?[①-⑳0-9가-하]+\)?(?:\.\d+)*)?(?:항|호|목|규정|기준|내용)?/i;

function candidateContent(candidate: Record<string, unknown>) {
  const content = text(candidate.content);
  const previousD = text(candidate.previous_content);
  return CONTEXT_REFERENCE.test(content) && previousD
    ? `[앞 조항] ${previousD}\n[현재 조항] ${content}`
    : content;
}

export async function getMeta() {
  const db = getDatabase();
  const result = await db.prepare("SELECT key, value FROM app_meta WHERE key IN ('kcs_snapshot','kcs_revision')").all<{ key: string; value: string }>();
  return Object.fromEntries((result.results || []).map((row) => [row.key, row.value]));
}

export async function projectSummary(projectId: string) {
  const db = getDatabase();
  const project = await db.prepare('SELECT * FROM projects WHERE id = ?').bind(projectId).first<Record<string, unknown>>();
  if (!project) return null;
  const counts = await db.prepare(`
    SELECT
      COUNT(*) AS total,
      SUM(CASE WHEN source_type != 'heading' THEN 1 ELSE 0 END) AS reviewable,
      SUM(CASE WHEN decision IS NOT NULL THEN 1 ELSE 0 END) AS reviewed,
      SUM(CASE WHEN decision = 'keep' THEN 1 ELSE 0 END) AS keep_count,
      SUM(CASE WHEN decision = 'delete' THEN 1 ELSE 0 END) AS delete_count,
      SUM(CASE WHEN decision = 'hold' THEN 1 ELSE 0 END) AS hold_count,
      SUM(CASE WHEN source_type = 'table' THEN 1 ELSE 0 END) AS table_count
    FROM clauses WHERE project_id = ?
  `).bind(projectId).first<Record<string, number>>();
  const candidateCounts = await db.prepare(`
    SELECT COUNT(DISTINCT clause_id) AS candidate_clauses,
           COUNT(DISTINCT CASE WHEN score >= 0.45 THEN clause_id END) AS high_match_clauses
      FROM project_candidates WHERE project_id = ? AND excluded = 0
  `).bind(projectId).first<Record<string, number>>();

  const total = Number(counts?.total || 0);
  const reviewable = Number(counts?.reviewable || 0);
  const reviewed = Number(counts?.reviewed || 0);
  const status = text(project.status, 'reviewing');
  const blockers: string[] = [];
  if (reviewable - reviewed > 0) blockers.push(`미검토 조항 ${reviewable - reviewed}건`);
  if (Number(counts?.hold_count || 0) > 0) blockers.push(`보류 조항 ${Number(counts?.hold_count || 0)}건`);

  return {
    id: text(project.id),
    title: text(project.title),
    source_filename: text(project.source_filename),
    uploaded_at: text(project.uploaded_at),
    kcs_snapshot: text(project.kcs_snapshot),
    kcs_revision: text(project.kcs_revision),
    kcs_stale: false,
    requires_source_reupload: false,
    kcs_scope: text(project.kcs_scope, 'all'),
    status,
    warning: text(project.warning),
    total_clauses: total,
    reviewed_clauses: reviewed,
    keep_count: Number(counts?.keep_count || 0),
    delete_count: Number(counts?.delete_count || 0),
    hold_count: Number(counts?.hold_count || 0),
    candidate_clauses: Number(candidateCounts?.candidate_clauses || 0),
    high_match_clauses: Number(candidateCounts?.high_match_clauses || 0),
    table_clauses: Number(counts?.table_count || 0),
    reviewable_clauses: reviewable,
    unreviewed_clauses: Math.max(0, reviewable - reviewed),
    invalid_decision_count: 0,
    unsafe_delete_count: 0,
    final_export_ready: blockers.length === 0 && status === 'approved',
    final_export_blockers: status === 'approved' ? blockers : ['승인 완료가 필요합니다.', ...blockers],
    review_submission_ready: blockers.length === 0,
    review_submission_blockers: blockers,
    review_locked: status === 'submitted' || status === 'approved',
    latest_review_submission: null,
    kcs_rematch: null,
    unacknowledged_kcs_impact_count: 0,
    archived_at: nullableText(project.archived_at),
    duplicate_title_count: 1,
    is_latest_for_title: true,
  };
}

export async function clauseSummaries(projectId: string) {
  const db = getDatabase();
  const result = await db.prepare(`
    SELECT c.*,
      (SELECT COUNT(*) FROM project_candidates pc WHERE pc.clause_id=c.id AND pc.excluded=0) AS candidate_count,
      (SELECT MAX(score) FROM project_candidates pc WHERE pc.clause_id=c.id AND pc.excluded=0) AS top_score
    FROM clauses c WHERE c.project_id=? ORDER BY c.source_order
  `).bind(projectId).all<Record<string, unknown>>();
  return (result.results || []).map((row) => ({
    id: text(row.id), source_order: Number(row.source_order), label: text(row.label), title: text(row.title),
    source_type: text(row.source_type), decision: nullableText(row.decision), decision_reason: text(row.decision_reason),
    coverage_confirmed: Boolean(row.coverage_confirmed), selected_candidate_id: nullableText(row.selected_candidate_id),
    reviewed_at: nullableText(row.reviewed_at), candidate_count: Number(row.candidate_count || 0),
    top_score: row.top_score == null ? null : Number(row.top_score), in_quality_sample: false, quality_verdict: null, kcs_impact: null,
  }));
}

export async function clauseDetail(projectId: string, clauseId: string) {
  const db = getDatabase();
  const row = await db.prepare(`
    SELECT c.*,
      (SELECT COUNT(*) FROM project_candidates pc WHERE pc.clause_id=c.id AND pc.excluded=0) AS candidate_count,
      (SELECT MAX(score) FROM project_candidates pc WHERE pc.clause_id=c.id AND pc.excluded=0) AS top_score
    FROM clauses c WHERE c.project_id=? AND c.id=?
  `).bind(projectId, clauseId).first<Record<string, unknown>>();
  if (!row) return null;
  const candidates = await db.prepare(`
    SELECT pc.id, pc.rank, pc.score, pc.reasons_json, pc.warnings_json, s.kcs_code, s.document_name,
           s.version, s.update_date, s.kcs_clause, s.title, s.content,
           (SELECT previous.content FROM kcs_sections previous
             WHERE previous.document_id=s.document_id AND previous.section_order<s.section_order
             ORDER BY previous.section_order DESC LIMIT 1) AS previous_content
      FROM project_candidates pc JOIN kcs_sections s ON s.id=pc.section_id
     WHERE pc.clause_id=? AND pc.excluded=0 ORDER BY pc.rank
  `).bind(clauseId).all<Record<string, unknown>>();
  return {
    id: text(row.id), project_id: text(row.project_id), source_order: Number(row.source_order), label: text(row.label),
    title: text(row.title), source_type: text(row.source_type), outline_level: row.outline_level == null ? null : Number(row.outline_level),
    content: text(row.content), edited_content: text(row.edited_content), review_note: text(row.review_note),
    decision: nullableText(row.decision), decision_reason: text(row.decision_reason), coverage_confirmed: Boolean(row.coverage_confirmed),
    selected_candidate_id: nullableText(row.selected_candidate_id), reviewed_at: nullableText(row.reviewed_at),
    candidate_count: Number(row.candidate_count || 0), top_score: row.top_score == null ? null : Number(row.top_score),
    candidates: (candidates.results || []).map((candidate) => ({
      id: text(candidate.id), rank: Number(candidate.rank), kcs_code: text(candidate.kcs_code), document_name: text(candidate.document_name),
      version: text(candidate.version), update_date: text(candidate.update_date), kcs_clause: text(candidate.kcs_clause),
      title: text(candidate.title), content: candidateContent(candidate), score: Number(candidate.score || 0), classification: '',
      reasons: [
        ...jsonArray(candidate.reasons_json),
        ...(CONTEXT_REFERENCE.test(text(candidate.content)) && text(candidate.previous_content)
          ? ['참조 표현의 앞 조항을 함께 표시합니다.'] : []),
      ], warnings: jsonArray(candidate.warnings_json), ai_analysis: null,
    })),
    excluded_candidates: [], coverage_analysis: null, quality_evaluation: null, kcs_impact: null,
  };
}

export async function saveClauseDecision(projectId: string, clauseId: string, payload: Record<string, unknown>) {
  const db = getDatabase();
  const decision = payload.decision == null ? null : text(payload.decision);
  if (decision != null && !['keep', 'delete', 'hold'].includes(decision)) throw new Error('판정 값이 올바르지 않습니다.');
  const now = nowIso();
  await db.prepare(`
    UPDATE clauses SET decision=?, edited_content=?, review_note=?, decision_reason=?, coverage_confirmed=?,
      selected_candidate_id=?, reviewed_at=?, updated_at=? WHERE project_id=? AND id=?
  `).bind(
    decision, text(payload.edited_content), text(payload.review_note), text(payload.decision_reason),
    payload.coverage_confirmed ? 1 : 0, payload.selected_candidate_id == null ? null : text(payload.selected_candidate_id),
    decision ? now : null, now, projectId, clauseId,
  ).run();
  return clauseDetail(projectId, clauseId);
}
