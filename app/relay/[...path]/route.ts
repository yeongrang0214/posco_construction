const RAILWAY_ORIGIN = 'https://posco-construction-api-production.up.railway.app';

type RouteContext = { params: Promise<{ path: string[] }> };

async function relay(request: Request, context: RouteContext) {
  const { path } = await context.params;
  const incomingUrl = new URL(request.url);
  const targetUrl = `${RAILWAY_ORIGIN}/${path.map(encodeURIComponent).join('/')}${incomingUrl.search}`;
  const headers = new Headers(request.headers);

  headers.delete('host');
  headers.delete('origin');
  headers.delete('referer');
  headers.delete('content-length');

  const upstream = await fetch(targetUrl, {
    method: request.method,
    headers,
    body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
    redirect: 'manual',
  });
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.set('cache-control', 'no-store');

  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = relay;
export const HEAD = relay;
export const POST = relay;
export const PUT = relay;
export const PATCH = relay;
export const DELETE = relay;
export const OPTIONS = relay;
