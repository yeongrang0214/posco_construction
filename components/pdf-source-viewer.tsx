'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import type { PDFDocumentProxy, RenderTask } from 'pdfjs-dist';
import { Crosshair, FileText, Minus, Plus } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Spinner } from '@/components/ui/spinner';
import { api, type DocumentMapItem } from '@/lib/api';

type Box = { page: number; left: number; top: number; right: number; bottom: number };
type Page = { page: number; width: number; height: number };
type Preview = { status: 'ready' | 'processing'; pages: Page[]; items: { id: string; status: string; boxes: Box[] }[] };
type Mark = Box & { clause: DocumentMapItem };

function PdfPage({ pdf, page, scale, marks, activeId, onSelect }: {
  pdf: PDFDocumentProxy; page: Page; scale: number; marks: Mark[];
  activeId: string | null; onSelect: (id: string) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const canvasHost = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const observer = new IntersectionObserver(([entry]) => setVisible(entry.isIntersecting), { rootMargin: '650px' });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible || !canvasHost.current) return;
    let cancelled = false;
    let task: RenderTask | undefined;
    const canvas = document.createElement('canvas');
    canvasHost.current.replaceChildren(canvas);
    setError('');
    void pdf.getPage(page.page).then(async (sourcePage) => {
      if (cancelled) return;
      const viewport = sourcePage.getViewport({ scale });
      const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(viewport.width * pixelRatio);
      canvas.height = Math.ceil(viewport.height * pixelRatio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      task = sourcePage.render({ canvas, viewport, transform: [pixelRatio, 0, 0, pixelRatio, 0, 0] });
      await task.promise;
    }).catch((cause) => {
      if (!cancelled && cause?.name !== 'RenderingCancelledException') setError('이 페이지를 표시하지 못했습니다. 텍스트 보기로 전환할 수 있습니다.');
    });
    return () => { cancelled = true; task?.cancel(); canvas.remove(); canvas.width = 0; canvas.height = 0; };
  }, [pdf, page.page, scale, visible]);
  return <div ref={host} data-pdf-page={page.page} className="relative mx-auto mb-4 bg-white shadow-md" style={{ width: page.width * scale, height: page.height * scale }}>
    <div ref={canvasHost} aria-label={`원문 ${page.page}쪽`} />
    {error && <p className="absolute left-4 top-4 bg-white p-3 text-sm text-red-700">{error}</p>}
    {marks.map((mark, i) => <button key={`${mark.clause.id}-${i}`} type="button"
      data-pdf-clause={mark.clause.id}
      aria-label={`${mark.clause.label} ${mark.clause.title} 원문 선택`}
      aria-pressed={activeId === mark.clause.id}
      title={`${mark.clause.label} ${mark.clause.title}`}
      className="absolute cursor-pointer hover:bg-blue-200/40 focus-visible:outline-2 focus-visible:outline-blue-700"
      style={{ left: mark.left * scale - 1, top: mark.top * scale - 1,
        width: (mark.right - mark.left) * scale + 2, height: (mark.bottom - mark.top) * scale + 2,
        backgroundColor: activeId === mark.clause.id ? 'rgb(253 224 71 / 45%)' : undefined,
        outline: activeId === mark.clause.id ? '1px solid #145a94' : undefined,
        borderLeft: mark.clause.decision ? `3px solid ${{ keep: '#10b981', delete: '#ef4444', hold: '#f59e0b' }[mark.clause.decision]}` : undefined }}
      onClick={() => onSelect(mark.clause.id)} />)}
  </div>;
}

type ViewerProps = {
  projectId: string; clauses: DocumentMapItem[]; activeClauseId: string | null; onSelectClause: (id: string) => void;
};

export function PdfSourceViewer(props: ViewerProps) {
  // Decisions preserve the PDF; structural edits remount it to cancel stale mapping requests.
  const key = JSON.stringify([props.projectId, props.clauses.map(({ id, label, title, content }) => [id, label, title, content])]);
  return <PdfSourceDocument key={key} {...props} />;
}

function PdfSourceDocument({ projectId, clauses, activeClauseId, onSelectClause }: ViewerProps) {
  const viewport = useRef<HTMLDivElement>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState('');
  const [mode, setMode] = useState<'pdf' | 'text'>('pdf');
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(700);
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let loadingTask: ReturnType<typeof import('pdfjs-dist')['getDocument']> | undefined;
    const started = Date.now();
    const poll = async () => {
      try {
        const response = await fetch(api.projectSourcePreviewUrl(projectId), { signal: controller.signal });
        const body = await response.json() as Preview & { detail?: string };
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : '원문 PDF를 준비하지 못했습니다.');
        if (controller.signal.aborted) return;
        if (body.status !== 'ready') {
          if (Date.now() - started > 180_000) throw new Error('PDF 준비가 지연되고 있습니다. 텍스트 보기로 검토하고 잠시 후 다시 열어주세요.');
          timer = setTimeout(() => void poll(), 2000);
          return;
        }
        const pdfjs = await import('pdfjs-dist');
        if (controller.signal.aborted) return;
        // Serve the unmodified, matching-version worker: Vite's dynamic-import
        // transform otherwise injects its window-only client into this worker.
        pdfjs.GlobalWorkerOptions.workerSrc = `/pdfjs/${pdfjs.version}/pdf.worker.min.mjs`;
        loadingTask = pdfjs.getDocument({ url: api.projectSourcePdfUrl(projectId) });
        const document = await loadingTask.promise;
        if (controller.signal.aborted) { void loadingTask.destroy(); return; }
        if (document.numPages !== body.pages.length) throw new Error('PDF와 원문 위치 정보가 일치하지 않습니다.');
        setPreview(body); setPdf(document);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '원문 PDF를 표시하지 못했습니다.');
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); void loadingTask?.destroy(); };
  }, [projectId]);
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const observer = new ResizeObserver(() => setWidth(Math.max(280, element.clientWidth - 24)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const marks = useMemo(() => {
    const source = new Map(clauses.map((clause) => [clause.id, clause]));
    const result = new Map<number, Mark[]>();
    for (const item of preview?.items || []) {
      const clause = source.get(item.id);
      if (!clause) continue;
      for (const box of item.boxes) result.set(box.page, [...(result.get(box.page) || []), { ...box, clause }]);
    }
    return result;
  }, [clauses, preview]);
  const active = clauses.find((clause) => clause.id === activeClauseId);
  const activeMapping = preview?.items.find((item) => item.id === activeClauseId);
  const activePage = activeMapping?.boxes[0]?.page;
  const moveToActive = () => {
    if (!activeClauseId) return;
    viewport.current?.querySelector(`[data-${mode === 'pdf' ? 'pdf' : 'text'}-clause="${CSS.escape(activeClauseId)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  };
  useEffect(() => { moveToActive(); }, [activeClauseId, preview, mode]); // eslint-disable-line react-hooks/exhaustive-deps
  return <section className="flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-card shadow-sm" aria-label="포스코 원문">
    <div className="flex flex-wrap items-center gap-2 border-b px-3 py-2">
      <div className="mr-auto"><p className="flex items-center gap-2 text-sm font-semibold"><FileText className="size-4" />포스코 원문</p><p className="text-xs text-muted-foreground">원문을 PDF로 표시 · 매칭은 Word 텍스트 기준</p></div>
      <Button size="sm" variant="outline" onClick={() => setMode(mode === 'pdf' ? 'text' : 'pdf')}>{mode === 'pdf' ? '텍스트 보기' : 'PDF 보기'}</Button>
      <Badge variant="outline">{activePage || '-'} / {preview?.pages.length || '-'}쪽</Badge>
      <Button size="icon-sm" variant="outline" aria-label="원문 축소" onClick={() => setZoom(value => Math.max(0.6, value - 0.1))}><Minus /></Button>
      <span className="text-xs">{Math.round(zoom * 100)}%</span>
      <Button size="icon-sm" variant="outline" aria-label="원문 확대" onClick={() => setZoom(value => Math.min(2, value + 0.1))}><Plus /></Button>
      <Button size="sm" variant="outline" onClick={moveToActive}><Crosshair />현재 위치</Button>
    </div>
    <div className="flex flex-wrap gap-3 border-b bg-muted/35 px-3 py-2 text-xs text-muted-foreground"><span>🟨 현재 조항</span><span className="text-emerald-700">남김</span><span className="text-red-700">삭제</span><span className="text-amber-700">보류</span>{active && <span className="ml-auto">원문 순서 {active.source_order}</span>}</div>
    {mode === 'pdf' && active && preview && !activeMapping?.boxes.length && <output className="block border-b bg-amber-50 px-3 py-2 text-sm text-amber-900">이 조항의 PDF 위치를 확정하지 못했습니다. 잘못된 위치에 표시하지 않습니다.<button className="ml-2 underline" onClick={() => setMode('text')}>원문 텍스트 확인</button></output>}
    <div ref={viewport} className="relative min-h-[520px] flex-1 overflow-auto bg-slate-200/75 p-3">
      {mode === 'pdf' && !pdf && !error && <output className="flex items-center justify-center gap-2 p-8 text-sm"><Spinner />원문 PDF 준비 중입니다. 첫 변환은 잠시 걸릴 수 있습니다.</output>}
      {mode === 'pdf' && error && <div role="alert" className="rounded-lg bg-white p-4 text-sm text-red-700">{error}<div className="mt-3 flex gap-3"><Button size="sm" variant="outline" onClick={() => setMode('text')}>텍스트 보기</Button><a className="underline" href={api.projectSourceDocxUrl(projectId)}>원본 DOCX 내려받기</a></div></div>}
      {mode === 'pdf' && pdf && preview?.pages.map(page => <PdfPage key={page.page} pdf={pdf} page={page} scale={width / page.width * zoom} marks={marks.get(page.page) || []} activeId={activeClauseId} onSelect={onSelectClause} />)}
      {mode === 'text' && <div className="space-y-2">{clauses.map(clause => <button key={clause.id} data-text-clause={clause.id} onClick={() => onSelectClause(clause.id)} className={`block w-full rounded border p-4 text-left text-sm leading-7 ${clause.id === activeClauseId ? 'border-blue-700 bg-yellow-100 text-black' : 'border-slate-200 bg-white text-black'}`}><span className="font-semibold">{clause.label} · {clause.title}</span><p className="whitespace-pre-wrap">{clause.content}</p></button>)}</div>}
    </div>
  </section>;
}
