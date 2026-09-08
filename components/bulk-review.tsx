'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Circle,
  Clock3,
  Combine,
  ExternalLink,
  FileText,
  Search,
  Scissors,
  Trash2,
} from 'lucide-react';

import { DocxSourceViewer } from '@/components/docx-source-viewer';

import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import {
  api,
  BulkReviewItem,
  BulkReviewStatus,
  Candidate,
  ClauseDetail,
  DECISION_REASON_OPTIONS,
  Decision,
  DocumentMapItem,
  isDecisionReason,
  Project,
  SourceContextItem,
} from '@/lib/api';
import { cn } from '@/lib/utils';

const PAGE_SIZE = 60;

function needsKcsImpactReview(item: Pick<BulkReviewItem, 'kcs_impact'>) {
  return Boolean(item.kcs_impact?.review_required && !item.kcs_impact.acknowledged_at);
}

function kcsImpactIsProtected(item: Pick<BulkReviewItem, 'kcs_impact'>) {
  const runStatus = item.kcs_impact?.run_status;
  return Boolean(runStatus && runStatus !== 'completed');
}

function kcsImpactStateLabel(item: Pick<BulkReviewItem, 'kcs_impact'>) {
  const runStatus = item.kcs_impact?.run_status;
  if (runStatus === 'failed') return '재매칭 실패';
  if (runStatus === 'superseded') return '더 최신 KCS 확인 필요';
  if (runStatus === 'pending' || runStatus === 'running') return '재매칭 중';
  return '';
}

function kcsImpactAcknowledgement(item: Pick<BulkReviewItem, 'kcs_impact'>) {
  const impact = item.kcs_impact;
  if (!impact?.review_required || impact.acknowledged_at) return {};
  return {
    expected_kcs_revision: impact.target_revision,
    impact_run_id: impact.run_id,
    acknowledge_kcs_impact: true,
  };
}

function scoreTone(score: number) {
  if (score >= 0.45) return 'border-emerald-300 bg-emerald-50/65 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950/25 dark:text-emerald-100';
  if (score >= 0.35) return 'border-amber-300 bg-amber-50/65 text-amber-950 dark:border-amber-800 dark:bg-amber-950/25 dark:text-amber-100';
  return 'border-orange-300 bg-orange-50/65 text-orange-950 dark:border-orange-800 dark:bg-orange-950/25 dark:text-orange-100';
}

function scoreBadgeTone(score: number) {
  if (score >= 0.45) return 'border-emerald-500/45 bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-200';
  if (score >= 0.35) return 'border-amber-500/45 bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200';
  return 'border-orange-500/45 bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-200';
}

function sourceText(item: Pick<SourceContextItem, 'title' | 'content'>) {
  const title = item.title.trim();
  const content = item.content.trim();
  if (!content || content === title) return title;
  return `${title}\n${content}`;
}

function ContextLine({ item, direction }: { item: SourceContextItem; direction: '이전' | '다음' }) {
  return (
    <div className="rounded-md border border-border/70 bg-background/55 px-3 py-2 text-xs text-muted-foreground">
      <p className="mb-1 font-medium text-foreground/70">{direction} 원문 · {item.label}</p>
      <p className="line-clamp-2 whitespace-pre-wrap leading-5">{sourceText(item)}</p>
    </div>
  );
}

function CandidateCard({
  candidate,
  selected,
  expanded,
  disabled,
  onSelect,
}: {
  candidate: Candidate;
  selected: boolean;
  expanded: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  const titleOnly = candidate.content.trim() === candidate.title.trim() || !candidate.content.trim();
  return (
    <button
      type="button"
      className={cn(
        'min-w-0 rounded-lg border p-3 text-left transition-shadow hover:shadow-sm disabled:cursor-not-allowed disabled:opacity-55',
        scoreTone(candidate.score),
        selected && 'ring-2 ring-primary ring-offset-2 ring-offset-card',
      )}
      onClick={onSelect}
      disabled={disabled}
      aria-pressed={selected}
      title={selected
        ? '선택한 KCS 근거를 해제'
        : titleOnly
          ? '제목만 있는 후보는 삭제 근거로 선택할 수 없습니다.'
          : '이 후보를 판정 근거로 선택하고 저장'}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="min-w-0 text-xs font-semibold leading-5">후보 {candidate.rank} · {candidate.kcs_code} · {candidate.kcs_clause || '본문'}</span>
        <Badge variant="outline" className={cn('shrink-0 tabular-nums', scoreBadgeTone(candidate.score))}>{Math.round(candidate.score * 100)}%</Badge>
      </div>
      <Badge variant="outline" className="mt-2 text-[10px]">{titleOnly ? '제목 후보' : '본문 후보'}</Badge>
      <p className="mt-1 line-clamp-2 text-xs opacity-75">
        {candidate.document_name} · {candidate.version || candidate.update_date || '버전 확인 필요'}
      </p>
      <p className="mt-2 text-sm font-medium leading-5">{candidate.title}</p>
      <p className={cn('mt-1 whitespace-pre-wrap text-sm leading-6 opacity-90', !expanded && 'line-clamp-4')}>{candidate.content}</p>
      <span className="mt-2 flex items-center gap-1 text-xs font-medium">
        {selected ? <Check className="size-3.5" /> : <Circle className="size-3.5" />}
        {selected ? '선택 해제' : titleOnly ? '삭제 근거 선택 불가' : '근거로 선택'}
      </span>
    </button>
  );
}

function candidateIsTitleOnly(candidate: Candidate) {
  return !candidate.content.trim() || candidate.content.trim() === candidate.title.trim();
}

function differenceTokens(text: string, counterpart: string, tone: 'source' | 'kcs') {
  const parts = text.split(/(\s+)/g).filter(Boolean);
  const counterpartWords = new Set(counterpart.toLocaleLowerCase('ko-KR').match(/[가-힣A-Za-z0-9.%℃°/-]+/g) || []);
  const sourceNumbers = new Set(text.match(/\d+(?:[.,]\d+)?\s*(?:%|mm|cm|m|kg|MPa|℃|도)?/gi) || []);
  const otherNumbers = new Set(counterpart.match(/\d+(?:[.,]\d+)?\s*(?:%|mm|cm|m|kg|MPa|℃|도)?/gi) || []);
  return parts.map((part, index) => {
    const normalized = part.toLocaleLowerCase('ko-KR').trim();
    const numeric = /\d/.test(normalized) && (!otherNumbers.has(part.trim()) || sourceNumbers.size !== otherNumbers.size);
    const unique = normalized.length >= 2 && !counterpartWords.has(normalized);
    return (
      <span
        key={`${part}-${index}`}
        className={numeric ? 'rounded bg-red-100 px-0.5 text-red-800' : unique ? tone === 'source' ? 'rounded bg-blue-100 px-0.5 text-blue-800' : 'rounded bg-emerald-100 px-0.5 text-emerald-800' : undefined}
      >{part}</span>
    );
  });
}

export function BulkReview({
  projectId,
  refreshKey,
  onSaved,
  onBusyChange,
  onOpenDetail,
  onSplit,
  onMergeNext,
  structureBusy,
  reviewLocked,
  kcsImpactFilterKey,
}: {
  projectId: string;
  refreshKey: number;
  onSaved: (clause: ClauseDetail, project: Project) => void;
  onBusyChange: (busy: boolean) => void;
  onOpenDetail: (clauseId: string) => void;
  onSplit: (clause: BulkReviewItem) => void;
  onMergeNext: (clause: BulkReviewItem) => void;
  structureBusy: boolean;
  reviewLocked: boolean;
  kcsImpactFilterKey: number;
}) {
  'use no memo';
  const [items, setItems] = useState<BulkReviewItem[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<BulkReviewStatus>('all');
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [rowReasons, setRowReasons] = useState<Record<string, string>>({});
  const [savingIds, setSavingIds] = useState<Set<string>>(() => new Set());
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [documentMap, setDocumentMap] = useState<DocumentMapItem[]>([]);
  const [activeClauseId, setActiveClauseId] = useState<string | null>(null);
  const [selectingClause, setSelectingClause] = useState(false);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const requestGeneration = useRef(0);
  const loadingMoreGenerationRef = useRef<number | null>(null);
  const refreshingGenerationRef = useRef<number | null>(null);
  const appliedRefreshKeyRef = useRef(refreshKey);
  const appliedKcsImpactFilterKeyRef = useRef(kcsImpactFilterKey);

  useEffect(() => () => onBusyChange(false), [onBusyChange]);

  useEffect(() => {
    let active = true;
    api.documentMap(projectId)
      .then((result) => {
        if (!active) return;
        setDocumentMap(result.items);
        setActiveClauseId((current) => current && result.items.some((item) => item.id === current)
          ? current
          : result.items.find((item) => item.source_type !== 'heading')?.id || result.items[0]?.id || null);
      })
      .catch((cause: Error) => {
        if (active) setLoadError(cause.message);
      });
    return () => { active = false; };
  }, [projectId, refreshKey]);

  useEffect(() => {
    let active = true;
    const generation = ++requestGeneration.current;
    api.bulkReview(projectId, { offset: 0, limit: PAGE_SIZE, status, q: query })
      .then((result) => {
        if (!active || generation !== requestGeneration.current) return;
        setItems(result.items);
        setTotal(Number(result.total || 0));
        setHasMore(Boolean(result.has_more));
        setLoadError('');
        setActiveClauseId((current) => current && result.items.some((item) => item.id === current)
          ? current
          : result.items.find((item) => item.source_type !== 'heading')?.id || result.items[0]?.id || current);
      })
      .catch((cause: Error) => {
        if (!active || generation !== requestGeneration.current) return;
        setItems([]);
        setTotal(0);
        setHasMore(false);
        setLoadError(cause.message);
      })
      .finally(() => {
        if (active && generation === requestGeneration.current) setLoading(false);
      });
    return () => { active = false; };
  }, [projectId, query, status]);

  useEffect(() => {
    if (appliedRefreshKeyRef.current === refreshKey) return;
    appliedRefreshKeyRef.current = refreshKey;
    let active = true;
    const generation = ++requestGeneration.current;
    const targetCount = Math.max(PAGE_SIZE, items.length);
    const requests = [];
    for (let offset = 0; offset < targetCount; offset += 200) {
      requests.push(api.bulkReview(projectId, {
        offset,
        limit: Math.min(200, targetCount - offset),
        status,
        q: query,
      }));
    }
    refreshingGenerationRef.current = generation;
    setRefreshing(true);
    setLoadError('');
    void Promise.all(requests)
      .then((results) => {
        if (!active || generation !== requestGeneration.current) return;
        const known = new Set<string>();
        const refreshedItems = results.flatMap((result) => result.items).filter((item) => {
          if (known.has(item.id)) return false;
          known.add(item.id);
          return true;
        });
        const nextTotal = Number(results[0]?.total || 0);
        setItems(refreshedItems);
        setTotal(nextTotal);
        setHasMore(refreshedItems.length < nextTotal);
      })
      .catch((cause: Error) => {
        if (!active || generation !== requestGeneration.current) return;
        setLoadError(cause.message);
      })
      .finally(() => {
        if (
          active
          && generation === requestGeneration.current
          && refreshingGenerationRef.current === generation
        ) {
          refreshingGenerationRef.current = null;
          setRefreshing(false);
          setLoading(false);
        }
      });
    return () => { active = false; };
  }, [items.length, projectId, query, refreshKey, status]);

  useEffect(() => {
    if (appliedKcsImpactFilterKeyRef.current === kcsImpactFilterKey) return;
    appliedKcsImpactFilterKeyRef.current = kcsImpactFilterKey;
    requestGeneration.current += 1;
    loadingMoreGenerationRef.current = null;
    refreshingGenerationRef.current = null;
    setLoadingMore(false);
    setRefreshing(false);
    setQuery('');
    setStatus('kcs_impact');
    setItems([]);
    setTotal(0);
    setHasMore(false);
    setLoading(true);
    setLoadError('');
  }, [kcsImpactFilterKey]);

  const loadMore = useCallback(async () => {
    if (
      !hasMore
      || loading
      || structureBusy
      || loadingMoreGenerationRef.current !== null
      || refreshingGenerationRef.current !== null
      || savingIds.size > 0
    ) return;
    const generation = requestGeneration.current;
    loadingMoreGenerationRef.current = generation;
    setLoadingMore(true);
    try {
      const result = await api.bulkReview(projectId, {
        offset: items.length,
        limit: PAGE_SIZE,
        status,
        q: query,
      });
      if (generation !== requestGeneration.current) return;
      setItems((current) => {
        const known = new Set(current.map((item) => item.id));
        return [...current, ...result.items.filter((item) => !known.has(item.id))];
      });
      setTotal(Number(result.total || 0));
      setHasMore(Boolean(result.has_more));
      setLoadError('');
    } catch (cause) {
      if (generation === requestGeneration.current) {
        setLoadError(cause instanceof Error ? cause.message : '다음 조항을 불러오지 못했습니다.');
      }
    } finally {
      if (loadingMoreGenerationRef.current === generation) {
        loadingMoreGenerationRef.current = null;
        setLoadingMore(false);
      }
    }
  }, [hasMore, items.length, loading, projectId, query, savingIds.size, status, structureBusy]);

  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !hasMore || loading || refreshing || structureBusy || savingIds.size > 0) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) void loadMore();
      },
      { rootMargin: '500px 0px' },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadMore, loading, refreshing, savingIds.size, structureBusy]);

  function resetAndSearch(nextQuery: string) {
    requestGeneration.current += 1;
    loadingMoreGenerationRef.current = null;
    refreshingGenerationRef.current = null;
    setLoadingMore(false);
    setRefreshing(false);
    setQuery(nextQuery);
    setItems([]);
    setTotal(0);
    setHasMore(false);
    setLoading(true);
    setLoadError('');
  }

  function resetAndFilter(nextStatus: BulkReviewStatus) {
    if (nextStatus === status) return;
    requestGeneration.current += 1;
    loadingMoreGenerationRef.current = null;
    refreshingGenerationRef.current = null;
    setLoadingMore(false);
    setRefreshing(false);
    setStatus(nextStatus);
    setItems([]);
    setTotal(0);
    setHasMore(false);
    setLoading(true);
    setLoadError('');
  }

  async function saveRow(
    item: BulkReviewItem,
    changes: {
      decision?: Decision;
      decision_reason?: string;
      coverage_confirmed?: boolean;
      selected_candidate_id?: string | null;
      expected_kcs_revision?: string;
      impact_run_id?: string;
      acknowledge_kcs_impact?: boolean;
    },
  ): Promise<boolean> {
    if (structureBusy || savingIds.size > 0 || loadingMore || refreshing) return false;
    onBusyChange(true);
    setSavingIds((current) => new Set(current).add(item.id));
    setRowErrors((current) => {
      const next = { ...current };
      delete next[item.id];
      return next;
    });
    try {
      const result = await api.quickReviewClause(projectId, item.id, changes);
      const remainsInFilter = status === 'all'
        || (status === 'kcs_impact'
          ? needsKcsImpactReview(result.clause)
          : status === 'unreviewed'
            ? result.clause.decision === null
            : result.clause.decision === status);
      const updatedRow: BulkReviewItem = {
        ...item,
        ...result.clause,
        excluded_candidate_count: result.clause.excluded_candidates?.length
          ?? item.excluded_candidate_count,
      };
      setItems((current) => remainsInFilter
        ? current.map((row) => row.id === result.clause.id ? updatedRow : row)
        : current.filter((row) => row.id !== result.clause.id));
      if (!remainsInFilter) setTotal((current) => Math.max(0, current - 1));
      onSaved(result.clause, result.project);
      setDocumentMap((current) => current.map((entry) => entry.id === result.clause.id
        ? { ...entry, decision: result.clause.decision }
        : entry));
      if (Object.prototype.hasOwnProperty.call(changes, 'decision_reason')) {
        setRowReasons((current) => {
          const next = { ...current };
          delete next[item.id];
          return next;
        });
      }
      return true;
    } catch (cause) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: cause instanceof Error ? cause.message : '판정을 저장하지 못했습니다.',
      }));
      return false;
    } finally {
      onBusyChange(false);
      setSavingIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  }

  function toggleExpanded(clauseId: string) {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(clauseId)) next.delete(clauseId);
      else next.add(clauseId);
      return next;
    });
  }

  function chooseReason(item: BulkReviewItem, reason: string) {
    setRowReasons((current) => ({ ...current, [item.id]: reason }));
    setRowErrors((current) => {
      const next = { ...current };
      delete next[item.id];
      return next;
    });
    if (reason === 'no_kcs_match' && item.selected_candidate_id) {
      void saveRow(item, { selected_candidate_id: null });
    }
  }

  function saveDecision(item: BulkReviewItem, decision: Exclude<Decision, null>) {
    const reason = rowReasons[item.id] ?? item.decision_reason ?? '';
    if (!isDecisionReason(decision, reason)) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: `${decision === 'keep' ? '남김' : '보류'} 사유를 먼저 선택해 주세요.`,
      }));
      return;
    }
    void saveRow(item, {
      decision,
      decision_reason: reason,
      coverage_confirmed: false,
      ...(decision === 'keep' && reason === 'no_kcs_match' ? { selected_candidate_id: null } : {}),
      ...kcsImpactAcknowledgement(item),
    });
  }

  function requestDelete(item: BulkReviewItem) {
    const reason = rowReasons[item.id] ?? item.decision_reason ?? '';
    if (!isDecisionReason('delete', reason)) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: '삭제 사유를 먼저 선택해 주세요.',
      }));
      return;
    }
    const kcsBasedDelete = reason === 'fully_covered_by_kcs';
    if (kcsBasedDelete && needsKcsImpactReview(item)) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: 'KCS가 개정된 조항의 삭제 판정은 상세 비교에서 GPT 전체포괄 분석을 다시 실행한 뒤 재확정해 주세요.',
      }));
      return;
    }
    if (kcsBasedDelete && !item.selected_candidate_id) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: '삭제 근거로 사용할 KCS 후보를 먼저 선택해 주세요.',
      }));
      return;
    }
    if (reason === 'management_decision' && !item.review_note.trim()) {
      setRowErrors((current) => ({
        ...current,
        [item.id]: '담당자 판단 삭제는 상세 비교에서 검토의견을 입력한 뒤 저장해 주세요.',
      }));
      return;
    }
    void saveRow(item, {
      decision: 'delete',
      decision_reason: reason,
      coverage_confirmed: kcsBasedDelete,
      ...(kcsBasedDelete ? {} : { selected_candidate_id: null }),
      ...kcsImpactAcknowledgement(item),
    });
  }

  function selectCandidate(item: BulkReviewItem, candidateId: string) {
    const selected = item.selected_candidate_id === candidateId;
    const reason = rowReasons[item.id] ?? item.decision_reason ?? '';
    if (!selected && reason === 'no_kcs_match') {
      setRowErrors((current) => ({
        ...current,
        [item.id]: 'KCS 후보를 선택하려면 판정 사유를 “대응 KCS 없음”이 아닌 사유로 변경해 주세요.',
      }));
      return;
    }
    void saveRow(item, { selected_candidate_id: selected ? null : candidateId });
  }

  const savingAny = savingIds.size > 0;
  const interactionBusy = savingAny || loadingMore || refreshing || structureBusy || selectingClause;
  const activeItem = useMemo(
    () => items.find((item) => item.id === activeClauseId) || items[0] || null,
    [activeClauseId, items],
  );
  const activeMapIndex = useMemo(
    () => documentMap.findIndex((item) => item.id === activeItem?.id),
    [activeItem?.id, documentMap],
  );

  const selectClause = useCallback(async (clauseId: string) => {
    if (clauseId === activeClauseId || selectingClause) return;
    const loaded = items.find((item) => item.id === clauseId);
    if (loaded) {
      setActiveClauseId(clauseId);
      return;
    }
    setSelectingClause(true);
    try {
      const result = await api.clause(projectId, clauseId);
      const mapped: BulkReviewItem = {
        ...result.clause,
        excluded_candidate_count: result.clause.excluded_candidates?.length || 0,
        source_context: { path: '', previous: null, next: null },
      };
      setItems((current) => current.some((item) => item.id === mapped.id) ? current : [...current, mapped]);
      setActiveClauseId(clauseId);
      setLoadError('');
    } catch (cause) {
      setLoadError(cause instanceof Error ? cause.message : '선택한 조항을 불러오지 못했습니다.');
    } finally {
      setSelectingClause(false);
    }
  }, [activeClauseId, items, projectId, selectingClause]);

  function moveActive(direction: -1 | 1) {
    if (activeMapIndex < 0) return;
    let nextIndex = activeMapIndex + direction;
    while (nextIndex >= 0 && nextIndex < documentMap.length) {
      const next = documentMap[nextIndex];
      if (next.source_type !== 'heading') {
        void selectClause(next.id);
        return;
      }
      nextIndex += direction;
    }
  }

  return (
    <section aria-label="시방서 일괄 검토">
      <div className="mb-3 rounded-xl border border-border bg-card/95 p-3 shadow-sm backdrop-blur xl:sticky xl:top-20 xl:z-10">
        <div className="flex flex-wrap items-center gap-2">
          <label className="relative min-w-60 flex-1">
            <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
            <input
              className="h-9 w-full rounded-lg border border-input bg-background py-1 pl-9 pr-3 text-sm outline-none placeholder:text-muted-foreground focus:border-ring focus:ring-3 focus:ring-ring/50"
              value={query}
              onChange={(event) => resetAndSearch(event.target.value)}
              placeholder="번호·제목·본문 검색"
              aria-label="일괄 검토 조항 검색"
              disabled={interactionBusy}
            />
          </label>
          <div className="flex flex-wrap gap-1" aria-label="판정 상태 필터">
            {([
              ['all', '전체'],
              ['kcs_impact', 'KCS 재검토'],
              ['unreviewed', '미검토'],
              ['keep', '남김'],
              ['delete', '삭제'],
              ['hold', '보류'],
            ] as [BulkReviewStatus, string][]).map(([value, label]) => (
              <Button key={value} size="sm" variant={status === value ? 'secondary' : 'ghost'} aria-pressed={status === value} onClick={() => resetAndFilter(value)} disabled={interactionBusy}>{label}</Button>
            ))}
          </div>
          <Badge variant="outline" className="ml-auto tabular-nums">{items.length} / {total}조항</Badge>
          {refreshing && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Spinner />최신 변경 반영 중</span>}
        </div>
        <div className="mt-3 hidden grid-cols-[minmax(0,1.1fr)_minmax(0,1.7fr)_220px] gap-3 border-t border-border pt-2 text-xs font-medium text-muted-foreground xl:grid">
          <span>포스코 원문 · 앞뒤 문맥</span><span>KCS 후보 · 최대 3개</span><span>담당자 판정</span>
        </div>
      </div>

      {loadError && (
        <Alert variant="destructive" className="mb-3">
          <AlertTriangle /><AlertTitle>일괄 검토 목록을 불러오지 못했습니다</AlertTitle><AlertDescription>{loadError}</AlertDescription>
        </Alert>
      )}

      {loading ? (
        <div className="grid min-h-72 place-items-center rounded-xl border border-border bg-card">
          <div className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner />조항 60개를 불러오는 중입니다.</div>
        </div>
      ) : items.length === 0 ? (
        <div className="grid min-h-72 place-items-center rounded-xl border border-dashed border-border bg-card p-8 text-center">
          <div><FileText className="mx-auto mb-3 size-8 text-muted-foreground" /><p className="font-medium">조건에 맞는 조항이 없습니다</p><p className="mt-1 text-sm text-muted-foreground">검색어나 판정 상태를 바꿔보세요.</p></div>
        </div>
      ) : (
        <>
          {activeItem && (() => {
            const item = activeItem;
            const candidates = (item.candidates || []).slice(0, 3);
            const selectedCandidate = candidates.find((candidate) => candidate.id === item.selected_candidate_id) || null;
            const saving = savingIds.has(item.id);
            const impactNeedsReview = needsKcsImpactReview(item);
            const impactProtected = kcsImpactIsProtected(item);
            const rowDecisionBusy = interactionBusy || impactProtected || reviewLocked;
            const reason = rowReasons[item.id] ?? item.decision_reason ?? '';
            const hasPreviousClause = activeMapIndex > 0
              && documentMap.slice(0, activeMapIndex).some((entry) => entry.source_type !== 'heading');
            const hasNextClause = activeMapIndex >= 0
              && documentMap.slice(activeMapIndex + 1).some((entry) => entry.source_type !== 'heading');
            return (
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card px-3 py-2 shadow-xs">
                  <Button size="sm" variant="outline" onClick={() => moveActive(-1)} disabled={rowDecisionBusy || !hasPreviousClause}><ArrowLeft />이전</Button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate text-sm font-semibold">{item.label} · {item.title}</p>
                      {item.source_type === 'heading' && <Badge variant="outline">문맥용 제목</Badge>}
                      {impactNeedsReview && <Badge variant="outline" className="border-amber-500/50 bg-amber-50 text-amber-900">KCS 재검토 필요</Badge>}
                      {saving && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Spinner />저장 중</span>}
                    </div>
                    <p className="text-xs text-muted-foreground">원문 순서 {item.source_order} · DOCX의 노란 표시가 현재 검토 위치입니다.</p>
                  </div>
                  {item.source_type === 'paragraph' && <>
                    {item.content.trim() && <Button size="sm" variant="outline" onClick={() => onSplit(item)} disabled={rowDecisionBusy}><Scissors />나누기</Button>}
                    <Button size="sm" variant="outline" onClick={() => onMergeNext(item)} disabled={rowDecisionBusy}><Combine />다음과 합치기</Button>
                  </>}
                  <Button size="sm" variant="outline" onClick={() => onOpenDetail(item.id)} disabled={interactionBusy}><ExternalLink />상세 비교</Button>
                  <Button size="sm" variant="outline" onClick={() => moveActive(1)} disabled={rowDecisionBusy || !hasNextClause}>다음<ArrowRight /></Button>
                </div>

                {rowErrors[item.id] && <p role="alert" className="flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{rowErrors[item.id]}</p>}

                <div className="grid min-h-[620px] gap-3 xl:h-[calc(100vh-16.5rem)] xl:grid-cols-[minmax(480px,1.15fr)_minmax(420px,1fr)_220px]">
                  <DocxSourceViewer
                    projectId={projectId}
                    clauses={documentMap}
                    activeClauseId={item.id}
                    onSelectClause={(clauseId) => void selectClause(clauseId)}
                  />

                  <section className="min-h-0 overflow-auto rounded-xl border border-border bg-card p-3 shadow-sm" aria-label="KCS 후보 비교">
                    <div className="mb-3 flex items-start justify-between gap-2 border-b border-border pb-2">
                      <div>
                        <p className="text-sm font-semibold">KCS 후보 · 최대 3개</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">본문 후보를 선택하면 아래에 문장 차이가 표시됩니다.</p>
                      </div>
                      <Badge variant="outline">{candidates.length}개</Badge>
                    </div>
                    {item.source_type === 'heading' ? (
                      <div className="rounded-lg border border-dashed border-border bg-muted/40 p-5 text-sm leading-6 text-muted-foreground">
                        목차와 구조 제목은 매칭·판정하지 않고, 뒤에 이어지는 문구의 검색 문맥으로만 사용합니다.
                      </div>
                    ) : candidates.length ? (
                      <>
                        {reason === 'no_kcs_match' && (
                          <p className="mb-3 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-sm leading-6 text-blue-900">
                            대응 KCS 없음으로 판정 중입니다. KCS 후보는 판정 근거로 선택되지 않습니다.
                          </p>
                        )}
                        <div className="grid min-w-0 gap-2 2xl:grid-cols-3">
                          {candidates.map((candidate) => {
                            const selected = item.selected_candidate_id === candidate.id;
                            const titleOnly = candidateIsTitleOnly(candidate);
                            return <CandidateCard
                              key={candidate.id}
                              candidate={candidate}
                              selected={selected}
                              expanded
                              disabled={rowDecisionBusy
                                || item.decision === 'delete'
                                || (reason === 'no_kcs_match' && !selected)
                                || (titleOnly && !selected)}
                              onSelect={() => selectCandidate(item, candidate.id)}
                            />;
                          })}
                        </div>
                      </>
                    ) : (
                      <div className="grid min-h-36 place-items-center rounded-lg border border-dashed border-border bg-muted/25 p-4 text-center text-sm leading-6 text-muted-foreground">
                        {item.excluded_candidate_count > 0
                          ? `관련성 낮음으로 제외된 후보 ${item.excluded_candidate_count}개가 있습니다.`
                          : '25% 이상인 KCS 본문 후보가 없습니다.'}
                      </div>
                    )}

                    <div className="mt-3 rounded-xl border border-border bg-muted/25 p-3">
                      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs">
                        <span className="font-semibold">문장 차이</span>
                        <span><i className="mr-1 inline-block size-2.5 rounded-sm bg-blue-100" />포스코에만 있음</span>
                        <span><i className="mr-1 inline-block size-2.5 rounded-sm bg-emerald-100" />KCS에만 있음</span>
                        <span><i className="mr-1 inline-block size-2.5 rounded-sm bg-red-100" />수치·단위 확인</span>
                      </div>
                      {selectedCandidate ? (
                        <div className="grid gap-2 text-sm leading-6">
                          <div className="rounded-lg border border-blue-200 bg-background p-3">
                            <p className="mb-1 text-xs font-semibold text-blue-800">포스코 원문</p>
                            <p className="whitespace-pre-wrap">{differenceTokens(sourceText(item), `${selectedCandidate.title} ${selectedCandidate.content}`, 'source')}</p>
                          </div>
                          <div className="rounded-lg border border-emerald-200 bg-background p-3">
                            <p className="mb-1 text-xs font-semibold text-emerald-800">선택 KCS</p>
                            <p className="whitespace-pre-wrap">{differenceTokens(`${selectedCandidate.title}\n${selectedCandidate.content}`, sourceText(item), 'kcs')}</p>
                          </div>
                          {!!selectedCandidate.warnings?.length && <p className="flex items-start gap-2 rounded-lg bg-amber-50 p-2 text-xs leading-5 text-amber-900"><AlertTriangle className="mt-0.5 size-3.5 shrink-0" />{selectedCandidate.warnings.join(' · ')}</p>}
                        </div>
                      ) : <p className="py-6 text-center text-sm text-muted-foreground">KCS 본문 후보를 선택하면 차이를 표시합니다.</p>}
                    </div>
                    <div className="sticky bottom-0 z-10 -mx-3 -mb-3 mt-3 flex items-center justify-between gap-3 border-t border-border bg-card/95 px-3 py-3 shadow-[0_-8px_18px_rgba(15,23,42,0.08)] backdrop-blur">
                      <Button size="sm" variant="outline" onClick={() => moveActive(-1)} disabled={rowDecisionBusy || !hasPreviousClause}>
                        <ArrowLeft />이전 문구
                      </Button>
                      <span className="text-xs tabular-nums text-muted-foreground">
                        {activeMapIndex + 1} / {documentMap.length}
                      </span>
                      <Button size="sm" variant="outline" onClick={() => moveActive(1)} disabled={rowDecisionBusy || !hasNextClause}>
                        다음 문구<ArrowRight />
                      </Button>
                    </div>
                  </section>

                  <section className="min-w-0 overflow-auto rounded-xl border border-border bg-card p-3 shadow-sm" aria-label="담당자 판정">
                    <p className="mb-3 border-b border-border pb-2 text-sm font-semibold">담당자 판정</p>
                    {item.source_type === 'heading' ? (
                      <div className="rounded-lg bg-muted/55 p-3 text-sm leading-6 text-muted-foreground"><Badge variant="outline" className="mb-2">판정 제외</Badge><p>뒤 조항의 문맥으로 유지됩니다.</p></div>
                    ) : <>
                      <label className="mb-3 block text-xs font-medium text-muted-foreground">
                        판정 사유
                        <select className="mt-1 h-9 w-full rounded-lg border border-input bg-background px-2 text-sm text-foreground" value={reason} onChange={(event) => chooseReason(item, event.target.value)} disabled={rowDecisionBusy}>
                          <option value="">사유를 선택하세요</option>
                          {(['keep', 'hold', 'delete'] as const).map((decision) => (
                            <optgroup key={decision} label={decision === 'keep' ? '남김' : decision === 'hold' ? '보류' : '삭제'}>
                              {DECISION_REASON_OPTIONS[decision].map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
                            </optgroup>
                          ))}
                        </select>
                      </label>
                      <div className="grid gap-2">
                        <Button variant={item.decision === 'keep' ? 'default' : 'outline'} onClick={() => saveDecision(item, 'keep')} disabled={rowDecisionBusy}><CheckCircle2 />남김</Button>
                        <Button variant={item.decision === 'delete' ? 'destructive' : 'outline'} onClick={() => requestDelete(item)} disabled={rowDecisionBusy}><Trash2 />삭제</Button>
                        <Button variant={item.decision === 'hold' ? 'secondary' : 'outline'} onClick={() => saveDecision(item, 'hold')} disabled={rowDecisionBusy}><Clock3 />보류</Button>
                      </div>
                      <div className="mt-4 space-y-2 text-xs leading-5 text-muted-foreground">
                        <p>KCS 중복 삭제만 본문 후보가 필요하며, KCS 외 삭제는 선택한 사유로 기록됩니다.</p>
                        <p>제목 후보는 위치 탐색용이며 삭제 근거로 사용할 수 없습니다.</p>
                        {reason === 'management_decision' && !item.review_note.trim() && <p className="text-amber-700 dark:text-amber-300">담당자 판단 삭제는 상세 비교에서 검토의견을 입력해야 합니다.</p>}
                        {item.decision && <Badge variant="outline">현재 판정 · {item.decision === 'keep' ? '남김' : item.decision === 'delete' ? '삭제' : '보류'}</Badge>}
                      </div>
                    </>}
                  </section>
                </div>
              </div>
            );
          })()}

          <div className="hidden">
          <div className="space-y-3">
          {items.map((item) => {
            const expanded = expandedIds.has(item.id);
            const saving = savingIds.has(item.id);
            const candidates = (item.candidates || []).slice(0, 3);
            const reason = rowReasons[item.id] ?? item.decision_reason ?? '';
            const impactNeedsReview = needsKcsImpactReview(item);
            const impactProtected = kcsImpactIsProtected(item);
            const impactStateLabel = kcsImpactStateLabel(item);
            const rowDecisionBusy = interactionBusy || impactProtected || reviewLocked;
            return (
              <article key={item.id} className={cn(
                'rounded-xl border bg-card p-3 shadow-xs',
                impactNeedsReview ? 'border-amber-400/70' : 'border-border',
              )}>
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b border-border pb-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-sm font-semibold">{item.label} · {item.title}</p>
                      {impactNeedsReview && <Badge variant="outline" className="border-amber-500/50 bg-amber-50 text-amber-900 dark:bg-amber-950/35 dark:text-amber-100">KCS 재검토 필요</Badge>}
                      {impactStateLabel && <Badge variant={impactStateLabel === '재매칭 실패' ? 'destructive' : 'secondary'}>{impactStateLabel}</Badge>}
                      {item.kcs_impact && !impactNeedsReview && item.kcs_impact.acknowledged_at && <Badge variant="outline">KCS 확인 완료</Badge>}
                      {item.kcs_impact && !item.kcs_impact.review_required && <Badge variant="outline">KCS 정보 변경 · 재검토 불필요</Badge>}
                    </div>
                    <p className="mt-0.5 text-xs text-muted-foreground">원문 순서 {item.source_order}{item.source_type === 'table' ? ' · 표 행' : ''}</p>
                    {item.kcs_impact?.reason && (
                      <p className={cn(
                        'mt-1 text-xs',
                        impactNeedsReview
                          ? 'text-amber-800 dark:text-amber-200'
                          : 'text-muted-foreground',
                      )}>{item.kcs_impact.reason}</p>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {saving && <span className="flex items-center gap-1 text-xs text-muted-foreground"><Spinner />저장 중</span>}
                    <Button size="sm" variant="ghost" onClick={() => toggleExpanded(item.id)} disabled={interactionBusy}>
                      {expanded ? <ChevronUp /> : <ChevronDown />}{expanded ? '접기' : '펼치기'}
                    </Button>
                    {item.source_type === 'paragraph' && (
                      <>
                        {item.content.trim() && <Button size="sm" variant="outline" onClick={() => onSplit(item)} disabled={rowDecisionBusy}><Scissors />나누기</Button>}
                        <Button size="sm" variant="outline" onClick={() => onMergeNext(item)} disabled={rowDecisionBusy}><Combine />다음과 합치기</Button>
                      </>
                    )}
                    <Button size="sm" variant="outline" onClick={() => onOpenDetail(item.id)} disabled={interactionBusy}><ExternalLink />상세 비교</Button>
                  </div>
                </div>

                {rowErrors[item.id] && <p role="alert" className="mb-3 flex items-start gap-2 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{rowErrors[item.id]}</p>}

                <div className="grid gap-3 xl:grid-cols-[minmax(0,1.1fr)_minmax(0,1.7fr)_220px]">
                  <section className="min-w-0 rounded-lg bg-muted/45 p-4">
                    <p className="mb-2 text-xs font-medium text-muted-foreground xl:hidden">포스코 원문 · 앞뒤 문맥</p>
                    {item.source_context.path && (
                      <p className="mb-2 text-xs font-medium leading-5 text-primary/80">{item.source_context.path}</p>
                    )}
                    {item.source_context.previous && <ContextLine item={item.source_context.previous} direction="이전" />}
                    <div className="my-2 rounded-lg border-l-4 border-primary bg-background px-3 py-3 shadow-xs">
                      <p className="mb-1 text-xs font-semibold text-primary">현재 검토 원문 · {item.label}</p>
                      <p className={cn('whitespace-pre-wrap text-sm leading-7', !expanded && 'line-clamp-8')}>{sourceText(item)}</p>
                      {!item.content.trim() && item.source_type !== 'heading' && (
                        <p className="mt-2 text-xs leading-5 text-amber-700 dark:text-amber-300">
                          원본에서 이 한 줄만 독립 번호 항목으로 인식되었습니다. 소제목이라면 앞뒤 문맥을 확인하세요.
                        </p>
                      )}
                    </div>
                    {item.source_context.next && <ContextLine item={item.source_context.next} direction="다음" />}
                  </section>

                  <section className="min-w-0">
                    <p className="mb-2 text-xs font-medium text-muted-foreground xl:hidden">KCS 후보 · 최대 3개</p>
                    {candidates.length ? (
                      <>
                        {reason === 'no_kcs_match' && (
                          <p className="mb-2 rounded-lg border border-blue-200 bg-blue-50 px-3 py-2 text-xs leading-5 text-blue-900">
                            대응 KCS 없음 · 후보 선택 안 함
                          </p>
                        )}
                        <div className={cn(
                          'grid min-w-0 gap-2',
                          candidates.length === 1
                            ? 'xl:grid-cols-1'
                            : candidates.length === 2
                              ? 'xl:grid-cols-2'
                              : 'xl:grid-cols-3',
                        )}>
                          {candidates.map((candidate) => {
                            const selected = item.selected_candidate_id === candidate.id;
                            return <CandidateCard
                              key={candidate.id}
                              candidate={candidate}
                              selected={selected}
                              expanded={expanded}
                              disabled={rowDecisionBusy
                                || item.decision === 'delete'
                                || (reason === 'no_kcs_match' && !selected)
                                || (candidateIsTitleOnly(candidate) && !selected)}
                              onSelect={() => selectCandidate(item, candidate.id)}
                            />;
                          })}
                        </div>
                      </>
                    ) : (
                      <div className="grid min-h-32 place-items-center rounded-lg border border-dashed border-border bg-muted/25 p-4 text-center text-sm leading-6 text-muted-foreground">
                        {item.source_type === 'heading'
                          ? '문서 구조 제목이라 KCS 매칭 대상에서 제외했습니다.'
                          : item.excluded_candidate_count > 0
                            ? `관련성 낮음으로 제외된 후보 ${item.excluded_candidate_count}개가 있습니다. 상세 비교에서 제외 근거를 확인할 수 있습니다.`
                            : '25% 이상인 KCS 후보가 없습니다.'}
                      </div>
                    )}
                  </section>

                  <section className="min-w-0 rounded-lg border border-border p-3">
                    <p className="mb-2 text-xs font-medium text-muted-foreground xl:hidden">담당자 판정</p>
                    {item.source_type === 'heading' ? (
                      <div className="rounded-lg bg-muted/55 p-3 text-sm leading-6 text-muted-foreground">
                        <Badge variant="outline" className="mb-2">문맥용 제목</Badge>
                        <p>매칭과 판정에서 제외되며, 뒤 조항의 검색 문맥으로만 사용됩니다.</p>
                      </div>
                    ) : (
                      <>
                        <label className="mb-3 block text-xs font-medium text-muted-foreground">
                          판정 사유
                          <select
                            className="mt-1 h-9 w-full rounded-lg border border-input bg-background px-2 text-sm text-foreground"
                            value={rowReasons[item.id] ?? item.decision_reason ?? ''}
                            onChange={(event) => chooseReason(item, event.target.value)}
                            disabled={rowDecisionBusy}
                          >
                            <option value="">사유를 선택하세요</option>
                            {(['keep', 'hold', 'delete'] as const).map((decision) => (
                              <optgroup key={decision} label={decision === 'keep' ? '남김' : decision === 'hold' ? '보류' : '삭제'}>
                                {DECISION_REASON_OPTIONS[decision].map((option) => (
                                  <option key={option.value} value={option.value}>{option.label}</option>
                                ))}
                              </optgroup>
                            ))}
                          </select>
                        </label>
                        <div className="grid gap-2">
                          <Button variant={item.decision === 'keep' ? 'default' : 'outline'} onClick={() => saveDecision(item, 'keep')} disabled={rowDecisionBusy}><CheckCircle2 />{impactNeedsReview && item.decision === 'keep' ? '남김 유지·확인' : '남김'}</Button>
                          <Button variant={item.decision === 'delete' ? 'destructive' : 'outline'} onClick={() => requestDelete(item)} disabled={rowDecisionBusy}><Trash2 />삭제</Button>
                          <Button variant={item.decision === 'hold' ? 'secondary' : 'outline'} onClick={() => saveDecision(item, 'hold')} disabled={rowDecisionBusy}><Clock3 />{impactNeedsReview && item.decision === 'hold' ? '보류 유지·확인' : '보류'}</Button>
                        </div>
                        <p className="mt-3 text-xs leading-5 text-muted-foreground">
                          KCS 중복 삭제만 본문 후보가 필요하며, KCS 외 삭제는 선택한 사유로 기록됩니다.
                          {item.decision === 'delete' && ' 후보를 바꾸려면 먼저 남김 또는 보류로 전환하세요.'}
                        </p>
                      </>
                    )}
                  </section>
                </div>
              </article>
            );
          })}
          </div>
          </div>
        </>
      )}

      <div ref={sentinelRef} className="grid min-h-20 place-items-center" aria-live="polite">
        {loadingMore ? <span className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner />다음 60개 조항을 불러오는 중입니다.</span> : !loading && !hasMore && items.length > 0 ? <span className="text-sm text-muted-foreground">전체 {total}개 조항을 모두 표시했습니다.</span> : null}
      </div>

    </section>
  );
}
