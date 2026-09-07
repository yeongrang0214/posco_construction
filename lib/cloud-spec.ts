import JSZip from 'jszip';
import { getBucket, getDatabase, newId, nowIso, optionalSecret } from '@/lib/cloud-runtime';

const KCSC_BASE = 'https://kcsc.re.kr/OpenApi';

export type ParsedClause = {
  source_order: number;
  label: string;
  title: string;
  source_type: 'paragraph' | 'table' | 'heading';
  outline_level: number | null;
  content: string;
};

function decodeXml(text: string) {
  return text
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
}

function xmlText(xml: string) {
  const chunks = Array.from(xml.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/g), (match) => decodeXml(match[1] || ''));
  return chunks.join('').replace(/\s+/g, ' ').trim();
}

function paragraphLevel(xml: string): number | null {
  const style = xml.match(/<w:pStyle[^>]*w:val="([^"]+)"/i)?.[1] || '';
  const outline = xml.match(/<w:outlineLvl[^>]*w:val="(\d+)"/i)?.[1];
  if (outline != null) return Number(outline) + 1;
  const heading = style.match(/(?:Heading|제목)(\d+)/i)?.[1];
  return heading ? Number(heading) : null;
}

function splitLabel(text: string) {
  const match = text.match(/^((?:\d+(?:\.\d+)*|[가-힣]|[A-Za-z])(?:[.)])?)\s+(.+)$/);
  if (!match) return { label: '', title: '', content: text };
  const label = (match[1] || '').trim();
  return { label, title: '', content: text };
}

export async function parseDocx(bytes: ArrayBuffer): Promise<ParsedClause[]> {
  const zip = await JSZip.loadAsync(bytes);
  const documentXml = await zip.file('word/document.xml')?.async('text');
  if (!documentXml) throw new Error('DOCX의 word/document.xml을 찾지 못했습니다. 손상된 파일인지 확인해 주세요.');

  const body = documentXml.match(/<w:body[\s\S]*?<\/w:body>/)?.[0] || documentXml;
  const blocks = Array.from(body.matchAll(/<w:(p|tbl)(?:\s[^>]*)?>[\s\S]*?<\/w:\1>/g));
  const clauses: ParsedClause[] = [];
  let order = 0;

  for (const block of blocks) {
    const kind = block[1];
    const xml = block[0];
    if (kind === 'tbl') {
      const rows = Array.from(xml.matchAll(/<w:tr(?:\s[^>]*)?>[\s\S]*?<\/w:tr>/g));
      for (const row of rows) {
        const cells = Array.from(row[0].matchAll(/<w:tc(?:\s[^>]*)?>[\s\S]*?<\/w:tc>/g), (cell) => xmlText(cell[0]))
          .map((value) => value.trim())
          .filter(Boolean);
        if (!cells.length) continue;
        order += 1;
        clauses.push({
          source_order: order,
          label: `표-${order}`,
          title: '',
          source_type: 'table',
          outline_level: null,
          content: cells.join(' | '),
        });
      }
      continue;
    }

    const text = xmlText(xml);
    if (!text) continue;
    order += 1;
    const level = paragraphLevel(xml);
    const parts = splitLabel(text);
    clauses.push({
      source_order: order,
      label: parts.label,
      title: level ? text : '',
      source_type: level ? 'heading' : 'paragraph',
      outline_level: level,
      content: text,
    });
  }

  if (!clauses.length) throw new Error('DOCX에서 검토 가능한 문단이나 표를 추출하지 못했습니다.');
  return clauses;
}

function normalize(text: string) {
  return text
    .toLowerCase()
    .replace(/[^0-9a-z가-힣.%/+-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function tokens(text: string) {
  return normalize(text).split(' ').filter((token) => token.length >= 2);
}

function similarity(source: string, candidate: string) {
  const a = tokens(source);
  const b = tokens(candidate);
  if (!a.length || !b.length) return 0;
  const counts = new Map<string, number>();
  for (const token of b) counts.set(token, (counts.get(token) || 0) + 1);
  let overlap = 0;
  for (const token of a) {
    const count = counts.get(token) || 0;
    if (count > 0) {
      overlap += 1;
      counts.set(token, count - 1);
    }
  }
  const recall = overlap / a.length;
  const precision = overlap / b.length;
  const numericA = new Set(source.match(/\d+(?:\.\d+)?%?/g) || []);
  const numericB = new Set(candidate.match(/\d+(?:\.\d+)?%?/g) || []);
  const numericBonus = [...numericA].some((value) => numericB.has(value)) ? 0.08 : 0;
  return Math.min(1, 0.62 * recall + 0.38 * precision + numericBonus);
}

export async function matchClause(projectId: string, clauseId: string, content: string) {
  const db = getDatabase();
  const queryTokens = tokens(content).filter((value, index, array) => array.indexOf(value) === index).slice(0, 12);
  if (!queryTokens.length) return 0;

  const conditions = queryTokens.map(() => 'search_text LIKE ?').join(' OR ');
  const params = queryTokens.map((token) => `%${token}%`);
  const result = await db.prepare(
    `SELECT id, kcs_code, kcs_clause, title, content, version, update_date, document_name
       FROM kcs_sections
      WHERE ${conditions}
      LIMIT 180`,
  ).bind(...params).all<Record<string, unknown>>();

  const ranked = (result.results || [])
    .map((row) => ({ row, score: similarity(content, String(row.content || '')) }))
    .filter((entry) => entry.score >= 0.25)
    .sort((a, b) => b.score - a.score)
    .slice(0, 3);

  await db.prepare('DELETE FROM project_candidates WHERE clause_id = ?').bind(clauseId).run();
  const createdAt = nowIso();
  for (let i = 0; i < ranked.length; i += 1) {
    const entry = ranked[i];
    await db.prepare(
      `INSERT INTO project_candidates
       (id, project_id, clause_id, section_id, rank, score, reasons_json, warnings_json, excluded, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)`,
    ).bind(
      newId('cand'), projectId, clauseId, String(entry.row.id), i + 1, entry.score,
      JSON.stringify(['클라우드 토큰 유사도', '수치 일치 보정']), '[]', createdAt,
    ).run();
  }
  return ranked.length;
}

export async function createProjectFromDocx(file: File) {
  if (!file.name.toLowerCase().endsWith('.docx')) {
    throw new Error('클라우드 배포에서는 우선 DOCX 업로드를 지원합니다. 구형 DOC는 Word에서 DOCX로 저장한 뒤 업로드해 주세요.');
  }
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength > 25 * 1024 * 1024) throw new Error('DOCX 파일은 25MB 이하만 업로드할 수 있습니다.');
  const clauses = await parseDocx(bytes);
  const db = getDatabase();
  const bucket = getBucket();
  const projectId = newId('prj');
  const objectKey = `uploads/${projectId}/${file.name.replace(/[^0-9A-Za-z가-힣._-]+/g, '_')}`;
  const uploadedAt = nowIso();
  const title = file.name.replace(/\.docx$/i, '');

  await bucket.put(objectKey, bytes, {
    httpMetadata: { contentType: file.type || 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' },
    customMetadata: { projectId, originalFilename: file.name },
  });

  const meta = await db.prepare("SELECT key, value FROM app_meta WHERE key IN ('kcs_snapshot','kcs_revision')").all<{ key: string; value: string }>();
  const metaMap = Object.fromEntries((meta.results || []).map((row) => [row.key, row.value]));
  await db.prepare(
    `INSERT INTO projects
     (id, title, source_filename, source_object_key, uploaded_at, status, warning, kcs_snapshot, kcs_revision, kcs_scope, updated_at)
     VALUES (?, ?, ?, ?, ?, 'reviewing', '', ?, ?, 'all', ?)`,
  ).bind(projectId, title, file.name, objectKey, uploadedAt, metaMap.kcs_snapshot || '', metaMap.kcs_revision || '', uploadedAt).run();

  for (const clause of clauses) {
    const clauseId = newId('cl');
    await db.prepare(
      `INSERT INTO clauses
       (id, project_id, source_order, label, title, source_type, outline_level, content, edited_content,
        decision, decision_reason, coverage_confirmed, selected_candidate_id, review_note, reviewed_at, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', 0, NULL, '', NULL, ?, ?)`,
    ).bind(
      clauseId, projectId, clause.source_order, clause.label, clause.title, clause.source_type,
      clause.outline_level, clause.content, clause.content, uploadedAt, uploadedAt,
    ).run();
    if (clause.source_type !== 'heading') await matchClause(projectId, clauseId, clause.content);
  }

  return projectId;
}

function stripHtml(value: unknown) {
  return String(value || '').replace(/<[^>]+>/g, ' ').replace(/&nbsp;/g, ' ').replace(/\s+/g, ' ').trim();
}

async function kcscJson(path: string, key: string) {
  const response = await fetch(`${KCSC_BASE}/${path}?key=${encodeURIComponent(key)}`, {
    headers: { Accept: 'application/json', 'User-Agent': 'posco-spec-manager-cloud/1.0' },
  });
  if (!response.ok) throw new Error(`KCSC API가 HTTP ${response.status}을 반환했습니다.`);
  return response.json() as Promise<unknown>;
}

export async function syncKcsCloud() {
  const key = optionalSecret('KCSC_API_KEY');
  if (!key) throw new Error('KCSC_API_KEY 서버 비밀값이 설정되지 않았습니다.');
  const db = getDatabase();
  const codeList = await kcscJson('CodeList', key);
  if (!Array.isArray(codeList)) throw new Error('KCSC CodeList 응답 형식이 올바르지 않습니다.');
  const items = codeList.filter((item) => item && typeof item === 'object' && String((item as Record<string, unknown>).codeType || '').toUpperCase() === 'KCS');
  if (!items.length) throw new Error('KCS 코드 목록이 비어 있습니다.');

  const revisionInput = items.map((item) => {
    const row = item as Record<string, unknown>;
    return [row.code, row.version, row.updateDate, row.name].join('|');
  }).sort().join('\n');
  const revisionBytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(revisionInput));
  const revision = [...new Uint8Array(revisionBytes)].map((value) => value.toString(16).padStart(2, '0')).join('');
  const syncedAt = nowIso();
  let usable = 0;
  let changed = 0;

  for (const item of items) {
    const row = item as Record<string, unknown>;
    const code = String(row.code || '').trim();
    if (!code) continue;
    const existing = await db.prepare('SELECT version, update_date FROM kcs_documents WHERE kcs_code = ?').bind(code).first<{ version: string; update_date: string }>();
    if (existing && existing.version === String(row.version || '') && existing.update_date === String(row.updateDate || '')) {
      usable += 1;
      continue;
    }
    const payload = await kcscJson(`CodeViewer/KCS/${encodeURIComponent(code)}`, key);
    const records = Array.isArray(payload) ? payload.filter((candidate) => candidate && typeof candidate === 'object') as Record<string, unknown>[] : [];
    const record = records.find((candidate) => String(candidate.code || '') === code) || records[0];
    if (!record || !Array.isArray(record.list)) continue;
    const documentId = `kcs_${code.replace(/[^0-9A-Za-z_-]/g, '_')}`;
    await db.prepare('DELETE FROM kcs_documents WHERE kcs_code = ?').bind(code).run();
    await db.prepare(
      `INSERT INTO kcs_documents (id, kcs_code, full_code, document_name, version, update_date, parent_names, synced_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      documentId, code, String(row.fullCode || ''), String(row.name || ''), String(row.version || ''),
      String(row.updateDate || ''), JSON.stringify(row.listParentCodes || []), syncedAt,
    ).run();
    let sectionOrder = 0;
    for (const section of record.list as Record<string, unknown>[]) {
      if (!section || typeof section !== 'object') continue;
      const content = stripHtml(section.contents);
      const title = stripHtml(section.title);
      if (!content && !title) continue;
      sectionOrder += 1;
      const clause = String(section.code || section.section || section.no || '');
      await db.prepare(
        `INSERT INTO kcs_sections
         (id, document_id, kcs_code, section_order, kcs_clause, title, content, search_text, version, update_date, document_name)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      ).bind(
        `${documentId}_s${sectionOrder}`, documentId, code, sectionOrder, clause, title, content,
        normalize(`${code} ${title} ${content}`), String(row.version || ''), String(row.updateDate || ''), String(row.name || ''),
      ).run();
    }
    usable += 1;
    changed += 1;
  }

  await db.prepare("INSERT INTO app_meta(key,value,updated_at) VALUES('kcs_revision',?,?,) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at").bind(revision, syncedAt).run().catch(async () => {
    await db.prepare("INSERT INTO app_meta(key,value,updated_at) VALUES('kcs_revision',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at").bind(revision, syncedAt).run();
  });
  await db.prepare("INSERT INTO app_meta(key,value,updated_at) VALUES('kcs_snapshot',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at").bind(syncedAt, syncedAt).run();

  return { kcs_snapshot: syncedAt, kcs_revision: revision, kcs_document_count: items.length, changed_count: changed, usable_document_count: usable, unavailable_document_count: Math.max(0, items.length - usable), queued_project_count: 0, rematch_run_ids: [] };
}
