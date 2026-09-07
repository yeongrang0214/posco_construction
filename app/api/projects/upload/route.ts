import { createProjectFromDocx } from '@/lib/cloud-spec';
import { jsonError } from '@/lib/cloud-runtime';
import { clauseSummaries, projectSummary } from '@/lib/cloud-api';

export async function POST(request: Request) {
  try {
    const form = await request.formData();
    const file = form.get('file');
    if (!(file instanceof File)) return Response.json({ detail: 'DOCX 파일을 선택해 주세요.' }, { status: 400 });
    const projectId = await createProjectFromDocx(file);
    const project = await projectSummary(projectId);
    const clauses = await clauseSummaries(projectId);
    return Response.json({ project, clauses }, { status: 201 });
  } catch (error) {
    return jsonError(error, 400);
  }
}
