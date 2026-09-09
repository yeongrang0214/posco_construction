import { jsonError } from '@/lib/cloud-runtime';
import { rematchCloudProject } from '@/lib/cloud-spec';

type RouteContext = { params: Promise<{ projectId: string }> };

export async function POST(_request: Request, context: RouteContext) {
  try {
    const { projectId } = await context.params;
    return Response.json({ run: await rematchCloudProject(projectId) });
  } catch (error) {
    return jsonError(error, 400);
  }
}
