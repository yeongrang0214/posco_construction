import { getDatabase, nowIso, optionalSecret } from '@/lib/cloud-runtime';

const KCSC_BASE = 'https://kcsc.re.kr/OpenApi';

function normalize(text: string) {
  return text.toLowerCase().replace(/[^0-9a-z가-힣.%/+-]+/g, ' ').replace(/\s+/g, ' ').trim();
}

function stripHtml(value: unknown) {
  return String(value || '')
    .replace(/<br\s*\/?>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/\s+/g, ' ')
    .trim();
}

async function kcscJson(path: string, key: string) {
  const response = await fetch(`${KCSC_BASE}/${path}?key=${encodeURIComponent(key)}`, {
    headers: { Accept: 'application/json', 'User-Agent': 'posco-spec-manager-cloud/1.0' },
  });
  if (!response.ok) throw new Error(`KCSC API가 HTTP ${response.status}을 반환했습니다.`);
  const payload = await response.json() as unknown;
  if (payload && typeof payload === 'object' && !Array.isArray(payload) && 'message' in payload) {
    throw new Error(String((payload as { message?: unknown }).message || 'KCSC API 오류'));
  }
  return payload;
}

async function sha256Hex(value: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

async function mapLimit<T, R>(items: T[], limit: number, worker: (item: T) => Promise<R>): Promise<R[]> {
  const output = new Array<R>(items.length);
  let cursor = 0;
  const runners = Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (cursor < items.length) {
      const index = cursor;
      cursor += 1;
      output[index] = await worker(items[index]);
    }
  });
  await Promise.all(runners);
  return output;
}

export async function syncKcsCloud() {
  const key = optionalSecret('KCSC_API_KEY');
  if (!key) throw new Error('KCSC_API_KEY 서버 비밀값이 설정되지 않았습니다.');

  const db = getDatabase();
  const codeList = await kcscJson('CodeList', key);
  if (!Array.isArray(codeList)) throw new Error('KCSC CodeList 응답 형식이 올바르지 않습니다.');

  const items = codeList
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .filter((item) => String(item.codeType || '').toUpperCase() === 'KCS' && String(item.code || '').trim());
  if (!items.length) throw new Error('KCS 코드 목록이 비어 있습니다.');

  const existingResult = await db.prepare('SELECT kcs_code, version, update_date, document_name FROM kcs_documents').all<{
    kcs_code: string; version: string; update_date: string; document_name: string;
  }>();
  const existing = new Map((existingResult.results || []).map((row) => [row.kcs_code, row]));
  const changedItems = items.filter((item) => {
    const code = String(item.code || '');
    const old = existing.get(code);
    return !old || old.version !== String(item.version || '') || old.update_date !== String(item.updateDate || '') || old.document_name !== String(item.name || '');
  });

  const downloaded = await mapLimit(changedItems, 6, async (item) => {
    const code = String(item.code || '');
    const payload = await kcscJson(`CodeViewer/KCS/${encodeURIComponent(code)}`, key);
    const records = Array.isArray(payload)
      ? payload.filter((candidate): candidate is Record<string, unknown> => Boolean(candidate && typeof candidate === 'object'))
      : [];
    const record = records.find((candidate) => String(candidate.code || '') === code && Array.isArray(candidate.list));
    return { item, record: record || null };
  });

  const syncedAt = nowIso();
  let usable = items.length - changedItems.length;
  let unavailable = 0;

  for (const { item, record } of downloaded) {
    const code = String(item.code || '');
    if (!record || !Array.isArray(record.list)) {
      unavailable += 1;
      continue;
    }
    const documentId = `kcs_${code.replace(/[^0-9A-Za-z_-]/g, '_')}`;
    await db.prepare('DELETE FROM kcs_documents WHERE kcs_code=?').bind(code).run();
    await db.prepare(`
      INSERT INTO kcs_documents (id,kcs_code,full_code,document_name,version,update_date,parent_names,synced_at)
      VALUES (?,?,?,?,?,?,?,?)
    `).bind(
      documentId, code, String(item.fullCode || ''), String(item.name || ''), String(item.version || ''),
      String(item.updateDate || ''), JSON.stringify(item.listParentCodes || []), syncedAt,
    ).run();

    let sectionOrder = 0;
    for (const rawSection of record.list as unknown[]) {
      if (!rawSection || typeof rawSection !== 'object') continue;
      const section = rawSection as Record<string, unknown>;
      const title = stripHtml(section.title);
      const content = stripHtml(section.contents);
      if (!title && !content) continue;
      sectionOrder += 1;
      const clause = String(section.code || section.section || section.no || '');
      await db.prepare(`
        INSERT INTO kcs_sections
        (id,document_id,kcs_code,section_order,kcs_clause,title,content,search_text,version,update_date,document_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
      `).bind(
        `${documentId}_s${sectionOrder}`, documentId, code, sectionOrder, clause, title, content,
        normalize(`${code} ${title} ${content}`), String(item.version || ''), String(item.updateDate || ''), String(item.name || ''),
      ).run();
    }
    usable += 1;
  }

  const revision = await sha256Hex(items.map((item) => [item.code, item.version, item.updateDate, item.name].join('|')).sort().join('\n'));
  await db.prepare("INSERT INTO app_meta(key,value,updated_at) VALUES('kcs_revision',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at").bind(revision, syncedAt).run();
  await db.prepare("INSERT INTO app_meta(key,value,updated_at) VALUES('kcs_snapshot',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at").bind(syncedAt, syncedAt).run();

  return {
    kcs_snapshot: syncedAt,
    kcs_revision: revision,
    kcs_document_count: items.length,
    changed_count: changedItems.length,
    usable_document_count: usable,
    unavailable_document_count: unavailable,
    queued_project_count: 0,
    rematch_run_ids: [],
  };
}
