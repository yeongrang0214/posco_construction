import { getDatabase, jsonError } from '@/lib/cloud-runtime';
import { projectSummary } from '@/lib/cloud-api';

export async function GET(request: Request) {
  try {
    const db = getDatabase();
    const url = new URL(request.url);
    const search = (url.searchParams.get('search') || '').trim();
    const archiveStatus = url.searchParams.get('archive_status') || 'active';
    const reviewStatus = (url.searchParams.get('review_status') || '').trim();
    const limit = Math.min(100, Math.max(1, Number(url.searchParams.get('limit') || 50)));
    const clauses: string[] = [];
    const params: unknown[] = [];
    if (archiveStatus === 'archived') clauses.push('archived_at IS NOT NULL');
    else clauses.push('archived_at IS NULL');
    if (search) { clauses.push('(title LIKE ? OR source_filename LIKE ?)'); params.push(`%${search}%`, `%${search}%`); }
    if (reviewStatus) { clauses.push('status = ?'); params.push(reviewStatus); }
    const where = clauses.length ? `WHERE ${clauses.join(' AND ')}` : '';
    const rows = await db.prepare(`SELECT id FROM projects ${where} ORDER BY uploaded_at DESC LIMIT ?`).bind(...params, limit).all<{ id: string }>();
    const projects = [];
    for (const row of rows.results || []) {
      const summary = await projectSummary(row.id);
      if (summary) projects.push(summary);
    }
    return Response.json({ projects, total: projects.length, limit, has_more: false, next_cursor: null });
  } catch (error) {
    return jsonError(error, 503);
  }
}
