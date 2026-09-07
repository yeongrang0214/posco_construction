import { clauseDetail } from '@/lib/cloud-api';
import { getDatabase, jsonError } from '@/lib/cloud-runtime';

export async function GET(request: Request, context: { params: Promise<{ projectId: string }> }) {
  try {
    const { projectId } = await context.params;
    const url = new URL(request.url);
    const offset = Math.max(0, Number(url.searchParams.get('offset') || 0));
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit') || 60)));
    const status = url.searchParams.get('status') || 'all';
    const q = (url.searchParams.get('q') || '').trim();
    const conditions = ['project_id = ?'];
    const params: unknown[] = [projectId];
    if (status === 'unreviewed') conditions.push("decision IS NULL AND source_type != 'heading'");
    else if (['keep', 'delete', 'hold'].includes(status)) { conditions.push('decision = ?'); params.push(status); }
    if (q) { conditions.push('(content LIKE ? OR title LIKE ? OR label LIKE ?)'); params.push(`%${q}%`, `%${q}%`, `%${q}%`); }
    const db = getDatabase();
    const count = await db.prepare(`SELECT COUNT(*) AS count FROM clauses WHERE ${conditions.join(' AND ')}`).bind(...params).first<{ count: number }>();
    const rows = await db.prepare(`SELECT id FROM clauses WHERE ${conditions.join(' AND ')} ORDER BY source_order LIMIT ? OFFSET ?`).bind(...params, limit, offset).all<{ id: string }>();
    const items = [];
    for (const row of rows.results || []) {
      const detail = await clauseDetail(projectId, row.id);
      if (!detail) continue;
      const previous = await db.prepare('SELECT source_order,label,title,content,source_type FROM clauses WHERE project_id=? AND source_order<? ORDER BY source_order DESC LIMIT 1').bind(projectId, detail.source_order).first();
      const next = await db.prepare('SELECT source_order,label,title,content,source_type FROM clauses WHERE project_id=? AND source_order>? ORDER BY source_order ASC LIMIT 1').bind(projectId, detail.source_order).first();
      items.push({ ...detail, excluded_candidate_count: 0, source_context: { path: detail.label || detail.title || String(detail.source_order), previous, next } });
    }
    const total = Number(count?.count || 0);
    return Response.json({ items, total, offset, limit, has_more: offset + items.length < total });
  } catch (error) {
    return jsonError(error);
  }
}
