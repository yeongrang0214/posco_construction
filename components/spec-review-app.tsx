'use client';

import { ChangeEvent, DragEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Circle,
  Clock3,
  Combine,
  Download,
  FileSpreadsheet,
  FileText,
  LibraryBig,
  Layers3,
  LockKeyhole,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Search,
  Scissors,
  Send,
  ShieldCheck,
  Sparkles,
  Table2,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button, buttonVariants } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Progress, ProgressLabel, ProgressValue } from '@/components/ui/progress';
import { Spinner } from '@/components/ui/spinner';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { BackupManager } from '@/components/backup-manager';
import { BulkReview } from '@/components/bulk-review';
import {
  api,
  BulkReviewItem,
  Candidate,
  ClauseDetail,
  ClauseStructurePayload,
  ClauseSummary,
  DECISION_REASON_OPTIONS,
  Decision,
  isDecisionReason,
  KcsConfig,
  KcsRematchRun,
  Project,
  ProjectDiscipline,
  ProjectReviewStatus,
  QualityEvaluation,
  QualityInsightRow,
  QualityVerdict,
  UploadJob,
} from '@/lib/api';
import { cn } from '@/lib/utils';

type StatusFilter = 'all' | 'unreviewed' | 'keep' | 'delete' | 'hold';
type ReviewMode = 'business' | 'quality';
type BusinessView = 'bulk' | 'detail';
type ReviewWorkflowDialog = 'submit' | 'decision' | null;

type StructureEditDialog =
  | {
      kind: 'split';
      clauseId: string;
      sourceOrder: number;
      label: string;
      title: string;
      sourceText: string;
      splitIndex: number;
    }
  | {
      kind: 'merge';
      firstClauseId: string;
      sourceOrder: number;
      firstLabel: string;
      firstTitle: string;
      secondClauseId: string;
      secondLabel: string;
      secondTitle: string;
    };

function percent(value: number | null | undefined) {
  return value == null ? '—' : `${Math.round(value * 100)}%`;
}

function reviewStatusLabel(status: ProjectReviewStatus) {
  if (status === 'submitted') return '승인 대기';
  if (status === 'changes_requested') return '반려 · 수정 중';
  if (status === 'approved') return '승인 완료';
  return '작성 중';
}

function projectDisplayTitle(project: Pick<Project, 'title' | 'source_filename'>) {
  const title = project.title.trim();
  if (title && !/^(word document|document|문서)$/i.test(title)) return title;
  return project.source_filename
    .replace(/\.docx?$/i, '')
    .replace(/^(건축설비|건축|전기시방서|전기)_/, '')
    .replace(/_\d{6}$/, '')
    .replaceAll('_', ' ');
}

function reviewStatusVariant(status: ProjectReviewStatus): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (status === 'approved') return 'default';
  if (status === 'changes_requested') return 'destructive';
  if (status === 'submitted') return 'secondary';
  return 'outline';
}

function snapshotDate(value?: string) {
  if (!value) return '확인 필요';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ko-KR');
}

function candidateSplitIndexes(sourceText: string) {
  const sentenceBoundaries = Array.from(sourceText.matchAll(/[.!?。]\s+|\n+|;\s+/g))
    .map((match) => (match.index || 0) + match[0].length)
    .filter((index) => index > 0 && index < sourceText.length)
    .filter((index) => sourceText.slice(0, index).trim() && sourceText.slice(index).trim());
  if (sentenceBoundaries.length) return sentenceBoundaries;
  return Array.from(sourceText.matchAll(/\s+/g))
    .map((match) => (match.index || 0) + match[0].length)
    .filter((index) => index > 0 && index < sourceText.length)
    .filter((index) => sourceText.slice(0, index).trim() && sourceText.slice(index).trim());
}

function suggestedSplitIndex(sourceText: string) {
  if (sourceText.length < 2) return 0;
  const midpoint = Math.floor(sourceText.length / 2);
  const boundaries = candidateSplitIndexes(sourceText);
  if (boundaries.length) {
    return boundaries.reduce((best, current) => (
      Math.abs(current - midpoint) < Math.abs(best - midpoint) ? current : best
    ));
  }
  const whitespace = sourceText.lastIndexOf(' ', midpoint);
  return whitespace > 0 ? whitespace + 1 : Math.max(1, midpoint);
}

function normalizeProject(project: Project): Project {
  const rematch = project.kcs_rematch;
  const status: ProjectReviewStatus = ['reviewing', 'submitted', 'changes_requested', 'approved'].includes(project.status)
    ? project.status
    : 'reviewing';
  return {
    ...project,
    status,
    total_clauses: Number(project.total_clauses || 0),
    reviewed_clauses: Number(project.reviewed_clauses || 0),
    keep_count: Number(project.keep_count || 0),
    delete_count: Number(project.delete_count || 0),
    hold_count: Number(project.hold_count || 0),
    candidate_clauses: Number(project.candidate_clauses || 0),
    high_match_clauses: Number(project.high_match_clauses || 0),
    table_clauses: Number(project.table_clauses || 0),
    reviewable_clauses: Number(project.reviewable_clauses || 0),
    unreviewed_clauses: Number(project.unreviewed_clauses || 0),
    unsafe_delete_count: Number(project.unsafe_delete_count || 0),
    requires_source_reupload: Boolean(project.requires_source_reupload),
    unacknowledged_kcs_impact_count: Number(
      project.unacknowledged_kcs_impact_count ?? rematch?.unacknowledged_count ?? 0,
    ),
    final_export_ready: Boolean(project.final_export_ready),
    final_export_blockers: Array.isArray(project.final_export_blockers)
      ? project.final_export_blockers
      : [],
    review_submission_ready: Boolean(project.review_submission_ready),
    review_submission_blockers: Array.isArray(project.review_submission_blockers)
      ? project.review_submission_blockers
      : [],
    review_locked: Boolean(project.review_locked),
    latest_review_submission: project.latest_review_submission || null,
    kcs_rematch: rematch ? {
      ...rematch,
      total_clauses: Number(rematch.total_clauses || 0),
      matched_count: Number(rematch.matched_count || 0),
      material_change_count: Number(rematch.material_change_count || 0),
      review_required_count: Number(rematch.review_required_count || 0),
      unacknowledged_count: Number(rematch.unacknowledged_count || 0),
      error: rematch.error || '',
    } : null,
  };
}

function kcsRematchLabel(run: KcsRematchRun, unacknowledgedCount = run.unacknowledged_count) {
  if (run.status === 'pending') return 'KCS 영향 분석 대기';
  if (run.status === 'running') return 'KCS 자동 재매칭 중';
  if (run.status === 'failed') return 'KCS 재매칭 일부 실패';
  if (run.status === 'superseded') return '더 최신 KCS 재매칭 필요';
  if (unacknowledgedCount > 0) return `KCS 재검토 ${unacknowledgedCount}건`;
  return '최신 KCS 반영 완료';
}

function unresolvedKcsImpact(clause: Pick<ClauseSummary, 'kcs_impact'> | null | undefined) {
  const impact = clause?.kcs_impact;
  return Boolean(impact?.review_required && !impact.acknowledged_at);
}

function kcsImpactAcknowledgement(clause: Pick<ClauseSummary, 'kcs_impact'> | null | undefined) {
  const impact = clause?.kcs_impact;
  if (!impact?.review_required || impact.acknowledged_at) return {};
  return {
    expected_kcs_revision: impact.target_revision,
    impact_run_id: impact.run_id,
    acknowledge_kcs_impact: true,
  };
}

function qualityVerdictLabel(verdict: QualityVerdict) {
  if (verdict === 'candidate_selected') return '현재 후보 적정';
  if (verdict === 'all_candidates_incorrect') return '적용 KCS 없음(후보 오탐)';
  if (verdict === 'no_candidate_correct') return '후보 없음이 적정';
  if (verdict === 'kcs_missing') return '정답 KCS Top3 누락';
  return '미평가';
}

function qualityCohortLabel(cohort: string | null | undefined) {
  if (cohort === 'high') return '고유사도';
  if (cohort === 'review') return '검토필요';
  if (cohort === 'no_candidate') return '후보없음';
  return cohort || '구간';
}

function qualityInsightStatusLabel(status: string | null | undefined) {
  if (status === 'complete') return '분석 완료';
  if (status === 'partial') return '중간 집계';
  return '평가 전';
}

function qualityScoreBandLabel(band: string | null | undefined) {
  if (band === 'gte_0_45') return '45% 이상';
  if (band === '0_35_to_0_45') return '35% 이상~45% 미만';
  if (band === '0_25_to_0_35') return '25% 이상~35% 미만';
  if (band === 'no_candidate') return '후보 없음';
  return band || '점수 구간';
}

function normalizeInsightRows(
  rows: Record<string, QualityInsightRow> | QualityInsightRow[] | null | undefined,
  keyName: 'cohort' | 'band',
) {
  if (Array.isArray(rows)) return rows;
  return Object.entries(rows || {}).map(([key, row]) => (
    keyName === 'cohort'
      ? { ...row, cohort: row.cohort || key }
      : { ...row, band: row.band || key }
  ));
}

function aiRelationLabel(relation: NonNullable<Candidate['ai_analysis']>['relation_type']) {
  if (relation === 'equivalent') return '실질적 동일';
  if (relation === 'kcs_covers') return 'KCS가 포괄';
  if (relation === 'partial_overlap') return '부분 일치';
  if (relation === 'posco_specific') return '포스코 특화 포함';
  if (relation === 'conflict') return '기준 충돌';
  return '무관 후보';
}

function coverageStatusLabel(status: NonNullable<ClauseDetail['coverage_analysis']>['coverage_status']) {
  if (status === 'fully_covered') return '전체 포괄';
  if (status === 'partially_covered') return '부분 포괄';
  if (status === 'posco_specific') return '포스코 고유';
  if (status === 'conflict') return '기준 충돌';
  return '판정 불확실';
}

function requirementStatusLabel(status: NonNullable<ClauseDetail['coverage_analysis']>['requirements'][number]['status']) {
  if (status === 'covered') return 'KCS 포함';
  if (status === 'not_covered') return '포스코 잔여';
  if (status === 'conflict') return '충돌';
  return '확인 필요';
}

type ProjectListFilter =
  | 'active'
  | 'approval_pending'
  | 'changes_requested'
  | 'current'
  | 'legacy'
  | 'kcs_impact'
  | 'archived';

function uploadProgress(value: number) {
  return Math.max(0, Math.min(100, Math.round(value)));
}

function uploadPhaseLabel(phase: UploadJob['items'][number]['phase']) {
  if (phase === 'preparing') return '파일 준비';
  if (phase === 'converting') return 'DOCX 변환';
  if (phase === 'parsing') return '조항 분리';
  if (phase === 'matching') return 'KCS 매칭';
  if (phase === 'saving') return '프로젝트 저장';
  if (phase === 'completed') return '완료';
  if (phase === 'failed') return '실패';
  return '대기';
}

function fileSizeLabel(value: number) {
  if (value >= 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)}MB`;
  return `${Math.max(1, Math.round(value / 1024))}KB`;
}

function UploadJobPanel({
  job,
  pollError,
  onOpenProject,
  onRetryFiles,
  onRetryItem,
  retryingItemId,
}: {
  job: UploadJob;
  pollError: string;
  onOpenProject: (projectId: string) => void;
  onRetryFiles: () => void;
  onRetryItem: (jobId: string, itemId: string) => void;
  retryingItemId: string | null;
}) {
  const processed = job.completed + job.failed;
  const progress = uploadProgress(job.progress);
  return (
    <section aria-label="문서 일괄 업로드 진행 상황" className="space-y-3">
      <Progress value={progress}>
        <ProgressLabel>{job.status === 'queued' ? '업로드 작업 대기' : job.status === 'running' ? '문서 분석 중' : '업로드 작업 결과'}</ProgressLabel>
        <ProgressValue>{() => `${processed} / ${job.total}개 · ${progress}%`}</ProgressValue>
      </Progress>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        <span>대기 {job.queued}</span><span>처리 중 {job.running}</span><span>완료 {job.completed}</span><span>실패 {job.failed}</span>
      </div>
      {pollError && <output className="block rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-950/30 dark:text-amber-100">{pollError}</output>}
      {job.error && <p role="alert" className="rounded-lg bg-destructive/10 px-3 py-2 text-xs text-destructive">{job.error}</p>}
      {job.failed > 0 && (job.status === 'completed' || job.status === 'failed') && (
        <Button size="sm" variant="outline" onClick={onRetryFiles}><UploadCloud />실패 파일 다시 선택</Button>
      )}
      <ul className="max-h-[48vh] space-y-1 overflow-y-auto pr-1">
        {[...job.items].sort((left, right) => left.position - right.position).map((item) => (
          <li key={item.id} className="rounded-lg border border-border px-3 py-2.5">
            <div className="flex items-start gap-3">
              {item.status === 'running'
                ? <Spinner className="mt-0.5 size-4 shrink-0" />
                : item.status === 'completed'
                  ? <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-emerald-600" />
                  : item.status === 'failed'
                    ? <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" />
                    : <Circle className="mt-0.5 size-4 shrink-0 text-muted-foreground" />}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">{item.filename}</span>
                <span className="mt-0.5 block text-xs text-muted-foreground">{fileSizeLabel(item.source_size)} · {uploadPhaseLabel(item.phase)}{item.status === 'running' ? ` · ${uploadProgress(item.progress)}%` : ''}</span>
                {item.error && <span className="mt-1 block text-xs text-destructive">{item.error}</span>}
              </span>
              <span className="flex shrink-0 flex-col items-end gap-1">
                {item.status === 'completed' && item.project_id && (
                  <Button size="sm" variant="ghost" onClick={() => item.project_id && onOpenProject(item.project_id)}>검토 열기</Button>
                )}
                {item.status === 'failed' && item.retryable && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={retryingItemId !== null}
                    onClick={() => onRetryItem(job.id, item.id)}
                  >
                    {retryingItemId === item.id ? <Spinner /> : <RefreshCw />}같은 파일 재시도
                  </Button>
                )}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

const PROJECT_CATALOG_PAGE_SIZE = 30;

function ProjectSelector({
  currentProject,
  disabled,
  archiveBusyId,
  refreshKey,
  onSelect,
  onArchive,
}: {
  currentProject: Project;
  disabled: boolean;
  archiveBusyId: string | null;
  refreshKey: number;
  onSelect: (projectId: string) => void;
  onArchive: (project: Project, archived: boolean) => void;
}) {
  const requestSequence = useRef(0);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [filter, setFilter] = useState<ProjectListFilter>('active');
  const [discipline, setDiscipline] = useState<ProjectDiscipline | 'all'>('all');
  const [catalogProjects, setCatalogProjects] = useState<Project[]>([]);
  const [catalogTotal, setCatalogTotal] = useState(0);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [retryKey, setRetryKey] = useState(0);
  const [currentCatalogMetadata, setCurrentCatalogMetadata] = useState<{
    projectId: string;
    refreshKey: number;
    duplicate_title_count: number;
    is_latest_for_title: boolean;
  } | null>(null);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query.trim()), 300);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const projectId = currentProject.id;
    const requestedRefreshKey = refreshKey;
    let cancelled = false;
    api.project(projectId)
      .then(({ project: refreshedProject }) => {
        if (cancelled) return;
        setCurrentCatalogMetadata({
          projectId,
          refreshKey: requestedRefreshKey,
          duplicate_title_count: Number(refreshedProject.duplicate_title_count || 0),
          is_latest_for_title: Boolean(refreshedProject.is_latest_for_title),
        });
      })
      .catch(() => {
        if (!cancelled) setCurrentCatalogMetadata(null);
      });
    return () => { cancelled = true; };
  }, [currentProject.id, open, refreshKey]);

  useEffect(() => {
    if (!open) return;
    const sequence = ++requestSequence.current;
    let cancelled = false;

    const loadFirstPage = async () => {
      await Promise.resolve();
      if (cancelled || requestSequence.current !== sequence) return;
      setCatalogLoading(true);
      setCatalogError('');
      setCatalogProjects([]);
      setCatalogTotal(0);
      setNextCursor(null);
      setHasMore(false);
      setLoadingMore(false);
      try {
        const result = await api.projects({
          search: debouncedQuery,
          archiveStatus: filter === 'archived' ? 'archived' : 'active',
          parserStatus: filter === 'current' ? 'current' : filter === 'legacy' ? 'legacy' : undefined,
          kcsImpactOnly: filter === 'kcs_impact',
          reviewStatus: filter === 'approval_pending'
            ? 'submitted'
            : filter === 'changes_requested'
              ? 'changes_requested'
              : undefined,
          discipline: discipline === 'all' ? undefined : discipline,
          limit: PROJECT_CATALOG_PAGE_SIZE,
        });
        if (cancelled || requestSequence.current !== sequence) return;
        setCatalogProjects(result.projects.map(normalizeProject));
        setCatalogTotal(result.total);
        setNextCursor(result.next_cursor);
        setHasMore(result.has_more);
      } catch (cause) {
        if (cancelled || requestSequence.current !== sequence) return;
        setCatalogError(cause instanceof Error ? cause.message : '프로젝트 목록을 불러오지 못했습니다.');
      } finally {
        if (!cancelled && requestSequence.current === sequence) setCatalogLoading(false);
      }
    };
    void loadFirstPage();

    return () => { cancelled = true; };
  }, [debouncedQuery, discipline, filter, open, refreshKey, retryKey]);

  const currentCatalogProject = currentCatalogMetadata?.projectId === currentProject.id
    && currentCatalogMetadata.refreshKey === refreshKey
    ? {
        ...currentProject,
        duplicate_title_count: currentCatalogMetadata.duplicate_title_count,
        is_latest_for_title: currentCatalogMetadata.is_latest_for_title,
      }
    : currentProject;
  const currentMatchesFilter = (filter === 'archived' && Boolean(currentCatalogProject.archived_at))
    || (!currentCatalogProject.archived_at && (
      filter === 'active'
      || (filter === 'current' && !currentCatalogProject.requires_source_reupload)
      || (filter === 'legacy' && currentCatalogProject.requires_source_reupload)
      || (filter === 'kcs_impact' && Boolean(currentCatalogProject.unacknowledged_kcs_impact_count))
      || (filter === 'approval_pending' && currentCatalogProject.status === 'submitted')
      || (filter === 'changes_requested' && currentCatalogProject.status === 'changes_requested')
    ));
  const showCurrentSeparately = !debouncedQuery
    && currentMatchesFilter
    && !catalogProjects.some((item) => item.id === currentCatalogProject.id);
  const visibleProjects = showCurrentSeparately
    ? [currentCatalogProject, ...catalogProjects]
    : catalogProjects;
  const searchPending = query.trim() !== debouncedQuery;

  function marker(item: Project) {
    if (item.requires_source_reupload) return '구버전·재업로드 필요';
    if (Number(item.duplicate_title_count || 0) > 1 && item.is_latest_for_title) return '최신';
    return '';
  }

  async function loadMoreProjects() {
    if (!nextCursor || loadingMore || catalogLoading) return;
    const sequence = requestSequence.current;
    setLoadingMore(true);
    setCatalogError('');
    try {
      const result = await api.projects({
        search: debouncedQuery,
        archiveStatus: filter === 'archived' ? 'archived' : 'active',
        parserStatus: filter === 'current' ? 'current' : filter === 'legacy' ? 'legacy' : undefined,
        kcsImpactOnly: filter === 'kcs_impact',
        reviewStatus: filter === 'approval_pending'
          ? 'submitted'
          : filter === 'changes_requested'
              ? 'changes_requested'
              : undefined,
        discipline: discipline === 'all' ? undefined : discipline,
        limit: PROJECT_CATALOG_PAGE_SIZE,
        cursor: nextCursor,
      });
      if (requestSequence.current !== sequence) return;
      setCatalogProjects((current) => {
        const loadedIds = new Set(current.map((item) => item.id));
        return [...current, ...result.projects.map(normalizeProject).filter((item) => !loadedIds.has(item.id))];
      });
      setCatalogTotal(result.total);
      setNextCursor(result.next_cursor);
      setHasMore(result.has_more);
    } catch (cause) {
      if (requestSequence.current === sequence) {
        setCatalogError(cause instanceof Error ? cause.message : '프로젝트를 더 불러오지 못했습니다.');
      }
    } finally {
      if (requestSequence.current === sequence) setLoadingMore(false);
    }
  }

  return (
    <>
      <Button
        variant="outline"
        className="max-w-72 justify-between"
        onClick={() => setOpen(true)}
        disabled={disabled}
        aria-label="시방서 라이브러리 열기"
      >
        <LibraryBig className="shrink-0" />
        <span className="truncate">{projectDisplayTitle(currentCatalogProject)}</span>
        <ChevronDown className="shrink-0" />
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-2xl gap-3">
          <DialogHeader>
            <DialogTitle>시방서 라이브러리</DialogTitle>
            <DialogDescription>서버에 등록된 시방서를 분야별로 찾아 바로 검토할 수 있습니다.</DialogDescription>
          </DialogHeader>
          <label className="relative block">
            {searchPending ? <Spinner className="absolute left-3 top-2.5 size-4" /> : <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />}
            <input
              className="h-9 w-full rounded-lg border border-input bg-background py-1 pl-9 pr-3 text-sm outline-none placeholder:text-muted-foreground focus:border-ring focus:ring-3 focus:ring-ring/50"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="프로젝트명·원본 파일명·KCS 범위 검색"
            />
          </label>
          <div className="flex flex-wrap items-center gap-1" aria-label="시방서 분야 필터">
            <span className="mr-1 text-xs font-medium text-muted-foreground">분야</span>
            {([['all', '전체'], ['architecture', '건축'], ['mechanical', '건축설비'], ['electrical', '전기']] as [ProjectDiscipline | 'all', string][]).map(([value, label]) => (
              <Button
                key={value}
                size="sm"
                variant={discipline === value ? 'secondary' : 'ghost'}
                aria-pressed={discipline === value}
                onClick={() => setDiscipline(value)}
              >
                {label}
              </Button>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-1" aria-label="프로젝트 상태 필터">
            {([['active', '전체'], ['approval_pending', '승인 대기'], ['changes_requested', '수정 요청'], ['kcs_impact', 'KCS 재검토'], ['current', '현행 파서'], ['legacy', '재업로드'], ['archived', '보관됨']] as [ProjectListFilter, string][]).map(([value, label]) => (
              <Button
                key={value}
                size="sm"
                variant={filter === value ? 'secondary' : 'ghost'}
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
              >
                {label}
              </Button>
            ))}
            <Badge variant="outline" className="ml-auto tabular-nums">{catalogProjects.length} / {catalogTotal}</Badge>
          </div>
          <div className="max-h-[55vh] space-y-1 overflow-y-auto pr-1">
            {catalogLoading || searchPending ? (
              <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed p-8 text-sm text-muted-foreground"><Spinner />프로젝트를 찾고 있습니다.</div>
            ) : catalogError && !visibleProjects.length ? (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                <p role="alert">{catalogError}</p>
                <Button className="mt-3" size="sm" variant="outline" onClick={() => setRetryKey((value) => value + 1)}><RefreshCw />다시 시도</Button>
              </div>
            ) : visibleProjects.length ? visibleProjects.map((item) => {
              const itemMarker = marker(item);
              const selected = item.id === currentCatalogProject.id;
              return (
                <div
                  key={item.id}
                  className={cn(
                    'flex w-full items-start gap-3 rounded-lg border px-3 py-2.5 text-left transition-colors hover:bg-muted/60',
                    selected ? 'border-primary/50 bg-primary/5' : 'border-transparent',
                  )}
                >
                  <button
                    type="button"
                    className="min-w-0 flex-1 text-left disabled:cursor-not-allowed disabled:opacity-60"
                    disabled={archiveBusyId !== null}
                    onClick={() => {
                      setOpen(false);
                      if (!selected) onSelect(item.id);
                    }}
                  >
                    <span className="flex flex-wrap items-center gap-1.5">
                      <span className="truncate font-medium">{projectDisplayTitle(item)}</span>
                      <Badge variant={reviewStatusVariant(item.status)}>{reviewStatusLabel(item.status)}</Badge>
                      {itemMarker && <Badge variant={item.requires_source_reupload ? 'destructive' : 'secondary'}>{itemMarker}</Badge>}
                      {item.unacknowledged_kcs_impact_count ? <Badge variant="outline">KCS 재검토 {item.unacknowledged_kcs_impact_count}</Badge> : null}
                      {item.archived_at && <Badge variant="outline">보관됨</Badge>}
                    </span>
                    <span className="mt-1 block truncate text-xs text-muted-foreground">{item.source_filename} · {snapshotDate(item.uploaded_at)} · ID {item.id.slice(0, 6)}</span>
                  </button>
                  {selected && <Check className="mt-0.5 size-4 shrink-0 text-primary" />}
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={archiveBusyId !== null || (selected && !item.archived_at)}
                    title={selected && !item.archived_at ? '현재 검토 중인 프로젝트는 다른 프로젝트로 이동한 뒤 보관할 수 있습니다.' : undefined}
                    onClick={() => onArchive(item, !item.archived_at)}
                  >
                    {archiveBusyId === item.id ? <Spinner /> : null}{item.archived_at ? '복원' : '보관'}
                  </Button>
                </div>
              );
            }) : (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">조건에 맞는 프로젝트가 없습니다.</div>
            )}
            {catalogError && visibleProjects.length ? <p role="alert" className="px-3 py-2 text-center text-xs text-destructive">{catalogError}</p> : null}
            {hasMore && nextCursor && (
              <Button className="w-full" variant="outline" onClick={() => { void loadMoreProjects(); }} disabled={loadingMore || catalogLoading}>
                {loadingMore ? <Spinner /> : <ChevronDown />}더 보기
              </Button>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

function UploadPanel({
  config,
  uploading,
  uploadJob,
  uploadPollError,
  syncingKcs,
  error,
  onUpload,
  onOpenProject,
  onRetryItem,
  retryingItemId,
  onRefresh,
  backupMutationBlocked,
  backupBlockedReason,
}: {
  config: KcsConfig | null;
  uploading: boolean;
  uploadJob: UploadJob | null;
  uploadPollError: string;
  syncingKcs: boolean;
  error: string;
  onUpload: (files: File[]) => void;
  onOpenProject: (projectId: string) => void;
  onRetryItem: (jobId: string, itemId: string) => void;
  retryingItemId: string | null;
  onRefresh: () => void;
  backupMutationBlocked: boolean;
  backupBlockedReason: string;
}) {
  const fileInput = useRef<HTMLInputElement>(null);

  function selectFile(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files || []);
    if (files.length) onUpload(files);
    event.target.value = '';
  }

  function dropFile(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault();
    const files = Array.from(event.dataTransfer.files || []);
    if (files.length && !uploading) onUpload(files);
  }

  return (
    <main className="grid min-h-screen place-items-center bg-background px-5 py-12 text-foreground">
      <section className="w-full max-w-3xl">
        <div className="mb-8 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="grid size-11 place-items-center rounded-xl bg-primary text-primary-foreground">
              <Layers3 className="size-6" />
            </div>
            <div>
              <h1 className="text-2xl font-semibold tracking-tight">시방서 정합성 검토</h1>
              <p className="text-sm text-muted-foreground">포스코 고유기준을 최신 KCS와 분리합니다.</p>
            </div>
          </div>
          <BackupManager mutationBlocked={backupMutationBlocked} blockedReason={backupBlockedReason} />
        </div>

        {error && (
          <Alert variant="destructive" className="mb-4">
            <AlertTriangle />
            <AlertTitle>업로드할 수 없습니다</AlertTitle>
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <input ref={fileInput} className="sr-only" type="file" accept=".doc,.docx" multiple onChange={selectFile} disabled={uploading} />
        <button
          type="button"
          className={cn(
            'flex min-h-80 w-full cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed bg-card px-8 text-center transition-colors',
            'border-border hover:border-primary/55',
            uploading && 'pointer-events-none opacity-70',
          )}
          onClick={() => fileInput.current?.click()}
          onDragOver={(event) => event.preventDefault()}
          onDrop={dropFile}
          disabled={uploading}
        >
          {uploading ? <Spinner className="mb-5 size-9 text-primary" /> : <UploadCloud className="mb-5 size-10 text-primary" />}
          <h2 className="text-xl font-semibold">{uploading ? '여러 문서를 순서대로 분석하고 있습니다' : '포스코 시방서 일괄 업로드'}</h2>
          <p className="mt-2 max-w-lg text-sm leading-6 text-muted-foreground">
            {uploading
              ? '완료된 문서부터 프로젝트에 저장됩니다. 이 창을 닫지 않으면 전체 진행 상황을 계속 확인할 수 있습니다.'
              : `DOC·DOCX 파일을 최대 200개까지 한 번에 끌어놓거나 선택하세요. DOC 파일은 ${config?.doc_upload_available === false ? '현재 서버에서 변환할 수 없으므로 DOCX로 저장 후 업로드하세요.' : '클라우드 분석 서버에서 DOCX로 자동 변환한 뒤 분석합니다.'}`}
          </p>
          {!uploading && <span className={cn(buttonVariants({ variant: 'default' }), 'mt-6')}><Plus />문서 선택</span>}
        </button>

        {uploadJob && (
          <div className="mt-5 rounded-xl border border-border bg-card p-4">
            <UploadJobPanel
              job={uploadJob}
              pollError={uploadPollError}
              onOpenProject={onOpenProject}
              onRetryFiles={() => fileInput.current?.click()}
              onRetryItem={onRetryItem}
              retryingItemId={retryingItemId}
            />
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3 text-sm text-muted-foreground">
          <span>{config?.doc_upload_available === false ? '지원 형식: DOCX · DOC는 변환 기능 점검 필요' : '지원 형식: DOCX · DOC 자동 변환 지원'}</span>
          <span className="flex items-center gap-2">
            KCS {config?.kcs_available ? `${config.kcs_usable_document_count ?? config.kcs_document_count ?? 0}건 검색 가능${config.kcs_unavailable_document_count ? ` · 본문 제외 ${config.kcs_unavailable_document_count}건` : ''} · ${snapshotDate(config.kcs_snapshot)}` : '데이터 연결 확인 필요'}
            <Badge variant="outline">{config?.openai_available ? 'GPT 설정됨' : '로컬 매칭'}</Badge>
            <Button size="sm" variant="ghost" onClick={onRefresh} disabled={syncingKcs || uploading}>{syncingKcs ? <Spinner /> : <RefreshCw />}갱신</Button>
          </span>
        </div>
      </section>

    </main>
  );
}

function CandidatePanel({
  candidate,
  selected,
  onSelect,
  aiAvailable,
  analysisAllowed,
  analyzing,
  interactionDisabled,
  selectionDisabled,
  onAnalyze,
  onApplySimplified,
  showSelection = true,
}: {
  candidate: Candidate;
  selected: boolean;
  onSelect: () => void;
  aiAvailable: boolean;
  analysisAllowed: boolean;
  analyzing: boolean;
  interactionDisabled?: boolean;
  selectionDisabled?: boolean;
  onAnalyze: () => void;
  onApplySimplified?: (content: string) => void;
  showSelection?: boolean;
}) {
  return (
    <div className="pt-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="font-semibold text-primary">{candidate.kcs_code} · {candidate.kcs_clause || '본문'}</p>
          <p className="mt-1 text-sm text-muted-foreground">{candidate.document_name}</p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={candidate.score >= 0.45 ? 'default' : 'secondary'}>{candidate.classification}</Badge>
          <Badge variant="outline">{percent(candidate.score)}</Badge>
        </div>
      </div>

      <div className="rounded-lg border border-border bg-background p-4">
        <p className="mb-2 text-sm font-medium">{candidate.title}</p>
        <p className="whitespace-pre-wrap text-base leading-7">{candidate.content}</p>
      </div>

      {(candidate.reasons.length > 0 || candidate.warnings.length > 0) && (
        <div className="mt-3 space-y-2 text-sm">
          {candidate.reasons.map((reason) => <p key={reason} className="text-muted-foreground">{reason}</p>)}
          {candidate.warnings.map((warning) => (
            <p key={warning} className="flex items-start gap-2 text-amber-700 dark:text-amber-300">
              <AlertTriangle className="mt-0.5 size-4 shrink-0" />{warning}
            </p>
          ))}
        </div>
      )}

      <div className="mt-4 rounded-lg border border-border bg-muted/35 p-4">
        {candidate.ai_analysis ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="flex items-center gap-2 text-sm font-medium"><Sparkles className="size-4 text-primary" />GPT 정밀 판정</p>
              <div className="flex items-center gap-2">
                <Badge variant={candidate.ai_analysis.relation_type === 'conflict' ? 'destructive' : 'secondary'}>
                  {aiRelationLabel(candidate.ai_analysis.relation_type)}
                </Badge>
                <Badge variant="outline">신뢰도 {percent(candidate.ai_analysis.confidence)}</Badge>
                {analysisAllowed && (
                  <Button size="sm" variant="ghost" onClick={onAnalyze} disabled={!aiAvailable || analyzing || interactionDisabled}>
                    {analyzing ? <Spinner /> : <RefreshCw />}{analyzing ? '분석 중' : '다시 분석'}
                  </Button>
                )}
              </div>
            </div>
            <p className="mt-3 text-sm leading-6 text-muted-foreground">{candidate.ai_analysis.rationale}</p>
            {candidate.ai_analysis.relation_type === 'unrelated' && candidate.ai_analysis.confidence < 0.85 && (
              <p className="mt-2 flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />신뢰도가 85% 미만이어서 자동 제외하지 않았습니다. 담당자가 직접 확인하세요.
              </p>
            )}
            {candidate.ai_analysis.simplified_content && (
              <div className="mt-3 rounded-md border border-border bg-background p-3">
                <p className="text-xs font-medium text-muted-foreground">포스코 고유내용 간소화 제안</p>
                <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{candidate.ai_analysis.simplified_content}</p>
                {onApplySimplified && (
                  <Button className="mt-3" size="sm" variant="outline" onClick={() => onApplySimplified(candidate.ai_analysis!.simplified_content)} disabled={interactionDisabled}>
                    출력문에 적용
                  </Button>
                )}
              </div>
            )}
          </>
        ) : (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <p className="flex items-center gap-2 text-sm font-medium"><Sparkles className="size-4 text-primary" />GPT 정밀 판정</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {aiAvailable
                  ? analysisAllowed
                    ? '기술적 의무와 적용 대상을 비교하고 포스코 고유내용을 제안합니다.'
                    : '이 조항은 50개 품질평가의 고정 표본이므로 후보군을 바꾸는 정밀분석을 실행하지 않습니다.'
                  : 'OPENAI_API_KEY를 설정하면 정밀 판정과 간소화 제안을 사용할 수 있습니다.'}
              </p>
            </div>
            <Button size="sm" variant="outline" onClick={onAnalyze} disabled={!aiAvailable || !analysisAllowed || analyzing || interactionDisabled}>
              {analyzing ? <Spinner /> : <Sparkles />}{analyzing ? '분석 중' : '정밀분석'}
            </Button>
          </div>
        )}
      </div>

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
        <span className="text-sm text-muted-foreground">버전 {candidate.version} · 개정 {candidate.update_date || '확인 필요'}</span>
        {showSelection && (
          <Button variant={selected ? 'default' : 'outline'} aria-pressed={selected} onClick={onSelect} disabled={interactionDisabled || selectionDisabled}>
            {selected ? <Check /> : <Circle />}{selected ? '판정 근거 선택 해제' : '판정 근거로 선택'}
          </Button>
        )}
      </div>
    </div>
  );
}

export function SpecReviewApp() {
  const uploadInput = useRef<HTMLInputElement>(null);
  const bulkScrollPosition = useRef(0);
  const currentProjectId = useRef<string | null>(null);
  const currentClauseId = useRef<string | null>(null);
  const projectRequestSequence = useRef(0);
  const bulkSavingRef = useRef(false);
  const detailSavingRef = useRef(false);
  const dirtyRef = useRef(false);
  const startingKcsRematchRef = useRef(false);
  const finalizedUploadJobsRef = useRef(new Set<string>());
  const detailHeadingRef = useRef<HTMLHeadingElement>(null);
  const focusDetailAfterOpen = useRef(false);
  const [config, setConfig] = useState<KcsConfig | null>(null);
  const [project, setProject] = useState<Project | null>(null);
  const [clauses, setClauses] = useState<ClauseSummary[]>([]);
  const [activeClauseId, setActiveClauseId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ClauseDetail | null>(null);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [reviewMode, setReviewMode] = useState<ReviewMode>('business');
  const [businessView, setBusinessView] = useState<BusinessView>('bulk');
  const [bulkRefreshKey, setBulkRefreshKey] = useState(0);
  const [bulkKcsImpactFilterKey, setBulkKcsImpactFilterKey] = useState(0);
  const [bulkSaving, setBulkSaving] = useState(false);
  const [qualityEvaluation, setQualityEvaluation] = useState<QualityEvaluation | null>(null);
  const [qualityLoading, setQualityLoading] = useState(false);
  const [qualitySaving, setQualitySaving] = useState(false);
  const [qualityDraftDirty, setQualityDraftDirty] = useState(false);
  const [analyzingCandidateId, setAnalyzingCandidateId] = useState<string | null>(null);
  const [analyzingCoverage, setAnalyzingCoverage] = useState(false);
  const [restoringCandidateId, setRestoringCandidateId] = useState<string | null>(null);
  const [structureDialog, setStructureDialog] = useState<StructureEditDialog | null>(null);
  const [structureBusy, setStructureBusy] = useState(false);
  const [structureError, setStructureError] = useState('');
  const [reviewWorkflowDialog, setReviewWorkflowDialog] = useState<ReviewWorkflowDialog>(null);
  const [reviewActorName, setReviewActorName] = useState('');
  const [reviewWorkflowNote, setReviewWorkflowNote] = useState('');
  const [reviewChangeReason, setReviewChangeReason] = useState('');
  const [reviewWorkflowBusy, setReviewWorkflowBusy] = useState(false);
  const [reviewWorkflowError, setReviewWorkflowError] = useState('');
  const [qualityVerdict, setQualityVerdict] = useState<QualityVerdict>(null);
  const [expectedKcsCode, setExpectedKcsCode] = useState('');
  const [expectedKcsClause, setExpectedKcsClause] = useState('');
  const [qualityNote, setQualityNote] = useState('');
  const [decision, setDecision] = useState<Decision>(null);
  const [editedContent, setEditedContent] = useState('');
  const [reviewNote, setReviewNote] = useState('');
  const [decisionReason, setDecisionReason] = useState('');
  const [coverageConfirmed, setCoverageConfirmed] = useState(false);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [candidateTab, setCandidateTab] = useState('');
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [uploadJob, setUploadJob] = useState<UploadJob | null>(null);
  const [uploadPollError, setUploadPollError] = useState('');
  const [uploadStatusOpen, setUploadStatusOpen] = useState(false);
  const [startingUploadJob, setStartingUploadJob] = useState(false);
  const [retryingUploadItemId, setRetryingUploadItemId] = useState<string | null>(null);
  const [archiveBusyId, setArchiveBusyId] = useState<string | null>(null);
  const [projectCatalogRefreshKey, setProjectCatalogRefreshKey] = useState(0);
  const [switchingProject, setSwitchingProject] = useState(false);
  const [syncingKcs, setSyncingKcs] = useState(false);
  const [startingKcsRematch, setStartingKcsRematch] = useState(false);
  const [kcsRematchPollError, setKcsRematchPollError] = useState('');
  const [detailRefreshKey, setDetailRefreshKey] = useState(0);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const uploading = startingUploadJob || Boolean(uploadJob && (uploadJob.status === 'queued' || uploadJob.status === 'running'));

  const handleBulkBusyChange = useCallback((busy: boolean) => {
    bulkSavingRef.current = busy;
    setBulkSaving(busy);
  }, []);

  const openProject = useCallback(async (projectId: string) => {
    if (bulkSavingRef.current || detailSavingRef.current) return;
    const requestSequence = ++projectRequestSequence.current;
    detailSavingRef.current = true;
    setSwitchingProject(true);
    setError('');
    try {
      const result = await api.project(projectId);
      if (projectRequestSequence.current !== requestSequence) return;
      const nextProject = normalizeProject(result.project);
      currentProjectId.current = nextProject.id;
      setProject(nextProject);
      setKcsRematchPollError('');
      bulkSavingRef.current = false;
      setBulkSaving(false);
      setClauses(result.clauses);
      setReviewMode('business');
      setBusinessView('bulk');
      bulkScrollPosition.current = 0;
      setReviewWorkflowDialog(null);
      setReviewActorName('');
      setReviewWorkflowNote('');
      setReviewChangeReason('');
      setReviewWorkflowError('');
      setQualityEvaluation(null);
      setQualityDraftDirty(false);
      setDetail(null);
      const first = result.clauses.find((clause) => clause.decision === null) || result.clauses[0];
      currentClauseId.current = first?.id || null;
      setActiveClauseId(first?.id || null);
    } finally {
      if (projectRequestSequence.current === requestSequence) {
        detailSavingRef.current = false;
        setSwitchingProject(false);
      }
    }
  }, []);

  const activeQualityItem = useMemo(
    () => qualityEvaluation?.items.find((item) => item.clause_id === activeClauseId) || null,
    [qualityEvaluation, activeClauseId],
  );
  const activeProjectId = project?.id ?? null;
  const activeKcsRematchId = project?.kcs_rematch?.id ?? null;
  const activeKcsRematchStatus = project?.kcs_rematch?.status ?? null;
  const activeUploadJobId = uploadJob?.id ?? null;
  const activeUploadJobStatus = uploadJob?.status ?? null;

  useEffect(() => {
    currentClauseId.current = activeClauseId;
  }, [activeClauseId]);

  useEffect(() => {
    dirtyRef.current = dirty;
  }, [dirty]);

  useEffect(() => {
    if (!dirty && !qualityDraftDirty && !saving && !qualitySaving && !bulkSaving && !structureBusy && !reviewWorkflowBusy && !startingKcsRematch && !uploading) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, [bulkSaving, dirty, qualityDraftDirty, qualitySaving, reviewWorkflowBusy, saving, startingKcsRematch, structureBusy, uploading]);

  useEffect(() => {
    if (businessView !== 'detail' || !detail || !focusDetailAfterOpen.current) return;
    const frame = window.requestAnimationFrame(() => {
      detailHeadingRef.current?.focus();
      focusDetailAfterOpen.current = false;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [businessView, detail]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      api.config(),
      api.projects({ archiveStatus: 'active' }),
      api.uploadJobs(20).catch(() => ({ jobs: [] as UploadJob[] })),
    ])
      .then(async ([configuration, result, uploadJobs]) => {
        if (cancelled) return;
        setConfig(configuration);
        const normalized = result.projects.map(normalizeProject);
        const latestUploadJob = uploadJobs.jobs.find((job) => ['queued', 'running'].includes(job.status)) || uploadJobs.jobs[0];
        if (latestUploadJob) {
          if (latestUploadJob.status === 'completed' || latestUploadJob.status === 'failed') {
            finalizedUploadJobsRef.current.add(latestUploadJob.id);
          }
          setUploadJob(latestUploadJob);
        }
        let initialProject: Project | undefined = normalized[0];
        if (!initialProject) {
          const archived = await api.projects({ archiveStatus: 'archived', limit: 1 });
          if (cancelled) return;
          initialProject = archived.projects[0] ? normalizeProject(archived.projects[0]) : undefined;
        }
        if (initialProject) await openProject(initialProject.id);
      })
      .catch((cause: Error) => setError(`로컬 분석 서버에 연결할 수 없습니다. ${cause.message}`))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [openProject]);

  useEffect(() => {
    if (
      !activeUploadJobId
      || !activeUploadJobStatus
      || !['queued', 'running'].includes(activeUploadJobStatus)
    ) return;
    const jobId = activeUploadJobId;
    let cancelled = false;
    let retryDelay = 1500;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const poll = async () => {
      try {
        const result = await api.uploadJob(jobId);
        if (cancelled) return;
        setUploadJob(result.job);
        setUploadPollError('');
        retryDelay = 1500;
      } catch (cause) {
        if (cancelled) return;
        setUploadPollError(
          cause instanceof Error
            ? `업로드 진행 상태를 확인하지 못했습니다. 자동으로 다시 시도합니다: ${cause.message}`
            : '업로드 진행 상태를 확인하지 못했습니다. 자동으로 다시 시도합니다.',
        );
        retryDelay = Math.min(retryDelay * 2, 15000);
      }
      if (!cancelled) timer = setTimeout(poll, retryDelay);
    };

    timer = setTimeout(poll, activeUploadJobStatus === 'queued' ? 500 : 1500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeUploadJobId, activeUploadJobStatus]);

  useEffect(() => {
    if (
      !uploadJob
      || !['completed', 'failed'].includes(uploadJob.status)
      || finalizedUploadJobsRef.current.has(uploadJob.id)
    ) return;
    finalizedUploadJobsRef.current.add(uploadJob.id);
    const completedJob = uploadJob;
    setUploadPollError('');
    setProjectCatalogRefreshKey((current) => current + 1);
    const firstCompletedProjectId = completedJob.items.find(
      (item) => item.status === 'completed' && item.project_id,
    )?.project_id;
    const openCompletedProject = !currentProjectId.current && firstCompletedProjectId
      ? openProject(firstCompletedProjectId)
      : Promise.resolve();
    openCompletedProject
      .then(() => {
        if (completedJob.completed > 0) {
          setNotice(`문서 ${completedJob.completed}개를 프로젝트로 만들었습니다${completedJob.failed ? ` · 실패 ${completedJob.failed}개는 업로드 현황에서 확인해 주세요` : ''}.`);
        } else {
          setError(completedJob.error || '업로드한 문서를 처리하지 못했습니다. 실패 파일을 다시 선택해 주세요.');
        }
      })
      .catch((cause: Error) => {
        setUploadPollError(`업로드는 끝났지만 완료 프로젝트를 열지 못했습니다: ${cause.message}`);
      });
  }, [openProject, uploadJob]);

  useEffect(() => {
    if (
      !activeProjectId
      || !activeKcsRematchId
      || !activeKcsRematchStatus
      || !['pending', 'running'].includes(activeKcsRematchStatus)
    ) return;
    const projectId = activeProjectId;
    let cancelled = false;
    let retryDelay = 1500;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const updateRun = (nextRun: KcsRematchRun | null) => {
      if (!nextRun || currentProjectId.current !== projectId) return;
      setProject((current) => current?.id === projectId
        ? normalizeProject({ ...current, kcs_rematch: nextRun })
        : current);
    };

    const poll = async () => {
      try {
        const result = await api.kcsImpact(projectId);
        if (cancelled || currentProjectId.current !== projectId) return;
        setKcsRematchPollError('');
        const impactByClause = new Map(result.impacts.map((impact) => [impact.clause_id, impact]));
        if (impactByClause.size) {
          setClauses((current) => current.map((clause) => impactByClause.has(clause.id)
            ? { ...clause, kcs_impact: impactByClause.get(clause.id) || null }
            : clause));
          setDetail((current) => current && impactByClause.has(current.id)
            ? { ...current, kcs_impact: impactByClause.get(current.id) || null }
            : current);
        }

        if (!result.run || !['pending', 'running'].includes(result.run.status)) {
          const refreshed = await api.project(projectId);
          if (cancelled || currentProjectId.current !== projectId) return;
          const nextProject = normalizeProject(refreshed.project);
          setProject(nextProject);
          setClauses(refreshed.clauses);
          setBulkRefreshKey((current) => current + 1);
          if (!dirtyRef.current && !detailSavingRef.current) {
            setDetail(null);
            setDetailRefreshKey((current) => current + 1);
          }
          return;
        }
        updateRun(result.run);
        retryDelay = 1500;
      } catch (cause) {
        if (cancelled) return;
        setKcsRematchPollError(
          cause instanceof Error
            ? `진행 상태를 확인하지 못했습니다. 자동으로 다시 시도합니다: ${cause.message}`
            : '진행 상태를 확인하지 못했습니다. 자동으로 다시 시도합니다.',
        );
        retryDelay = Math.min(retryDelay * 2, 15000);
      }
      if (!cancelled) timer = setTimeout(poll, retryDelay);
    };

    timer = setTimeout(poll, activeKcsRematchStatus === 'pending' ? 500 : 1500);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeKcsRematchId, activeKcsRematchStatus, activeProjectId]);

  useEffect(() => {
    if (
      !activeProjectId
      || !activeClauseId
      || (reviewMode === 'business' && businessView === 'bulk')
    ) return;
    let cancelled = false;
    api.clause(activeProjectId, activeClauseId)
      .then(({ clause }) => {
        if (cancelled) return;
        const qualityItem = qualityEvaluation?.items.find((item) => item.clause_id === clause.id) || null;
        setDetail(clause);
        setDecision(clause.decision);
        setEditedContent(clause.edited_content || clause.content || clause.title);
        setReviewNote(clause.review_note || '');
        setDecisionReason(clause.decision_reason || '');
        setCoverageConfirmed(Boolean(clause.coverage_confirmed));
        setSelectedCandidateId(clause.selected_candidate_id);
        const preferredCandidateId = qualityItem?.relevant_candidate_id;
        setCandidateTab(
          preferredCandidateId && clause.candidates.some((candidate) => candidate.id === preferredCandidateId)
            ? preferredCandidateId
            : clause.candidates[0]?.id || 'none',
        );
        setQualityVerdict(qualityItem?.verdict || null);
        setExpectedKcsCode(qualityItem?.expected_kcs_code || '');
        setExpectedKcsClause(qualityItem?.expected_kcs_clause || '');
        setQualityNote(qualityItem?.quality_note || '');
        setQualityDraftDirty(false);
        setDirty(false);
        setSavedAt(null);
      })
      .catch((cause: Error) => setError(cause.message));
    return () => { cancelled = true; };
  }, [activeProjectId, activeClauseId, businessView, detailRefreshKey, qualityEvaluation, reviewMode]);

  useEffect(() => {
    const context = typeof document === 'undefined' ? undefined : document.modelContext;
    if (
      !context?.registerTool
      || !project
      || !detail
      || (reviewMode === 'business' && businessView === 'bulk')
    ) return;
    const lifecycle = new AbortController();
    const allowedDecisions = new Set(['keep', 'delete', 'hold']);
    void Promise.resolve(context.registerTool({
      name: 'save_current_clause_decision',
      title: '현재 조항 판정 저장',
      description: '현재 화면의 포스코 시방서 조항을 남김, 삭제 또는 보류로 판정하고 검토의견을 저장합니다.',
      inputSchema: {
        type: 'object',
        properties: {
          decision: { type: 'string', enum: ['keep', 'delete', 'hold'] },
          reviewNote: { type: 'string' },
          editedContent: { type: 'string' },
          decisionReason: {
            type: 'string',
            enum: Object.values(DECISION_REASON_OPTIONS).flat().map((option) => option.value),
          },
          coverageConfirmed: { type: 'boolean' },
          selectedCandidateId: { type: ['string', 'null'] },
        },
        required: ['decision', 'decisionReason', 'coverageConfirmed'],
        additionalProperties: false,
      },
      annotations: { readOnlyHint: false, untrustedContentHint: false },
      async execute(input) {
        if (detailSavingRef.current || bulkSavingRef.current) {
          throw new Error('다른 저장 작업이 끝난 뒤 다시 시도해 주세요.');
        }
        if (!input || typeof input !== 'object') throw new Error('판정 입력이 필요합니다.');
        const values = input as Record<string, unknown>;
        if (typeof values.decision !== 'string' || !allowedDecisions.has(values.decision)) {
          throw new Error('decision은 keep, delete, hold 중 하나여야 합니다.');
        }
        if (detail.source_type === 'heading') {
          throw new Error('목차와 구조 제목은 판정 대상이 아닙니다.');
        }
        const nextDecision = values.decision as Exclude<Decision, null>;
        if (nextDecision === 'delete') {
          throw new Error('삭제 판정은 담당자가 화면의 확인창에서 직접 확인해야 합니다.');
        }
        const nextReason = typeof values.decisionReason === 'string' ? values.decisionReason : '';
        if (!isDecisionReason(nextDecision, nextReason)) {
          throw new Error('판정에 맞는 decisionReason을 지정해 주세요.');
        }
        const nextCoverageConfirmed = values.coverageConfirmed === true;
        const nextCandidateId = typeof values.selectedCandidateId === 'string' || values.selectedCandidateId === null
          ? values.selectedCandidateId
          : selectedCandidateId;
        const requestProjectId = project.id;
        const requestClauseId = detail.id;
        detailSavingRef.current = true;
        setSaving(true);
        try {
          const response = await api.saveClause(project.id, detail.id, {
            decision: nextDecision,
            edited_content: typeof values.editedContent === 'string' ? values.editedContent : editedContent,
            review_note: typeof values.reviewNote === 'string' ? values.reviewNote : reviewNote,
            decision_reason: nextReason,
            coverage_confirmed: nextCoverageConfirmed,
            selected_candidate_id: nextCandidateId,
            ...kcsImpactAcknowledgement(detail),
          });
          if (
            currentProjectId.current !== requestProjectId
            || currentClauseId.current !== requestClauseId
          ) {
            throw new Error('검토 위치가 변경되어 이전 저장 응답을 화면에 반영하지 않았습니다.');
          }
          const nextProject = normalizeProject(response.project);
          setDetail(response.clause);
          setProject(nextProject);
          setDecision(response.clause.decision);
          setEditedContent(response.clause.edited_content || response.clause.content || response.clause.title);
          setReviewNote(response.clause.review_note || '');
          setDecisionReason(response.clause.decision_reason || '');
          setCoverageConfirmed(Boolean(response.clause.coverage_confirmed));
          setSelectedCandidateId(response.clause.selected_candidate_id);
          setBulkRefreshKey((current) => current + 1);
          setClauses((items) => items.map((item) => item.id === response.clause.id ? {
            ...item,
            decision: response.clause.decision,
            decision_reason: response.clause.decision_reason,
            coverage_confirmed: response.clause.coverage_confirmed,
            reviewed_at: response.clause.reviewed_at,
            kcs_impact: response.clause.kcs_impact,
          } : item));
          setDirty(false);
          setSavedAt(new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }));
          return { clauseId: response.clause.id, decision: response.clause.decision, saved: true };
        } finally {
          detailSavingRef.current = false;
          setSaving(false);
        }
      },
    }, { signal: lifecycle.signal })).catch(() => undefined);
    return () => lifecycle.abort();
  }, [project, detail, editedContent, reviewNote, selectedCandidateId, businessView, reviewMode]);

  async function upload(files: File[]) {
    if (uploading || bulkSavingRef.current || detailSavingRef.current || !files.length) return;
    if (files.length > 200) {
      setError('한 번에 최대 200개 파일까지 업로드할 수 있습니다.');
      return;
    }
    const unsupported = files.find((file) => !/\.docx?$/i.test(file.name));
    if (unsupported) {
      setError(`${unsupported.name}: DOC 또는 DOCX 파일만 업로드할 수 있습니다.`);
      return;
    }
    const oversized = files.find((file) => file.size > 25 * 1024 * 1024);
    if (oversized) {
      setError(`${oversized.name}: 파일 크기는 25MB 이하여야 합니다.`);
      return;
    }
    const empty = files.find((file) => file.size === 0);
    if (empty) {
      setError(`${empty.name}: 빈 파일은 업로드할 수 없습니다.`);
      return;
    }
    const totalSize = files.reduce((sum, file) => sum + file.size, 0);
    if (totalSize > 250 * 1024 * 1024) {
      setError('한 업로드 작업의 전체 파일 크기는 250MB 이하여야 합니다. 여러 번 나누어 업로드해 주세요.');
      return;
    }
    if (qualityDraftDirty) {
      setError('작성 중인 품질평가 누락 근거를 먼저 저장한 뒤 새 문서를 업로드해 주세요.');
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    setError('');
    setNotice('');
    setUploadPollError('');
    setUploadStatusOpen(Boolean(currentProjectId.current));
    setUploadJob(null);
    setStartingUploadJob(true);
    try {
      const { job } = await api.startUploadJob(files);
      finalizedUploadJobsRef.current.delete(job.id);
      setUploadJob(job);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '업로드 중 오류가 발생했습니다.');
    } finally {
      setStartingUploadJob(false);
    }
  }

  async function retryUploadItem(jobId: string, itemId: string) {
    if (retryingUploadItemId || startingUploadJob) return;
    setRetryingUploadItemId(itemId);
    setUploadPollError('');
    setError('');
    try {
      const { job } = await api.retryUploadJobItem(jobId, itemId);
      finalizedUploadJobsRef.current.delete(job.id);
      setUploadJob(job);
      setUploadStatusOpen(Boolean(currentProjectId.current));
    } catch (cause) {
      setUploadPollError(cause instanceof Error ? cause.message : '실패한 파일을 다시 처리하지 못했습니다.');
    } finally {
      setRetryingUploadItemId(null);
    }
  }

  async function setProjectArchived(item: Project, archived: boolean) {
    if (archiveBusyId || (archived && item.id === currentProjectId.current)) return;
    setArchiveBusyId(item.id);
    setError('');
    try {
      const result = await api.archiveProject(item.id, archived);
      const nextProject = normalizeProject(result.project);
      if (currentProjectId.current === nextProject.id) setProject(nextProject);
      setProjectCatalogRefreshKey((current) => current + 1);
      setNotice(archived ? `${projectDisplayTitle(item)} 프로젝트를 보관했습니다.` : `${projectDisplayTitle(item)} 프로젝트를 복원했습니다.`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '프로젝트 보관 상태를 변경하지 못했습니다.');
    } finally {
      setArchiveBusyId(null);
    }
  }

  function applyKcsRematchRun(projectId: string, run: KcsRematchRun) {
    if (currentProjectId.current === projectId) {
      setProject((current) => current?.id === projectId
        ? normalizeProject({ ...current, kcs_rematch: run })
        : current);
    }
  }

  async function startKcsRematch(projectId: string, announce = true) {
    if (startingKcsRematchRef.current) return null;
    startingKcsRematchRef.current = true;
    setStartingKcsRematch(true);
    setKcsRematchPollError('');
    setError('');
    try {
      const { run } = await api.startKcsRematch(projectId);
      applyKcsRematchRun(projectId, run);
      if (!['pending', 'running'].includes(run.status) && currentProjectId.current === projectId) {
        const refreshed = await api.project(projectId);
        if (currentProjectId.current === projectId) {
          const nextProject = normalizeProject(refreshed.project);
          setProject(nextProject);
          setClauses(refreshed.clauses);
          setBulkRefreshKey((current) => current + 1);
          if (!dirtyRef.current && !detailSavingRef.current) setDetailRefreshKey((current) => current + 1);
        }
      }
      if (announce && currentProjectId.current === projectId) {
        setNotice(run.status === 'completed'
          ? '최신 KCS 영향 분석과 자동 재매칭을 완료했습니다.'
          : '최신 KCS 영향 분석과 자동 재매칭을 시작했습니다. 다른 조항은 계속 검토할 수 있습니다.');
      }
      return run;
    } catch (cause) {
      if (currentProjectId.current === projectId) {
        setError(cause instanceof Error ? cause.message : 'KCS 자동 재매칭을 시작하지 못했습니다.');
      }
      return null;
    } finally {
      startingKcsRematchRef.current = false;
      setStartingKcsRematch(false);
    }
  }

  async function refreshKcs() {
    if (bulkSavingRef.current || detailSavingRef.current) return;
    const requestProjectId = currentProjectId.current;
    const previousRevision = project?.kcs_revision || config?.kcs_revision || '';
    setSyncingKcs(true);
    setError('');
    setNotice('');
    try {
      const result = await api.syncKcs();
      const latest = await api.config();
      setConfig(latest);
      setProjectCatalogRefreshKey((current) => current + 1);
      let rematchRun: KcsRematchRun | null = null;
      let requiresSourceReupload = false;
      if (requestProjectId && currentProjectId.current === requestProjectId) {
        const refreshed = await api.project(requestProjectId);
        if (currentProjectId.current === requestProjectId) {
          const refreshedProject = normalizeProject(refreshed.project);
          setProject(refreshedProject);
          requiresSourceReupload = Boolean(refreshedProject.requires_source_reupload);
          if (refreshedProject.kcs_stale && !refreshedProject.requires_source_reupload) {
            rematchRun = refreshedProject.kcs_rematch
              && ['pending', 'running'].includes(refreshedProject.kcs_rematch.status)
              ? refreshedProject.kcs_rematch
              : await startKcsRematch(requestProjectId, false);
          }
        }
      }
      setNotice(
        requiresSourceReupload
          ? `KCS 목록을 갱신했습니다. 현재 프로젝트는 이전 문서 해석기로 생성되어 자동 재매칭만으로 조항 구조를 보정할 수 없습니다. 같은 원본을 새 문서로 다시 업로드해 주세요.`
          : rematchRun
          ? `KCS 목록 개정이 확인되어 현재 프로젝트의 영향 분석과 자동 재매칭을 시작했습니다 (검색 가능 ${result.usable_document_count}건, 본문 제외 ${result.unavailable_document_count}건).`
          : previousRevision && previousRevision !== result.kcs_revision
            ? `KCS 목록 개정이 확인되었습니다 (목록 ${result.kcs_document_count}건 중 검색 가능 ${result.usable_document_count}건, 본문 제외 ${result.unavailable_document_count}건, 다운로드·갱신 ${result.changed_count}건). 영향도 재매칭을 지원하지 않는 기존 프로젝트는 같은 문서를 다시 업로드해야 합니다.`
          : `KCS 목록 ${result.kcs_document_count}건 중 ${result.usable_document_count}건을 검색에 사용합니다${result.unavailable_document_count ? ` (본문 미제공·코드오류 ${result.unavailable_document_count}건 제외)` : ''}${result.changed_count > 0 ? ` · 본문 파일 ${result.changed_count}건 복구·갱신` : ''}.`,
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'KCS 데이터를 갱신하지 못했습니다.');
    } finally {
      setSyncingKcs(false);
    }
  }

  async function analyzeCandidate(candidateId: string) {
    if (!project || !detail || analyzingCandidateId || detailSavingRef.current || bulkSavingRef.current) return;
    if (dirty && !(await saveCurrent())) return;
    const requestProjectId = project.id;
    const requestClauseId = detail.id;
    detailSavingRef.current = true;
    setAnalyzingCandidateId(candidateId);
    setError('');
    setNotice('');
    try {
      const result = await api.analyzeCandidate(project.id, detail.id, candidateId);
      if (
        currentProjectId.current !== requestProjectId
        || currentClauseId.current !== requestClauseId
      ) {
        setError('검토 위치가 변경되어 이전 GPT 분석 응답을 화면에 반영하지 않았습니다.');
        return;
      }
      const nextProject = normalizeProject(result.project);
      setProject(nextProject);
      setDetail(result.clause);
      setDecision(result.clause.decision);
      setEditedContent(result.clause.edited_content || result.clause.content || result.clause.title);
      setReviewNote(result.clause.review_note || '');
      setDecisionReason(result.clause.decision_reason || '');
      setCoverageConfirmed(Boolean(result.clause.coverage_confirmed));
      setSelectedCandidateId(result.clause.selected_candidate_id);
      setDirty(false);
      setBulkRefreshKey((current) => current + 1);
      setClauses((items) => items.map((item) => item.id === result.clause.id ? {
        ...item,
        candidate_count: result.clause.candidates.length,
        top_score: result.clause.candidates.length
          ? Math.max(...result.clause.candidates.map((candidate) => candidate.score))
          : null,
      } : item));
      if (reviewMode === 'quality') {
        const { evaluation } = await api.qualityEvaluation(project.id);
        setQualityEvaluation(evaluation);
      }
      if (result.excluded) {
        setCandidateTab(result.clause.candidates[0]?.id || 'none');
        setNotice('GPT가 현재 항목을 무관 후보로 판정해 매칭 목록에서 제외했습니다.');
      } else {
        setCandidateTab(candidateId);
        setNotice('GPT 개별 후보 분석을 완료했습니다. 관계·신뢰도·차이 근거를 확인하세요.');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'GPT 정밀분석을 완료하지 못했습니다.');
    } finally {
      detailSavingRef.current = false;
      setAnalyzingCandidateId(null);
    }
  }

  async function analyzeCoverage() {
    if (!project || !detail || analyzingCoverage || detailSavingRef.current || bulkSavingRef.current) return;
    if (dirty && !(await saveCurrent())) return;
    const requestProjectId = project.id;
    const requestClauseId = detail.id;
    detailSavingRef.current = true;
    setAnalyzingCoverage(true);
    setError('');
    setNotice('');
    try {
      const result = await api.analyzeCoverage(
        project.id,
        detail.id,
        Boolean(detail.coverage_analysis),
      );
      if (
        currentProjectId.current !== requestProjectId
        || currentClauseId.current !== requestClauseId
      ) {
        setError('검토 위치가 변경되어 이전 GPT 전체포괄 분석 응답을 화면에 반영하지 않았습니다.');
        return;
      }
      const nextProject = normalizeProject(result.project);
      setProject(nextProject);
      setDetail(result.clause);
      setDecision(result.clause.decision);
      setEditedContent(result.clause.edited_content || result.clause.content || result.clause.title);
      setReviewNote(result.clause.review_note || '');
      setDecisionReason(result.clause.decision_reason || '');
      setCoverageConfirmed(Boolean(result.clause.coverage_confirmed));
      setSelectedCandidateId(result.clause.selected_candidate_id);
      setDirty(false);
      setBulkRefreshKey((current) => current + 1);
      if (result.analysis.deletion_safe) {
        setNotice('GPT가 최대 3개 KCS의 조합으로 모든 요구사항을 찾았습니다. 삭제 여부는 담당자가 근거를 직접 확인한 뒤 확정하세요.');
      } else if (result.analysis.residual_content) {
        setNotice('KCS에 포함되지 않는 포스코 잔여 요구사항을 찾았습니다. 제안 문구를 확인해 출력문에 적용할 수 있습니다.');
      } else {
        setNotice(`GPT 전체포괄 분석을 완료했습니다. 결과는 '${coverageStatusLabel(result.analysis.coverage_status)}'이며 삭제 안전 조건을 충족하지 않습니다.`);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'GPT 전체포괄 분석을 완료하지 못했습니다.');
    } finally {
      detailSavingRef.current = false;
      setAnalyzingCoverage(false);
    }
  }

  async function restoreCandidate(candidateId: string) {
    if (!project || !detail || restoringCandidateId || detailSavingRef.current || bulkSavingRef.current) return;
    if (dirty && !(await saveCurrent())) return;
    const requestProjectId = project.id;
    const requestClauseId = detail.id;
    detailSavingRef.current = true;
    setRestoringCandidateId(candidateId);
    setError('');
    setNotice('');
    try {
      const result = await api.restoreCandidate(project.id, detail.id, candidateId);
      if (
        currentProjectId.current !== requestProjectId
        || currentClauseId.current !== requestClauseId
      ) {
        setError('검토 위치가 변경되어 이전 후보 복원 응답을 화면에 반영하지 않았습니다.');
        return;
      }
      const nextProject = normalizeProject(result.project);
      setProject(nextProject);
      setDetail(result.clause);
      setDecision(result.clause.decision);
      setEditedContent(result.clause.edited_content || result.clause.content || result.clause.title);
      setReviewNote(result.clause.review_note || '');
      setDecisionReason(result.clause.decision_reason || '');
      setCoverageConfirmed(Boolean(result.clause.coverage_confirmed));
      setSelectedCandidateId(result.clause.selected_candidate_id);
      setDirty(false);
      setBulkRefreshKey((current) => current + 1);
      setClauses((items) => items.map((item) => item.id === result.clause.id ? {
        ...item,
        candidate_count: result.clause.candidates.length,
        top_score: result.clause.candidates.length
          ? Math.max(...result.clause.candidates.map((candidate) => candidate.score))
          : null,
      } : item));
      setCandidateTab(candidateId);
      setNotice('GPT 제외 판정을 취소하고 후보를 매칭 목록에 복원했습니다.');
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '제외 후보를 복원하지 못했습니다.');
    } finally {
      detailSavingRef.current = false;
      setRestoringCandidateId(null);
    }
  }

  async function switchReviewMode(nextMode: ReviewMode) {
    if (!project || nextMode === reviewMode || bulkSavingRef.current || detailSavingRef.current) return;
    if (qualityDraftDirty) {
      setError('작성 중인 품질평가 누락 근거를 먼저 저장한 뒤 검토 모드를 전환해 주세요.');
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    setError('');
    setNotice('');
    if (nextMode === 'business') {
      setReviewMode('business');
      if (!clauses.some((clause) => clause.id === activeClauseId)) {
        setActiveClauseId(clauses.find((clause) => clause.decision === null)?.id || clauses[0]?.id || null);
      }
      if (businessView === 'bulk') {
        setBulkRefreshKey((current) => current + 1);
        requestAnimationFrame(() => window.scrollTo({ top: bulkScrollPosition.current }));
      }
      return;
    }

    if (businessView === 'bulk') bulkScrollPosition.current = window.scrollY;
    const requestProjectId = project.id;
    detailSavingRef.current = true;
    setQualityLoading(true);
    try {
      const { evaluation } = await api.ensureQualityEvaluation(project.id);
      if (currentProjectId.current !== requestProjectId) {
        setError('프로젝트가 변경되어 이전 품질평가 응답을 화면에 반영하지 않았습니다.');
        return;
      }
      setQualityEvaluation(evaluation);
      setReviewMode('quality');
      const first = evaluation.items.find((item) => item.verdict === null) || evaluation.items[0];
      setDetail(null);
      currentClauseId.current = first?.clause_id || null;
      setActiveClauseId(first?.clause_id || null);
      requestAnimationFrame(() => window.scrollTo({ top: 0 }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '품질평가 표본을 준비하지 못했습니다.');
    } finally {
      detailSavingRef.current = false;
      setQualityLoading(false);
    }
  }

  async function saveQualityVerdict(nextVerdict: Exclude<QualityVerdict, null>) {
    if (!project || !detail || !activeQualityItem || qualitySaving || detailSavingRef.current) return;
    const relevantCandidateId = nextVerdict === 'candidate_selected' && candidateTab !== 'none'
      ? candidateTab
      : null;
    if (nextVerdict === 'candidate_selected' && !relevantCandidateId) {
      setError('적정한 KCS 후보를 먼저 선택하세요.');
      return;
    }
    if (nextVerdict === 'kcs_missing' && !expectedKcsCode.trim()) {
      setError('누락된 정답 KCS 코드를 입력하세요.');
      return;
    }

    const requestProjectId = project.id;
    const requestClauseId = detail.id;
    detailSavingRef.current = true;
    setQualitySaving(true);
    setError('');
    setNotice('');
    try {
      const { evaluation } = await api.saveQualityItem(project.id, detail.id, {
        verdict: nextVerdict,
        relevant_candidate_id: relevantCandidateId,
        expected_kcs_code: nextVerdict === 'kcs_missing' ? expectedKcsCode : '',
        expected_kcs_clause: nextVerdict === 'kcs_missing' ? expectedKcsClause : '',
        quality_note: nextVerdict === 'kcs_missing' ? qualityNote : '',
      });
      if (
        currentProjectId.current !== requestProjectId
        || currentClauseId.current !== requestClauseId
      ) {
        setError('검토 위치가 변경되어 이전 품질평가 저장 응답을 화면에 반영하지 않았습니다.');
        return;
      }
      setQualityDraftDirty(false);
      setQualityEvaluation(evaluation);
      setClauses((items) => items.map((item) => item.id === detail.id ? {
        ...item,
        in_quality_sample: true,
        quality_verdict: nextVerdict,
      } : item));

      const currentOrder = activeQualityItem.sample_order;
      const nextItem = evaluation.items.find((item) => item.verdict === null && item.sample_order > currentOrder)
        || evaluation.items.find((item) => item.verdict === null);
      if (nextItem) {
        setDetail(null);
        currentClauseId.current = nextItem.clause_id;
        setActiveClauseId(nextItem.clause_id);
      } else {
        setQualityVerdict(nextVerdict);
        setNotice('50개 표본 품질평가를 모두 완료했습니다. 지표를 최종값으로 확인할 수 있습니다.');
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '품질평가 판정을 저장하지 못했습니다.');
    } finally {
      detailSavingRef.current = false;
      setQualitySaving(false);
    }
  }

  function updateDraft(action: () => void) {
    action();
    setDirty(true);
    setSavedAt(null);
  }

  async function saveCurrent(
    decisionOverride?: Decision,
    coverageOverride?: boolean,
    reasonOverride?: string,
    acknowledgeImpact = false,
  ): Promise<boolean> {
    if (!project || !detail || saving || detailSavingRef.current) return false;
    const nextDecision = decisionOverride === undefined ? decision : decisionOverride;
    const nextReason = nextDecision === null ? '' : (reasonOverride ?? decisionReason);
    const kcsBasedDelete = nextDecision === 'delete' && nextReason === 'fully_covered_by_kcs';
    const noKcsMatch = nextDecision === 'keep' && nextReason === 'no_kcs_match';
    const nextCoverageConfirmed = kcsBasedDelete
      ? (coverageOverride ?? coverageConfirmed)
      : false;
    if (detail.source_type === 'heading' && nextDecision !== null) {
      setError('목차와 구조 제목은 판정 대상이 아닙니다.');
      return false;
    }
    if (nextDecision !== null && !isDecisionReason(nextDecision, nextReason)) {
      setError('판정에 맞는 사유를 먼저 선택해 주세요.');
      return false;
    }
    if (kcsBasedDelete && (!selectedCandidateId || !nextCoverageConfirmed)) {
      setError('삭제하려면 KCS 근거를 선택하고 전체 요구사항 포함 여부를 확인해 주세요.');
      return false;
    }
    if (nextDecision === 'delete' && nextReason === 'management_decision' && !reviewNote.trim()) {
      setError('담당자 판단으로 삭제하려면 검토의견에 삭제 사유를 입력해 주세요.');
      return false;
    }
    const requestProjectId = project.id;
    const requestClauseId = detail.id;
    detailSavingRef.current = true;
    setSaving(true);
    setError('');
    try {
      const result = await api.saveClause(project.id, detail.id, {
        decision: nextDecision,
        edited_content: editedContent,
        review_note: reviewNote,
        decision_reason: nextReason,
        coverage_confirmed: nextCoverageConfirmed,
        selected_candidate_id: (nextDecision === 'delete' && !kcsBasedDelete) || noKcsMatch
          ? null
          : selectedCandidateId,
        ...(acknowledgeImpact ? kcsImpactAcknowledgement(detail) : {}),
      });
      if (
        currentProjectId.current !== requestProjectId
        || currentClauseId.current !== requestClauseId
      ) {
        setError('검토 위치가 변경되어 이전 저장 응답을 화면에 반영하지 않았습니다.');
        return false;
      }
      setDetail(result.clause);
      setDecision(result.clause.decision);
      setDecisionReason(result.clause.decision_reason || '');
      setCoverageConfirmed(Boolean(result.clause.coverage_confirmed));
      setSelectedCandidateId(result.clause.selected_candidate_id);
      const nextProject = normalizeProject(result.project);
      setProject(nextProject);
      setClauses((items) => items.map((item) => item.id === result.clause.id ? {
        ...item,
        decision: result.clause.decision,
        decision_reason: result.clause.decision_reason,
        coverage_confirmed: result.clause.coverage_confirmed,
        reviewed_at: result.clause.reviewed_at,
        kcs_impact: result.clause.kcs_impact,
      } : item));
      setBulkRefreshKey((current) => current + 1);
      setDirty(false);
      setSavedAt(new Date().toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' }));
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '저장하지 못했습니다.');
      return false;
    } finally {
      detailSavingRef.current = false;
      setSaving(false);
    }
  }

  function openReviewWorkflow(mode: Exclude<ReviewWorkflowDialog, null>) {
    if (!project || reviewWorkflowBusy || detailSavingRef.current || bulkSavingRef.current) return;
    if (dirty || qualityDraftDirty) {
      setError('작성 중인 변경사항을 먼저 저장한 뒤 승인 절차를 진행해 주세요.');
      return;
    }
    setReviewActorName('');
    setReviewWorkflowNote('');
    setReviewChangeReason('');
    setReviewWorkflowError('');
    setReviewWorkflowDialog(mode);
  }

  async function submitReviewWorkflow() {
    if (!project || reviewWorkflowBusy) return;
    const authorName = reviewActorName.trim();
    if (!authorName) {
      setReviewWorkflowError('작성자 이름을 입력해 주세요.');
      return;
    }
    if (!project.review_submission_ready) {
      setReviewWorkflowError(
        project.review_submission_blockers.join(' · ') || '모든 조항의 판정과 삭제 근거를 완료한 뒤 제출해 주세요.',
      );
      return;
    }
    const requestProjectId = project.id;
    setReviewWorkflowBusy(true);
    setReviewWorkflowError('');
    try {
      const result = await api.submitReviewWorkflow(project.id, {
        author_name: authorName,
        note: reviewWorkflowNote.trim(),
      });
      if (currentProjectId.current !== requestProjectId) return;
      setProject(normalizeProject(result.project));
      setProjectCatalogRefreshKey((current) => current + 1);
      setReviewWorkflowDialog(null);
      setNotice('작성자 검토본을 제출했습니다. 승인자 검토가 끝날 때까지 판정 내용은 잠깁니다.');
    } catch (cause) {
      setReviewWorkflowError(cause instanceof Error ? cause.message : '검토본을 제출하지 못했습니다.');
    } finally {
      setReviewWorkflowBusy(false);
    }
  }

  function validateApprover(): { projectId: string; submissionId: string; approverName: string } | null {
    if (!project || project.status !== 'submitted' || !project.latest_review_submission) {
      setReviewWorkflowError('현재 승인 대기 중인 제출본을 찾을 수 없습니다. 프로젝트를 다시 불러와 주세요.');
      return null;
    }
    const approverName = reviewActorName.trim();
    if (!approverName) {
      setReviewWorkflowError('승인자 이름을 입력해 주세요.');
      return null;
    }
    if (approverName.localeCompare(project.latest_review_submission.author_name.trim(), 'ko', { sensitivity: 'base' }) === 0) {
      setReviewWorkflowError('작성자와 다른 승인자를 입력해 주세요.');
      return null;
    }
    return {
      projectId: project.id,
      submissionId: project.latest_review_submission.id,
      approverName,
    };
  }

  async function approveReviewWorkflow() {
    if (reviewWorkflowBusy) return;
    const target = validateApprover();
    if (!target) return;
    setReviewWorkflowBusy(true);
    setReviewWorkflowError('');
    try {
      const result = await api.approveReviewWorkflow(target.projectId, target.submissionId, {
        approver_name: target.approverName,
        note: reviewWorkflowNote.trim(),
      });
      if (currentProjectId.current !== target.projectId) return;
      setProject(normalizeProject(result.project));
      setProjectCatalogRefreshKey((current) => current + 1);
      setReviewWorkflowDialog(null);
      setNotice('승인자 확정을 완료했습니다. 안전 조건을 충족한 최종 DOCX를 내려받을 수 있습니다.');
    } catch (cause) {
      setReviewWorkflowError(cause instanceof Error ? cause.message : '제출본을 승인하지 못했습니다.');
    } finally {
      setReviewWorkflowBusy(false);
    }
  }

  async function requestReviewChanges() {
    if (reviewWorkflowBusy) return;
    const target = validateApprover();
    if (!target) return;
    const reason = reviewChangeReason.trim();
    if (!reason) {
      setReviewWorkflowError('반려 사유를 입력해 주세요.');
      return;
    }
    setReviewWorkflowBusy(true);
    setReviewWorkflowError('');
    try {
      const result = await api.requestReviewChanges(target.projectId, target.submissionId, {
        approver_name: target.approverName,
        reason,
      });
      if (currentProjectId.current !== target.projectId) return;
      setProject(normalizeProject(result.project));
      setProjectCatalogRefreshKey((current) => current + 1);
      setReviewWorkflowDialog(null);
      setNotice('수정 요청을 작성자에게 돌려보냈습니다. 판정 내용을 다시 수정할 수 있습니다.');
    } catch (cause) {
      setReviewWorkflowError(cause instanceof Error ? cause.message : '수정 요청을 저장하지 못했습니다.');
    } finally {
      setReviewWorkflowBusy(false);
    }
  }

  async function switchProject(projectId: string) {
    if (!project || projectId === project.id || reviewWorkflowBusy || detailSavingRef.current || bulkSavingRef.current) return;
    if (qualityDraftDirty) {
      setError('작성 중인 품질평가 누락 근거를 먼저 저장한 뒤 프로젝트를 전환해 주세요.');
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    try {
      await openProject(projectId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '프로젝트를 불러오지 못했습니다.');
    }
  }

  async function chooseDecision(value: Exclude<Decision, null>) {
    if (detailSavingRef.current || bulkSavingRef.current) return;
    if (detail?.source_type === 'heading') {
      setError('목차와 구조 제목은 판정 대상이 아닙니다.');
      return;
    }
    if (value === 'delete') {
      if (!isDecisionReason('delete', decisionReason)) {
        setError('삭제 사유를 먼저 선택해 주세요.');
        return;
      }
      if (decisionReason === 'management_decision' && !reviewNote.trim()) {
        setError('담당자 판단으로 삭제하려면 검토의견에 삭제 사유를 입력해 주세요.');
        return;
      }
      if (decisionReason !== 'fully_covered_by_kcs') {
        await saveCurrent('delete', false, decisionReason, true);
        return;
      }
      if (!selectedCandidateId) {
        setError('삭제 근거로 사용할 KCS 후보를 먼저 선택해 주세요.');
        return;
      }
      if (detailNeedsKcsReview && !detail?.coverage_analysis) {
        setError('KCS가 개정된 삭제 조항은 GPT 전체포괄 분석을 다시 실행한 뒤 재확정해 주세요.');
        return;
      }
      if (detail?.coverage_analysis && !detail.coverage_analysis.deletion_safe) {
        setError('GPT 전체포괄 분석에서 포스코 잔여 요구사항 또는 불확실성이 확인되어 삭제할 수 없습니다.');
        return;
      }
      if (
        detail?.coverage_analysis
        && !detail.coverage_analysis.evidence_candidate_ids.includes(selectedCandidateId)
      ) {
        setError('GPT 전체포괄 분석에서 실제 근거로 사용된 KCS 후보를 선택해 주세요.');
        return;
      }
      await saveCurrent('delete', true, decisionReason, true);
      return;
    }
    if (!isDecisionReason(value, decisionReason)) {
      setError(`${value === 'keep' ? '남김' : '보류'} 사유를 먼저 선택해 주세요.`);
      return;
    }
    await saveCurrent(value, false, undefined, true);
  }

  async function navigateTo(clauseId: string) {
    if (clauseId === activeClauseId || detailSavingRef.current || bulkSavingRef.current) return;
    if (qualityDraftDirty) {
      setError('작성 중인 품질평가 누락 근거를 먼저 저장한 뒤 다른 표본으로 이동해 주세요.');
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    setDetail(null);
    setActiveClauseId(clauseId);
  }

  function handleBulkSaved(clause: ClauseDetail, updatedProject: Project) {
    const nextProject = normalizeProject(updatedProject);
    if (currentProjectId.current !== nextProject.id) return;
    setProject(nextProject);
    setClauses((items) => items.map((item) => item.id === clause.id ? {
      ...item,
      decision: clause.decision,
      decision_reason: clause.decision_reason,
      coverage_confirmed: clause.coverage_confirmed,
      reviewed_at: clause.reviewed_at,
      kcs_impact: clause.kcs_impact,
      candidate_count: clause.candidates.length,
      top_score: clause.candidates.length ? Math.max(...clause.candidates.map((candidate) => candidate.score)) : null,
    } : item));
    if (detail?.id === clause.id) {
      setDetail(clause);
      setDecision(clause.decision);
      setDecisionReason(clause.decision_reason || '');
      setCoverageConfirmed(Boolean(clause.coverage_confirmed));
      setSelectedCandidateId(clause.selected_candidate_id);
    }
  }

  async function openDetailedClause(clauseId?: string) {
    if (bulkSavingRef.current || detailSavingRef.current) return;
    if (dirty && !(await saveCurrent())) return;
    if (reviewMode === 'business' && businessView === 'bulk') {
      bulkScrollPosition.current = window.scrollY;
    }
    focusDetailAfterOpen.current = true;
    setBusinessView('detail');
    if (clauseId && clauseId !== activeClauseId) {
      setDetail(null);
      setActiveClauseId(clauseId);
    }
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function structureEditBlocker(clause: Pick<
    ClauseDetail,
    'decision' | 'source_type' | 'selected_candidate_id' | 'decision_reason' | 'coverage_confirmed' | 'reviewed_at'
  >) {
    if (
      structureBusy
      || switchingProject
      || syncingKcs
      || qualityLoading
      || qualitySaving
      || analyzingCoverage
      || analyzingCandidateId !== null
      || restoringCandidateId !== null
      || bulkSaving
    ) return '진행 중인 저장·분석·KCS 갱신 작업이 끝난 뒤 구조를 변경해 주세요.';
    if (project?.requires_source_reupload) return '이 프로젝트는 이전 문서 해석기로 생성되었습니다. 정확한 조항 구조를 위해 같은 원본을 한 번 다시 업로드해 주세요.';
    if (project?.kcs_stale) return '최신 KCS 영향 분석과 자동 재매칭이 끝난 뒤 구조를 보정해 주세요.';
    if (reviewMode === 'quality' || qualityEvaluation) return '품질평가 표본이 생성된 프로젝트는 조항 구조를 변경할 수 없습니다.';
    if (clause.source_type !== 'paragraph') return '조항 나누기와 합치기는 일반 문단에만 사용할 수 있습니다.';
    if (
      clause.decision !== null
      || clause.selected_candidate_id
      || clause.decision_reason
      || clause.coverage_confirmed
      || clause.reviewed_at
    ) return '판정·후보 선택 이력을 보호하기 위해 아직 검토를 시작하지 않은 조항만 구조를 변경할 수 있습니다.';
    if (dirty || qualityDraftDirty) return '현재 화면의 저장되지 않은 입력을 정리한 뒤 구조를 변경해 주세요.';
    return '';
  }

  function requestSplit(clause: BulkReviewItem | ClauseDetail) {
    const blocker = structureEditBlocker(clause);
    if (blocker) {
      setError(blocker);
      return;
    }
    if (!clause.content.trim()) {
      setError('본문이 없는 항목은 나누지 않습니다. 목차·구조 제목 분류를 먼저 확인해 주세요.');
      return;
    }
    const sourceText = clause.content;
    const splitIndex = suggestedSplitIndex(sourceText);
    if (!splitIndex || !sourceText.slice(0, splitIndex).trim() || !sourceText.slice(splitIndex).trim()) {
      setError('이 조항은 두 개의 의미 있는 문장으로 나눌 수 있을 만큼 내용이 길지 않습니다.');
      return;
    }
    setError('');
    setNotice('');
    setStructureError('');
    if (businessView === 'bulk') bulkScrollPosition.current = window.scrollY;
    setStructureDialog({
      kind: 'split',
      clauseId: clause.id,
      sourceOrder: clause.source_order,
      label: clause.label,
      title: clause.title,
      sourceText,
      splitIndex,
    });
  }

  function requestMergeNext(clause: BulkReviewItem | ClauseDetail) {
    const blocker = structureEditBlocker(clause);
    if (blocker) {
      setError(blocker);
      return;
    }
    const currentIndex = clauses.findIndex((item) => item.id === clause.id);
    const nextClause = currentIndex >= 0 ? clauses[currentIndex + 1] : undefined;
    if (!nextClause || nextClause.source_type !== 'paragraph') {
      setError('바로 다음 항목이 일반 문단인 경우에만 합칠 수 있습니다. 목차·표를 건너뛰어 합치지는 않습니다.');
      return;
    }
    const nextBlocker = structureEditBlocker(nextClause);
    if (nextBlocker) {
      setError(`바로 다음 조항을 합칠 수 없습니다. ${nextBlocker}`);
      return;
    }
    setError('');
    setNotice('');
    setStructureError('');
    if (businessView === 'bulk') bulkScrollPosition.current = window.scrollY;
    setStructureDialog({
      kind: 'merge',
      firstClauseId: clause.id,
      sourceOrder: clause.source_order,
      firstLabel: clause.label,
      firstTitle: clause.title,
      secondClauseId: nextClause.id,
      secondLabel: nextClause.label,
      secondTitle: nextClause.title,
    });
  }

  function applyStructurePayload(payload: ClauseStructurePayload, message: string) {
    const nextProject = normalizeProject(payload.project);
    currentProjectId.current = nextProject.id;
    currentClauseId.current = payload.active_clause_id;
    setProject(nextProject);
    setClauses(payload.clauses);
    setQualityEvaluation(null);
    setQualityDraftDirty(false);
    setDetail(null);
    setActiveClauseId(payload.active_clause_id);
    setDecision(null);
    setDecisionReason('');
    setCoverageConfirmed(false);
    setSelectedCandidateId(null);
    setEditedContent('');
    setReviewNote('');
    setDirty(false);
    setSavedAt(null);
    setBulkRefreshKey((current) => current + 1);
    focusDetailAfterOpen.current = true;
    setBusinessView('detail');
    setNotice(message);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function confirmStructureEdit() {
    if (!project || !structureDialog || structureBusy || detailSavingRef.current || bulkSavingRef.current) return;
    const requestProjectId = project.id;
    const pending = structureDialog;
    const currentClause = clauses.find((clause) => clause.id === (
      pending.kind === 'merge' ? pending.firstClauseId : pending.clauseId
    ));
    const blocker = currentClause ? structureEditBlocker(currentClause) : '편집할 조항의 최신 상태를 찾을 수 없습니다.';
    if (blocker) {
      setStructureError(blocker);
      return;
    }
    detailSavingRef.current = true;
    setStructureBusy(true);
    setError('');
    setNotice('');
    setStructureError('');
    try {
      const result = pending.kind === 'merge'
        ? await api.mergeClauses(project.id, [pending.firstClauseId, pending.secondClauseId])
        : await api.splitClause(project.id, pending.clauseId, [
            pending.sourceText.slice(0, pending.splitIndex),
            pending.sourceText.slice(pending.splitIndex),
          ]);
      if (currentProjectId.current !== requestProjectId) {
        throw new Error('프로젝트가 변경되어 구조 편집 응답을 화면에 반영하지 않았습니다.');
      }
      setStructureDialog(null);
      setStructureError('');
      applyStructurePayload(
        result,
        pending.kind === 'merge'
          ? '두 조항을 하나로 합치고 최신 KCS 후보를 다시 찾았습니다.'
          : '조항을 두 개로 나누고 각 조항의 최신 KCS 후보를 다시 찾았습니다.',
      );
    } catch (cause) {
      let recovered = false;
      if (currentProjectId.current === requestProjectId) {
        try {
          const refreshed = await api.project(requestProjectId);
          const sourceIds = pending.kind === 'merge'
            ? [pending.firstClauseId, pending.secondClauseId]
            : [pending.clauseId];
          if (sourceIds.every((sourceId) => !refreshed.clauses.some((clause) => clause.id === sourceId))) {
            const activeClauseId = refreshed.clauses.find((clause) => clause.source_order === pending.sourceOrder)?.id;
            if (activeClauseId) {
              setStructureDialog(null);
              setStructureError('');
              applyStructurePayload(
                { ...refreshed, active_clause_id: activeClauseId },
                '구조 변경은 완료되었으며 최신 프로젝트 상태를 다시 불러왔습니다.',
              );
              recovered = true;
            }
          }
        } catch {
          // Preserve the original operation error when reconciliation is unavailable.
        }
      }
      if (!recovered) {
        setStructureError(cause instanceof Error ? cause.message : '조항 구조를 변경하지 못했습니다.');
      }
    } finally {
      detailSavingRef.current = false;
      setStructureBusy(false);
    }
  }

  async function switchBusinessView(nextView: BusinessView) {
    if (nextView === businessView || bulkSavingRef.current || detailSavingRef.current) return;
    if (nextView === 'detail') {
      await openDetailedClause();
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    setBusinessView('bulk');
    setBulkRefreshKey((current) => current + 1);
    requestAnimationFrame(() => window.scrollTo({ top: bulkScrollPosition.current }));
  }

  async function showKcsImpactQueue() {
    if (qualityDraftDirty) {
      setError('작성 중인 품질평가 누락 근거를 먼저 저장한 뒤 KCS 재검토 목록을 열어 주세요.');
      return;
    }
    if (dirty && !(await saveCurrent())) return;
    setReviewMode('business');
    setBusinessView('bulk');
    bulkScrollPosition.current = 0;
    setBulkKcsImpactFilterKey((current) => current + 1);
    requestAnimationFrame(() => window.scrollTo({ top: 0 }));
  }

  const filteredClauses = useMemo(() => {
    const term = search.trim().toLowerCase();
    return clauses.filter((clause) => {
      const statusMatches = statusFilter === 'all'
        || (statusFilter === 'unreviewed'
          ? clause.source_type !== 'heading' && clause.decision === null
          : clause.decision === statusFilter);
      const textMatches = !term || `${clause.label} ${clause.title}`.toLowerCase().includes(term);
      return statusMatches && textMatches;
    });
  }, [clauses, search, statusFilter]);

  const filteredQualityItems = useMemo(() => {
    const term = search.trim().toLowerCase();
    return (qualityEvaluation?.items || []).filter((item) => (
      !term || `${item.label} ${item.title}`.toLowerCase().includes(term)
    ));
  }, [qualityEvaluation, search]);

  const navigationIds = reviewMode === 'quality'
    ? (qualityEvaluation?.items || []).map((item) => item.clause_id)
    : clauses.map((clause) => clause.id);
  const activeIndex = navigationIds.findIndex((id) => id === activeClauseId);
  const previousClauseId = activeIndex > 0 ? navigationIds[activeIndex - 1] : null;
  const nextClauseId = activeIndex >= 0 && activeIndex < navigationIds.length - 1 ? navigationIds[activeIndex + 1] : null;
  const progress = project?.reviewable_clauses ? Math.round((project.reviewed_clauses / project.reviewable_clauses) * 100) : 0;
  const candidateRate = project?.reviewable_clauses ? Math.round((project.candidate_clauses / project.reviewable_clauses) * 100) : 0;
  const kcsRematch = project?.kcs_rematch || null;
  const latestReviewSubmission = project?.latest_review_submission || null;
  const reviewActorIsSubmissionAuthor = Boolean(
    latestReviewSubmission
    && reviewActorName.trim()
    && reviewActorName.trim().localeCompare(latestReviewSubmission.author_name.trim(), 'ko', { sensitivity: 'base' }) === 0,
  );
  const kcsRematchProgress = kcsRematch?.total_clauses
    ? Math.round((kcsRematch.matched_count / kcsRematch.total_clauses) * 100)
    : 0;
  const kcsRematchActive = Boolean(kcsRematch && ['pending', 'running'].includes(kcsRematch.status));
  const reviewMutationLocked = Boolean(
    project?.review_locked
    || project?.status === 'submitted'
    || project?.status === 'approved'
    || project?.requires_source_reupload
    || project?.kcs_stale
    || (kcsRematch && ['pending', 'running', 'failed', 'superseded'].includes(kcsRematch.status)),
  );
  const unacknowledgedKcsImpactCount = Number(
    project?.unacknowledged_kcs_impact_count ?? kcsRematch?.unacknowledged_count ?? 0,
  );
  const kcsRematchBlocksFinal = Boolean(
    unacknowledgedKcsImpactCount > 0
    || (kcsRematch && (
      kcsRematchActive
      || ['failed', 'superseded'].includes(kcsRematch.status)
    )),
  );
  const detailNeedsKcsReview = unresolvedKcsImpact(detail);
  const detailMutationBusy = saving
    || switchingProject
    || structureBusy
    || qualityLoading
    || qualitySaving
    || analyzingCoverage
    || analyzingCandidateId !== null
    || restoringCandidateId !== null
    || reviewWorkflowBusy;
  const artifactDownloadBlocked = dirty
    || qualityDraftDirty
    || detailMutationBusy
    || bulkSaving
    || startingKcsRematch
    || syncingKcs;
  const artifactDownloadBlockMessage = syncingKcs
    ? 'KCS 갱신이 끝난 뒤 내려받으세요.'
    : '현재 화면의 변경사항 저장이 끝난 뒤 내려받으세요.';
  const backupMutationBlocked = artifactDownloadBlocked
    || uploading
    || kcsRematchActive
    || archiveBusyId !== null
    || retryingUploadItemId !== null;
  const backupBlockedReason = uploading
    ? '문서 업로드와 분석이 끝난 뒤 백업 또는 복구를 진행해 주세요.'
    : syncingKcs
      ? 'KCS 갱신이 끝난 뒤 백업 또는 복구를 진행해 주세요.'
      : kcsRematchActive
        ? 'KCS 영향 분석과 자동 재매칭이 끝난 뒤 백업 또는 복구를 진행해 주세요.'
        : archiveBusyId !== null
          ? '프로젝트 보관 또는 복원이 끝난 뒤 백업 또는 복구를 진행해 주세요.'
          : retryingUploadItemId !== null
            ? '업로드 재시도가 끝난 뒤 백업 또는 복구를 진행해 주세요.'
      : '저장하지 않은 내용이나 진행 중인 작업이 있습니다. 먼저 저장하거나 작업이 끝날 때까지 기다려 주세요.';
  const finalDownloadReady = Boolean(project?.final_export_ready)
    && Boolean(config?.kcs_available)
    && !kcsRematchBlocksFinal
    && !artifactDownloadBlocked;
  const draftIncludedCount = Number(project?.keep_count || 0)
    + Number(project?.hold_count || 0)
    + Number(project?.unreviewed_clauses || 0);
  const draftDownloadReady = draftIncludedCount > 0 && !artifactDownloadBlocked;
  const documentDownloadReady = finalDownloadReady || draftDownloadReady;
  const documentDownloadKind = finalDownloadReady ? 'final' : 'review';
  const documentDownloadBlockMessage = artifactDownloadBlocked
    ? artifactDownloadBlockMessage
    : '삭제로 확정되지 않은 조항이 있어야 간소화 DOCX를 만들 수 있습니다.';
  const finalDownloadBlockers = Array.from(new Set([
    ...(project?.final_export_blockers || []),
    ...(!config?.kcs_available ? ['최신 KCS 상태를 먼저 확인하세요.'] : []),
    ...(kcsRematchActive ? ['KCS 영향 분석과 자동 재매칭이 진행 중입니다.'] : []),
    ...(kcsRematch?.status === 'failed' ? ['KCS 자동 재매칭 실패 항목을 다시 처리하세요.'] : []),
    ...(kcsRematch?.status === 'superseded' ? ['더 최신 KCS 기준으로 자동 재매칭을 다시 실행하세요.'] : []),
    ...(unacknowledgedKcsImpactCount ? [`KCS 개정 영향 조항 ${unacknowledgedKcsImpactCount}건을 재검토하세요.`] : []),
    ...(dirty ? ['현재 조항의 변경사항을 먼저 저장하세요.'] : []),
    ...(qualityDraftDirty ? ['품질평가 변경사항을 먼저 저장하세요.'] : []),
    ...(detailMutationBusy ? ['현재 조항의 저장·구조 편집·GPT 분석·후보 복원이 끝난 뒤 내려받으세요.'] : []),
    ...(bulkSaving ? ['일괄 저장이 끝난 뒤 내려받으세요.'] : []),
    ...(syncingKcs ? ['KCS 갱신이 끝난 뒤 내려받으세요.'] : []),
  ]));
  const qualityInsights = qualityEvaluation?.insights || null;
  const qualityErrorSignals = qualityInsights?.error_signals || null;
  const qualityCohortRows = normalizeInsightRows(qualityInsights?.cohorts ?? qualityInsights?.cohort_rows, 'cohort');
  const qualityScoreBandRows = normalizeInsightRows(qualityInsights?.score_bands ?? qualityInsights?.score_band_rows, 'band');
  const evaluatedCohortRows = qualityCohortRows.filter((row) => Number(row.evaluated_count ?? row.evaluated ?? 0) > 0);
  const evaluatedScoreBandRows = qualityScoreBandRows.filter((row) => Number(row.evaluated_count ?? row.evaluated ?? 0) > 0);

  if (loading) {
    return <main className="grid min-h-screen place-items-center bg-background"><div className="flex items-center gap-3 text-muted-foreground"><Spinner />검토 환경을 준비하고 있습니다.</div></main>;
  }

  if (!project) {
    return (
      <UploadPanel
        config={config}
        uploading={uploading}
        uploadJob={uploadJob}
        uploadPollError={uploadPollError}
        syncingKcs={syncingKcs}
        error={error}
        onUpload={upload}
        onOpenProject={(projectId) => { void openProject(projectId); }}
        onRetryItem={(jobId, itemId) => { void retryUploadItem(jobId, itemId); }}
        retryingItemId={retryingUploadItemId}
        onRefresh={refreshKcs}
        backupMutationBlocked={backupMutationBlocked}
        backupBlockedReason={backupBlockedReason}
      />
    );
  }

  return (
    <main className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b border-border bg-card/95 backdrop-blur">
        <div className="mx-auto flex min-h-16 max-w-[1700px] flex-wrap items-center justify-between gap-3 px-4 py-3 lg:px-7">
          <div className="flex min-w-0 items-center gap-3">
            <div className="grid size-9 shrink-0 place-items-center rounded-lg bg-primary text-primary-foreground"><Layers3 className="size-5" /></div>
            <div className="min-w-0">
              <p className="truncate text-lg font-semibold tracking-tight">시방서 정합성 검토</p>
              <p className="truncate text-xs text-muted-foreground">포스코 고유기준 선별 · 최신 KCS 기준</p>
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-end gap-2">
            <ProjectSelector
              currentProject={project}
              onSelect={(projectId) => { void switchProject(projectId); }}
              disabled={bulkSaving || detailMutationBusy || syncingKcs}
              archiveBusyId={archiveBusyId}
              refreshKey={projectCatalogRefreshKey}
              onArchive={(item, archived) => { void setProjectArchived(item, archived); }}
            />
            <input ref={uploadInput} className="hidden" type="file" accept=".doc,.docx" multiple disabled={uploading} onChange={(event) => { const files = Array.from(event.target.files || []); if (files.length) void upload(files); event.target.value = ''; }} />
            {uploadJob && (
              <Button variant="outline" onClick={() => setUploadStatusOpen(true)}>
                {uploading ? <Spinner /> : uploadJob.failed ? <AlertTriangle /> : <CheckCircle2 />}
                업로드 {uploadJob.completed + uploadJob.failed}/{uploadJob.total}
              </Button>
            )}
            <Button variant="outline" onClick={refreshKcs} disabled={syncingKcs || uploading || detailMutationBusy || bulkSaving || kcsRematchActive || startingKcsRematch}>{syncingKcs ? <Spinner /> : <RefreshCw />}KCS 갱신</Button>
            <Button variant="outline" onClick={() => uploadInput.current?.click()} disabled={uploading || bulkSaving || detailMutationBusy || syncingKcs}>{uploading ? <Spinner /> : <UploadCloud />}새 문서</Button>
            <BackupManager mutationBlocked={backupMutationBlocked} blockedReason={backupBlockedReason} />
            {project.status === 'submitted' ? (
              <Button variant="outline" onClick={() => openReviewWorkflow('decision')} disabled={reviewWorkflowBusy || artifactDownloadBlocked}>
                <ShieldCheck />승인 처리
              </Button>
            ) : project.status === 'approved' ? (
              <Button variant="outline" disabled><ShieldCheck />승인 완료</Button>
            ) : (
              <Button variant="outline" onClick={() => openReviewWorkflow('submit')} disabled={reviewWorkflowBusy || artifactDownloadBlocked}>
                <Send />검토 제출
              </Button>
            )}
            {reviewMode === 'quality' && (artifactDownloadBlocked
              ? <Button variant="outline" disabled title={artifactDownloadBlockMessage}><FileSpreadsheet />품질평가 XLSX</Button>
              : <a className={buttonVariants({ variant: 'outline' })} href={api.qualityReportUrl(project.id)}><FileSpreadsheet />품질평가 XLSX</a>)}
            {artifactDownloadBlocked
              ? <Button variant="outline" disabled title={artifactDownloadBlockMessage}><FileSpreadsheet />판정·KCS 이력</Button>
              : <a className={buttonVariants({ variant: 'outline' })} href={api.auditUrl(project.id)}><FileSpreadsheet />판정·KCS 이력</a>}
            {documentDownloadReady ? (
              <a className={buttonVariants()} href={api.exportUrl(project.id, documentDownloadKind)} title="남김·보류·미검토 조항 포함, 삭제 조항 제외"><Download />간소화 DOCX</a>
            ) : (
              <Button disabled aria-describedby="document-download-status" title={documentDownloadBlockMessage}><Download />간소화 DOCX</Button>
            )}
          </div>
        </div>
      </header>

      <section className="mx-auto max-w-[1700px] px-4 py-5 lg:px-7">
        {error && (
          <Alert variant="destructive" className="fixed right-4 top-20 z-50 max-w-lg shadow-xl">
            <AlertTriangle /><AlertTitle>처리 중 오류가 발생했습니다</AlertTitle><AlertDescription className="pr-7">{error}</AlertDescription>
            <button type="button" className="absolute right-2 top-2 rounded p-1 hover:bg-destructive/10" aria-label="오류 메시지 닫기" onClick={() => setError('')}><X className="size-4" /></button>
          </Alert>
        )}
        {notice && (
          <Alert className="mb-4"><CheckCircle2 /><AlertTitle>처리 완료</AlertTitle><AlertDescription>{notice}</AlertDescription></Alert>
        )}
        {!finalDownloadReady && (
          <Alert className="mb-4">
            <AlertTriangle />
            <AlertTitle>{documentDownloadReady ? '간소화 DOCX를 내려받을 수 있습니다' : 'DOCX 생성 전 확인이 필요합니다'}</AlertTitle>
            <AlertDescription id="document-download-status" className="space-y-3">
              {documentDownloadReady ? (
                <p>
                  현재 남김 {project.keep_count}건, 보류 {project.hold_count}건, 미검토 {project.unreviewed_clauses}건이 포함되고 삭제 {project.delete_count}건은 제외됩니다.
                  {' '}이후 판정과 승인이 변경되면 같은 버튼의 파일에도 반영됩니다.
                </p>
              ) : (
                <p>{documentDownloadBlockMessage}</p>
              )}
              <p>
                <strong>검토 완료까지 남은 단계:</strong>{' '}
                {(finalDownloadBlockers.length
                  ? finalDownloadBlockers
                  : ['최종 출력 조건을 아직 충족하지 않았습니다.']).join(' · ')}
              </p>
              <div className="flex flex-wrap gap-2">
                {project.review_submission_ready && ['reviewing', 'changes_requested'].includes(project.status) && (
                  <Button size="sm" onClick={() => openReviewWorkflow('submit')} disabled={reviewWorkflowBusy || artifactDownloadBlocked}><Send />검토 제출</Button>
                )}
                {project.status === 'submitted' && (
                  <Button size="sm" onClick={() => openReviewWorkflow('decision')} disabled={reviewWorkflowBusy || artifactDownloadBlocked}><ShieldCheck />승인 처리</Button>
                )}
              </div>
            </AlertDescription>
          </Alert>
        )}
        {project.status === 'submitted' && latestReviewSubmission && (
          <Alert className="mb-4">
            <LockKeyhole />
            <AlertTitle>승인 대기 중 · 제출본은 읽기 전용입니다</AlertTitle>
            <AlertDescription>
              작성자 {latestReviewSubmission.author_name} · {snapshotDate(latestReviewSubmission.submitted_at)} 제출. 승인자는 일괄 검토와 상세 비교에서 같은 내용을 확인한 뒤 승인하거나 수정 요청할 수 있습니다.
            </AlertDescription>
          </Alert>
        )}
        {project.status === 'changes_requested' && latestReviewSubmission && (
          <Alert variant="destructive" className="mb-4">
            <RotateCcw />
            <AlertTitle>승인자가 수정을 요청했습니다</AlertTitle>
            <AlertDescription>
              <p>{latestReviewSubmission.decision_note || '반려 사유를 확인하고 판정 내용을 보완해 주세요.'}</p>
              {latestReviewSubmission.decided_by && <p className="mt-1 text-xs">승인자 {latestReviewSubmission.decided_by}{latestReviewSubmission.decided_at ? ` · ${snapshotDate(latestReviewSubmission.decided_at)}` : ''}</p>}
            </AlertDescription>
          </Alert>
        )}
        {project.status === 'approved' && latestReviewSubmission && (
          <Alert className="mb-4">
            <ShieldCheck />
            <AlertTitle>승인 완료 · 판정 내용이 잠겼습니다</AlertTitle>
            <AlertDescription>
              승인자 {latestReviewSubmission.decided_by || '미상'}{latestReviewSubmission.decided_at ? ` · ${snapshotDate(latestReviewSubmission.decided_at)}` : ''}. 최신 KCS와 나머지 안전 조건을 충족하면 최종 DOCX를 내려받을 수 있습니다.
            </AlertDescription>
          </Alert>
        )}
        {project.warning && (
          <Alert className="mb-4"><AlertTriangle /><AlertTitle>문서 구조 확인 필요</AlertTitle><AlertDescription>{project.warning}</AlertDescription></Alert>
        )}
        {project.requires_source_reupload && (
          <Alert variant="destructive" className="mb-4">
            <AlertTriangle />
            <AlertTitle>새 문서 해석기를 적용하려면 원본 재업로드가 필요합니다</AlertTitle>
            <AlertDescription>
              <div className="space-y-3">
                <p>이 프로젝트의 기존 판정과 이력은 보존되지만, 자동 재매칭만으로는 조항 나누기·목차 제외 등 새 해석 결과가 반영되지 않습니다. 같은 원본 DOC 또는 DOCX를 새 문서로 다시 업로드해 검토해 주세요.</p>
                <Button size="sm" variant="outline" onClick={() => uploadInput.current?.click()} disabled={uploading || bulkSaving || detailMutationBusy || syncingKcs}>
                  {uploading ? <Spinner /> : <UploadCloud />}원본 다시 업로드
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        )}
        {kcsRematch && (
          <Alert variant={kcsRematch.status === 'failed' || kcsRematch.status === 'superseded' ? 'destructive' : 'default'} className="mb-4">
            {kcsRematch.status === 'completed' && kcsRematch.unacknowledged_count === 0 ? <CheckCircle2 /> : <RefreshCw />}
            <AlertTitle>{kcsRematchLabel(kcsRematch, unacknowledgedKcsImpactCount)}</AlertTitle>
            <AlertDescription>
              <div className="space-y-3">
                <p>
                  기존 판정은 보존됩니다. 재매칭에서 실질적인 후보 변화가 확인된 조항만 담당자 재검토 대상으로 표시합니다.
                </p>
                <p className="text-xs">
                  KCS 기준 {kcsRematch.from_revision.slice(0, 10)} → {kcsRematch.target_revision.slice(0, 10)}
                </p>
                {kcsRematchActive && (
                  <Progress value={kcsRematchProgress}>
                    <ProgressLabel>영향 분석·재매칭 진행률</ProgressLabel>
                    <ProgressValue>{() => `${kcsRematch.matched_count} / ${kcsRematch.total_clauses}조항 · ${kcsRematchProgress}%`}</ProgressValue>
                  </Progress>
                )}
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
                  <span>실질 변경 {kcsRematch.material_change_count}건</span>
                  <span>재검토 지정 {kcsRematch.review_required_count}건</span>
                  <span>남은 재검토 {kcsRematch.unacknowledged_count}건</span>
                </div>
                {kcsRematchPollError && <output className="block text-xs">{kcsRematchPollError}</output>}
                {kcsRematch.error && <p role="alert" className="text-sm">{kcsRematch.error}</p>}
                <div className="flex flex-wrap gap-2">
                  {unacknowledgedKcsImpactCount > 0 && (
                    <Button size="sm" variant="outline" onClick={() => { void showKcsImpactQueue(); }} disabled={dirty || qualityDraftDirty || detailMutationBusy || bulkSaving}>
                      재검토만 보기
                    </Button>
                  )}
                  {!project.requires_source_reupload && (kcsRematch.status === 'failed' || kcsRematch.status === 'superseded') && (
                    <Button size="sm" variant="outline" onClick={() => { void startKcsRematch(project.id); }} disabled={startingKcsRematch || syncingKcs}>
                      {startingKcsRematch ? <Spinner /> : <RefreshCw />}{kcsRematch.status === 'failed' ? '실패 항목 재시도' : '최신 KCS로 다시 매칭'}
                    </Button>
                  )}
                </div>
              </div>
            </AlertDescription>
          </Alert>
        )}

        <div className="mb-5 grid gap-4 border-b border-border pb-5 lg:grid-cols-[1fr_440px] lg:items-end">
          <div>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant={reviewStatusVariant(project.status)}>{reviewStatusLabel(project.status)}</Badge>
              {project.requires_source_reupload && <Badge variant="destructive">새 파서 적용 · 원본 재업로드 필요</Badge>}
              {kcsRematch ? (
                <Badge variant={kcsRematch.status === 'failed' || kcsRematch.status === 'superseded' ? 'destructive' : 'secondary'}>
                  {kcsRematchLabel(kcsRematch, unacknowledgedKcsImpactCount)}
                </Badge>
              ) : project.kcs_stale && !project.requires_source_reupload ? <Badge variant="destructive">KCS 갱신됨 · 자동 재매칭 필요</Badge> : null}
              <Badge variant="outline" className="gap-1"><Sparkles className="size-3" />{config?.openai_available ? 'GPT 설정됨' : '로컬 매칭'}</Badge>
              <span className="text-sm text-muted-foreground">{project.source_filename}</span>
              <span className="text-sm text-muted-foreground">· {project.kcs_scope}</span>
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">{projectDisplayTitle(project)}</h1>
            <p className="mt-1 text-sm text-muted-foreground">KCS 스냅샷 {snapshotDate(project.kcs_snapshot)} · 검색 가능 {config?.kcs_usable_document_count ?? config?.kcs_document_count ?? 0}건{config?.kcs_unavailable_document_count ? ` · 본문 제외 ${config.kcs_unavailable_document_count}건` : ''}</p>
            <div className="mt-4 flex flex-wrap gap-2">
              <div className="inline-flex rounded-lg border border-border bg-muted/40 p-1" aria-label="검토 모드">
                <Button size="sm" variant={reviewMode === 'business' ? 'default' : 'ghost'} aria-pressed={reviewMode === 'business'} onClick={() => void switchReviewMode('business')} disabled={qualityLoading || bulkSaving || detailMutationBusy}>
                  시방서 판정
                </Button>
                <Button size="sm" variant={reviewMode === 'quality' ? 'default' : 'ghost'} aria-pressed={reviewMode === 'quality'} onClick={() => void switchReviewMode('quality')} disabled={qualityLoading || bulkSaving || detailMutationBusy || reviewMutationLocked}>
                  {qualityLoading ? <Spinner /> : null}50개 표본 품질평가
                </Button>
              </div>
              {reviewMode === 'business' && (
                <div className="inline-flex rounded-lg border border-border bg-muted/40 p-1" aria-label="업무 검토 화면">
                  <Button size="sm" variant={businessView === 'bulk' ? 'secondary' : 'ghost'} aria-pressed={businessView === 'bulk'} onClick={() => void switchBusinessView('bulk')} disabled={bulkSaving || detailMutationBusy}>일괄 검토</Button>
                  <Button size="sm" variant={businessView === 'detail' ? 'secondary' : 'ghost'} aria-pressed={businessView === 'detail'} onClick={() => void switchBusinessView('detail')} disabled={bulkSaving || detailMutationBusy}>상세 비교</Button>
                </div>
              )}
            </div>
          </div>
          <div>
            {reviewMode === 'business' ? (
              <>
                <Progress value={progress}>
                  <ProgressLabel>검토 진행률</ProgressLabel>
                  <ProgressValue>{() => `${project.reviewed_clauses} / ${project.reviewable_clauses} 조항 · ${progress}%`}</ProgressValue>
                </Progress>
                <div className="mt-2 flex justify-between text-xs text-muted-foreground">
                  <span>남김 {project.keep_count}</span><span>삭제 {project.delete_count}</span><span>보류 {project.hold_count}</span><span>미검토 {project.unreviewed_clauses}</span>
                </div>
              </>
            ) : qualityEvaluation ? (
              <>
                <Progress value={qualityEvaluation.metrics.sample_size ? Math.round((qualityEvaluation.metrics.evaluated_count / qualityEvaluation.metrics.sample_size) * 100) : 0}>
                  <ProgressLabel>표본 평가 진행률</ProgressLabel>
                  <ProgressValue>{() => `${qualityEvaluation.metrics.evaluated_count} / ${qualityEvaluation.metrics.sample_size}개`}</ProgressValue>
                </Progress>
                <p className="mt-2 text-xs text-muted-foreground">
                  고유사도·검토필요·후보없음 구간을 나눠 추출한 고정 표본입니다.
                </p>
              </>
            ) : null}
          </div>
          {reviewMode === 'business' ? (
            <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-5 lg:col-span-2">
              {[
                ['추출 조항', project.total_clauses],
                ['KCS 후보 있음', `${project.candidate_clauses} (${candidateRate}%)`],
                ['관련성 높음', project.high_match_clauses],
                ['후보 없음', Math.max(0, project.reviewable_clauses - project.candidate_clauses)],
                ['표에서 추출', project.table_clauses],
              ].map(([label, value]) => (
                <div key={label} className="bg-card px-4 py-3">
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          ) : qualityEvaluation ? (
            <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-border bg-border sm:grid-cols-4 lg:col-span-2">
              {[
                ['평가 완료', `${qualityEvaluation.metrics.evaluated_count} / ${qualityEvaluation.metrics.sample_size}`],
                ['Recall@3', percent(qualityEvaluation.metrics.recall_at_3)],
                ['MRR', qualityEvaluation.metrics.mrr == null ? '—' : qualityEvaluation.metrics.mrr.toFixed(3)],
                ['표본 내 정확도', percent(qualityEvaluation.metrics.accuracy)],
              ].map(([label, value]) => (
                <div key={label} className="bg-card px-4 py-3">
                  <dt className="flex items-center gap-1 text-xs text-muted-foreground">
                    {label}{!qualityEvaluation.metrics.complete && label !== '평가 완료' ? <span>(잠정)</span> : null}
                  </dt>
                  <dd className="mt-1 text-lg font-semibold tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          ) : null}

          {reviewMode === 'quality' && qualityEvaluation ? (
            <section className="rounded-xl border border-border bg-card p-4 lg:col-span-2" aria-labelledby="quality-insights-title">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div>
                  <h2 id="quality-insights-title" className="font-semibold">오류 분석</h2>
                  <p className="mt-1 text-xs text-muted-foreground">{qualityInsights?.scope_notice || '층화 표본에서 관측한 값이며 프로젝트 전체 조항의 추정치가 아닙니다.'}</p>
                </div>
                <Badge variant={qualityInsights?.status === 'complete' ? 'default' : 'outline'}>
                  {qualityInsightStatusLabel(qualityInsights?.status)}
                </Badge>
              </div>

              {qualityEvaluation.metrics.evaluated_count === 0 ? (
                <div className="mt-4 rounded-lg border border-dashed border-border bg-muted/25 px-4 py-5 text-sm text-muted-foreground">
                  아직 평가된 표본이 없습니다. 표본 판정을 시작하면 오류 신호와 구간 통계가 표시됩니다.
                </div>
              ) : (
                <>
                  <p className="mt-4 text-sm leading-6 text-muted-foreground">
                    {qualityInsights?.message || `${qualityEvaluation.metrics.evaluated_count}개 표본의 평가 결과를 집계했습니다.`}
                  </p>

                  <dl className="mt-4 grid gap-2 sm:grid-cols-3">
                    {[
                      ['정답 KCS Top3 누락(미탐)', Number(qualityErrorSignals?.kcs_missing ?? 0)],
                      ['적용 KCS 없음(오탐 후보)', Number(qualityErrorSignals?.all_candidates_incorrect ?? 0)],
                      ['정답이 2·3순위', Number(qualityErrorSignals?.candidate_selected_rank_2_or_3 ?? qualityErrorSignals?.rank_2_or_3_selected ?? qualityErrorSignals?.rank_2_or_3_hits ?? 0)],
                    ].map(([label, value]) => (
                      <div key={label} className="rounded-lg border border-border bg-background px-3 py-2.5">
                        <dt className="text-xs text-muted-foreground">{label}</dt>
                        <dd className="mt-1 text-lg font-semibold tabular-nums">{value}건</dd>
                      </div>
                    ))}
                  </dl>
                  <p className="mt-2 text-xs leading-5 text-muted-foreground">미탐은 정답 KCS가 상위 3개 후보에 없었던 경우, 오탐 후보는 적용할 KCS가 없는데 후보가 제시된 경우입니다.</p>

                  {(evaluatedCohortRows.length > 0 || evaluatedScoreBandRows.length > 0) && (
                    <div className="mt-4 grid gap-4 lg:grid-cols-2">
                      {evaluatedCohortRows.length > 0 && (
                        <div>
                          <h3 className="mb-2 text-sm font-medium">표본 구간</h3>
                          <div className="overflow-hidden rounded-lg border border-border">
                            {evaluatedCohortRows.map((row, index) => {
                              const total = Number(row.sample_count ?? row.total ?? 0);
                              const evaluated = Number(row.evaluated_count ?? row.evaluated ?? 0);
                              return (
                                <div key={`${row.cohort || 'cohort'}-${index}`} className="flex items-center justify-between gap-3 border-b border-border px-3 py-2.5 last:border-b-0">
                                  <span className="text-sm">{qualityCohortLabel(row.cohort)}</span>
                                  <span className="text-xs text-muted-foreground">평가 {evaluated}/{total} · 정확도 {percent(row.accuracy ?? row.sample_accuracy)}</span>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}
                      {evaluatedScoreBandRows.length > 0 && (
                        <div>
                          <h3 className="mb-2 text-sm font-medium">매칭 점수 구간</h3>
                          <div className="overflow-hidden rounded-lg border border-border">
                            {evaluatedScoreBandRows.map((row, index) => {
                              const total = Number(row.sample_count ?? row.total ?? 0);
                              const evaluated = Number(row.evaluated_count ?? row.evaluated ?? 0);
                              return (
                                <div key={`${row.band || 'band'}-${index}`} className="flex items-center justify-between gap-3 border-b border-border px-3 py-2.5 last:border-b-0">
                                  <span className="text-sm">{row.label || qualityScoreBandLabel(row.band)}</span>
                                  <span className="text-xs text-muted-foreground">평가 {evaluated}/{total} · 정확도 {percent(row.accuracy ?? row.sample_accuracy)}</span>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </section>
          ) : null}
        </div>

        <div
          className={cn(reviewMode === 'business' && businessView === 'bulk' ? 'block' : 'hidden')}
          aria-hidden={reviewMode !== 'business' || businessView !== 'bulk'}
        >
          <BulkReview
            key={project.id}
            projectId={project.id}
            refreshKey={bulkRefreshKey}
            onSaved={handleBulkSaved}
            onBusyChange={handleBulkBusyChange}
            onOpenDetail={(clauseId) => { void openDetailedClause(clauseId); }}
            onSplit={requestSplit}
            onMergeNext={requestMergeNext}
            structureBusy={detailMutationBusy || bulkSaving || syncingKcs}
            reviewLocked={reviewMutationLocked}
            kcsImpactFilterKey={bulkKcsImpactFilterKey}
          />
        </div>
        {(reviewMode !== 'business' || businessView !== 'bulk') && (
          <>
        <div className="grid gap-4 xl:grid-cols-[280px_minmax(0,1fr)_minmax(0,1.08fr)]">
          <aside className="self-start rounded-xl border border-border bg-card p-3 xl:sticky xl:top-24">
            <div className="mb-3 flex items-center justify-between px-1">
              <h2 className="font-medium">{reviewMode === 'quality' ? '품질평가 표본' : '문서 조항'}</h2>
              <Badge variant="outline">{reviewMode === 'quality' ? filteredQualityItems.length : filteredClauses.length}</Badge>
            </div>
            <label className="relative mb-3 block">
              <Search className="pointer-events-none absolute left-2.5 top-2 size-4 text-muted-foreground" />
              <input className="h-8 w-full rounded-lg border border-input bg-transparent py-1 pl-8 pr-2.5 text-sm outline-none placeholder:text-muted-foreground focus:border-ring focus:ring-3 focus:ring-ring/50" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="번호 또는 제목 검색" aria-label="조항 검색" />
            </label>
            {reviewMode === 'business' ? (
              <div className="mb-3 grid grid-cols-2 gap-1">
                {([
                  ['all', '전체'], ['unreviewed', '미검토'], ['keep', '남김'], ['delete', '삭제'], ['hold', '보류'],
                ] as [StatusFilter, string][]).map(([value, label]) => (
                  <Button key={value} size="sm" variant={statusFilter === value ? 'secondary' : 'ghost'} aria-pressed={statusFilter === value} onClick={() => setStatusFilter(value)}>{label}</Button>
                ))}
              </div>
            ) : qualityEvaluation ? (
              <p className="mb-3 px-1 text-xs text-muted-foreground">
                남은 표본 {qualityEvaluation.metrics.remaining_count}개
              </p>
            ) : null}
            <nav className="max-h-[62vh] space-y-1 overflow-y-auto pr-1" aria-label="문서 조항 목록">
              {reviewMode === 'business' ? filteredClauses.map((clause) => (
                  <button
                    key={clause.id}
                    className={cn(
                      'flex w-full items-start gap-2 rounded-lg px-3 py-2.5 text-left text-sm transition-colors hover:bg-muted',
                      clause.id === activeClauseId && 'bg-primary text-primary-foreground hover:bg-primary/90',
                    )}
                    onClick={() => { void navigateTo(clause.id); }}
                    disabled={detailMutationBusy || bulkSaving}
                  >
                    <span className="mt-0.5 shrink-0">
                      {clause.source_type === 'heading' ? <FileText className="size-4 opacity-60" /> : (
                        <>
                          {clause.decision === 'keep' && <CheckCircle2 className="size-4" />}
                          {clause.decision === 'delete' && <Trash2 className="size-4" />}
                          {clause.decision === 'hold' && <Clock3 className="size-4" />}
                          {clause.decision === null && <Circle className="size-4 opacity-50" />}
                        </>
                      )}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block font-medium">{clause.label}</span>
                      <span className={cn('mt-0.5 block line-clamp-2 text-xs opacity-70', clause.id === activeClauseId && 'opacity-85')}>{clause.title}</span>
                      {clause.source_type === 'heading' && <span className="mt-1 block text-[11px] opacity-70">문맥용 제목 · 판정 제외</span>}
                    </span>
                    {clause.source_type === 'table' && <Table2 className="mt-0.5 size-4 shrink-0 opacity-60" />}
                  </button>
                )) : filteredQualityItems.map((item) => (
                  <button
                    key={item.clause_id}
                    className={cn(
                      'flex w-full items-start gap-2 rounded-lg px-3 py-2.5 text-left text-sm transition-colors hover:bg-muted',
                      item.clause_id === activeClauseId && 'bg-primary text-primary-foreground hover:bg-primary/90',
                    )}
                    onClick={() => { void navigateTo(item.clause_id); }}
                    disabled={detailMutationBusy || bulkSaving}
                  >
                    <span className="mt-0.5 shrink-0">
                      {item.verdict === null ? <Circle className="size-4 opacity-50" /> : item.verdict === 'kcs_missing' ? <AlertTriangle className="size-4" /> : <CheckCircle2 className="size-4" />}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center justify-between gap-2 font-medium">
                        <span>{item.sample_order}. {item.label}</span>
                        <span className="text-[10px] opacity-70">{qualityCohortLabel(item.cohort)}</span>
                      </span>
                      <span className={cn('mt-0.5 block line-clamp-2 text-xs opacity-70', item.clause_id === activeClauseId && 'opacity-85')}>{item.title}</span>
                      {item.verdict !== null && <span className="mt-1 block text-[11px] opacity-75">{qualityVerdictLabel(item.verdict)}</span>}
                    </span>
                  </button>
                ))}
              {(reviewMode === 'business' ? filteredClauses.length : filteredQualityItems.length) === 0 && <p className="px-3 py-8 text-center text-sm text-muted-foreground">조건에 맞는 조항이 없습니다.</p>}
            </nav>
          </aside>

          {!detail ? (
            <div className="col-span-2 grid min-h-96 place-items-center rounded-xl border border-border bg-card"><div className="flex items-center gap-2 text-muted-foreground"><Spinner />조항을 불러오는 중입니다.</div></div>
          ) : (
            <>
              <article className="rounded-xl border border-border bg-card p-5">
                <div className="mb-5 flex items-start justify-between gap-3">
                  <div><p className="mb-1 text-sm text-muted-foreground">포스코 시방서 · {detail.label}</p><h2 ref={detailHeadingRef} tabIndex={-1} className="text-xl font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring">{detail.title}</h2></div>
                  <div className="flex shrink-0 flex-wrap justify-end gap-2">
                    {reviewMode === 'business' && detail.source_type === 'paragraph' && (
                      <>
                        {detail.content.trim() && <Button size="sm" variant="outline" onClick={() => requestSplit(detail)} disabled={detailMutationBusy || bulkSaving || reviewMutationLocked}><Scissors />나누기</Button>}
                        <Button size="sm" variant="outline" onClick={() => requestMergeNext(detail)} disabled={detailMutationBusy || bulkSaving || reviewMutationLocked}><Combine />다음과 합치기</Button>
                      </>
                    )}
                    <Badge variant="outline">{detail.source_type === 'table' ? '표 행' : detail.source_type === 'heading' ? '구조 제목' : '원문'}</Badge>
                  </div>
                </div>
                {detailNeedsKcsReview && detail.kcs_impact && (
                  <Alert className="mb-4 border-amber-400/70 bg-amber-50/60 dark:bg-amber-950/20">
                    <AlertTriangle />
                    <AlertTitle>최신 KCS 기준으로 재검토가 필요합니다</AlertTitle>
                    <AlertDescription>
                      <p>{detail.kcs_impact.reason || 'KCS 후보 또는 근거 내용에 실질적인 변경이 확인되었습니다.'}</p>
                      <p className="mt-2 text-xs">
                        기존 {detail.decision === 'keep' ? '남김' : detail.decision === 'delete' ? '삭제' : detail.decision === 'hold' ? '보류' : '미검토'} 판정은 보존되어 있습니다. 후보와 차이를 확인한 뒤 담당자 판정 버튼을 다시 눌러 현재 KCS 기준 검토를 완료하세요.
                      </p>
                      <p className="mt-2 text-xs">확인 대상 KCS revision: {detail.kcs_impact.target_revision.slice(0, 10)}</p>
                    </AlertDescription>
                  </Alert>
                )}
                <div className="min-h-48 rounded-lg bg-muted/55 p-5 text-base leading-8 whitespace-pre-wrap">{detail.content || detail.title}</div>

                {reviewMode === 'business' ? (
                  <div className="mt-5 border-t border-border pt-5">
                    {detail.source_type === 'heading' ? (
                      <div className="rounded-lg bg-muted/55 p-4 text-sm leading-6 text-muted-foreground">
                        <Badge variant="outline" className="mb-2">문맥용 제목</Badge>
                        <p>목차와 구조 제목은 KCS 매칭 및 담당자 판정에서 제외됩니다. 뒤 조항의 후보 검색 문맥으로만 사용됩니다.</p>
                      </div>
                    ) : (
                      <>
                        <p className="mb-3 text-sm font-medium">담당자 판정</p>
                        <label className="mb-3 block text-sm font-medium" htmlFor="decision-reason">
                          판정 사유
                          <select
                            id="decision-reason"
                            className="mt-2 h-10 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground"
                            value={decisionReason}
                            onChange={(event) => updateDraft(() => {
                              setDecisionReason(event.target.value);
                              if (event.target.value !== 'fully_covered_by_kcs') setCoverageConfirmed(false);
                              if (event.target.value === 'no_kcs_match') setSelectedCandidateId(null);
                            })}
                            disabled={detailMutationBusy || reviewMutationLocked}
                          >
                            <option value="">사유를 선택하세요</option>
                            {(['keep', 'hold', 'delete'] as const).map((value) => (
                              <optgroup key={value} label={value === 'keep' ? '남김' : value === 'hold' ? '보류' : '삭제'}>
                                {DECISION_REASON_OPTIONS[value].map((option) => (
                                  <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                              </optgroup>
                            ))}
                          </select>
                        </label>
                        <fieldset className="grid grid-cols-3 gap-2">
                          <legend className="sr-only">담당자 판정</legend>
                          <Button variant={decision === 'keep' ? 'default' : 'outline'} aria-pressed={decision === 'keep'} onClick={() => chooseDecision('keep')} disabled={detailMutationBusy || reviewMutationLocked}><CheckCircle2 />{detailNeedsKcsReview && decision === 'keep' ? '남김 유지·확인' : '남김'}</Button>
                          <Button variant={decision === 'delete' ? 'destructive' : 'outline'} aria-pressed={decision === 'delete'} onClick={() => chooseDecision('delete')} disabled={detailMutationBusy || reviewMutationLocked}><Trash2 />삭제</Button>
                          <Button variant={decision === 'hold' ? 'secondary' : 'outline'} aria-pressed={decision === 'hold'} onClick={() => chooseDecision('hold')} disabled={detailMutationBusy || reviewMutationLocked}><Clock3 />{detailNeedsKcsReview && decision === 'hold' ? '보류 유지·확인' : '보류'}</Button>
                        </fieldset>
                        <p className="mt-3 text-sm leading-6 text-muted-foreground">
                          KCS 중복 삭제만 본문 후보가 필요하며, KCS 외 삭제는 선택한 사유로 기록됩니다.
                        </p>
                        {decisionReason === 'management_decision' && !reviewNote.trim() && (
                          <p className="mt-1 text-sm text-amber-700 dark:text-amber-300">담당자 판단 삭제는 아래 검토의견에 사유를 입력해야 합니다.</p>
                        )}

                        <label className="mt-4 block text-sm font-medium" htmlFor="edited-content">간소화 시방서 출력문</label>
                        <Textarea id="edited-content" className="mt-2 min-h-36 text-base leading-7" value={editedContent} onChange={(event) => updateDraft(() => setEditedContent(event.target.value))} disabled={decision === 'delete' || detailMutationBusy || reviewMutationLocked} />
                        {decision === 'delete' && <p className="mt-2 text-sm text-muted-foreground">삭제 판정 조항은 DOCX에서 제외되지만 원문과 판정 근거는 보존됩니다.</p>}

                        <label className="mt-4 block text-sm font-medium" htmlFor="review-note">검토의견</label>
                        <Textarea id="review-note" className="mt-2 min-h-24" value={reviewNote} onChange={(event) => updateDraft(() => setReviewNote(event.target.value))} placeholder="포스코 특화 사유, 수치 차이, 추가 확인사항 등을 기록하세요." disabled={detailMutationBusy || reviewMutationLocked} />
                        <div className="mt-4 flex items-center justify-between gap-3">
                          <span className="text-sm text-muted-foreground">{saving ? '저장 중…' : dirty ? '저장되지 않은 변경사항' : savedAt ? `${savedAt} 저장됨` : detail.reviewed_at ? '저장된 판정' : '미검토'}</span>
                          <Button onClick={() => saveCurrent()} disabled={!dirty || detailMutationBusy || reviewMutationLocked}>{saving ? <Spinner /> : <Save />}변경사항 저장</Button>
                        </div>
                      </>
                    )}
                  </div>
                ) : (
                  <div className="mt-5 border-t border-border pt-5">
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-medium">매칭 품질 판정</p>
                        <p className="mt-1 text-xs text-muted-foreground">업무 판정(남김·삭제·보류)과 분리 저장됩니다.</p>
                      </div>
                      {activeQualityItem && <Badge variant={activeQualityItem.verdict ? 'secondary' : 'outline'}>표본 {activeQualityItem.sample_order} · {qualityVerdictLabel(activeQualityItem.verdict)}</Badge>}
                    </div>

                    <fieldset className="grid gap-2">
                      <legend className="sr-only">매칭 품질 판정</legend>
                      {detail.candidates.length > 0 ? (
                        <>
                          <Button
                            variant={qualityVerdict === 'candidate_selected' ? 'default' : 'outline'}
                            aria-pressed={qualityVerdict === 'candidate_selected'}
                            onClick={() => void saveQualityVerdict('candidate_selected')}
                            disabled={detailMutationBusy || reviewMutationLocked || candidateTab === 'none'}
                          >
                            {qualitySaving ? <Spinner /> : <CheckCircle2 />}현재 후보가 적정
                          </Button>
                          <Button
                            variant={qualityVerdict === 'all_candidates_incorrect' ? 'secondary' : 'outline'}
                            aria-pressed={qualityVerdict === 'all_candidates_incorrect'}
                            onClick={() => void saveQualityVerdict('all_candidates_incorrect')}
                            disabled={detailMutationBusy || reviewMutationLocked}
                          >
                            <AlertTriangle />적용 KCS 없음(제시 후보 오탐)
                          </Button>
                        </>
                      ) : (
                        <Button
                          variant={qualityVerdict === 'no_candidate_correct' ? 'default' : 'outline'}
                          aria-pressed={qualityVerdict === 'no_candidate_correct'}
                          onClick={() => void saveQualityVerdict('no_candidate_correct')}
                          disabled={detailMutationBusy || reviewMutationLocked}
                        >
                          {qualitySaving ? <Spinner /> : <CheckCircle2 />}후보 없음이 적정
                        </Button>
                      )}
                      <Button
                        variant={qualityVerdict === 'kcs_missing' ? 'secondary' : 'outline'}
                        aria-pressed={qualityVerdict === 'kcs_missing'}
                        onClick={() => {
                          setQualityVerdict('kcs_missing');
                          setQualityDraftDirty(true);
                        }}
                        disabled={detailMutationBusy || reviewMutationLocked}
                      >
                        <Search />정답 KCS가 Top3에 없음(미탐)
                      </Button>
                    </fieldset>
                    <p className="mt-2 text-xs leading-5 text-muted-foreground">적용할 KCS 자체가 없으면 오탐 후보를 선택하고, 정답 KCS가 따로 존재하지만 세 후보에 없으면 미탐을 선택하세요.</p>

                    {qualityVerdict === 'kcs_missing' && (
                      <div className="mt-4 rounded-lg border border-border bg-muted/35 p-4">
                        <p className="mb-3 text-sm font-medium">누락된 정답 KCS</p>
                        <div className="grid gap-3 sm:grid-cols-2">
                          <label className="text-sm">
                            <span className="mb-1.5 block font-medium">KCS 코드 <span className="text-destructive">*</span></span>
                            <input
                              className="h-9 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/50"
                              value={expectedKcsCode}
                              onChange={(event) => {
                                setExpectedKcsCode(event.target.value);
                                setQualityDraftDirty(true);
                              }}
                              placeholder="예: KCS 41 31 00"
                              disabled={detailMutationBusy || reviewMutationLocked}
                            />
                          </label>
                          <label className="text-sm">
                            <span className="mb-1.5 block font-medium">정답 조항</span>
                            <input
                              className="h-9 w-full rounded-lg border border-input bg-background px-3 text-sm outline-none focus:border-ring focus:ring-3 focus:ring-ring/50"
                              value={expectedKcsClause}
                              onChange={(event) => {
                                setExpectedKcsClause(event.target.value);
                                setQualityDraftDirty(true);
                              }}
                              placeholder="예: 3.2.1"
                              disabled={detailMutationBusy || reviewMutationLocked}
                            />
                          </label>
                        </div>
                        <label className="mt-3 block text-sm font-medium" htmlFor="quality-note">평가 메모</label>
                        <Textarea id="quality-note" className="mt-2 min-h-20" value={qualityNote} onChange={(event) => {
                          setQualityNote(event.target.value);
                          setQualityDraftDirty(true);
                        }} placeholder="정답 근거 또는 검색 실패 원인을 기록하세요." disabled={detailMutationBusy || reviewMutationLocked} />
                        <div className="mt-3 flex justify-end">
                          <Button onClick={() => void saveQualityVerdict('kcs_missing')} disabled={detailMutationBusy || reviewMutationLocked || !expectedKcsCode.trim()}>
                            {qualitySaving ? <Spinner /> : <Save />}누락 판정 저장
                          </Button>
                        </div>
                      </div>
                    )}
                    <p className="mt-3 text-xs leading-5 text-muted-foreground">
                      {qualityDraftDirty
                        ? '저장되지 않은 누락 근거가 있습니다. 저장 후 다른 표본이나 프로젝트로 이동할 수 있습니다.'
                        : '판정을 저장하면 다음 미평가 표본으로 자동 이동합니다. 오른쪽 후보 탭을 바꾸면 해당 후보를 정답으로 선택할 수 있습니다.'}
                    </p>
                  </div>
                )}
              </article>

              <article className="rounded-xl border border-border bg-card p-5">
                <div className="mb-4 flex items-start justify-between gap-3">
                  <div><p className="mb-1 text-sm text-muted-foreground">최신 KCS 매칭 결과</p><h2 className="text-xl font-semibold">후보 {detail.candidates.length}개</h2></div>
                  <Badge>{detail.candidates.length ? '자동 분석' : '후보 없음'}</Badge>
                </div>
                <p className="mb-4 text-sm leading-6 text-muted-foreground">
                  {reviewMode === 'quality'
                    ? '후보 탭을 바꿔 원문과 대조한 뒤, 왼쪽에서 현재 후보의 적정 여부를 판정하세요.'
                    : '유사도는 검토할 후보를 찾기 위한 검색 점수이며, 두 조항이 동일하거나 삭제 가능하다는 자동 판정이 아닙니다.'}
                </p>
                {reviewMode === 'business' && detail.source_type !== 'heading' && detail.candidates.length > 0 && (
                  <section className="mb-4 rounded-lg border border-primary/25 bg-primary/5 p-4" aria-labelledby="coverage-analysis-title" aria-busy={analyzingCoverage}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <p id="coverage-analysis-title" className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-primary" />GPT 요구사항 전체포괄 검사</p>
                        <p className="mt-1 text-xs leading-5 text-muted-foreground">포스코 원문과 장문 KCS를 내용 누락 없이 자동 분할하고, 후보 최대 3개의 수치·조건·예외·시험 빈도·책임·의무를 모두 대조한 뒤 보수적으로 통합합니다. 한 원문 구간의 일부만 KCS에 있으면 부분 포괄로 구분합니다.</p>
                      </div>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => void analyzeCoverage()}
                        disabled={!config?.openai_available || detailMutationBusy || reviewMutationLocked}
                      >
                        {analyzingCoverage ? <Spinner /> : detail.coverage_analysis ? <RefreshCw /> : <Sparkles />}
                        {analyzingCoverage ? '분석 중' : detail.coverage_analysis ? '다시 분석' : '전체포괄 분석'}
                      </Button>
                    </div>

                    {detail.coverage_analysis ? (
                      <output className="mt-4 block space-y-3" aria-live="polite">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={detail.coverage_analysis.coverage_status === 'conflict' ? 'destructive' : detail.coverage_analysis.deletion_safe ? 'default' : 'secondary'}>
                            {coverageStatusLabel(detail.coverage_analysis.coverage_status)}
                          </Badge>
                          <Badge variant="outline">신뢰도 {percent(detail.coverage_analysis.confidence)}</Badge>
                          <Badge variant={detail.coverage_analysis.deletion_safe ? 'default' : 'outline'}>
                            {detail.coverage_analysis.deletion_safe ? '삭제 검토 가능' : '삭제 불가 · 잔여 확인'}
                          </Badge>
                        </div>
                        <p className="text-sm leading-6 text-muted-foreground">{detail.coverage_analysis.rationale}</p>
                        <div className="space-y-2">
                          {detail.coverage_analysis.requirements.map((requirement, index) => {
                            const evidence = requirement.evidence_candidate_ids
                              .map((candidateId) => detail.candidates.find((candidate) => candidate.id === candidateId))
                              .filter((candidate): candidate is Candidate => Boolean(candidate))
                              .map((candidate) => `${candidate.kcs_code} ${candidate.kcs_clause || ''}`.trim())
                              .join(', ');
                            return (
                              <div key={`${requirement.requirement}-${index}`} className="rounded-md border border-border bg-background p-3">
                                <div className="flex flex-wrap items-start justify-between gap-2">
                                  <p className="text-sm font-medium">{index + 1}. {requirement.requirement}</p>
                                  <div className="flex items-center gap-1.5">
                                    <Badge variant="outline">원문 {requirement.source_segment_ids.join(', ')}</Badge>
                                    <Badge variant={requirement.status === 'conflict' ? 'destructive' : requirement.status === 'covered' ? 'secondary' : 'outline'}>{requirementStatusLabel(requirement.status)}</Badge>
                                  </div>
                                </div>
                                <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                                  {evidence ? `근거 ${evidence} · ` : ''}{requirement.evidence || '명시 근거 없음'}
                                </p>
                              </div>
                            );
                          })}
                        </div>
                        {detail.coverage_analysis.residual_content && (
                          <div className="rounded-md border border-amber-300/60 bg-amber-50/70 p-3 dark:bg-amber-950/20">
                            <p className="text-xs font-medium text-amber-900 dark:text-amber-200">KCS와 중복되지 않는 포스코 잔여 문구</p>
                            <p className="mt-2 whitespace-pre-wrap text-sm leading-6">{detail.coverage_analysis.residual_content}</p>
                            <Button
                              className="mt-3"
                              size="sm"
                              variant="outline"
                              disabled={detailMutationBusy || reviewMutationLocked}
                              onClick={() => updateDraft(() => {
                                setEditedContent(detail.coverage_analysis!.residual_content);
                                setDecision('keep');
                                setDecisionReason(detail.coverage_analysis!.coverage_status === 'posco_specific' ? 'posco_specific' : 'partial_overlap_residual');
                                setCoverageConfirmed(false);
                              })}
                            >
                              <Check />잔여 문구를 출력문에 적용
                            </Button>
                          </div>
                        )}
                        <p className="text-xs leading-5 text-muted-foreground">GPT 판정은 검토 보조자료입니다. 장문 분할 호출 중 하나라도 실패하거나 하나의 의무·KCS 후보가 여러 입력 묶음에 걸치거나 충돌·불확실성이 있으면 삭제할 수 없습니다. 삭제는 신뢰도 85% 이상·차이 경고 없음·담당자 직접 확인을 모두 만족해야 저장됩니다.</p>
                      </output>
                    ) : (
                      <p className="mt-3 text-xs leading-5 text-muted-foreground">
                        {config?.openai_available
                          ? '개별 후보의 유사도만으로 삭제하지 않고, 포스코 문장의 모든 독립 요구사항을 후보 조합이 실제로 포함하는지 검사합니다.'
                          : '프로젝트 .env에 OPENAI_API_KEY를 설정하면 요구사항 단위 전체포괄 검사를 사용할 수 있습니다.'}
                      </p>
                    )}
                  </section>
                )}
                {detail.candidates.length === 0 ? (
                  <div className="grid min-h-72 place-items-center rounded-lg border border-dashed border-border bg-muted/30 p-8 text-center">
                    <div><FileText className="mx-auto mb-3 size-8 text-muted-foreground" /><p className="font-medium">적절한 KCS 후보가 없습니다</p><p className="mt-2 text-sm text-muted-foreground">유사도 25% 미만 후보는 숨기되, 포스코 원문에 KCS 코드가 명시된 경우에는 확인용 후보로 표시합니다. 포스코 고유기준 여부를 검토하세요.</p></div>
                  </div>
                ) : (
                  <>
                    {reviewMode === 'business' && decisionReason === 'no_kcs_match' && (
                      <p className="mb-3 rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm leading-6 text-blue-900">
                        대응 KCS 없음으로 판정 중입니다. 후보는 비교용으로만 표시되며 판정 근거로 선택되지 않습니다.
                      </p>
                    )}
                    <Tabs value={candidateTab} onValueChange={setCandidateTab}>
                    <TabsList className="grid w-full" style={{ gridTemplateColumns: `repeat(${detail.candidates.length}, minmax(0, 1fr))` }}>
                      {detail.candidates.map((candidate) => <TabsTrigger key={candidate.id} value={candidate.id}>후보 {candidate.rank} · {percent(candidate.score)}</TabsTrigger>)}
                    </TabsList>
                    {detail.candidates.map((candidate) => (
                      <TabsContent key={candidate.id} value={candidate.id}>
                        <CandidatePanel
                          candidate={candidate}
                          selected={(reviewMode === 'business' ? selectedCandidateId : activeQualityItem?.relevant_candidate_id) === candidate.id}
                          onSelect={() => {
                            if (decision === 'delete') {
                              setError('삭제 판정을 변경한 뒤 KCS 후보를 다시 선택해 주세요.');
                              return;
                            }
                            const selected = selectedCandidateId === candidate.id;
                            if (!selected && decisionReason === 'no_kcs_match') {
                              setError('KCS 후보를 선택하려면 판정 사유를 “대응 KCS 없음”이 아닌 사유로 변경해 주세요.');
                              return;
                            }
                            updateDraft(() => {
                              setSelectedCandidateId(selected ? null : candidate.id);
                              setCoverageConfirmed(false);
                            });
                          }}
                          aiAvailable={Boolean(config?.openai_available)}
                          analysisAllowed={reviewMode === 'business' && !detail.quality_evaluation}
                          analyzing={analyzingCandidateId === candidate.id}
                          interactionDisabled={detailMutationBusy || reviewMutationLocked}
                          selectionDisabled={reviewMode === 'business'
                            && decisionReason === 'no_kcs_match'
                            && selectedCandidateId !== candidate.id}
                          onAnalyze={() => void analyzeCandidate(candidate.id)}
                          onApplySimplified={reviewMode === 'business'
                            ? (content) => updateDraft(() => setEditedContent(content))
                            : undefined}
                          showSelection={reviewMode === 'business'}
                        />
                      </TabsContent>
                    ))}
                    </Tabs>
                  </>
                )}
                {reviewMode === 'business' && Boolean(detail.excluded_candidates?.length) && (
                  <details className="mt-4 rounded-lg border border-border bg-muted/30 p-4">
                    <summary className="cursor-pointer text-sm font-medium">
                      GPT 제외 후보 {detail.excluded_candidates?.length}개 보기
                    </summary>
                    <div className="mt-3 space-y-3">
                      {detail.excluded_candidates?.map((candidate) => (
                        <div key={candidate.id} className="rounded-md border border-border bg-background p-3">
                          <div className="flex flex-wrap items-start justify-between gap-3">
                            <div>
                              <p className="text-sm font-medium">{candidate.kcs_code} · {candidate.kcs_clause || '본문'}</p>
                              <p className="mt-1 text-xs text-muted-foreground">{candidate.title}</p>
                            </div>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => void restoreCandidate(candidate.id)}
                              disabled={detailMutationBusy || reviewMutationLocked || Boolean(detail.quality_evaluation)}
                              title={detail.quality_evaluation ? '품질평가 표본의 후보군은 복원할 수 없습니다.' : undefined}
                            >
                              {restoringCandidateId === candidate.id ? <Spinner /> : <RefreshCw />}후보 복원
                            </Button>
                          </div>
                          <p className="mt-2 text-xs leading-5 text-muted-foreground">
                            {candidate.ai_analysis?.rationale} · 신뢰도 {percent(candidate.ai_analysis?.confidence)}
                          </p>
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </article>
            </>
          )}
        </div>

        <footer className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-border bg-card px-4 py-3">
          <Button variant="ghost" disabled={!previousClauseId || detailMutationBusy || qualitySaving} onClick={() => previousClauseId && navigateTo(previousClauseId)}><ChevronLeft />이전 {reviewMode === 'quality' ? '표본' : '조항'}</Button>
          <p className="text-sm text-muted-foreground">
            {reviewMode === 'quality' ? '품질평가는 업무 판정을 변경하지 않으며 고정된 표본과 함께 저장됩니다.' : '판정과 수정문은 원문·KCS 스냅샷과 함께 이력으로 저장됩니다.'}
          </p>
          <Button variant="outline" disabled={!nextClauseId || detailMutationBusy || qualitySaving} onClick={() => nextClauseId && navigateTo(nextClauseId)}>다음 {reviewMode === 'quality' ? '표본' : '조항'}<ChevronRight /></Button>
        </footer>
          </>
        )}
      </section>

      {uploadJob && (
        <Dialog open={uploadStatusOpen} onOpenChange={setUploadStatusOpen}>
          <DialogContent className="max-w-3xl">
            <DialogHeader>
              <DialogTitle>문서 업로드 현황</DialogTitle>
              <DialogDescription>서버에서 순서대로 처리하므로 창을 닫아도 현재 프로젝트 검토를 계속할 수 있습니다.</DialogDescription>
            </DialogHeader>
            <UploadJobPanel
              job={uploadJob}
              pollError={uploadPollError}
              onOpenProject={(projectId) => {
                setUploadStatusOpen(false);
                void switchProject(projectId);
              }}
              onRetryFiles={() => {
                setUploadStatusOpen(false);
                uploadInput.current?.click();
              }}
              onRetryItem={(jobId, itemId) => { void retryUploadItem(jobId, itemId); }}
              retryingItemId={retryingUploadItemId}
            />
          </DialogContent>
        </Dialog>
      )}

      <Dialog
        open={reviewWorkflowDialog !== null}
        onOpenChange={(open) => {
          if (!open && !reviewWorkflowBusy) {
            setReviewWorkflowDialog(null);
            setReviewWorkflowError('');
          }
        }}
      >
        <DialogContent className="max-w-xl">
          <DialogHeader>
            <DialogTitle>{reviewWorkflowDialog === 'submit' ? '작성자 검토본 제출' : '승인자 검토'}</DialogTitle>
            <DialogDescription>
              {reviewWorkflowDialog === 'submit'
                ? '현재 판정과 KCS·GPT 근거를 제출본으로 고정합니다. 제출 후에는 승인 또는 수정 요청 전까지 내용을 변경할 수 없습니다.'
                : '제출 당시 내용을 일괄 검토와 상세 비교에서 확인한 뒤 승인하거나 작성자에게 돌려보내세요.'}
            </DialogDescription>
          </DialogHeader>

          {reviewWorkflowError && (
            <Alert variant="destructive">
              <AlertTriangle /><AlertTitle>승인 절차를 진행하지 못했습니다</AlertTitle><AlertDescription>{reviewWorkflowError}</AlertDescription>
            </Alert>
          )}

          <div className="space-y-4">
            <div className="rounded-lg border border-border bg-muted/45 p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="outline">{reviewWorkflowDialog === 'submit' ? '역할 · 작성자' : '역할 · 승인자'}</Badge>
                <span className="font-medium">{projectDisplayTitle(project)}</span>
              </div>
              {reviewWorkflowDialog === 'decision' && latestReviewSubmission ? (
                <div className="mt-2 space-y-1 text-xs text-muted-foreground">
                  <p>제출자 {latestReviewSubmission.author_name} · {snapshotDate(latestReviewSubmission.submitted_at)}</p>
                  <p>KCS 개정 {latestReviewSubmission.kcs_revision || project.kcs_revision}</p>
                  {latestReviewSubmission.author_note && <p className="whitespace-pre-wrap">작성자 메모 · {latestReviewSubmission.author_note}</p>}
                </div>
              ) : (
                <p className="mt-2 text-xs text-muted-foreground">현재 KCS 개정 {project.kcs_revision}</p>
              )}
            </div>

            <label className="block text-sm font-medium" htmlFor="review-workflow-name">
              {reviewWorkflowDialog === 'submit' ? '작성자 이름' : '승인자 이름'}
              <input
                id="review-workflow-name"
                className="mt-2 h-10 w-full rounded-lg border border-input bg-background px-3 text-sm text-foreground outline-none placeholder:text-muted-foreground focus:border-ring focus:ring-3 focus:ring-ring/50"
                value={reviewActorName}
                onChange={(event) => {
                  setReviewActorName(event.target.value);
                  setReviewWorkflowError('');
                }}
                placeholder={reviewWorkflowDialog === 'submit' ? '작성자 실명' : '승인자 실명'}
                maxLength={100}
                autoComplete="name"
                disabled={reviewWorkflowBusy}
              />
            </label>

            {reviewWorkflowDialog === 'submit' ? (
              <>
                <label className="block text-sm font-medium" htmlFor="review-workflow-note">
                  제출 메모 <span className="font-normal text-muted-foreground">(선택)</span>
                  <Textarea
                    id="review-workflow-note"
                    className="mt-2 min-h-24"
                    value={reviewWorkflowNote}
                    onChange={(event) => setReviewWorkflowNote(event.target.value)}
                    placeholder="승인자가 확인할 사항을 기록하세요."
                    maxLength={5000}
                    disabled={reviewWorkflowBusy}
                  />
                </label>
                {!project.review_submission_ready && (
                  <Alert>
                    <AlertTriangle /><AlertTitle>아직 제출할 수 없습니다</AlertTitle>
                    <AlertDescription>
                      {(project.review_submission_blockers.length
                        ? project.review_submission_blockers
                        : ['모든 조항의 판정과 삭제 근거를 완료해 주세요.']).join(' · ')}
                    </AlertDescription>
                  </Alert>
                )}
              </>
            ) : (
              <>
                {reviewActorIsSubmissionAuthor && (
                  <p role="alert" className="text-sm text-destructive">작성자와 다른 승인자를 입력해 주세요.</p>
                )}
                <label className="block text-sm font-medium" htmlFor="review-approval-note">
                  승인 메모 <span className="font-normal text-muted-foreground">(승인 시 선택)</span>
                  <Textarea
                    id="review-approval-note"
                    className="mt-2 min-h-20"
                    value={reviewWorkflowNote}
                    onChange={(event) => setReviewWorkflowNote(event.target.value)}
                    placeholder="승인 판단에 남길 메모가 있으면 기록하세요."
                    maxLength={5000}
                    disabled={reviewWorkflowBusy}
                  />
                </label>
                <label className="block text-sm font-medium" htmlFor="review-change-reason">
                  수정 요청 사유 <span className="text-destructive">(수정 요청 시 필수)</span>
                  <Textarea
                    id="review-change-reason"
                    className="mt-2 min-h-24"
                    value={reviewChangeReason}
                    onChange={(event) => {
                      setReviewChangeReason(event.target.value);
                      setReviewWorkflowError('');
                    }}
                    placeholder="보완할 조항과 이유를 구체적으로 기록하세요."
                    maxLength={5000}
                    disabled={reviewWorkflowBusy}
                  />
                </label>
              </>
            )}
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => setReviewWorkflowDialog(null)} disabled={reviewWorkflowBusy}>취소</Button>
            {reviewWorkflowDialog === 'submit' ? (
              <Button
                onClick={() => { void submitReviewWorkflow(); }}
                disabled={reviewWorkflowBusy || !reviewActorName.trim() || !project.review_submission_ready}
              >
                {reviewWorkflowBusy ? <Spinner /> : <Send />}{reviewWorkflowBusy ? '제출 중' : '승인자에게 제출'}
              </Button>
            ) : (
              <>
                <Button
                  variant="destructive"
                  onClick={() => { void requestReviewChanges(); }}
                  disabled={reviewWorkflowBusy || !reviewActorName.trim() || reviewActorIsSubmissionAuthor || !reviewChangeReason.trim()}
                >
                  {reviewWorkflowBusy ? <Spinner /> : <RotateCcw />}수정 요청
                </Button>
                <Button
                  onClick={() => { void approveReviewWorkflow(); }}
                  disabled={reviewWorkflowBusy || !reviewActorName.trim() || reviewActorIsSubmissionAuthor}
                >
                  {reviewWorkflowBusy ? <Spinner /> : <ShieldCheck />}승인 확정
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={structureDialog !== null}
        onOpenChange={(open) => {
          if (!open && !structureBusy) {
            setStructureDialog(null);
            setStructureError('');
          }
        }}
      >
        <AlertDialogContent className="max-w-3xl">
          <AlertDialogHeader>
            <AlertDialogTitle>
              {structureDialog?.kind === 'split' ? '포스코 조항을 두 개로 나눌까요?' : '인접한 두 조항을 합칠까요?'}
            </AlertDialogTitle>
            <AlertDialogDescription>
              구조를 바꾸면 기존 후보·후보 선택·수정문·검토 메모·GPT 분석을 승계하지 않고, 동일한 최신 KCS 개정으로 새 후보를 찾습니다. 판정 이력이 없는 일반 문단에만 적용됩니다.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {structureError && (
            <Alert variant="destructive">
              <AlertTriangle /><AlertTitle>구조를 변경하지 못했습니다</AlertTitle><AlertDescription>{structureError}</AlertDescription>
            </Alert>
          )}

          {structureDialog?.kind === 'split' ? (
            <div className="space-y-4">
              <div className="rounded-lg border border-border bg-muted/45 p-3 text-sm">
                <p className="font-medium">{structureDialog.label} · {structureDialog.title}</p>
                <p className="mt-1 text-xs text-muted-foreground">슬라이더로 두 번째 조항이 시작될 위치를 정하세요. 원문 글자는 수정되거나 삭제되지 않습니다.</p>
              </div>
              <div>
                <label className="block text-sm font-medium" htmlFor="clause-split-position">
                  분할 위치 · {structureDialog.splitIndex} / {structureDialog.sourceText.length}자
                </label>
                <input
                  id="clause-split-position"
                  type="range"
                  min={1}
                  max={Math.max(1, structureDialog.sourceText.length - 1)}
                  value={structureDialog.splitIndex}
                  onChange={(event) => {
                    const splitIndex = Number(event.target.value);
                    setStructureDialog((current) => current?.kind === 'split' ? { ...current, splitIndex } : current);
                  }}
                  disabled={structureBusy}
                  className="mt-3 w-full accent-primary"
                />
                <span className="mt-2 flex flex-wrap gap-2">
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={structureBusy || !candidateSplitIndexes(structureDialog.sourceText).some((index) => index < structureDialog.splitIndex)}
                    onClick={() => {
                      const previous = candidateSplitIndexes(structureDialog.sourceText)
                        .filter((index) => index < structureDialog.splitIndex)
                        .at(-1);
                      if (previous) setStructureDialog({ ...structureDialog, splitIndex: previous });
                    }}
                  >
                    <ChevronLeft />이전 문장 경계
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    disabled={structureBusy || !candidateSplitIndexes(structureDialog.sourceText).some((index) => index > structureDialog.splitIndex)}
                    onClick={() => {
                      const next = candidateSplitIndexes(structureDialog.sourceText)
                        .find((index) => index > structureDialog.splitIndex);
                      if (next) setStructureDialog({ ...structureDialog, splitIndex: next });
                    }}
                  >
                    다음 문장 경계<ChevronRight />
                  </Button>
                </span>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <section className="min-w-0 rounded-lg border border-border p-3">
                  <p className="mb-2 text-xs font-medium text-muted-foreground">앞 조항</p>
                  <p className="max-h-44 overflow-y-auto whitespace-pre-wrap text-sm leading-6">{structureDialog.sourceText.slice(0, structureDialog.splitIndex)}</p>
                </section>
                <section className="min-w-0 rounded-lg border border-border p-3">
                  <p className="mb-2 text-xs font-medium text-muted-foreground">뒤 조항</p>
                  <p className="max-h-44 overflow-y-auto whitespace-pre-wrap text-sm leading-6">{structureDialog.sourceText.slice(structureDialog.splitIndex)}</p>
                </section>
              </div>
            </div>
          ) : structureDialog?.kind === 'merge' ? (
            <div className="grid gap-3 md:grid-cols-2">
              <section className="rounded-lg border border-border bg-muted/45 p-3 text-sm">
                <p className="text-xs text-muted-foreground">현재 조항</p>
                <p className="mt-1 font-medium">{structureDialog.firstLabel} · {structureDialog.firstTitle}</p>
              </section>
              <section className="rounded-lg border border-border bg-muted/45 p-3 text-sm">
                <p className="text-xs text-muted-foreground">바로 다음 조항</p>
                <p className="mt-1 font-medium">{structureDialog.secondLabel} · {structureDialog.secondTitle}</p>
              </section>
            </div>
          ) : null}

          <AlertDialogFooter>
            <AlertDialogCancel disabled={structureBusy}>취소</AlertDialogCancel>
            <Button
              onClick={() => { void confirmStructureEdit(); }}
              disabled={
                structureBusy
                || (structureDialog?.kind === 'split'
                  && (!structureDialog.sourceText.slice(0, structureDialog.splitIndex).trim()
                    || !structureDialog.sourceText.slice(structureDialog.splitIndex).trim()))
              }
            >
              {structureBusy ? <Spinner /> : structureDialog?.kind === 'split' ? <Scissors /> : <Combine />}
              {structureBusy ? '재매칭 중' : structureDialog?.kind === 'split' ? '나누고 재매칭' : '합치고 재매칭'}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

    </main>
  );
}
