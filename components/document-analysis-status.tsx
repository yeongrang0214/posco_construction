'use client';

import { useEffect, useRef, useState } from 'react';
import { api, DetailedAnalysisJob } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';

export function DocumentAnalysisStatus({ projectId, revision, enabled, onUpdated }: {
  projectId: string; revision: string; enabled: boolean; onUpdated: () => void;
}) {
  const [job, setJob] = useState<DetailedAnalysisJob | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const updated = useRef(onUpdated);
  updated.current = onUpdated;
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let previous = '';
    setJob(null);
    setError('');
    async function poll(start = false) {
      try {
        const response = start && enabled
          ? await api.startDetailedAnalysis(projectId)
          : await api.detailedAnalysis(projectId);
        if (!active) return;
        setJob(response.job);
        setError('');
        const key = `${response.job?.completed}:${response.job?.status}`;
        if (key !== previous) { previous = key; updated.current(); }
        if (response.job && ['queued', 'running'].includes(response.job.status)) timer = setTimeout(() => void poll(), 4000);
      } catch (cause) {
        if (!active) return;
        setError(cause instanceof Error ? cause.message : 'GPT 상세비교 상태를 확인하지 못했습니다.');
        // Never repeat a POST automatically after an uncertain response.
        if (!start) timer = setTimeout(() => void poll(), 10000);
      }
    }
    void poll(true);
    return () => { active = false; clearTimeout(timer); };
  }, [projectId, revision, enabled, attempt]);

  const running = job && ['queued', 'running'].includes(job.status);
  async function control() {
    setBusy(true);
    try {
      if (running) await api.pauseDetailedAnalysis(projectId);
      else await api.startDetailedAnalysis(projectId, true);
      setAttempt(value => value + 1);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '분석 상태를 변경하지 못했습니다.'); }
    finally { setBusy(false); }
  }
  return <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm" aria-live="polite">
    {running && <Spinner />}
    <span className="font-medium">GPT 상세비교 {job ? `${job.completed} / ${job.total}` : '상태 확인 중'}</span>
    <span className="text-muted-foreground">{error || job?.error || (job?.status === 'completed' ? '완료 · 담당자 판정은 유지됩니다' : job?.status === 'paused' ? '일시정지 · 진행 중인 요청까지만 처리합니다' : job?.status === 'failed' ? '중단됨' : running ? '서버에서 자동 분석 중 · 창을 닫아도 계속됩니다' : '목차·제목은 제외합니다')}</span>
    {(running || job?.status === 'paused' || job?.status === 'failed') && <Button size="sm" variant="outline" disabled={busy || (!enabled && !running)} onClick={() => void control()}>{running ? '일시정지' : job?.status === 'failed' ? '미완료 분석 재시도' : '계속 분석'}</Button>}
    {error && <Button size="sm" variant="outline" disabled={busy} onClick={() => setAttempt(value => value + 1)}>상태 다시 확인</Button>}
  </div>;
}
