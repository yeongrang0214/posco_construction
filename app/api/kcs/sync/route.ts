import { jsonError } from '@/lib/cloud-runtime';
import { syncKcsCloud } from '@/lib/cloud-spec';

export async function POST() {
  try {
    return Response.json(await syncKcsCloud());
  } catch (error) {
    return jsonError(error, 400);
  }
}
