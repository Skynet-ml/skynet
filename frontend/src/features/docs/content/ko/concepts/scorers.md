채점기는 리포지토리의 한 버전이 얼마나 좋은지 Skynet에 알려 줍니다. 채점기는 `scorer.py` 안의 Python 함수입니다. 모든 엔진이 같은 함수를 호출합니다. 이 페이지는 계약, 반환값, 예제를 제공합니다.

## 계약

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| 인수        | 담긴 내용                                                                 |
| ----------- | ------------------------------------------------------------------------- |
| `repo_path` | 지표 파일만 담은 폴더입니다. 다른 리포지토리 파일과 버전 코드는 담지 않습니다 |
| `case`      | dict입니다. `case["input"]`는 입력 필드를 담습니다. `case["expected"]`는 기댓값 또는 `None`을 담습니다 |
| `output`    | `[run]` 엔트리가 케이스에 대해 반환한 값 |

리포지토리의 `optimize.toml` 파일에는 `[run]` 섹션 또는 `[target.<name>]` 스테이지가 있어야 합니다. `[run]`의 `entry`는 한 버전을 케이스 하나에 대해 자체 샌드박스에서 실행합니다. 둘 다 없는 파일은 Skynet이 거부합니다. 모든 버전이 같은 점수를 받기 때문입니다.

- 함수 엔트리(예: `entry = "serve.py:serve"`)는 `case["input"]`를 인수로 받습니다. `output`은 그 반환값입니다.
- 명령 엔트리(예: `entry = ["python", "-m", "pytest", "-q"]`)는 입력을 stdin의 JSON으로 받습니다. `output`은 `{"exit_code": ..., "stdout": "..."}`입니다.

채점기는 별도의 샌드박스에서 실행됩니다. 그 샌드박스에는 지표 파일만 있습니다. 채점기는 버전의 코드를 가져오거나 실행할 수 없습니다. 채점기는 `output`을 통해서만 버전을 봅니다. [최적화의 작동 방식](/docs/concepts/optimization)을 읽어 보세요.

## 반환값

`score`는 피드백이 있는 점수를 반환해야 합니다. 다음 형태가 유효합니다.

1. 쌍: 숫자 하나와, 피드백 문자열 또는 `"feedback"` 키가 있는 dict.
2. `"score"`에 숫자, `"feedback"`에 문자열이 있는 dict.

피드백은 비어 있지 않은 문자열이어야 합니다.

부가 정보에는 `"scores"` 키도 넣을 수 있습니다. 이름을 이름 있는 점수에 매핑합니다. 각 이름 있는 점수에는 자체 점수와 피드백이 있습니다. 예: `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

부가 정보에는 같은 형태의 `"cases"` 키도 넣을 수 있습니다. 각 항목은 케이스 하나의 결과입니다. 모든 엔진은 각 케이스를 별도의 목표로 사용합니다.

반환값의 형태가 다르면 검사가 실패하고 그 이유를 보여 줍니다.

| 반환값                                                                          | 유효 여부                                  |
| ------------------------------------------------------------------------------- | ------------------------------------------ |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | 예                                         |
| `(0.8, "Close. Missing the date.")`                                             | 예                                         |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | 예                              |
| `0.8`                                                                           | 아니요. 피드백이 없습니다.                 |
| `(0.8, {})`                                                                     | 아니요. 피드백 문자열이 없습니다.          |
| `("0.8", {"feedback": "OK"})`                                                   | 아니요. 점수가 문자열입니다.               |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | 아니요. 이름 있는 점수가 숫자가 아닙니다. |

## 유용한 피드백 작성하기

GEPA와 Auto는 피드백 텍스트를 읽고 다음 버전을 작성합니다. 무엇이 틀렸는지, 올바른 출력은 어떤 모습인지 알려 주세요.

| 약한 피드백   | 유용한 피드백                                                                       |
| ------------- | ----------------------------------------------------------------------------------- |
| "틀렸습니다." | "'billing'을 기대했지만 'other'가 나왔습니다. 메시지에 결제 내역이 언급되어 있습니다." |
| "형식 오류."  | "출력이 유효한 JSON이 아닙니다. 마지막 필드 뒤에 쉼표가 남아 있습니다."             |

> [!NOTE]
> 코딩 에이전트는 채점기 소스, 기댓값, 실행 시크릿을 절대 보지 않습니다. 코딩 에이전트는 점수와 피드백 텍스트만 봅니다.

## 예제

### 정확히 일치

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]`는 출력 열 이름을 키로 하는 dict입니다. [데이터셋과 분할](/docs/concepts/datasets)을 읽어 보세요.

### 리포지토리의 테스트 실행

`[run]` 엔트리가 테스트를 실행합니다. 채점기는 종료 코드와 테스트 출력을 읽습니다.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

버전의 `[run]` 엔트리가 실패하면 Skynet이 직접 그 버전에 0점을 줍니다. 엔트리가 시작되지 않거나, 시간 제한을 넘기거나, 시그널로 멈추면 피드백은 `Run failed: …`이고, Skynet은 채점기를 호출하지 않습니다. 0이 아닌 종료 코드는 실패가 아닙니다. 채점기는 그 값을 `output`으로 받습니다.

### 모델을 심사자로 사용

채점기는 모델을 호출할 수 있습니다. `skynet` 헬퍼 모듈에서 `llm`을 가져오세요. 호출은 Skynet의 모델 게이트웨이를 거쳐 실행의 채점기 모델로 갑니다.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

채점기는 `OPENAI_BASE_URL`, `APP_API_KEY`, `APP_MODEL` 환경 변수로 OpenAI 호환 클라이언트를 사용할 수도 있습니다. 게이트웨이는 실행의 채점기 모델만 받습니다.

## 모델 호출과 시크릿

- Skynet 키로 하는 호출은 실행 예산에 대해 계량됩니다. 내 키(BYOK)를 쓰면 호출은 내 제공업체 키로 가며, Skynet 모델 요금은 없습니다. [예산과 결제](/docs/concepts/billing)를 읽어 보세요.
- 채점기는 실행 시크릿을 환경 변수로 받지 않습니다. 시크릿은 `[metric] services`에 나열된 `[services.<name>]` 테이블을 통해서만 채점기에 전달됩니다. 시크릿을 테이블의 `headers`에 `${NAME}`으로 쓰세요. 채점기는 `SKYNET_SERVICE_<NAME>_URL`의 URL로 요청을 보냅니다. Skynet은 샌드박스 밖에서 시크릿을 추가합니다.
- Skynet은 채점기 출력과 로그에서 시크릿 값을 숨깁니다.

## 제한

| 항목                 | 값                             |
| -------------------- | ------------------------------ |
| 채점기 타임아웃      | 기본 60초, 최대 600초          |

## 관련 페이지

- [데이터셋과 분할](/docs/concepts/datasets)
- [엔진](/docs/concepts/engines)
- [마법사로 실행 시작](/docs/guides/submit)
