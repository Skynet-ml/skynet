评分器告诉 Skynet 你仓库的某个版本有多好。它是 `scorer.py` 中的一个 Python 函数。每个引擎都调用同一个函数。本页给出契约、返回值和示例。

## 契约

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| 参数        | 包含的内容                                                                       |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 仓库某个版本的文件夹，安装命令已经在其中运行过                                   |
| `case`      | 一个 dict。`case["input"]` 包含输入字段。`case["expected"]` 包含期望值，或为 `None` |

如果你的函数只接受一个参数，Skynet 会调用 `score(repo_path)`。当评分器运行自己的测试、不需要用例时，请使用这种形式。

评分器在沙箱中、在该版本的文件夹里运行。它可以从仓库导入代码、运行命令，或者调用 `serve.py` 中的 `serve()`。请阅读[优化的工作原理](/docs/concepts/optimization)。

## 返回值

`score` 必须返回一个分数和反馈。以下结构有效：

1. 一对值：一个数字，以及一个反馈字符串或一个带 `"feedback"` 键的 dict。
2. 一个 dict，`"score"` 下是一个数字，`"feedback"` 下是一个字符串。

反馈必须是非空字符串。

附加信息还可以有一个 `"scores"` 键。它把名称映射到命名分数。每个命名分数都有自己的分数和反馈，例如 `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`。

附加信息还可以有一个结构相同的 `"cases"` 键。每一项是一个用例的结果。每个引擎都把每个用例当作一个单独的目标。

如果返回值的结构不同，检查会失败并显示原因。

| 返回值 | 是否有效 |
| --- | --- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | 是 |
| `(0.8, "Close. Missing the date.")`                                             | 是 |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | 是 |
| `0.8`                                                                           | 否。没有反馈。 |
| `(0.8, {})`                                                                     | 否。没有反馈字符串。 |
| `("0.8", {"feedback": "OK"})`                                                   | 否。分数是字符串。 |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | 否。某个命名分数不是数字。 |

## 编写有用的反馈

GEPA 和 Auto 会读取反馈文本来编写下一个版本。请说明哪里错了，以及正确的输出是什么样子。

| 薄弱的反馈   | 有用的反馈                                               |
| ------------ | -------------------------------------------------------- |
| “Wrong.”     | “Expected 'billing', got 'other'. The message mentions a charge.” |
| “Bad format.” | “The output is not valid JSON. It has a trailing comma after the last field.” |

> [!NOTE]
> 编码智能体永远看不到你的评分器源代码、期望值或运行密钥。它只能看到分数和反馈文本。

## 示例

### 精确匹配

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

`case["expected"]` 是一个以输出列名称为键的 dict。请阅读[数据集与划分](/docs/concepts/datasets)。

### 运行仓库的测试

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### 用模型作为评判

评分器可以调用模型。从 `skynet` 辅助模块导入 `llm`。调用会通过 Skynet 的模型网关，发送到本次运行的评分器模型。

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

评分器也可以使用任何兼容 OpenAI 的客户端，配合 `OPENAI_BASE_URL`、`APP_API_KEY` 和 `APP_MODEL` 环境变量。网关只接受本次运行的评分器模型。

## 模型调用和密钥

- 使用 Skynet 密钥的调用会计入运行预算。使用你自己的密钥（BYOK）时，调用计到你的提供商密钥上，Skynet 不收取模型费用。请阅读[计费](/docs/concepts/billing)。
- 运行密钥以环境变量的形式传给评分器。变量名就是密钥名，例如 `os.environ["SEARCH_API_KEY"]`。
- Skynet 会在评分器输出和日志中隐藏密钥的值。

## 限制

| 项目       | 值                      |
| ---------- | ----------------------- |
| 评分器超时 | 默认 60 秒，最多 600 秒 |

## 相关页面

- [数据集与划分](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交运行](/docs/guides/submit)
