指标告诉 Skynet 一个输出有多好。它是 `metric.py` 中的一个 Python 函数。每个引擎都调用同一个函数。本页给出契约、返回值和示例。

## 契约

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| 参数       | 包含的内容                                           |
| ---------- | ---------------------------------------------------- |
| `input`    | 一个 dict，包含该用例的输入字段                      |
| `output`   | 你的 `run(artifact, input)` 为该用例返回的内容       |
| `expected` | 该用例的期望值；如果用例没有期望值，则为 `None`      |

如果你的函数只接受两个参数，Skynet 会调用 `score(input, output)`。

对于 DSPy 程序，Skynet 会把 DSPy 的指标调用适配到这同一个函数。你只需为每种产物类型编写一个指标。请阅读[优化的工作原理](/docs/concepts/optimization)。

## 返回值

`score` 必须返回一对值（一个包含两个元素的 tuple 或 list）：

1. **分数。** 一个数字。优化器会尽量让它变高。
2. **附加信息。** 一个 dict。它必须有一个 `"feedback"` 键，其值为字符串。

附加信息还可以有一个 `"scores"` 键。它把名称映射到数字，例如 `{"format": 1.0, "accuracy": 0.5}`。

如果返回值的结构不同，契约检查会在 **指标返回值** 处失败。

| 返回值                                                 | 是否有效                     |
| ------------------------------------------------------ | ---------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | 是                           |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | 是                           |
| `0.8`                                                  | 否。没有附加信息。           |
| `(0.8, "Close.")`                                      | 否。附加信息不是 dict。      |
| `(0.8, {})`                                            | 否。没有反馈字符串。         |
| `("0.8", {"feedback": "OK"})`                          | 否。分数是字符串。           |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | 否。某个命名分数不是数字。   |

## 编写有用的反馈

GEPA、DSPy GEPA 和 Auto 会读取反馈文本来编写下一个版本。请说明哪里错了，以及正确的输出是什么样子。

| 薄弱的反馈    | 有用的反馈                                                            |
| ------------- | --------------------------------------------------------------------- |
| “Wrong.”      | “Expected 'billing', got 'other'. The message mentions a charge.”     |
| “Bad format.” | “The output is not valid JSON. It has a trailing comma after the last field.” |

> [!NOTE]
> 优化器永远看不到你的指标源代码。使用 DSPy 优化器时，反思模型只能看到反馈文本。你的期望值只留在指标中。

## 示例

### 精确匹配

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` 是一个以输出列名称为键的 dict。请阅读[数据集与划分](/docs/concepts/datasets)。

### 多项检查

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

### 用模型作为评判

指标可以调用模型。从 `skynet` 辅助模块导入 `llm`。从指标发出的调用会使用指标模型。

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

指标在自己的 `llm()` 调用中设置模型。示例中的四个参数均为可选参数。省略 `model=` 时，指标使用反思模型。

## 准确率、速度和成本

指标可以把准确率与测得的运行时间和模型成本结合起来。在模块级定义 `REWARD_SPEC`，并返回 `reward` 的值。

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

- `weights` 必须包含 `accuracy`、`speed` 和 `cost`。每个权重都为 0 或更大，且不能全部为 0。Skynet 会把它们缩放到总和为 1。
- `speed.reference` 的单位是毫秒。`cost.reference` 的单位是美元。两者都必须为正数。
- 没有 `accuracy.reference` 时，准确率是 0 到 1 之间的分数。有它时，准确率是一个误差，越低越好。

Skynet 自己测量时间和成本。运行代码无法更改它们。如果 Skynet 无法测量时间或成本，那一部分记为 0 分，反馈中也会说明。

指标也可以用 `skynet` 中的 `observation()` 和 `service_cost()` 直接读取这些测量值。

## 限制

| 项目           | 值                               |
| -------------- | -------------------------------- |
| 指标超时       | 默认 60 秒，最多 600 秒          |
| 契约检查样例   | 3 个用例                         |

## 相关页面

- [数据集与划分](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交运行](/docs/guides/submit)
