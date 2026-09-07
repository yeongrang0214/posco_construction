import { clauseSummaries, projectSummary } from '@/lib/cloud-api';
import { jsonError } from '@/lib/cloud-runtime';

export async function GET(_request: Request, context: { params: Promise<{ projectId: string }> }) {
  try {
    const { projectId } = await context.params;
    const project = await projectSummary(projectId);
    if (!project) return Response.json({ detail: '프로젝트를 찾지 못했습니다.' }, { status: 404 });
    return Response.json({ project, clauses: await clauseSummaries(projectId) });
  } catch (error) {
    return jsonError(error);
  }
}
