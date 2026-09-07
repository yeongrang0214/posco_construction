import JSZip from 'jszip';
import { getBucket, getDatabase, newId, nowIso } from '@/lib/cloud-runtime';

export type ParsedClause = {
  source_order: number;
  label: string;
  title: string;
  source_type: 'paragraph' | 'table' | 'heading';
  outline_level: number | null;
  content: string;
};

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') return `${value}`;
  return '';
}

function decodeXml(value: string) {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'");
}

function xmlText(xml: string) {
  return Array.from(xml.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/g), (match) => decodeXml(match[1] || ''))
    .join('')
    .replace(/\s+/g, ' ')
    .trim();
}

function paragraphLevel(xml: string): number | null {
  const style = xml.match(/<w:pStyle[^>]*w:val="([^"]+)"/i)?.[1] || '';
  const outline = xml.match(/<w:outlineLvl[^>]*w:val="(\d+)"/i)?.[1];
  if (outline != null) return Number(outline) + 1;
  const heading = style.match(/(?:Heading|제목)(\d+)/i)?.[1];
  return heading ? Number(heading) : null;
}

function splitLabel(value: string) {
  const match = value.match(/^((?:\d+(?:\.\d+)*|[가-힣]|[A-Za-z])(?:[.)])?)\s+(.+)$/);
  return match ? (match[1] || '').trim() : '';
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
        clauses.push({ source_order: order, label: `표-${order}`, title: '', source_type: 'table', outline_level: null, content: cells.join(' | ') });
      }
      continue;
    }

    const content = xmlText(xml);
    if (!content) continue;
    order += 1;
    const level = paragraphLevel(xml);
    clauses.push({
      source_order: order,
      label: splitLabel(content),
      title: level ? content : '',
      source_type: level ? 'heading' : 'paragraph',
      outline_level: level,
      content,
    });
  }

  if (!clauses.length) throw new Error('DOCX에서 검토 가능한 문단이나 표를 추출하지 못했습니다.');
  return clauses;
}

function normalize(value: string) {
  return value.toLowerCase().replace(/[^0-9a-z가-힣.%/+-]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function tokens(value: string) {
  return normalize(value).split(' ').filter((token) => token.length >= 2);
}

function similarity(source: string, candidate: string) {
  const sourceTokens = tokens(source);
  const candidateTokens = tokens(candidate);
  if (!sourceTokens.length || !candidateTokens.length) return 0;
  const counts = new Map<string, number>();
  for (const token of candidateTokens) counts.set(token, (counts.get(token) || 0) + 1);
  let overlap = 0;
  for (const token of sourceTokens) {
    const count = counts.get(token) || 0;
    if (count > 0) {
      overlap += 1;
      counts.set(token, count - 1);
    }
  }
  const recall = overlap / sourceTokens.length;
  const precision = overlap / candidateTokens.length;
  const sourceNumbers = new Set(source.match(/\d+(?:\.\d+)?%?/g) || []);
  const candidateNumbers = new Set(candidate.match(/\d+(?:\.\d+)?%?/g) || []);
  const numericBonus = [...sourceNumbers].some((value) => candidateNumbers.has(value)) ? 0.08 : 0;
  return Math.min(1, 0.62 * recall + 0.38 * precision + numericBonus);
}

export async function matchClause(projectId: string, clauseId: string, content: string) {
  const db = getDatabase();
  const queryTokens = tokens(content).filter((value, index, array) => array.indexOf(value) === index).slice(0, 12);
  if (!queryTokens.length) return 0;

  const conditions = queryTokens.map(() => 'search_text LIKE ?').join(' OR ');
  const result = await db.prepare(`
    SELECT id, content FROM kcs_sections WHERE ${conditions} LIMIT 180
  `).bind(...queryTokens.map((token) => `%${token}%`)).all<Record<string, unknown>>();

  const ranked = (result.results || [])
    .map((row) => ({ id: text(row.id), score: similarity(content, text(row.content)) }))
    .filter((entry) => entry.id && entry.score >= 0.25)
    .sort((a, b) => b.score - a.score)
    .slice(0, 3);

  await db.prepare('DELETE FROM project_candidates WHERE clause_id=?').bind(clauseId).run();
  const createdAt = nowIso();
  for (let index = 0; index < ranked.length; index += 1) {
    const entry = ranked[index];
    await db.prepare(`
      INSERT INTO project_candidates
      (id,project_id,clause_id,section_id,rank,score,reasons_json,warnings_json,excluded,created_at)
      VALUES (?,?,?,?,?,?,?,?,0,?)
    `).bind(
      newId('cand'), projectId, clauseId, entry.id, index + 1, entry.score,
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

  const meta = await db.prepare("SELECT key,value FROM app_meta WHERE key IN ('kcs_snapshot','kcs_revision')").all<{ key: string; value: string }>();
  const metaMap = Object.fromEntries((meta.results || []).map((row) => [row.key, row.value]));
  await db.prepare(`
    INSERT INTO projects
    (id,title,source_filename,source_object_key,uploaded_at,status,warning,kcs_snapshot,kcs_revision,kcs_scope,updated_at)
    VALUES (?,?,?,?,?,'reviewing','',?,?,'all',?)
  `).bind(projectId, title, file.name, objectKey, uploadedAt, metaMap.kcs_snapshot || '', metaMap.kcs_revision || '', uploadedAt).run();

  for (const clause of clauses) {
    const clauseId = newId('cl');
    await db.prepare(`
      INSERT INTO clauses
      (id,project_id,source_order,label,title,source_type,outline_level,content,edited_content,decision,decision_reason,coverage_confirmed,selected_candidate_id,review_note,reviewed_at,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,NULL,'',0,NULL,'',NULL,?,?)
    `).bind(
      clauseId, projectId, clause.source_order, clause.label, clause.title, clause.source_type,
      clause.outline_level, clause.content, clause.content, uploadedAt, uploadedAt,
    ).run();
    if (clause.source_type !== 'heading') await matchClause(projectId, clauseId, clause.content);
  }

  return projectId;
}
