Skynet Mac 앱은 내 컴퓨터에서 Skynet을 실행합니다. 앱은 Mac에서 로그인된 코딩 CLI를 통해 모델 호출을 보내므로, 그 호출 비용은 CLI 요금제가 부담합니다. 앱을 호스팅된 Skynet 계정에 연결할 수도 있습니다.

## 요구 사항

- Apple silicon이 탑재된 Mac. Intel 빌드도 있지만 Intel 하드웨어에서 테스트한 사람은 없습니다.
- 실행 중인 Docker Desktop. **Use Rosetta for x86_64/amd64 emulation**이 켜져 있어야 합니다.
- 로그인된 코딩 CLI 하나:
  - Claude Code(`claude`) 2.1.226 이상, 또는
  - Codex(`codex`) 0.160.0 이상.

> [!WARNING]
> Codex는 ChatGPT 로그인을 사용해야 합니다. API 키로 로그인한 경우는 인정되지 않습니다. macOS 키체인에 보관된 로그인도 인정되지 않습니다.

### Codex 설정하기

Codex는 자격 증명을 파일에 보관해야 합니다. 처음 실행하기 전에 다음 단계를 따르세요.

1. `~/.codex/config.toml`을 열고 다음 줄을 추가합니다.

```toml
cli_auth_credentials_store = "file"
```

2. ChatGPT 계정으로 로그인합니다.

```bash
codex login
```

## 앱 설치하기

빌드는 애드혹(ad hoc) 서명되어 있으며 공증(notarization)을 받지 않았습니다. 격리(quarantine) 플래그를 제거할 때까지 macOS가 앱을 차단합니다.

1. `.dmg` 파일을 엽니다.
2. **Skynet**을 **응용 프로그램**으로 끌어다 놓습니다.
3. 터미널에서 다음 명령을 실행합니다.

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. **응용 프로그램**에서 Skynet을 엽니다.

> [!NOTE]
> 처음 실행할 때는 몇 분이 걸립니다. 앱이 로컬 샌드박스 이미지를 빌드하고 데이터베이스를 준비합니다.

## 실행 시 일어나는 일

앱은 다음 단계를 순서대로 수행합니다.

1. 로그인 셸에서 `PATH`를 읽어 Docker와 CLI를 찾을 수 있게 합니다.
2. Docker와 코딩 CLI를 검사합니다.
3. Docker에서 로컬 Postgres 데이터베이스를 시작합니다.
4. `linux/amd64`용 샌드박스 이미지 `skynet-local-sandbox`를 빌드합니다. Docker는 이 이미지를 Rosetta에서 실행합니다.
5. 데이터베이스 스키마를 업데이트합니다.
6. 로컬 API와 웹 앱을 시작하고, 창에 웹 앱을 표시합니다.

### 온보딩 검사

앱은 검사 목록을 보여 줍니다: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services**, **Account**. 검사가 실패하면 앱은 해야 할 일을 알려 주는 화면을 보여 줍니다.

| 화면                               | 해야 할 일                                                             |
| ---------------------------------- | ---------------------------------------------------------------------- |
| **Install Docker Desktop**            | Docker Desktop을 설치합니다.                                           |
| **Start Docker**                    | Docker Desktop을 시작합니다.                                           |
| **Install a coding CLI**                  | Claude Code 또는 Codex를 설치합니다.                                   |
| **Update your coding CLI**              | CLI를 위에 적힌 최소 버전으로 업데이트합니다.                          |
| **Sign in to your CLI**                   | 터미널에서 CLI에 로그인합니다.                                         |
| **Usage limit reached**               | 초기화 시간까지 기다립니다. 초기화 후에 새 작업을 다시 시작할 수 있습니다. |

문제를 고친 후 **Check again**을 클릭합니다. 로그 파일을 보려면 **Open logs**를 클릭합니다. **Usage limit reached** 화면에서는 **Open Skynet anyway**를 클릭할 수 있습니다.

## 로컬 실행

앱에서는 모델 호출이 Mac의 CLI를 거칩니다. 모델 메뉴는 이 모드를 **로컬**로 표시합니다. 힌트에는 "호출 비용은 요금제가 부담하므로 이 모델은 실행에 $0를 더합니다."라고 적혀 있습니다.

앱에서 CLI 이름은 **Claude Code**와 **Codex**입니다.

> [!NOTE]
> 앱은 자신이 시작하는 프로세스의 환경에서 API 키를 제거합니다. 그래서 CLI는 API 키가 아니라 구독을 사용합니다.

### 제안자를 내 CLI에서 실행하기

**제안자를 내 CLI에서 실행** 설정은 제안자 에이전트가 실행되는 위치를 정합니다. 선택지는 **에이전트 엔진 (기본값)**과 **끔 (샌드박스 제안자)**입니다.

**제안자 권한**은 CLI가 접근할 수 있는 범위를 정합니다.

- **작업 공간만**이 기본값입니다. 각 CLI의 자체 샌드박스에 의존합니다.
- **전체 액세스**는 내 사용자 권한으로 CLI를 실행합니다.

> [!WARNING]
> **전체 액세스**에서는 CLI가 내가 접근할 수 있는 모든 파일을 읽고, 바꾸고, 삭제할 수 있으며 네트워크에도 접근할 수 있습니다. Claude의 acceptEdits 모드는 **작업 공간만**에서도 네트워크를 격리하지 않습니다.

### 앱에서 태깅하기

Mac 앱에서 AI 태깅을 하려면 자신의 제공업체 키가 필요합니다. **내 키 사용**에서 모델을 선택하세요. [태거로 데이터 라벨링](/docs/guides/tagger)을 참고하세요.

## Skynet 계정에 연결하기

계정 없이도 앱을 사용할 수 있습니다. 그러려면 **Use without signing in**을 클릭합니다. 호스팅된 계정과 동기화하려면 앱을 연결하세요.

### 이 Mac 연결하기

1. **Sign in to Skynet** 단계에서 앱이 기본 브라우저로 **Link a Mac** 페이지를 엽니다.
2. 브라우저에서 요청하면 Skynet에 로그인합니다.
3. **연결하면 이 Mac에서 다음을 할 수 있습니다:** 아래 목록을 확인합니다.
4. **이 Mac 연결**을 클릭합니다. 페이지에 "Mac 앱으로 돌아가는 중…"이 표시됩니다.

로그인이 실패하거나 취소하면 앱에 **Try again**과 **Use without signing in**이 표시됩니다.

연결하면 Mac에서 다음을 할 수 있습니다.

- 내 이름과 이메일을 봅니다.
- 내 잔액을 봅니다.
- 실행을 계정과 동기화합니다.
- 잔액에서 비용을 내는 호스팅 모델을 사용합니다.
- 연결된 데이터 소스를 사용합니다.
- Skynet을 통해 웹을 검색하고 탐색합니다.

연결에는 안전한 로그인 흐름(OAuth with PKCE)을 사용합니다. 앱은 토큰을 암호화해 Mac에 저장합니다. 연결을 해제하면 Skynet이 토큰을 해지합니다.

### 연결 제한

| 제한                         | 값                                    |
| ---------------------------- | ------------------------------------- |
| 계정당 연결된 Mac            | 10                                    |
| 액세스 토큰 수명             | 1시간, 이후 앱이 갱신합니다           |
| 연결 유휴 시간 제한          | 30일 동안 사용하지 않을 때            |
| 연결 최대 기간               | 365일                                 |

유휴 시간 제한이나 최대 기간이 지나면 Mac을 다시 연결하세요.

### 연결 상태

앱은 다음 상태 중 하나를 표시합니다: **로컬 모드**, **이 Mac에 연결됨**, **로그인하여 동기화**, **연결됨(오프라인)**.

## 로컬 서비스와 데이터

모든 로컬 서비스는 `127.0.0.1`에서만 수신합니다.

| 서비스   | 포트 범위       |
| -------- | --------------- |
| API      | 48100~48119     |
| 웹 앱    | 48120~48139     |
| Postgres | 48140~48159     |

앱은 데이터를 `~/Library/Application Support/Skynet`에 보관합니다. `logs/` 폴더에는 `desktop.log`, `api.log`, `frontend.log`, `migrate.log`, `sandbox-build.log`가 있습니다.

앱을 종료하면 웹 앱, API, Postgres, 모든 작업 컨테이너가 멈춥니다.

### 앱 초기화하기

> [!WARNING]
> 다음 명령은 로컬 실행과 데이터셋을 포함한 모든 로컬 데이터를 삭제합니다.

먼저 앱을 종료합니다. 그런 다음 실행합니다.

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## 다음 단계

- [실행을 제출합니다](/docs/guides/submit).
- 호스팅 모델의 [결제](/docs/concepts/billing)에 대해 읽어 보세요.
- [보안](/docs/security)을 읽어 보세요.
