Skynet은 Optimize Anything입니다. 메트릭을 기준으로 DSPy 프로그램이나 리포지토리를 개선하는 하나의 제품입니다. 아티팩트 종류에 따라 사용할 수 있는 엔진이 정해집니다.

## 공통 계약

모든 실행에는 파일이 두 개 있습니다. `run.py`는 케이스 하나에서 아티팩트를 실행합니다. `metric.py`는 출력을 채점합니다.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact`는 옵티마이저가 제안한 현재 버전입니다.
- `input`은 케이스 하나의 입력 필드를 담은 dict입니다.
- `expected`는 케이스의 기댓값이며, 케이스에 기댓값이 없으면 `None`입니다.
- `score`는 쌍을 반환합니다. 숫자 하나와 `"feedback"` 문자열이 있는 dict 하나입니다.

`skynet` 헬퍼 모듈은 `llm`, `Image`, `log`를 제공합니다. 실행 코드에서 `llm`을 호출하면 작업 모델로 갑니다. 메트릭에서 `llm`을 호출하면 메트릭 모델로 갑니다.

> [!NOTE]
> 실행 코드는 샌드박스 안의 별도 프로세스에서 실행됩니다. 실행 코드는 메트릭 소스, 기댓값, 메트릭 키를 볼 수 없습니다.

전체 메트릭 계약은 [지표와 채점기](/docs/concepts/scorers)를 읽어 보세요.

## 아티팩트 종류와 엔진

| 아티팩트 종류 | 엔진                                                                         |
| ------------- | ---------------------------------------------------------------------------- |
| 리포지토리 | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| DSPy 프로그램 | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

엔진은 두 계열로 나뉩니다.

- **네이티브 엔진**은 아티팩트를 블랙박스로 다룹니다. 리포지토리에서 작동합니다.
- **DSPy 옵티마이저**는 DSPy 프로그램 내부에서 작동합니다. DSPy 프로그램 아티팩트에서만 작동합니다.

DSPy MIPROv2와 DSPy BootstrapFewShot에는 쌍(기대 출력이 있는 케이스)이 필요합니다. 입력이 하나뿐이면 DSPy 프로그램은 Auto와 DSPy GEPA만 사용할 수 있습니다.

## 리포지토리

네이티브 엔진은 허용한 경로의 파일을 변경합니다. 메트릭이 리포지토리의 각 버전에 점수를 매깁니다.

여섯 개의 네이티브 엔진은 서로 다른 탐색 방법을 사용합니다.

- **GEPA**는 리플렉션으로 버전을 진화시키고 파레토 프런트를 유지합니다.
- **Best-of-N**은 리플렉션 모델에 독립적인 제안을 요청하고 가장 좋은 것을 유지합니다.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler**, **ShinkaEvolve**는 코딩 에이전트로 버전을 제안합니다.

각 엔진과 설정은 [엔진](/docs/concepts/engines)을 읽어 보세요.

## DSPy 프로그램 아티팩트

DSPy 프로그램은 아티팩트 종류 중 하나입니다. `run.py`는 모듈 수준에서 `dspy.Module`을 만들고 `run` 안에서 이를 호출합니다. 파일에 모듈이 두 개 이상 있으면, 최적화할 모듈의 이름을 `program`으로 지정하세요.

DSPy 옵티마이저가 실행될 때 일어나는 일은 다음과 같습니다.

1. Skynet이 모듈을 찾아 `run` 함수로 감쌉니다. 모든 케이스는 사용자가 작성한 `run`을 거칩니다.
2. DSPy 옵티마이저가 학습 케이스에서 프로그램을 컴파일합니다.
3. 검증 케이스에서 선택합니다.
4. 우승 결과는 각 예측기의 지시문 텍스트와 모듈의 전체 컴파일 상태입니다. 이 상태에는 퓨샷(few-shot) 데모가 포함됩니다.

최종 점수는 Skynet이 저장하는 바로 그 프로그램을 측정합니다.

| 옵티마이저            | 피드백 텍스트 읽기 |
| --------------------- | ------------------ |
| DSPy GEPA             | 예                 |
| DSPy MIPROv2          | 아니요, 점수만     |
| DSPy BootstrapFewShot | 아니요, 점수만     |

DSPy BootstrapFewShot은 리플렉션 모델을 사용하지 않습니다.

> [!TIP]
> DSPy 리플렉션 모델은 메트릭이 반환하는 피드백 텍스트만 봅니다. 메트릭 소스는 절대 보지 않습니다.

## Auto

**Auto**는 마법사의 기본 선택입니다.

- **리포지토리**: Auto는 사용할 수 없습니다. 네이티브 엔진 하나를 선택하세요.
- DSPy 프로그램에서 Auto는 DSPy GEPA를 실행합니다.

Auto는 메트릭의 피드백 텍스트를 읽습니다.

## 어떤 엔진을 고를까요

| 상황                                                     | 사용할 것                                                             |
| -------------------------------------------------------- | --------------------------------------------------------------------- |
| GitHub 리포지토리에 코드가 있음 | 리포지토리. 네이티브 엔진 하나를 선택하세요. |
| DSPy 프로그램이 있습니다                                 | DSPy 프로그램 아티팩트. Auto로 시작하세요.                            |
| 데이터에서 퓨샷 데모를 고르고 싶습니다                   | DSPy BootstrapFewShot 또는 DSPy MIPROv2를 사용하는 DSPy 프로그램 아티팩트 |

> [!WARNING]
> GEPA, DSPy GEPA, Auto는 피드백 텍스트에서 학습합니다. 피드백이 항상 같은 문자열이면 이 옵티마이저들이 활용할 정보가 줄어듭니다.

## 관련 페이지

- [엔진](/docs/concepts/engines)
- [데이터셋과 분할](/docs/concepts/datasets)
- [지표와 채점기](/docs/concepts/scorers)
- [마법사로 실행 시작](/docs/guides/submit)
