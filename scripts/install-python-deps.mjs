import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { spawnSync } from 'node:child_process';

const root = process.cwd();
const python = process.platform === 'win32'
  ? join(root, '.venv', 'Scripts', 'python.exe')
  : join(root, '.venv', 'bin', 'python');

if (!existsSync(python)) {
  throw new Error('먼저 python -m venv .venv 명령으로 가상환경을 만드세요.');
}
if (process.platform !== 'win32' || process.arch !== 'x64') {
  throw new Error('현재 초기화 스크립트는 이 프로젝트의 Windows 64비트 환경을 대상으로 합니다.');
}

const version = spawnSync(python, ['-c', 'import sys; print(f"{sys.version_info.major}{sys.version_info.minor}")'], {
  encoding: 'utf8',
});
if (version.status !== 0) throw new Error('가상환경의 Python 버전을 확인하지 못했습니다.');
const cpTag = `cp${version.stdout.trim()}`;
const pythonMinor = Number(version.stdout.trim().slice(1));
const packages = [
  ['annotated-doc', '0.0.5'],
  ['annotated-types', '0.8.0'],
  ['anyio', '4.15.0'],
  ['certifi', '2026.7.22'],
  ['click', '8.5.0'],
  ['colorama', '0.4.6'],
  ['et-xmlfile', '2.0.0'],
  ['fastapi', '0.141.1'],
  ['h11', '0.16.0'],
  ['httpcore', '1.0.9'],
  ['httpx', '0.28.1'],
  ['idna', '3.19'],
  ['iniconfig', '2.3.0'],
  ['lxml', '6.1.3'],
  ['openpyxl', '3.1.5'],
  ['packaging', '26.3'],
  ['pluggy', '1.6.0'],
  ['pydantic-core', '2.46.5'],
  ['pydantic', '2.13.5'],
  ['pygments', '2.21.0'],
  ['pytest', '8.4.2'],
  ['python-docx', '1.2.0'],
  ['python-dotenv', '1.2.3'],
  ['python-multipart', '0.0.32'],
  ['starlette', '1.6.0'],
  ['typing-extensions', '4.16.0'],
  ['typing-inspection', '0.4.4'],
  ['uvicorn', '0.52.4'],
];

const wheelDirectory = join(root, 'data', 'wheels');
mkdirSync(wheelDirectory, { recursive: true });

function selectWheel(urls) {
  const wheels = urls.filter((item) => item.packagetype === 'bdist_wheel');
  const compatibleAbi3 = wheels
    .map((item) => ({ item, match: item.filename.match(/-cp3(\d{1,2})-abi3-win_amd64\.whl$/) }))
    .filter(({ match }) => match && Number(match[1]) <= pythonMinor)
    .sort((a, b) => Number(b.match[1]) - Number(a.match[1]));

  return wheels.find((item) => item.filename.includes(`${cpTag}-${cpTag}-win_amd64.whl`))
    ?? compatibleAbi3[0]?.item
    ?? wheels.find((item) => item.filename.endsWith('-py3-none-any.whl'))
    ?? wheels.find((item) => item.filename.endsWith('-py2.py3-none-any.whl'));
}

async function downloadWheel(name, packageVersion) {
  const metadataResponse = await fetch(`https://pypi.org/pypi/${name}/${packageVersion}/json`);
  if (!metadataResponse.ok) throw new Error(`${name} ${packageVersion} 메타데이터를 받지 못했습니다.`);
  const metadata = await metadataResponse.json();
  const wheel = selectWheel(metadata.urls ?? []);
  if (!wheel) throw new Error(`${name} ${packageVersion}에 맞는 ${cpTag} Windows 휠이 없습니다.`);
  const target = join(wheelDirectory, basename(wheel.filename));
  if (existsSync(target) && readFileSync(target).length === wheel.size) return target;
  const wheelResponse = await fetch(wheel.url);
  if (!wheelResponse.ok) throw new Error(`${wheel.filename}을 받지 못했습니다.`);
  writeFileSync(target, Buffer.from(await wheelResponse.arrayBuffer()));
  return target;
}

console.log(`Python ${cpTag}용 패키지를 준비합니다.`);
const wheelResults = await Promise.allSettled(
  packages.map(([name, packageVersion]) => downloadWheel(name, packageVersion)),
);
const failures = wheelResults.filter((result) => result.status === 'rejected');
if (failures.length > 0) {
  throw new Error(failures.map((result) => result.reason?.message ?? String(result.reason)).join('\n'));
}
const wheels = wheelResults.map((result) => result.value);
const install = spawnSync(
  python,
  ['-m', 'pip', 'install', '--no-index', '--no-deps', '--disable-pip-version-check', ...wheels],
  { cwd: root, stdio: 'inherit' },
);
if (install.status !== 0) process.exit(install.status ?? 1);

const check = spawnSync(python, ['-m', 'pip', 'check'], { cwd: root, stdio: 'inherit' });
process.exit(check.status ?? 1);
