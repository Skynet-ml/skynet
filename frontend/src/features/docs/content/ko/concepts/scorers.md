채점기는 리포지토리의 한 버전이 얼마나 좋은지 Skynet에 알려 줍니다. 채점기는 `scorer.py` 안의 Python 함수입니다. 모든 엔진이 같은 함수를 호출합니다. 이 페이지는 계약, 반환값, 예제를 제공합니다.

## 계약

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| 인수        | 담긴 내용                                                                 |
| ----------- | ------------------------------------------------------------------------- |
| `repo_path` | 설정 명령이 실행된 후의 리포지토리 한 버전의 폴더                         |
| `case`      | dict입니다. `case["input"]`는 입력 필드를 담습니다. `case["expected"]`는 기댓값 또는 `None`을 담습니다 |

함수가 인수를 하나만 받으면 Skynet은 `score(repo_path)`를 호출합니다. 채점기가 자체 테스트를 실행하고 케이스가 필요 없을 때 이 형태를 사용하세요.

채점기는 샌드박스 안의 버전 폴더에서 실행됩니다. 리포지토리의 코드를 가져오거나, 명령을 실행하거나, `serve.py`의 `serve()`를 호출할 수 있습니다. [최적화의 작동 방식](/docs/concepts/optimization)을 읽어 보세요.

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

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]`는 출력 열 이름을 키로 하는 dict입니다. [데이터셋과 분할](/docs/concepts/datasets)을 읽어 보세요.

### 리포지토리의 테스트 실행

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### 모델을 심사자로 사용

채점기는 모델을 호출할 수 있습니다. `skynet` 헬퍼 모듈에서 `llm`을 가져오세요. 호출은 Skynet의 모델 게이트웨이를 거쳐 실행의 채점기 모델로 갑니다.

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

채점기는 `OPENAI_BASE_URL`, `APP_API_KEY`, `APP_MODEL` 환경 변수로 OpenAI 호환 클라이언트를 사용할 수도 있습니다. 게이트웨이는 실행의 채점기 모델만 받습니다.

## 모델 호출과 시크릿

- Skynet 키로 하는 호출은 실행 예산에 대해 계량됩니다. 내 키(BYOK)를 쓰면 호출은 내 제공업체 키로 가며, Skynet 모델 요금은 없습니다. [예산과 결제](/docs/concepts/billing)를 읽어 보세요.
- 실행 시크릿은 환경 변수로 채점기에 전달됩니다. 변수 이름은 시크릿 이름입니다. 예: `os.environ["SEARCH_API_KEY"]`.
- Skynet은 채점기 출력과 로그에서 시크릿 값을 숨깁니다.

## 제한

| 항목                 | 값                             |
| -------------------- | ------------------------------ |
| 채점기 타임아웃      | 기본 60초, 최대 600초          |

## 관련 페이지

- [데이터셋과 분할](/docs/concepts/datasets)
- [엔진](/docs/concepts/engines)
- [마법사로 실행 시작](/docs/guides/submit)
