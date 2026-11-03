메트릭은 출력이 얼마나 좋은지 Skynet에 알려 줍니다. 메트릭은 `metric.py` 안의 Python 함수입니다. 모든 엔진이 같은 함수를 호출합니다. 이 페이지는 계약, 반환값, 예제를 제공합니다.

## 계약

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| 인수       | 담긴 내용                                                     |
| ---------- | ------------------------------------------------------------- |
| `input`    | 케이스의 입력 필드를 담은 dict                                |
| `output`   | 해당 케이스에서 `run(artifact, input)`이 반환한 값            |
| `expected` | 케이스의 기댓값. 케이스에 기댓값이 없으면 `None`              |

함수가 인수를 두 개만 받으면 Skynet은 `score(input, output)`을 호출합니다.

DSPy 프로그램에서는 Skynet이 DSPy 메트릭 호출을 이 같은 함수에 맞게 변환합니다. 모든 아티팩트 종류에 대해 메트릭 하나만 작성하면 됩니다. [최적화의 작동 방식](/docs/concepts/optimization)을 읽어 보세요.

## 반환값

`score`는 쌍(항목이 두 개인 튜플 또는 리스트)을 반환해야 합니다.

1. **점수.** 숫자입니다. 옵티마이저는 이 값을 높이려고 합니다.
2. **부가 정보.** dict입니다. 문자열 값을 가진 `"feedback"` 키가 있어야 합니다.

부가 정보에는 `"scores"` 키도 넣을 수 있습니다. 이름을 숫자에 매핑합니다. 예: `{"format": 1.0, "accuracy": 0.5}`.

반환값의 형태가 다르면 계약 검사의 **메트릭 반환값** 항목에서 실패합니다.

| 반환값                                                 | 유효 여부                                  |
| ------------------------------------------------------ | ------------------------------------------ |
| `(0.8, {"feedback": "Close. Missing the date."})`      | 예                                         |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | 예                                         |
| `0.8`                                                  | 아니요. 부가 정보가 없습니다.              |
| `(0.8, "Close.")`                                      | 아니요. 부가 정보가 dict가 아닙니다.       |
| `(0.8, {})`                                            | 아니요. 피드백 문자열이 없습니다.          |
| `("0.8", {"feedback": "OK"})`                          | 아니요. 점수가 문자열입니다.               |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | 아니요. 이름 있는 점수가 숫자가 아닙니다.  |

## 유용한 피드백 작성하기

GEPA, DSPy GEPA, Auto는 피드백 텍스트를 읽고 다음 버전을 작성합니다. 무엇이 틀렸는지, 올바른 출력은 어떤 모습인지 알려 주세요.

| 약한 피드백   | 유용한 피드백                                                                       |
| ------------- | ----------------------------------------------------------------------------------- |
| "틀렸습니다." | "'billing'을 기대했지만 'other'가 나왔습니다. 메시지에 결제 내역이 언급되어 있습니다." |
| "형식 오류."  | "출력이 유효한 JSON이 아닙니다. 마지막 필드 뒤에 쉼표가 남아 있습니다."             |

> [!NOTE]
> 옵티마이저는 메트릭 소스를 절대 보지 않습니다. DSPy 옵티마이저에서는 리플렉션 모델이 피드백 텍스트만 봅니다. 기댓값은 메트릭 안에만 머뭅니다.

## 예제

### 정확히 일치

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected`는 출력 열 이름을 키로 하는 dict입니다. [데이터셋과 분할](/docs/concepts/datasets)을 읽어 보세요.

### 여러 검사

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### 모델을 심사자로 사용

메트릭은 모델을 호출할 수 있습니다. `skynet` 헬퍼 모듈에서 `llm`을 가져오세요. 메트릭에서 하는 호출은 메트릭 모델로 갑니다.

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

메트릭은 자체 `llm()` 호출에서 모델을 지정합니다. 표시된 네 인수는 모두 선택 사항입니다. `model=`을 생략하면 메트릭은 리플렉션 모델을 사용합니다.

## 정확도, 속도, 비용

메트릭은 정확도를 측정된 실행 시간 및 모델 비용과 함께 섞을 수 있습니다. 모듈 수준에서 `REWARD_SPEC`을 정의하고 `reward`의 값을 반환하세요.

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights`에는 `accuracy`, `speed`, `cost`가 있어야 합니다. 각 가중치는 0 이상이며, 모두 0일 수는 없습니다. Skynet은 합이 1이 되도록 가중치를 조정합니다.
- `speed.reference`의 단위는 밀리초입니다. `cost.reference`의 단위는 미국 달러입니다. 둘 다 양수여야 합니다.
- `accuracy.reference`가 없으면 정확도는 0에서 1 사이의 점수입니다. 있으면 정확도는 오차이며, 낮을수록 좋습니다.

Skynet은 시간과 비용을 직접 측정합니다. 실행 코드는 이 값을 바꿀 수 없습니다. Skynet이 시간이나 비용을 측정할 수 없으면 그 부분은 0점이 되고, 피드백에 그 사실이 표시됩니다.

메트릭은 `skynet`의 `observation()`과 `service_cost()`로 측정값을 직접 읽을 수도 있습니다.

## 제한

| 항목                 | 값                             |
| -------------------- | ------------------------------ |
| 메트릭 타임아웃      | 기본 60초, 최대 600초          |
| 계약 검사 샘플       | 케이스 3개                     |

## 관련 페이지

- [데이터셋과 분할](/docs/concepts/datasets)
- [엔진](/docs/concepts/engines)
- [마법사로 실행 시작](/docs/guides/submit)
