'use client';

import { useEffect, useState } from 'react';
import { api, ClauseCoverageAnalysis } from '@/lib/api';
import { Button } from '@/components/ui/button';

const labels = {
  fully_covered: 'GPT: 전체 요구사항 대응',
  partially_covered: 'GPT: 일부만 대응 · 남길 내용 있음',
  posco_specific: 'GPT: 제시 후보에서 대응 근거 못 찾음',
  conflict: 'GPT: 재료·수치·조건 충돌 확인',
  uncertain: 'GPT: 추가 확인 필요',
};

export function CoverageStatusPanel({ projectId, clauseId, refreshKey, onOpenDetail }: {
  projectId: string; clauseId: string; refreshKey: number; onOpenDetail: () => void;
}) {
  const requestKey = `${projectId}:${clauseId}:${refreshKey}`;
  const [result, setResult] = useState<{ key: string; loading: boolean; error?: string; analysis?: ClauseCoverageAnalysis | null }>({ key: '', loading: true });
  const state: typeof result = result.key === requestKey ? result : { key: requestKey, loading: true };
  useEffect(() => {
    let active = true;
    // Read saved analysis only. Navigation must never trigger paid GPT requests.
    api.clause(projectId, clauseId).then(({ clause }) => {
      if (active) setResult({ key: requestKey, loading: false, analysis: clause.coverage_analysis });
    }).catch(() => {
      if (active) setResult({ key: requestKey, loading: false, error: '저장된 GPT 검증 결과를 불러오지 못했습니다.' });
    });
    return () => { active = false; };
  }, [projectId, clauseId, requestKey]);

  return <section className="mb-3 rounded-lg border border-primary/25 bg-primary/5 p-3 text-xs leading-5" aria-label="GPT 전체 요구사항 검증" aria-live="polite">
    <div className="flex items-start justify-between gap-2">
      <div>
        <p className="font-semibold">{state.loading ? 'GPT 검증 상태 확인 중…' : state.error || (state.analysis ? labels[state.analysis.coverage_status] : 'GPT 전체 요구사항 검증 전')}</p>
        <p className="mt-1 text-muted-foreground">검색 점수·의미 대응과 전체 포괄은 다릅니다. 최종 남김·삭제는 담당자가 결정합니다.</p>
      </div>
      <Button size="sm" variant="outline" onClick={onOpenDetail}>검증 상세</Button>
    </div>
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
