Skynet HTTP API를 사용하면 스크립트와 도구가 웹 앱과 같은 일을 할 수 있습니다. 실행을 시작하고, 지켜보고, 결과를 읽을 수 있습니다. 이 페이지는 요청에 서명하는 방법, 전체 레퍼런스의 위치, 그리고 완전한 예제 하나를 보여 줍니다.

> [!TIP]
> Claude Code, Codex, Cursor 같은 AI 에이전트를 사용하시나요? 그렇다면 대신 [MCP 서버](/docs/mcp)로 연결하세요. 그러면 에이전트가 실행마다 지출 한도를 두고 실행을 시작하고 지켜봅니다.

## 기본 URL

모든 요청을 다음 주소로 보냅니다.

```text
https://api.skynetml.com
```

## 인증

API는 API 토큰을 사용합니다.

1. Skynet에서 **설정**을 열고 **API** 탭을 엽니다.
2. **토큰 생성**을 클릭합니다.
3. 토큰을 바로 복사합니다. Skynet은 전체 토큰을 한 번만 보여 줍니다.

모든 요청의 `Authorization` 헤더에 토큰을 넣어 보냅니다.

```http
Authorization: Bearer skyd_...
```

- 토큰은 `skyd_`로 시작합니다.
- 사용자마다 유효한 토큰은 최대 하나입니다. 새 토큰을 만들면 이전 토큰은 더 이상 작동하지 않습니다.
- 토큰을 중지하려면 **토큰 해지**를 클릭합니다.

> [!WARNING]
> 토큰은 비밀번호처럼 다루세요. 소스 코드와 로그에 넣지 마세요. Skynet이 토큰을 저장하는 방식은 [보안](/docs/security)을 읽어 보세요.

## 대화형 레퍼런스

전체 레퍼런스는 [api.skynetml.com/scalar](https://api.skynetml.com/scalar)에 있습니다. 각 공개 엔드포인트와 필드, 응답을 나열합니다. 이 페이지에서 테스트 요청을 보낼 수도 있습니다.

레퍼런스는 `https://api.skynetml.com/openapi.public.json`의 OpenAPI 문서를 읽습니다. 이 파일로 원하는 언어의 클라이언트를 생성하세요.

## 주요 엔드포인트

| 메서드와 경로                                   | 하는 일                                                      |
| ----------------------------------------------- | ------------------------------------------------------------ |
| `GET /models`                                   | 사용할 수 있는 모델을 나열합니다.                            |
| `GET /optimizers`                               | 설정에 맞는 옵티마이저를 나열합니다.                         |
| `GET /blackbox/engines`                         | 네이티브 엔진과 각 엔진의 사용 가능 여부를 나열합니다.       |
| `POST /blackbox/run`                            | GitHub 리포지토리에서 실행을 시작합니다.                     |
| `GET /optimizations`                            | 내 실행을 나열합니다.                                        |
| `GET /optimizations/{optimization_id}`          | 실행 하나의 상태, 진행 상황, 로그, 결과를 반환합니다.        |
| `GET /optimizations/{optimization_id}/artifact` | 실행이 찾은 최고 버전을 반환합니다.                          |
| `POST /optimizations/{optimization_id}/cancel`  | 실행을 중지합니다.                                           |
| `POST /serve/{optimization_id}`                 | 새 입력에서 최고 버전을 실행합니다.                          |
| `GET /health`                                   | 서비스가 작동 중인지 보여 줍니다.                            |

## 실행 시작하기

Skynet은 Optimize Anything입니다. 모든 실행은 리포지토리를 최적화합니다. `POST /blackbox/run`은 제출 마법사와 같은 설정을 받습니다. 예제는 지원 메시지를 라벨로 분류하는 리포지토리를 개선합니다.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

중요한 필드:

- `target.repo.repository`는 `owner/name` 형식의 GitHub 리포지토리입니다. 먼저 **설정 → 커넥터**에서 GitHub를 연결하세요.
- `target.repo.editable_paths`는 옵티마이저가 변경할 수 있는 파일과 폴더의 목록입니다. `"."`는 리포지토리 전체를 뜻합니다.
- `target.repo.commit`은 선택 사항입니다. 40자 SHA입니다. 없으면 Skynet은 브랜치의 최신 커밋을 사용합니다.
- `target.repo.secrets`는 선택 사항입니다. 각 시크릿에는 `name`과 `value`가 있습니다. 시크릿은 환경 변수로 코드와 채점기에 전달됩니다.
- `scorer.metric_code`는 `score(repo_path, case)`를 정의합니다. `scorer.model`은 코드와 채점기가 모델 게이트웨이를 통해 호출할 수 있는 모델입니다.
- `strategy.mode`는 `auto`(기본값) 또는 `single`입니다. `single`이면 `strategy.engine`을 `GET /blackbox/engines`가 반환한 엔진 이름 하나로 설정합니다.
- `max_cost_cents`는 이 실행의 지출 한도이며 단위는 미국 센트입니다.
- `is_private`의 기본값은 `false`입니다. 실행을 비공개로 유지하려면 `true`로 설정합니다.

`metric_code`의 규칙은 [지표와 채점기](/docs/concepts/scorers)를 읽어 보세요.

### 응답

| 상태  | 의미                                                                                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `201` | Skynet이 실행을 대기열에 넣었습니다. 본문에 `optimization_id`와 `status`가 있습니다.                                                  |
| `400` | 대상이 리포지토리가 아니거나 채점기를 불러올 수 없습니다. |
| `202` | 설정 검사가 아직 진행 중입니다. `Retry-After` 헤더에 있는 초만큼 기다린 다음, 같은 본문을 같은 `Idempotency-Key`로 다시 보냅니다.     |
| `402` | 잔액이 너무 적습니다.                                                                                                                 |
| `409` | 할당량에 도달했거나 설정 검사가 최신 상태가 아닙니다.                                                                                 |
| `422` | 요청이 옵티마이저와 맞지 않습니다. |

> [!NOTE]
> 항상 `Idempotency-Key` 헤더를 보내세요. 같은 키로 같은 요청을 다시 보내면 Skynet은 첫 번째 실행을 반환합니다. 두 번째 실행을 시작하지 않습니다.

## 실행 지켜보기

상태가 최종 상태가 될 때까지 실행을 폴링합니다.

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` 필드는 다음 중 하나입니다.

| 상태                               | 최종 여부 |
| ---------------------------------- | --------- |
| `pending`, `validating`, `running` | 아니요    |
| `paused`                           | 아니요    |
| `success`                          | 예        |
| `failed`, `cancelled`, `stopped`   | 예        |

10~30초마다 폴링하세요. 실행은 몇 분에서 몇 시간까지 걸릴 수 있습니다.

실행 응답의 모든 필드는 [API 레퍼런스](https://api.skynetml.com/scalar)에 나와 있습니다.

## 결과 사용하기

상태가 `success`이면 최고 버전을 가져옵니다.

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

새 입력에서 최고 버전을 실행하려면 `Idempotency-Key` 헤더와 함께 `POST /serve/{optimization_id}`를 보냅니다. 리포지토리에는 `def serve(input)`이 있는 `serve.py` 파일이 있어야 합니다. 요청, 응답, 각 점수의 의미는 [결과 읽기](/docs/guides/results)를 읽어 보세요.

## 제한

API는 웹 앱과 같은 할당량, 요청 속도 제한, 청구 방식을 사용합니다. [제한](/docs/limits)과 [예산과 결제](/docs/concepts/billing)를 읽어 보세요.
