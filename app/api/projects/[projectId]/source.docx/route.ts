import { getBucket, getDatabase, jsonError } from '@/lib/cloud-runtime';

export async function GET(_request: Request, context: { params: Promise<{ projectId: string }> }) {
  try {
    const { projectId } = await context.params;
    const db = getDatabase();
    const project = await db.prepare('SELECT source_filename, source_object_key FROM projects WHERE id=?').bind(projectId).first<{ source_filename: string; source_object_key: string }>();
    if (!project) return Response.json({ detail: '프로젝트를 찾지 못했습니다.' }, { status: 404 });
    const object = await getBucket().get(project.source_object_key);
    if (!object) return Response.json({ detail: '업로드 원본을 찾지 못했습니다.' }, { status: 404 });
    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Content-Type', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
    headers.set('Content-Disposition', `inline; filename*=UTF-8''${encodeURIComponent(project.source_filename)}`);
    return new Response(object.body, { headers });
  } catch (error) {
    return jsonError(error);
  }
}
