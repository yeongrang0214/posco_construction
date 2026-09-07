'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { Crosshair, FileText, Minus, Plus } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { api, DocumentMapItem } from '@/lib/api';

const MARKER_CLASSES = [
  'spec-source-marker',
  'spec-source-active',
  'spec-source-keep',
  'spec-source-delete',
  'spec-source-hold',
];

function normalized(value: string) {
  return value
    .normalize('NFKC')
    .toLocaleLowerCase('ko-KR')
    .replace(/[\s\u00a0]+/g, '')
    .replace(/[·ㆍ:：;；,，.。()[\]{}「」『』〈〉《》<>\-–—]/g, '');
}

function matchScore(target: string, rendered: string) {
  if (!target || !rendered) return 0;
  if (target === rendered) return 1;
  if (rendered.includes(target)) return 0.82 + Math.min(0.16, target.length / rendered.length / 6);
  if (target.includes(rendered) && rendered.length >= 8) return 0.68 + Math.min(0.12, rendered.length / target.length / 6);
  const targetChunks = new Set(target.match(/[가-힣A-Za-z0-9]{2,}/g) || []);
  const renderedChunks = new Set(rendered.match(/[가-힣A-Za-z0-9]{2,}/g) || []);
  if (!targetChunks.size || !renderedChunks.size) return 0;
  const common = [...targetChunks].filter((chunk) => renderedChunks.has(chunk)).length;
  return common / Math.max(targetChunks.size, renderedChunks.size);
}

function clauseSearchText(clause: DocumentMapItem) {
  const content = clause.content.trim();
  const title = clause.title.trim();
  if (!content) return normalized(title);
  const normalizedContent = normalized(content);
  const normalizedTitle = normalized(title.replace(/…$/, ''));
  return normalizedTitle && normalizedContent.startsWith(normalizedTitle)
    ? normalizedContent
    : normalized(`${title} ${content}`);
}

type RenderedCandidate = {
  element: HTMLElement;
  text: string;
};

function findBestElements(
  target: string,
  candidates: RenderedCandidate[],
  sourceType: DocumentMapItem['source_type'],
) {
  let best: { elements: HTMLElement[]; score: number; lengthDelta: number } | null = null;
  const isBetter = (score: number, lengthDelta: number, elementCount: number) => (
    !best
    || score > best.score
    || (score === best.score && lengthDelta < best.lengthDelta)
    || (score === best.score && lengthDelta === best.lengthDelta && elementCount < best.elements.length)
  );

  for (let start = 0; start < candidates.length; start += 1) {
    const first = candidates[start];
    const isTableRow = first.element.tagName === 'TR';
    if (first.element.dataset.clauseId) continue;
    if ((sourceType === 'table') !== isTableRow) continue;

    if (isTableRow) {
      const score = matchScore(target, first.text);
      const lengthDelta = Math.abs(target.length - first.text.length);
      if (isBetter(score, lengthDelta, 1)) {
        best = { elements: [first.element], score, lengthDelta };
      }
      continue;
    }

    let rendered = '';
    const elements: HTMLElement[] = [];
    for (let index = start; index < candidates.length && elements.length < 24; index += 1) {
      const candidate = candidates[index];
      if (candidate.element.tagName === 'TR' || candidate.element.dataset.clauseId) break;
      rendered += candidate.text;
      elements.push(candidate.element);
      const score = matchScore(target, rendered);
      const lengthDelta = Math.abs(target.length - rendered.length);
      if (isBetter(score, lengthDelta, elements.length)) {
        best = { elements: [...elements], score, lengthDelta };
      }
      if (rendered.length > target.length * 1.6) break;
    }
  }
  return best;
}

export function DocxSourceViewer({
  projectId,
  clauses,
  activeClauseId,
  onSelectClause,
}: {
  projectId: string;
  clauses: DocumentMapItem[];
  activeClauseId: string | null;
  onSelectClause: (clauseId: string) => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const scaleRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const styleRef = useRef<HTMLDivElement>(null);
  const elementMapRef = useRef<Map<string, HTMLElement[]>>(new Map());
  const [zoom, setZoom] = useState(0.82);
  const [renderSize, setRenderSize] = useState({ width: 0, height: 0 });
  const [pageCount, setPageCount] = useState(0);
  const [activePage, setActivePage] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    async function renderSource() {
      setLoading(true);
      setError('');
      setRenderSize({ width: 0, height: 0 });
      try {
        const response = await fetch(api.projectSourceDocxUrl(projectId));
        if (!response.ok) {
          let message = `원문을 불러오지 못했습니다. (${response.status})`;
          try {
            const payload = await response.json() as { detail?: string };
            if (payload.detail) message = payload.detail;
          } catch {
            // Keep the status-based message for non-JSON responses.
          }
          throw new Error(message);
        }
        const blob = await response.blob();
        const { renderAsync } = await import('docx-preview');
        if (cancelled || !bodyRef.current) return;
        bodyRef.current.replaceChildren();
        if (styleRef.current) styleRef.current.replaceChildren();
        await renderAsync(blob, bodyRef.current, styleRef.current || undefined, {
          breakPages: true,
          inWrapper: true,
          ignoreWidth: false,
          ignoreHeight: false,
          renderHeaders: true,
          renderFooters: true,
          renderFootnotes: true,
          experimental: true,
        });
        if (cancelled || !bodyRef.current) return;
        setPageCount(bodyRef.current.querySelectorAll('section.docx').length || 1);
      } catch (cause) {
        if (!cancelled) setError(cause instanceof Error ? cause.message : 'DOCX 원문을 표시하지 못했습니다.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void renderSource();
    return () => { cancelled = true; };
  }, [projectId]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || loading || error) return;

    let frame = 0;
    const measure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const wrapper = body.querySelector<HTMLElement>('.docx-wrapper');
        const pages = Array.from(body.querySelectorAll<HTMLElement>('section.docx'));
        const width = Math.ceil(Math.max(0, ...pages.map((page) => page.offsetWidth), wrapper?.scrollWidth || 0));
        const height = Math.ceil(wrapper?.scrollHeight || body.scrollHeight || 0);
        if (width && height) {
          setRenderSize((current) => current.width === width && current.height === height
            ? current
            : { width, height });
        }
      });
    };

    measure();
    const observed = body.querySelector<HTMLElement>('.docx-wrapper') || body;
    const observer = new ResizeObserver(measure);
    observer.observe(observed);
    const images = Array.from(body.querySelectorAll('img'));
    images.forEach((image) => image.addEventListener('load', measure));

    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      images.forEach((image) => image.removeEventListener('load', measure));
    };
  }, [error, loading, projectId]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body || loading || error) return;
    const candidates = Array.from(body.querySelectorAll<HTMLElement>('p, tr'))
      .filter((element) => !(element.tagName === 'P' && element.closest('tr')))
      .map((element) => ({ element, text: normalized(element.innerText || element.textContent || '') }))
      .filter(({ text }) => text.length >= 2);
    const nextMap = new Map<string, HTMLElement[]>();
    for (const { element } of candidates) {
      element.classList.remove(...MARKER_CLASSES);
      delete element.dataset.clauseId;
    }

    for (const clause of clauses) {
      if (clause.source_type === 'heading') continue;
      const target = clauseSearchText(clause);
      if (target.length < 2) continue;
      const best = findBestElements(target, candidates, clause.source_type);
      if (!best || best.score < 0.56) continue;
      for (const element of best.elements) {
        element.dataset.clauseId = clause.id;
        element.classList.add('spec-source-marker');
        if (clause.decision) element.classList.add(`spec-source-${clause.decision}`);
        if (clause.id === activeClauseId) element.classList.add('spec-source-active');
      }
      nextMap.set(clause.id, best.elements);
    }
    elementMapRef.current = nextMap;
  }, [activeClauseId, clauses, error, loading]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    body.querySelectorAll('.spec-source-active').forEach((element) => element.classList.remove('spec-source-active'));
    if (!activeClauseId) return;
    const elements = elementMapRef.current.get(activeClauseId) || [];
    elements.forEach((element) => element.classList.add('spec-source-active'));
    const page = elements[0]?.closest('section.docx');
    const pages = Array.from(body.querySelectorAll('section.docx'));
    const pageIndex = page ? pages.indexOf(page) : -1;
    setActivePage(pageIndex >= 0 ? pageIndex + 1 : null);
    elements[0]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [activeClauseId, clauses, error, loading]);

  useEffect(() => {
    const body = bodyRef.current;
    if (!body) return;
    const handleClick = (event: MouseEvent) => {
      const target = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-clause-id]');
      if (target?.dataset.clauseId) onSelectClause(target.dataset.clauseId);
    };
    body.addEventListener('click', handleClick);
    return () => body.removeEventListener('click', handleClick);
  }, [onSelectClause]);

  const activeIndex = useMemo(
    () => clauses.findIndex((clause) => clause.id === activeClauseId),
    [activeClauseId, clauses],
  );
  return (
    <section className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-sm" aria-label="포스코 DOCX 원문">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <div className="mr-auto flex min-w-0 items-center gap-2">
          <FileText className="size-4 shrink-0 text-primary" />
          <div className="min-w-0">
            <p className="text-sm font-semibold">포스코 DOCX 원문</p>
            <p className="text-[11px] text-muted-foreground">원문을 클릭하면 해당 KCS 비교로 이동합니다.</p>
          </div>
        </div>
        <Badge variant="outline" className="tabular-nums">{activePage || '-'} / {pageCount || '-'}쪽</Badge>
        <Button size="icon-sm" variant="outline" aria-label="원문 축소" onClick={() => setZoom((value) => Math.max(0.55, value - 0.1))}><Minus /></Button>
        <span className="w-11 text-center text-xs tabular-nums">{Math.round(zoom * 100)}%</span>
        <Button size="icon-sm" variant="outline" aria-label="원문 확대" onClick={() => setZoom((value) => Math.min(1.3, value + 0.1))}><Plus /></Button>
        <Button size="sm" variant="outline" onClick={() => {
          if (!activeClauseId) return;
          elementMapRef.current.get(activeClauseId)?.[0]?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }}><Crosshair />현재 위치</Button>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 border-b border-border bg-muted/35 px-3 py-2 text-[11px] text-muted-foreground">
        <span><i className="mr-1 inline-block size-2.5 rounded-sm bg-yellow-300" />현재 조항</span>
        <span><i className="mr-1 inline-block h-2.5 w-1 rounded-sm bg-emerald-500" />남김</span>
        <span><i className="mr-1 inline-block h-2.5 w-1 rounded-sm bg-red-500" />삭제</span>
        <span><i className="mr-1 inline-block h-2.5 w-1 rounded-sm bg-amber-500" />보류</span>
        {activeIndex >= 0 && <span className="ml-auto">원문 순서 {clauses[activeIndex].source_order}</span>}
      </div>
      <div ref={viewportRef} className="relative min-h-[520px] flex-1 overflow-auto bg-slate-200/75 p-3 dark:bg-slate-950/50">
        {loading && <div className="absolute inset-0 z-10 grid place-items-center bg-background/75"><span className="flex items-center gap-2 text-sm text-muted-foreground"><Spinner />DOCX 원문을 그리는 중입니다.</span></div>}
        {error && <div className="m-4 rounded-lg border border-destructive/40 bg-destructive/10 p-4 text-sm text-destructive">{error}</div>}
        <div ref={styleRef} />
        <div
          className="docx-source-stage relative mx-auto"
          style={renderSize.width && renderSize.height ? {
            width: renderSize.width * zoom,
            height: renderSize.height * zoom,
          } : undefined}
        >
          <div
            ref={scaleRef}
            className="docx-source-scale origin-top-left"
            style={{ transform: `scale(${zoom})` }}
          >
            <div ref={bodyRef} />
          </div>
        </div>
      </div>
      <style>{`
        .docx-source-stage { min-width: 1px; min-height: 1px; }
        .docx-source-scale { position: absolute; inset: 0 auto auto 0; width: max-content; transform-origin: top left; }
        .docx-source-scale .docx-wrapper { background: transparent !important; padding: 0 !important; width: max-content !important; }
        .docx-source-scale section.docx { margin: 0 auto 18px !important; box-shadow: 0 4px 18px rgb(15 23 42 / 18%); }
        .docx-source-scale .spec-source-marker { cursor: pointer; position: relative; transition: background-color .15s, outline-color .15s; }
        .docx-source-scale .spec-source-marker:not(tr) { border-left: 4px solid transparent !important; }
        .docx-source-scale tr.spec-source-marker { box-shadow: inset 4px 0 transparent; }
        .docx-source-scale .spec-source-marker:hover { background: rgb(219 234 254 / 60%) !important; outline: 1px solid rgb(59 130 246 / 45%); }
        .docx-source-scale .spec-source-keep:not(tr) { border-left-color: #10b981 !important; }
        .docx-source-scale .spec-source-delete:not(tr) { border-left-color: #ef4444 !important; }
        .docx-source-scale .spec-source-hold:not(tr) { border-left-color: #f59e0b !important; }
        .docx-source-scale tr.spec-source-keep { box-shadow: inset 4px 0 #10b981; }
        .docx-source-scale tr.spec-source-delete { box-shadow: inset 4px 0 #ef4444; }
        .docx-source-scale tr.spec-source-hold { box-shadow: inset 4px 0 #f59e0b; }
        .docx-source-scale .spec-source-active { background: rgb(253 224 71 / 55%) !important; outline: 2px solid #0f4c81 !important; outline-offset: 2px; }
      `}</style>
    </section>
  );
}
