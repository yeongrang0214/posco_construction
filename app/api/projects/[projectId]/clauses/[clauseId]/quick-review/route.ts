import { clauseDetail, projectSummary, saveClauseDecision } from '@/lib/cloud-api';
import { jsonError } from '@/lib/cloud-runtime';

export async function PATCH(request: Request, context: { params: Promise<{ projectId: string; clauseId: string }> }) {
  try {
    const { projectId, clauseId } = await context.params;
    const current = await clauseDetail(projectId, clauseId);
    if (!current) return Response.json({ detail: '조항을 찾지 못했습니다.' }, { status: 404 });
    const patch = await request.json() as Record<string, unknown>;
    const payload = {
      decision: patch.decision !== undefined ? patch.decision : current.decision,
      decision_reason: patch.decision_reason !== undefined ? patch.decision_reason : current.decision_reason,
      coverage_confirmed: patch.coverage_confirmed !== undefined ? patch.coverage_confirmed : current.coverage_confirmed,
      selected_candidate_id: patch.selected_candidate_id !== undefined ? patch.selected_candidate_id : current.selected_candidate_id,
      edited_content: current.edited_content,
      review_note: current.review_note,
    };
    const clause = await saveClauseDecision(projectId, clauseId, payload);
    return Response.json({ clause, project: await projectSummary(projectId) });
  } catch (error) {
    return jsonError(error, 400);
  }
}
