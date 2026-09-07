import { getDatabase, jsonError } from '@/lib/cloud-runtime';

export async function GET(_request: Request, context: { params: Promise<{ projectId: string }> }) {
  try {
    const { projectId } = await context.params;
    const db = getDatabase();
    const result = await db.prepare('SELECT id,source_order,label,title,content,source_type,decision FROM clauses WHERE project_id=? ORDER BY source_order').bind(projectId).all();
    return Response.json({ items: result.results || [] });
  } catch (error) {
    return jsonError(error);
  }
}
