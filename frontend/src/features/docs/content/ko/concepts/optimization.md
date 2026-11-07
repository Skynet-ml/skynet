Skynet은 Optimize Anything입니다. 모든 실행은 리포지토리를 최적화합니다. 엔진이 리포지토리의 새 버전을 제안하고, 채점기가 각 버전을 채점하며, Skynet이 가장 좋은 버전을 유지합니다.

## 공통 계약

모든 실행에는 리포지토리와 채점기가 있습니다. 채점기는 Python 함수입니다. 채점기는 리포지토리의 한 버전 경로와 케이스 하나를 받습니다.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path`는 설정 명령이 실행된 후의 리포지토리 한 버전의 폴더입니다.
- `case`는 dict입니다. `case["input"]`는 케이스 하나의 입력 필드를 담습니다. `case["expected"]`는 기댓값을 담으며, 케이스에 기댓값이 없으면 `None`입니다.
- `score`는 쌍을 반환합니다. 숫자 하나와 `"feedback"` 문자열이 있는 dict 하나입니다.

리포지토리 루트에는 `def serve(input)`이 있는 `serve.py` 파일이 있습니다. 채점기는 이 함수를 호출할 수 있습니다. Skynet은 실행 후 이 함수로 가장 좋은 버전을 서빙합니다.

> [!NOTE]
> 버전을 작성하는 코딩 에이전트는 채점기 소스, 기댓값, 실행 시크릿을 볼 수 없습니다.

전체 채점기 계약은 [지표와 채점기](/docs/concepts/scorers)를 읽어 보세요.

## 코드에서의 모델 호출

리포지토리 코드와 채점기는 Skynet의 모델 게이트웨이를 통해 모델을 호출할 수 있습니다. 샌드박스는 OpenAI 호환 엔드포인트를 환경 변수로 제공합니다.

| 변수                 | 담는 값                                                  |
| -------------------- | -------------------------------------------------------- |
| `OPENAI_BASE_URL`    | 게이트웨이 URL입니다. `APP_GATEWAY_URL`도 같은 값입니다. |
| `APP_API_KEY`        | 이 실행 전용 토큰입니다. 제공업체 키가 아닙니다.         |
| `APP_MODEL`          | 보낼 모델 이름입니다. 실행의 채점기 모델입니다.          |
| `ANTHROPIC_BASE_URL` | Anthropic SDK용 게이트웨이 URL입니다.                    |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Skynet 키로 하는 호출은 실행 예산에 대해 계량됩니다.
- 내 키(BYOK)를 쓰면 호출은 내 제공업체 키로 갑니다. Skynet은 모델 요금을 청구하지 않습니다.
- 게이트웨이는 다른 모델에 대한 요청을 거부합니다.
- 실행 시크릿은 환경 변수로 코드와 채점기에 전달됩니다. 변수 이름은 시크릿 이름입니다. Skynet은 출력에서 시크릿 값을 숨깁니다.

샌드박스에는 다른 네트워크 접근이 없습니다.

## 엔진

모든 엔진은 리포지토리에서 작동합니다.

- **Auto**는 기본값입니다. 여러 엔진을 시도한 다음, 가장 좋은 버전에서 GEPA를 이어 갑니다.
- **GEPA**는 리플렉션으로 버전을 진화시키고 파레토 프런트를 유지합니다.
- **Best-of-N**은 리플렉션 모델에 독립적인 제안을 요청하고 가장 좋은 것을 유지합니다.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler**, **ShinkaEvolve**는 코딩 에이전트로 버전을 제안합니다.

엔진은 허용한 경로의 파일만 변경합니다. 각 엔진과 설정은 [엔진](/docs/concepts/engines)을 읽어 보세요.

## Auto

**Auto**는 모든 실행에서 마법사의 기본 선택입니다.

1. Auto는 다섯 엔진으로 탐색합니다. GEPA, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve입니다.
2. 이 엔진들이 찾은 가장 좋은 버전을 고릅니다.
3. 예산이 다 떨어질 때까지 그 버전에서 GEPA를 이어 갑니다.

Auto는 채점기의 피드백 텍스트를 읽습니다.

## 시작 버전의 출처

| 상황                                 | 사용할 것                                                                      |
| ------------------------------------ | ------------------------------------------------------------------------------ |
| GitHub 리포지토리에 코드가 있음      | **저장소 사용**. 고른 브랜치나 커밋의 코드가 시작 버전입니다.              |
| 작업과 데이터만 있음                 | **새 저장소 만들기**. Skynet이 첫 번째 해결책을 작성합니다. 그것이 시작 버전입니다. |

> [!WARNING]
> GEPA와 Auto는 피드백 텍스트에서 학습합니다. 피드백이 항상 같은 문자열이면 이 옵티마이저들이 활용할 정보가 줄어듭니다.

## 관련 페이지

- [엔진](/docs/concepts/engines)
- [데이터셋과 분할](/docs/concepts/datasets)
- [지표와 채점기](/docs/concepts/scorers)
- [마법사로 실행 시작](/docs/guides/submit)
