'use client';

import { useEffect, useState } from 'react';
import { BookOpen, RefreshCw } from 'lucide-react';
import { api, type RelatedReferences } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';

export function RelatedReferencesPanel({ projectId, clauseId, refreshKey = 0 }: {
  projectId: string; clauseId: string; refreshKey?: number;
}) {
  const [retry, setRetry] = useState(0);
  const key = `${projectId}:${clauseId}:${refreshKey}:${retry}`;
  const [result, setResult] = useState<{ key: string; data?: RelatedReferences; error?: boolean } | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    // Read only: no paid AI call, rematching, or changes to saved decisions.
    api.relatedReferences(projectId, clauseId, controller.signal).then(
      (data) => { if (!controller.signal.aborted) setResult({ key, data }); },
      () => { if (!controller.signal.aborted) setResult({ key, error: true }); },
    );
    return () => controller.abort();
  }, [projectId, clauseId, key]);
  const current = result?.key === key ? result : null;
  if (!current) return <p role="status" className="my-3 text-sm text-muted-foreground">참고용 관련 조항 확인 중…</p>;
  if (current.error) return <div role="alert" className="my-3 rounded-lg border p-3 text-sm">
    <p>참고용 관련 조항을 불러오지 못했습니다. 기준이 없다는 뜻은 아닙니다.</p>
    <Button variant="outline" size="sm" className="mt-2" onClick={() => setRetry((value) => value + 1)}><RefreshCw />다시 조회</Button>
  </div>;
  const data = current.data;
  if (!data?.applicable) return null;
  return <section className="my-3 space-y-3 rounded-xl border border-blue-200 bg-blue-50/40 p-3 dark:border-blue-900 dark:bg-blue-950/20" aria-label="참고용 관련 조항">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="flex items-center gap-2 text-sm font-semibold"><BookOpen className="size-4" />참고용 관련 조항</h3>
      <Badge variant="outline">삭제 근거 아님 · {data.references.length}개</Badge>
    </div>
    <p className="text-sm leading-6 text-muted-foreground">{data.notice}</p>
    {data.references.length ? data.references.map((reference, index) => <article key={reference.id} className="min-w-0 rounded-lg border border-blue-200 bg-background p-3 dark:border-blue-900">
      <p className="text-sm font-semibold">참고 {index + 1} · {reference.kcs_code} · {reference.kcs_clause}</p>
      <p className="mt-1 text-sm text-muted-foreground">{reference.document_name} · {reference.version} · 개정 {reference.update_date.slice(0, 10) || '미표기'}</p>
      <p className="mt-2 text-sm font-semibold">{reference.title}</p>
      <p className="mt-2 whitespace-pre-wrap break-words text-base leading-7">{reference.content}</p>
      <div className="mt-3 space-y-1 border-t pt-2 text-sm leading-6">
        <p><strong>차이·미확인 사항</strong> · {reference.comparison_note}</p>
        <p className="text-muted-foreground">{reference.scope_note}</p>
      </div>
    </article>) : <p className="text-sm leading-6">이 조회 범위에서 추가 참고 조항을 찾지 못했습니다. KCS 전체에 해당 요구가 없다는 뜻은 아닙니다.</p>}
    <p className="text-sm text-muted-foreground">조회 범위 · {data.search_scope} · 서버 보유 원문 조회</p>
  </section>;
}
