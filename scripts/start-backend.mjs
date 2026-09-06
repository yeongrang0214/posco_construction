import { existsSync } from 'node:fs';
import { join } from 'node:path';
import { spawn } from 'node:child_process';

const root = process.cwd();
const candidates = process.platform === 'win32'
  ? [process.env.SPEC_MANAGER_PYTHON, join(root, '.venv', 'Scripts', 'python.exe'), 'python']
  : [process.env.SPEC_MANAGER_PYTHON, join(root, '.venv', 'bin', 'python'), 'python3'];

const python = candidates.find((candidate) => candidate && (candidate === 'python' || candidate === 'python3' || existsSync(candidate)));
if (!python) {
  throw new Error('Python 실행 파일을 찾지 못했습니다. SPEC_MANAGER_PYTHON 환경변수에 Python 경로를 지정하세요.');
}

const child = spawn(
  python,
  ['-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', '8000'],
  { cwd: root, env: process.env, stdio: 'inherit' },
);

child.on('exit', (code) => process.exit(code ?? 1));
