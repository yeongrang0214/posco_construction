import JSZip from 'jszip';
import { getBucket, getDatabase, newId, nowIso } from '@/lib/cloud-runtime';

export type ParsedClause = {
  source_order: number;
  label: string;
  title: string;
  source_type: 'paragraph' | 'table' | 'heading';
  outline_level: number | null;
  content: string;
  match_context: string;
};

const DISCIPLINE_SCOPES: Array<[string[], string[]]> = [
  [['가설'], ['21']],
  [['조적', '벽돌', '블록'], ['4134']],
  [['석공', '석재'], ['4135']],
  [['목공', '목재'], ['4133']],
  [['수장', '보드', '천장'], ['4151']],
  [['미장', '모르타르'], ['4146']],
  [['타일'], ['4148']],
  [['창호', '문틀', '유리'], ['4155']],
  [
    ['철물', '잡공'],
    ['4149', '4131', '1431', '4156'],
  ],
  [['지붕'], ['4156']],
  [
    ['철골', '강구조'],
    ['4131', '1431'],
  ],
  [['방수'], ['4140']],
];

const TERM_EQUIVALENTS: string[][] = [
  ['공작도', '철골제작도', '제작도', '시공상세도', 'shop drawing', 'shopdrawing'],
  ['철골세우기', '철골 설치', '강구조 설치', 'steel erection', 'erection'],
  ['고장력볼트', '고력볼트', 'high strength bolt'],
  ['앵커볼트', '기초볼트', 'anchor bolt'],
  ['데크플레이트', '데크 플레이트', 'steel deck'],
  ['관 이음', '배관 이음', 'pipe joint', 'piping joint'],
  ['보온', '단열', 'thermal insulation'],
];

const CONTEXT_REFERENCE = /(?:상기|전항|앞(?:의|서)?|위(?:의)?)\s*(?:제?\s*)?(?:\(?[①-⑳0-9가-하]+\)?(?:\.\d+)*)?(?:항|호|목|규정|기준|내용)?/i;

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (
    typeof value === 'number' ||
    typeof value === 'boolean' ||
    typeof value === 'bigint'
  )
    return `${value}`;
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
  return Array.from(
    xml.matchAll(/<w:t(?:\s[^>]*)?>([\s\S]*?)<\/w:t>/g),
    (match) => decodeXml(match[1] || ''),
  )
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
  const match = value.match(
    /^((?:\d+(?:\.\d+)*|[가-힣]|[A-Za-z])(?:[.)])?)\s+(.+)$/,
  );
  return match ? (match[1] || '').trim() : '';
}

export async function parseDocx(bytes: ArrayBuffer): Promise<ParsedClause[]> {
  const zip = await JSZip.loadAsync(bytes);
  const documentXml = await zip.file('word/document.xml')?.async('text');
  if (!documentXml)
    throw new Error(
      'DOCX의 word/document.xml을 찾지 못했습니다. 손상된 파일인지 확인해 주세요.',
    );

  const body =
    documentXml.match(/<w:body[\s\S]*?<\/w:body>/)?.[0] || documentXml;
  const blocks = Array.from(
    body.matchAll(/<w:(p|tbl)(?:\s[^>]*)?>[\s\S]*?<\/w:\1>/g),
  );
  const clauses: ParsedClause[] = [];
  let order = 0;
  const headingPath: string[] = [];

  for (const block of blocks) {
    const kind = block[1];
    const xml = block[0];
    if (kind === 'tbl') {
      const rows = Array.from(
        xml.matchAll(/<w:tr(?:\s[^>]*)?>[\s\S]*?<\/w:tr>/g),
      );
      for (const row of rows) {
        const cells = Array.from(
          row[0].matchAll(/<w:tc(?:\s[^>]*)?>[\s\S]*?<\/w:tc>/g),
          (cell) => xmlText(cell[0]),
        )
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
          match_context: headingPath.join(' > '),
        });
      }
      continue;
    }

    const content = xmlText(xml);
    if (!content) continue;
    order += 1;
    const level = paragraphLevel(xml);
    if (level) {
      headingPath.splice(level - 1);
      headingPath[level - 1] = content;
    }
    clauses.push({
      source_order: order,
      label: splitLabel(content),
      title: level ? content : '',
      source_type: level ? 'heading' : 'paragraph',
      outline_level: level,
      content,
      match_context: level
        ? headingPath.slice(0, -1).join(' > ')
        : headingPath.join(' > '),
    });
  }

  if (!clauses.length)
    throw new Error('DOCX에서 검토 가능한 문단이나 표를 추출하지 못했습니다.');
  return clauses;
}

function normalize(value: string) {
  return value
    .replace(
      /\bKS\s*([A-Z])\s*[-_]?\s*(\d{3,})\b/gi,
      (_, family, number) => `KS ${String(family).toUpperCase()} ${number}`,
    )
    .toLowerCase()
    .replace(/[^0-9a-z가-힣.%/+-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function equivalentGroups(value: string) {
  const compact = normalize(value).replace(/\s+/g, '');
  return TERM_EQUIVALENTS.filter((group) =>
    group.some((term) => compact.includes(normalize(term).replace(/\s+/g, ''))),
  );
}

function expandEquivalentTerms(value: string) {
  const additions = equivalentGroups(value).flat();
  return additions.length ? `${value} ${[...new Set(additions)].join(' ')}` : value;
}

function tokens(value: string) {
  return normalize(expandEquivalentTerms(value))
    .split(' ')
    .filter((token) => token.length >= 2);
}

function similarity(source: string, candidate: string) {
  const sourceTokens = tokens(source);
  const candidateTokens = tokens(candidate);
  if (!sourceTokens.length || !candidateTokens.length) return 0;
  const counts = new Map<string, number>();
  for (const token of candidateTokens)
    counts.set(token, (counts.get(token) || 0) + 1);
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
  const numericBonus = [...sourceNumbers].some((value) =>
    candidateNumbers.has(value),
  )
    ? 0.08
    : 0;
  return Math.min(1, 0.62 * recall + 0.38 * precision + numericBonus);
}

function disciplineScope(value: string) {
  const normalized = normalize(value);
  return (
    DISCIPLINE_SCOPES.find(([keywords]) =>
      keywords.some((keyword) => normalized.includes(keyword)),
    )?.[1] || []
  );
}

export async function matchClause(
  projectId: string,
  clauseId: string,
  content: string,
  context = '',
  scope: string[] = [],
) {
  const db = getDatabase();
  const sourceText = `${context} ${content}`.trim();
  const sourceEquivalentGroups = equivalentGroups(sourceText);
  const equivalentTokens = sourceEquivalentGroups.flatMap((group) =>
    group.flatMap((term) => normalize(term).split(' ').filter(Boolean)),
  );
  const queryTokens = [...equivalentTokens, ...tokens(sourceText)]
    .filter((value, index, array) => array.indexOf(value) === index)
    .slice(0, 20);
  if (!queryTokens.length) return 0;

  const tokenConditions = queryTokens
    .map(() => 'search_text LIKE ?')
    .join(' OR ');
  const tokenBindings = queryTokens.map((token) => `%${token}%`);
  const fetchPool = async (prefixes: string[]) => {
    const scopeCondition = prefixes.length
      ? ` AND (${prefixes.map(() => "REPLACE(kcs_code,' ','') LIKE ?").join(' OR ')})`
      : '';
    return db
      .prepare(`
      SELECT id,document_id,section_order,kcs_code,kcs_clause,document_name,title,content,
        (SELECT previous.content FROM kcs_sections previous
          WHERE previous.document_id=kcs_sections.document_id
            AND previous.section_order<kcs_sections.section_order
          ORDER BY previous.section_order DESC LIMIT 1) AS previous_content
      FROM kcs_sections
      WHERE (${tokenConditions})${scopeCondition} LIMIT 180
    `)
      .bind(...tokenBindings, ...prefixes.map((prefix) => `KCS${prefix}%`))
      .all<Record<string, unknown>>();
  };
  const scoped = scope.length
    ? await fetchPool(scope)
    : { results: [] as Record<string, unknown>[] };
  const global = await fetchPool([]);
  const pool = new Map<string, Record<string, unknown>>();
  for (const row of [...(scoped.results || []), ...(global.results || [])])
    pool.set(text(row.id), row);

  const ranked = [...pool.values()]
    .map((row) => {
      const candidateContent = text(row.content);
      const candidateContext = CONTEXT_REFERENCE.test(candidateContent)
        ? text(row.previous_content) : '';
      const candidateText = `${text(row.document_name)} ${text(row.title)} ${candidateContext} ${candidateContent}`;
      const candidateGroups = equivalentGroups(candidateText);
      const equivalentMatch = sourceEquivalentGroups.some((group) => candidateGroups.includes(group));
      const baseScore = similarity(sourceText, candidateText);
      const code = text(row.kcs_code).replace(/\D/g, '');
      const inScope = scope.some((prefix) => code.startsWith(prefix));
      const titleOnly = normalize(candidateContent) === normalize(text(row.title));
      const substantive = normalize(candidateContent).replace(/[^0-9a-z가-힣]/g, '').length >= 20
        || /(?:한다|된다|있다|없다|따른다|하여야|해야|원칙)/.test(candidateContent);
      const contextPenalty = CONTEXT_REFERENCE.test(candidateContent) ? 0.88 : 1;
      return {
        id: text(row.id),
        score: baseScore * contextPenalty,
        eligible: !titleOnly && substantive,
        rankScore: baseScore * contextPenalty + (inScope ? 0.12 : 0) + (equivalentMatch ? 0.2 : 0),
        equivalentMatch,
      };
    })
    .filter((entry) => entry.id && entry.eligible && entry.score >= 0.25)
    .sort((a, b) => b.rankScore - a.rankScore || b.score - a.score)
    .slice(0, 3);

  await db
    .prepare('DELETE FROM project_candidates WHERE clause_id=?')
    .bind(clauseId)
    .run();
  const createdAt = nowIso();
  for (let index = 0; index < ranked.length; index += 1) {
    const entry = ranked[index];
    await db
      .prepare(`
      INSERT INTO project_candidates
      (id,project_id,clause_id,section_id,rank,score,reasons_json,warnings_json,excluded,created_at)
      VALUES (?,?,?,?,?,?,?,?,0,?)
    `)
      .bind(
        newId('cand'),
        projectId,
        clauseId,
        entry.id,
        index + 1,
        entry.score,
        JSON.stringify([
          '클라우드 토큰 유사도',
          ...(entry.equivalentMatch ? ['건설 전문용어 동의어 일치'] : []),
          '수치 일치 보정',
        ]),
        '[]',
        createdAt,
      )
      .run();
  }
  return ranked.length;
}

export async function createProjectFromDocx(file: File) {
  if (!file.name.toLowerCase().endsWith('.docx')) {
    throw new Error(
      '클라우드 배포에서는 우선 DOCX 업로드를 지원합니다. 구형 DOC는 Word에서 DOCX로 저장한 뒤 업로드해 주세요.',
    );
  }
  const bytes = await file.arrayBuffer();
  if (bytes.byteLength > 25 * 1024 * 1024)
    throw new Error('DOCX 파일은 25MB 이하만 업로드할 수 있습니다.');
  const clauses = await parseDocx(bytes);
  const db = getDatabase();
  const bucket = getBucket();
  const projectId = newId('prj');
  const objectKey = `uploads/${projectId}/${file.name.replace(/[^0-9A-Za-z가-힣._-]+/g, '_')}`;
  const uploadedAt = nowIso();
  const title = file.name.replace(/\.docx$/i, '');
  const scope = disciplineScope(`${file.name} ${title}`);

  await bucket.put(objectKey, bytes, {
    httpMetadata: {
      contentType:
        file.type ||
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    },
    customMetadata: { projectId, originalFilename: file.name },
  });

  const meta = await db
    .prepare(
      "SELECT key,value FROM app_meta WHERE key IN ('kcs_snapshot','kcs_revision')",
    )
    .all<{ key: string; value: string }>();
  const metaMap = Object.fromEntries(
    (meta.results || []).map((row) => [row.key, row.value]),
  );
  await db
    .prepare(`
    INSERT INTO projects
    (id,title,source_filename,source_object_key,uploaded_at,status,warning,kcs_snapshot,kcs_revision,kcs_scope,updated_at)
    VALUES (?,?,?,?,?,'reviewing','',?,?,?,?)
  `)
    .bind(
      projectId,
      title,
      file.name,
      objectKey,
      uploadedAt,
      metaMap.kcs_snapshot || '',
      metaMap.kcs_revision || '',
      scope.length
        ? scope
            .map((prefix) => `KCS ${prefix.slice(0, 2)} ${prefix.slice(2)}`)
            .join(', ')
        : 'all',
      uploadedAt,
    )
    .run();

  for (const clause of clauses) {
    const clauseId = newId('cl');
    await db
      .prepare(`
      INSERT INTO clauses
      (id,project_id,source_order,label,title,source_type,outline_level,content,edited_content,decision,decision_reason,coverage_confirmed,selected_candidate_id,review_note,reviewed_at,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,NULL,'',0,NULL,'',NULL,?,?)
    `)
      .bind(
        clauseId,
        projectId,
        clause.source_order,
        clause.label,
        clause.title,
        clause.source_type,
        clause.outline_level,
        clause.content,
        clause.content,
        uploadedAt,
        uploadedAt,
      )
      .run();
    if (clause.source_type !== 'heading')
      await matchClause(
        projectId,
        clauseId,
        clause.content,
        clause.match_context,
        scope,
      );
  }

  return projectId;
}

export async function rematchCloudProject(projectId: string) {
  const db = getDatabase();
  const project = await db.prepare('SELECT title,source_filename,kcs_revision FROM projects WHERE id=?')
    .bind(projectId).first<Record<string, unknown>>();
  if (!project) throw new Error('재매칭할 프로젝트를 찾지 못했습니다.');
  const meta = await db.prepare("SELECT value FROM app_meta WHERE key='kcs_revision'")
    .first<{ value: string }>();
  const clauses = await db.prepare(`
    SELECT id,source_order,title,source_type,outline_level,content,selected_candidate_id,coverage_confirmed
    FROM clauses WHERE project_id=? ORDER BY source_order
  `).bind(projectId).all<Record<string, unknown>>();
  const scope = disciplineScope(`${text(project.title)} ${text(project.source_filename)}`);
  const headingPath: string[] = [];
  let matchedCount = 0;
  let reviewableCount = 0;
  for (const clause of clauses.results || []) {
    const sourceType = text(clause.source_type);
    const level = Number(clause.outline_level || 0);
    if (sourceType === 'heading') {
      if (level > 0) {
        headingPath.splice(level - 1);
        headingPath[level - 1] = text(clause.content);
      }
      continue;
    }
    reviewableCount += 1;
    const selectedCandidateId = text(clause.selected_candidate_id);
    const selectedSection = selectedCandidateId
      ? await db.prepare('SELECT section_id FROM project_candidates WHERE id=? AND clause_id=?')
          .bind(selectedCandidateId, text(clause.id)).first<{ section_id: string }>()
      : null;
    const candidateCount = await matchClause(
      projectId,
      text(clause.id),
      text(clause.content),
      headingPath.join(' > '),
      scope,
    );
    if (candidateCount > 0) matchedCount += 1;
    if (selectedSection?.section_id) {
      const replacement = await db.prepare(
        'SELECT id FROM project_candidates WHERE clause_id=? AND section_id=? ORDER BY rank LIMIT 1',
      ).bind(text(clause.id), selectedSection.section_id).first<{ id: string }>();
      await db.prepare('UPDATE clauses SET selected_candidate_id=?,coverage_confirmed=?,updated_at=? WHERE id=?')
        .bind(
          replacement?.id || null,
          replacement ? Number(clause.coverage_confirmed || 0) : 0,
          nowIso(),
          text(clause.id),
        ).run();
    }
  }
  const finishedAt = nowIso();
  const targetRevision = text(meta?.value);
  await db.prepare('UPDATE projects SET kcs_revision=?,kcs_snapshot=?,updated_at=? WHERE id=?')
    .bind(targetRevision, finishedAt, finishedAt, projectId).run();
  return {
    id: newId('rematch'),
    from_revision: text(project.kcs_revision),
    target_revision: targetRevision,
    status: 'completed' as const,
    total_clauses: reviewableCount,
    matched_count: matchedCount,
    material_change_count: 0,
    review_required_count: 0,
    unacknowledged_count: 0,
    error: '',
    started_at: finishedAt,
    finished_at: finishedAt,
    matcher_signature: { domain_equivalents: true, reference_context: true },
  };
}
