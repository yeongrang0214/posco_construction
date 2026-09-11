'use client';

import { useEffect, useState } from 'react';
import { api, ClauseCoverageAnalysis, ClauseDetail } from '@/lib/api';
import { Button } from '@/components/ui/button';

const labels = {
  fully_covered: 'GPT: 전체 요구사항 대응',
  partially_covered: 'GPT: 일부 요구사항 대응',
  posco_specific: 'GPT: 제시 후보에서 대응 근거 못 찾음',
  conflict: 'GPT: 재료·수치·조건 충돌 확인',
  uncertain: 'GPT: 추가 확인 필요',
};

export function CoverageStatusPanel({ projectId, clauseId, refreshKey, onOpenDetail }: {
  projectId: string; clauseId: string; refreshKey: number | string; onOpenDetail: () => void;
}) {
  const requestKey = `${projectId}:${clauseId}:${refreshKey}`;
  const [result, setResult] = useState<{ key: string; loading: boolean; error?: string; analysis?: ClauseCoverageAnalysis | null; standardLinks?: ClauseDetail['standard_links'] }>({ key: '', loading: true });
  const state: typeof result = result.key === requestKey ? result : { key: requestKey, loading: true };
  useEffect(() => {
    let active = true;
    // Paid work belongs to the durable document queue; this panel only reads saved results.
    api.clause(projectId, clauseId).then(({ clause }) => {
      if (active) setResult({ key: requestKey, loading: false, analysis: clause.coverage_analysis, standardLinks: clause.standard_links });
    }).catch(() => {
      if (active) setResult({ key: requestKey, loading: false, error: '저장된 GPT 검증 결과를 불러오지 못했습니다.' });
    });
    return () => { active = false; };
  }, [projectId, clauseId, requestKey]);

  return <section className="mb-3 rounded-lg border border-primary/25 bg-primary/5 p-3 text-sm leading-6" aria-label="GPT 전체 요구사항 검증" aria-live="polite">
    <div className="flex items-start justify-between gap-2">
      <div>
        <p className="font-semibold">{state.loading ? 'GPT 검증 상태 확인 중…' : state.error || (state.analysis ? labels[state.analysis.coverage_status] : 'GPT 전체 요구사항 검증 전')}</p>
        <p className="mt-1 text-muted-foreground">검색 점수·의미 대응과 전체 포괄은 다릅니다. 최종 남김·삭제는 담당자가 결정합니다.</p>
      </div>
      <Button size="sm" variant="outline" onClick={onOpenDetail}>검증 상세</Button>
    </div>
    {!state.loading && !!state.standardLinks?.length && <div className="mt-2 rounded-md border border-primary/20 bg-background p-3" aria-label="KS 적용 범위 연계">
      <p className="font-semibold">KS 적용 범위 연계 · 같은 용도의 품질 요구</p>
      <p className="text-muted-foreground">KCS에 번호가 직접 없어도 규격의 적용 범위로 비교합니다. 전체 대체 여부는 별도 확인이 필요합니다.</p>
      <details className="mt-1">
        <summary className="cursor-pointer">공식 표준 정보와 대응 본문 보기</summary>
        {state.standardLinks.map((link) => <div key={`${link.candidate_id}:${link.standard}`} className="mt-2 space-y-1 border-t pt-2">
          <p><a className="underline underline-offset-2" href={link.source_url} target="_blank" rel="noreferrer">{link.standard} · {link.name} ↗</a></p>
          <p>{link.scope}</p>
          <p className="text-muted-foreground">{link.status} · 정보 확인 {link.verified_at}</p>
          <p>{link.kcs_code} · {link.kcs_title} {link.kcs_clause}</p>
          <p>{link.kcs_requirement}</p>
          <p className="text-muted-foreground">{link.verification_level}</p>
        </div>)}
      </details>
    </div>}
    {!state.loading && state.analysis && <details className="mt-2">
      <summary className="cursor-pointer font-medium">검증 근거 · 원문 {state.analysis.requirements.length}구간</summary>
      <p className="mt-2 whitespace-pre-wrap">{state.analysis.rationale}</p>
      <ul className="mt-2 space-y-2">
        {state.analysis.requirements.map((requirement, index) => <li key={index} className="border-l-2 border-border pl-2">
          <p>{requirement.status === 'covered' ? '대응' : requirement.status === 'conflict' ? '충돌' : requirement.status === 'uncertain' ? '확인 필요' : '미포괄'} · {requirement.requirement}</p>
          <p className="text-muted-foreground">{requirement.evidence}</p>
        </li>)}
      </ul>
      <p className="mt-2 text-muted-foreground">{state.analysis.model} · {state.analysis.analyzed_at}</p>
    </details>}
  </section>;
}
