'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { AlertTriangle, Check, Circle, Table2 } from 'lucide-react';
import { api, type TableComparison } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

export function TableComparisonPanel({ projectId, clauseId, selectedId, disabled, enabled, onSelect, fallback }: {
  projectId: string; clauseId: string; selectedId: string | null; disabled: boolean;
  enabled: boolean; onSelect: (candidateId: string) => void; fallback: ReactNode;
}) {
  const [result, setResult] = useState<{ key: string; data?: TableComparison; error?: string } | null>(null);
  const key = `${projectId}/${clauseId}`;
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api.tableComparison(projectId, clauseId).then(
      (data) => { if (!cancelled) setResult({ key, data }); },
      () => { if (!cancelled) setResult({ key, error: '표의 기준 원문을 조회하지 못했습니다. 아래 검색 후보 또는 원문으로 검토하세요.' }); },
    );
    return () => { cancelled = true; };
  }, [projectId, clauseId, key, enabled]);

  const current = result?.key === key ? result : null;
  if (!enabled) return <>{fallback}</>;
  if (!current) return <><output className="mb-3 block rounded-lg bg-muted p-3 text-sm">표의 KS 번호·강종과 KCS·KDS 근거를 확인하고 있습니다.</output>{fallback}</>;
  if (current.error) return <><p role="alert" className="mb-3 text-sm text-amber-800">{current.error}</p>{fallback}</>;
  const data = current.data;
  if (!data?.applicable || !data.source) return <>{fallback}</>;

  return <div className="space-y-3" aria-label="표 행 기준 비교">
    <div className="rounded-lg border border-blue-200 bg-blue-50/60 p-3 text-sm">
      <p className="flex items-center gap-2 font-semibold text-blue-950"><Table2 className="size-4 shrink-0" />표 제목과 현재 행 연결</p>
      <p className="mt-2 break-words leading-6">{data.source.caption}</p>
      <div className="mt-2 flex flex-wrap gap-1">{data.source.standards.map((code) => <Badge key={code} variant="outline">{code}</Badge>)}{data.source.grades.map((grade) => <Badge key={grade} variant="secondary">{grade}</Badge>)}</div>
      <p className="mt-2 whitespace-pre-wrap text-xs leading-5 text-muted-foreground">선택 행 · {data.source.row}</p>
    </div>
    <div className="flex items-center justify-between text-sm font-semibold"><span>KCS 근거 · KDS 설계 참고</span><Badge variant="outline">최대 3개</Badge></div>
    {!data.evidence?.length && <p className="rounded-lg border border-dashed p-3 text-sm leading-6">조회 범위에서 같은 KS 번호·강종의 표를 확인하지 못했습니다. 기준에 없다고 확정한 것은 아닙니다.</p>}
    {data.evidence?.map((evidence) => {
      const selected = evidence.kind === 'KCS' && !!evidence.candidate_id && evidence.candidate_id === selectedId;
      return <article key={`${evidence.code}/${evidence.table}`} className={cn('min-w-0 rounded-lg border p-3', evidence.kind === 'KDS' ? 'border-blue-200 bg-blue-50/35' : 'border-emerald-200 bg-emerald-50/35', selected && 'ring-2 ring-primary')}>
        <div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{evidence.kind === 'KCS' ? '시공 기준' : '설계 참고'}</Badge><span className="text-xs font-semibold">{evidence.code}</span></div>
        <p className="mt-2 text-sm font-semibold leading-6">{evidence.document_name}</p>
        <p className="text-xs leading-5 text-muted-foreground">원문 버전 {evidence.version || '미표기'} · 개정 정보 {evidence.update_date?.slice(0, 10) || '미표기'}</p>
        <p className="mt-2 text-sm font-medium">{evidence.section} · {evidence.table}</p>
        <p className="mt-1 text-xs font-medium text-emerald-800">목록에서 확인 · {evidence.matched_grades.join(', ')}</p>
        <details className="mt-2 text-sm" open>
          <summary className="cursor-pointer text-xs text-muted-foreground">해당 표의 근거 행</summary>
          <p className="mt-2 whitespace-pre-wrap break-words rounded-md border bg-background p-2 leading-6">{evidence.excerpt}</p>
        </details>
        {evidence.kind === 'KDS' ? <p className="mt-2 text-xs leading-5 text-blue-900">설계 참고용 · KCS 중복 삭제 근거로 선택되지 않습니다.</p> : evidence.candidate_id ?
          <Button type="button" variant="ghost" size="sm" className="mt-2" disabled={disabled} aria-pressed={selected} onClick={() => onSelect(evidence.candidate_id!)}>{selected ? <Check /> : <Circle />}{selected ? '선택 해제' : 'KCS 근거로 선택'}</Button>
          : <p className="mt-2 text-xs leading-5 text-muted-foreground">현재 원문에서 찾은 참고 행입니다. 저장된 후보와 다르므로 KCS 재매칭 후 근거로 선택할 수 있습니다.</p>}
      </article>;
    })}
    <div className="overflow-hidden rounded-lg border">
      <p className="border-b bg-muted/45 px-3 py-2 text-sm font-semibold">항목별 확인 결과</p>
      <table className="w-full table-fixed text-left text-xs leading-5">
        <thead className="bg-muted/25"><tr><th className="w-1/4 p-2 font-medium">항목</th><th className="w-1/3 p-2 font-medium">포스코 원문</th><th className="p-2 font-medium">확인 결과</th></tr></thead>
        <tbody>{data.checks?.map((check) => <tr key={check.field} className="border-t"><th className="p-2 align-top font-medium">{check.field}</th><td className="break-words p-2 align-top">{check.source}</td><td className={cn('p-2 align-top', check.status === 'found' ? 'text-emerald-800' : 'text-amber-900')}>{check.status === 'found' ? '✓ ' : '△ '}{check.result}</td></tr>)}</tbody>
      </table>
    </div>
    <p className="flex gap-2 rounded-lg bg-amber-50 p-3 text-xs leading-5 text-amber-950"><AlertTriangle className="mt-0.5 size-4 shrink-0" />{data.notice}</p>
    {data.warnings?.map((warning) => <output key={warning} className="block text-xs leading-5 text-amber-900">{warning}</output>)}
  </div>;
}
