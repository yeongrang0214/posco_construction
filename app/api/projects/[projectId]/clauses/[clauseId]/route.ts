import { clauseDetail, projectSummary, saveClauseDecision } from '@/lib/cloud-api';
import { jsonError } from '@/lib/cloud-runtime';

export async function GET(_request: Request, context: { params: Promise<{ projectId: string; clauseId: string }> }) {
  try {
    const { projectId, clauseId } = await context.params;
    const clause = await clauseDetail(projectId, clauseId);
    if (!clause) return Response.json({ detail: '조항을 찾지 못했습니다.' }, { status: 404 });
    return Response.json({ clause });
  } catch (error) {
    return jsonError(error);
  }
}

export async function PATCH(request: Request, context: { params: Promise<{ projectId: string; clauseId: string }> }) {
  try {
    const { projectId, clauseId } = await context.params;
    const payload = await request.json() as Record<string, unknown>;
    const clause = await saveClauseDecision(projectId, clauseId, payload);
    if (!clause) return Response.json({ detail: '조항을 찾지 못했습니다.' }, { status: 404 });
    return Response.json({ clause, project: await projectSummary(projectId) });
  } catch (error) {
    return jsonError(error, 400);
  }
}
