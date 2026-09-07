import { createProjectFromDocx } from '@/lib/cloud-spec';
import { jsonError, newId, nowIso } from '@/lib/cloud-runtime';

export async function POST(request: Request) {
  try {
    const form = await request.formData();
    const files = form.getAll('files').filter((value): value is File => value instanceof File);
    if (!files.length) return Response.json({ detail: '업로드할 DOCX 파일을 선택해 주세요.' }, { status: 400 });
    const jobId = newId('job');
    const createdAt = nowIso();
    const items = [];
    for (let index = 0; index < files.length; index += 1) {
      const file = files[index];
      const itemId = newId('jobitem');
      try {
        const projectId = await createProjectFromDocx(file);
        items.push({ id: itemId, job_id: jobId, position: index + 1, filename: file.name, source_size: file.size, project_id: projectId, status: 'completed', progress: 1, phase: 'completed', error: '', retryable: false, queued_at: createdAt, started_at: createdAt, finished_at: nowIso() });
      } catch (error) {
        items.push({ id: itemId, job_id: jobId, position: index + 1, filename: file.name, source_size: file.size, project_id: null, status: 'failed', progress: 1, phase: 'failed', error: error instanceof Error ? error.message : '업로드 실패', retryable: true, queued_at: createdAt, started_at: createdAt, finished_at: nowIso() });
      }
    }
    const failed = items.filter((item) => item.status === 'failed').length;
    const completed = items.length - failed;
    return Response.json({ job: { id: jobId, status: failed ? (completed ? 'failed' : 'failed') : 'completed', total: items.length, queued: 0, running: 0, completed, failed, progress: 1, error: failed ? `${failed}개 파일 업로드 실패` : '', kcs_snapshot: '', kcs_revision: '', created_at: createdAt, started_at: createdAt, finished_at: nowIso(), items } }, { status: 201 });
  } catch (error) {
    return jsonError(error, 400);
  }
}

export async function GET() {
  return Response.json({ jobs: [] });
}
