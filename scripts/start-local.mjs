import { existsSync, readFileSync, unlinkSync } from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
import { join } from 'node:path';

const root = process.cwd();
const pythonCandidates = process.platform === 'win32'
  ? [process.env.SPEC_MANAGER_PYTHON, join(root, '.venv', 'Scripts', 'python.exe'), 'python']
  : [process.env.SPEC_MANAGER_PYTHON, join(root, '.venv', 'bin', 'python'), 'python3'];
const python = pythonCandidates.find(
  (candidate) => candidate && (candidate === 'python' || candidate === 'python3' || existsSync(candidate)),
);

if (!python) {
  throw new Error('Python 실행 파일을 찾지 못했습니다. SPEC_MANAGER_PYTHON 환경변수에 Python 경로를 지정하세요.');
}

const vinextCli = join(root, 'node_modules', 'vinext', 'dist', 'cli.js');
if (!existsSync(vinextCli)) {
  throw new Error('프런트엔드 패키지를 찾지 못했습니다. npm install을 먼저 실행하세요.');
}

const vinextLock = join(root, '.vinext', 'dev', 'lock.json');
if (existsSync(vinextLock)) {
  try {
    const { pid } = JSON.parse(readFileSync(vinextLock, 'utf8'));
    process.kill(Number(pid), 0);
  } catch {
    unlinkSync(vinextLock);
  }
}

const children = [
  spawn(
    python,
    ['-m', 'uvicorn', 'server.app:app', '--host', '127.0.0.1', '--port', '8000'],
    { cwd: root, env: process.env, stdio: 'inherit' },
  ),
  spawn(process.execPath, [vinextCli, 'dev'], { cwd: root, stdio: 'inherit' }),
];

console.log('\n시방서 AI 분석 시스템');
console.log('화면: http://localhost:3000');
console.log('분석 API: http://127.0.0.1:8000\n');

let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (!child.pid || child.killed) continue;
    if (process.platform === 'win32') {
      spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    } else {
      child.kill('SIGTERM');
    }
  }
  process.exit(code);
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));
for (const child of children) {
  child.on('error', (error) => {
    console.error(error.message);
    stop(1);
  });
  child.on('exit', (code) => {
    if (!stopping) stop(code ?? 1);
  });
}
