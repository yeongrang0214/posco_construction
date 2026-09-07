import { getDatabase, jsonError, nowIso } from '@/lib/cloud-runtime';
import { projectSummary } from '@/lib/cloud-api';

export async function PUT(request: Request, context: { params: Promise<{ projectId: string }> }) {
  try {
    const { projectId } = await context.params;
    const payload = await request.json() as { archived?: boolean };
    const db = getDatabase();
    await db.prepare('UPDATE projects SET archived_at=?, updated_at=? WHERE id=?').bind(payload.archived ? nowIso() : null, nowIso(), projectId).run();
    const project = await projectSummary(projectId);
    if (!project) return Response.json({ detail: '프로젝트를 찾지 못했습니다.' }, { status: 404 });
    return Response.json({ project });
  } catch (error) {
    return jsonError(error, 400);
  }
}
