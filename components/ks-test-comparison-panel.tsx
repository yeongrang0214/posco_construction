'use client';

import { useEffect, useRef, useState } from 'react';
import { api, KsTestComparison } from '@/lib/api';
import { Button } from '@/components/ui/button';

const labels = { corresponds: 'KS 대응', differs: '조건 차이 · 검토', unconfirmed: '확인 필요' };

export function KsTestComparisonPanel({ projectId, clauseId, initial }: {
  projectId: string; clauseId: string; initial: KsTestComparison;
}) {
  const [result, setResult] = useState(initial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setResult(initial); }, [initial]);

  async function compare() {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const { comparison } = await api.compareKsTest(projectId, clauseId);
      if (mounted.current) setResult(comparison);
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : 'KS 비교를 완료하지 못했습니다. 다시 시도해 주세요.');
    } finally { if (mounted.current) setBusy(false); }
  }

  if (!result.applicable) return null;
  return <section className="mt-3 rounded-md border border-primary/25 bg-background p-3" aria-label="KS 시험규격 비교">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <p className="font-semibold">KS 시험규격 비교</p>
      <Button variant="outline" size="sm" disabled={busy} onClick={compare}>
        {busy ? '공식 KS · GPT 비교 중…' : result.status === 'completed' ? '최신 원문 다시 확인' : 'KS 원문 비교'}
      </Button>
    </div>
    <p className="mt-1 text-muted-foreground">{result.notice}</p>
    {result.status === 'pending' && <p className="mt-1 text-muted-foreground">원문을 확인할 수 있는 규격만 GPT로 비교합니다. 이미 검증한 같은 원문은 재사용합니다.</p>}
    {error && <p role="alert" className="mt-2 text-destructive">{error}</p>}
    {result.stale && <p className="mt-2 text-amber-700">이전에 확인한 판의 결과입니다. 최신 원문 다시 확인을 눌러 현행 여부를 확인하세요.</p>}
    <div className="mt-2 space-y-2">
      {result.standards?.map((s) => <div key={s.standard} className="rounded border p-2">
        <a href={s.source_url} target="_blank" rel="noreferrer" className="font-medium underline underline-offset-2">{s.standard} · {s.name || '공식 표준 정보'} ↗</a>
        <p>{s.status === 'machine_verified' ? '기계가독 원문 확인' : '자동 원문 미확인'}{s.edition_date && ` · 최종 개정/확인 ${s.edition_date}`}</p>
        <p className="text-muted-foreground">{s.message}</p>
      </div>)}
      {result.fields?.map((field) => <div key={field.id} className="border-l-2 border-border pl-2">
        <p className="font-medium">{field.label}{field.status && ` · ${labels[field.status]}`}</p>
        <p className="whitespace-pre-wrap">원문: {field.source}</p>
        {field.explanation && <p className={field.status === 'differs' ? 'text-amber-700' : 'text-muted-foreground'}>{field.explanation}</p>}
        {!!field.evidence?.length && <details><summary className="cursor-pointer text-muted-foreground">KS 근거 위치</summary>
          {field.evidence.map((e, i) => <p key={i} className="mt-1">{e.standard} {e.section}절: {e.quote}</p>)}
        </details>}
      </div>)}
    </div>
    {result.suggested_text && <details className="mt-3 rounded border p-2">
      <summary className="cursor-pointer font-medium">코드 참조형 문구 제안 · 미적용</summary>
      <p className="mt-2 whitespace-pre-wrap">{result.suggested_text}</p>
      <p className="mt-2 text-amber-700">{result.warning}</p>
    </details>}
    {result.checked_at && <p className="mt-2 text-xs text-muted-foreground">확인 {new Date(result.checked_at).toLocaleString('ko-KR')}{result.model && ` · ${result.model}`}</p>}
  </section>;
}
