import { env } from 'cloudflare:workers';

type AnyEnv = Record<string, unknown>;

function values() {
  return Object.values(env as unknown as AnyEnv);
}

export function getDatabase(): D1Database {
  const configured = (env as unknown as AnyEnv).DB || (env as unknown as AnyEnv).D1;
  if (configured && typeof (configured as D1Database).prepare === 'function') {
    return configured as D1Database;
  }
  const discovered = values().find((value) => value && typeof (value as D1Database).prepare === 'function');
  if (discovered) return discovered as D1Database;
  throw new Error('Cloudflare D1 데이터베이스가 연결되지 않았습니다. ChatGPT Site의 저장소 설정에서 D1을 연결해 주세요.');
}

export function getBucket(): R2Bucket {
  const configured = (env as unknown as AnyEnv).BUCKET || (env as unknown as AnyEnv).R2;
  if (configured && typeof (configured as R2Bucket).put === 'function' && typeof (configured as R2Bucket).get === 'function') {
    return configured as R2Bucket;
  }
  const discovered = values().find(
    (value) => value && typeof (value as R2Bucket).put === 'function' && typeof (value as R2Bucket).get === 'function',
  );
  if (discovered) return discovered as R2Bucket;
  throw new Error('Cloudflare R2 버킷이 연결되지 않았습니다. ChatGPT Site의 저장소 설정에서 R2를 연결해 주세요.');
}

export function optionalSecret(name: string): string {
  const value = (env as unknown as AnyEnv)[name];
  return typeof value === 'string' ? value.trim() : '';
}

export function jsonError(error: unknown, status = 500) {
  const message = error instanceof Error ? error.message : '요청을 처리하지 못했습니다.';
  return Response.json({ detail: message }, { status });
}

export function nowIso() {
  return new Date().toISOString();
}

export function newId(prefix: string) {
  return `${prefix}_${crypto.randomUUID()}`;
}
