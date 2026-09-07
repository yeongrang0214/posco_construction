import { getDatabase, jsonError, optionalSecret } from '@/lib/cloud-runtime';
import { getMeta } from '@/lib/cloud-api';

export async function GET() {
  try {
    const db = getDatabase();
    const meta = await getMeta();
    const count = await db.prepare('SELECT COUNT(*) AS count FROM kcs_documents').first<{ count: number }>();
    return Response.json({
      kcs_available: Number(count?.count || 0) > 0,
      kcs_snapshot: meta.kcs_snapshot || '',
      kcs_revision: meta.kcs_revision || '',
      kcs_document_count: Number(count?.count || 0),
      kcs_usable_document_count: Number(count?.count || 0),
      kcs_unavailable_document_count: 0,
      latest_only: true,
      openai_available: Boolean(optionalSecret('OPENAI_API_KEY')),
      openai_embeddings_available: false,
      openai_embedding_model: '',
      openai_embedding_dimensions: 0,
      openai_rerank_model: '',
    });
  } catch (error) {
    return jsonError(error, 503);
  }
}
