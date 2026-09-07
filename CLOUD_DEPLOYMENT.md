# 클라우드 배포 전환

이 브랜치는 로컬 Python/FastAPI, `127.0.0.1:8000`, Windows 경로, 로컬 SQLite/OneDrive를 배포 런타임에서 사용하지 않습니다.

## 구조

- UI: 기존 vinext/React 화면 유지
- API: 동일 도메인의 `app/api/**` Cloudflare Workers Route Handlers
- 영구 DB: Cloudflare D1
- 업로드 원본: Cloudflare R2
- DOCX 추출: Workers에서 `word/document.xml`을 읽어 문단과 표 행 추출
- KCS: 국가건설기준센터 OpenAPI를 서버에서 호출하여 D1에 최신 KCS 문서/조항 인덱스 저장
- KCS 후보: D1 후보군 + 토큰 유사도/수치 일치 보정으로 Top 3 생성
- 비밀정보: `KCSC_API_KEY`, 선택적 `OPENAI_API_KEY`는 서버 환경변수/secret만 사용

기존 `server/*.py`는 로컬 레거시 구현을 보존하기 위해 삭제하지 않았지만 배포된 웹사이트의 업로드/조회/판정 흐름에서는 호출하지 않습니다.

## 최초 1회 필요한 Cloudflare/OpenAI Sites 설정

`.openai/hosting.json`은 D1을 `DB`, R2를 `BUCKET`으로 선언합니다. ChatGPT Sites 배포 시 두 저장 리소스가 연결되고, `drizzle/0001_cloud.sql`이 D1에 적용됩니다. 일반 Cloudflare 배포에서는 같은 스키마인 `migrations/0001_cloud.sql`을 사용합니다.

1. 서버 비밀값 `KCSC_API_KEY`를 설정합니다. 브라우저에 노출되는 `NEXT_PUBLIC_*` 변수로 넣지 마세요.
2. GPT 정밀분석을 다시 연결할 경우에만 `OPENAI_API_KEY`를 서버 secret으로 추가합니다. 현재 클라우드 1차 전환에서는 기본 KCS 매칭이 OpenAI 키 없이 동작합니다.
3. 배포 후 화면의 `KCS 갱신`을 한 번 실행하여 D1 KCS 인덱스를 채웁니다.

코드는 D1/R2의 고정 바인딩 이름에 의존하지 않고, `DB`/`D1`, `BUCKET`/`R2` 또는 호스팅 환경이 주입한 호환 바인딩을 탐색합니다.

## 지원 범위

### 클라우드 전환 완료

- DOCX 실제 업로드
- DOCX 문단/표 행 추출
- R2 원본 영구 저장 및 원본 보기
- D1 프로젝트/조항/판정 영구 저장
- 프로젝트 목록/상세/일괄검토
- 남김/삭제/보류 저장
- KCSC OpenAPI → D1 KCS 인덱스 갱신
- 기본 Top 3 KCS 후보 생성
- 프로젝트 보관/복원

### 후속 이식이 필요한 기존 고급 기능

Python 구현의 GPT 정밀분석/전체포괄검사, 제출-승인 스냅샷, XLSX/DOCX 결과물 생성, 서명 ZIP 백업, 품질평가, KCS 영향 재매칭은 로컬 파일·SQLite 트랜잭션과 결합도가 높아 이번 1차 서버리스 이식에서 가짜 구현하지 않았습니다. 관련 UI가 호출하는 엔드포인트는 순차적으로 Workers/D1 기반으로 이식해야 합니다.

구형 `.doc`은 서버리스 환경에서 Word COM/PowerShell 변환을 사용할 수 없으므로, 현재는 `.docx`를 정식 클라우드 입력 형식으로 사용합니다.
