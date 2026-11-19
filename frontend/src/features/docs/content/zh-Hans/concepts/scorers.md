评分器告诉 Skynet 你仓库的某个版本有多好。它是 `scorer.py` 中的一个 Python 函数。每个引擎都调用同一个函数。本页给出契约、返回值和示例。

## 契约

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| 参数        | 包含的内容                                                                       |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | 只包含你的指标文件的文件夹。它不包含其他仓库文件，也不包含版本代码 |
| `case`      | 一个 dict。`case["input"]` 包含输入字段。`case["expected"]` 包含期望值，或为 `None` |
| `output`    | `[run]` 入口为该用例返回的内容；如果文件没有 `[run]`，则为 `None` |

仓库的 `optimize.toml` 文件必须有一个 `[run]` 部分或一个 `[target.<name>]` 阶段。`[run]` 的 `entry` 在自己的沙箱中，对一个用例运行一个版本。如果两者都没有，Skynet 会拒绝该文件，因为每个版本都会得到相同的分数。

- 函数入口，例如 `entry = "serve.py:serve"`，以 `case["input"]` 作为参数。`output` 是它的返回值。
- 命令入口，例如 `entry = ["python", "-m", "pytest", "-q"]`，从 stdin 以 JSON 形式接收输入。`output` 是 `{"exit_code": ..., "stdout": "..."}`。

评分器在另一个独立的沙箱中运行。该沙箱只包含你的指标文件。评分器不能导入或运行版本的代码。它只能通过 `output`，或通过对 `[target.<name>]` 阶段的 `call()` 看到版本。用 `from skynet import call` 导入 `call`。请阅读[优化的工作原理](/docs/concepts/optimization)。

## 返回值

`score` 返回一个对象：

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` 是必需的。它是一个有限数字。只有这个分数决定版本的排名。
- `"feedback"` 是可选的文本。建议提供，因为 GEPA 和 Auto 会从中学习。没有反馈时，引擎只能看到分数，运行日志会提示一次。
- `"scores"` 是可选的。它把名称映射到命名分数。每个命名分数是一个对象，带有自己的 `"score"` 和一个可选的 `"feedback"`。每个引擎都把每个命名分数当作一个单独的目标。

任何其他结构或键都是错误。检查会显示出错的部分和有效的结构。

| 返回值 | 是否有效 |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | 是 |
| `{"score": 0.8}` | 是。没有反馈。 |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | 是 |
| `0.8` | 否。该值不是对象。 |
| `(0.8, "Close. Missing the date.")` | 否。该值不是对象。 |
| `{"score": "0.8"}` | 否。分数是字符串。 |
| `{"score": 0.8, "scores": {"format": 1.0}}` | 否。命名分数必须是对象。 |
| `{"score": 0.8, "feedbak": "OK"}` | 否。键 `feedbak` 未知。 |

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

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` 是一个以输出列名称为键的 dict。请阅读[数据集与划分](/docs/concepts/datasets)。

### 运行仓库的测试

`[run]` 入口运行测试。评分器读取退出码和测试输出。

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["stdout"][-2000:]}
```

当 `[run]` 入口在某个案例上失败时，该案例没有分数。如果入口无法启动、超时或被信号停止，或者函数入口在返回之前抛出异常或退出，反馈是 `Run failed: …`，Skynet 不会调用评分器。命令入口的非零退出码不算失败：评分器会在 `output` 中收到它。包含这种案例的版本没有分数。它不能成为最佳版本，不会达到任何 `[outcome] target`，也不会打开 pull request。如果失败必须算作一个数字，请在你的程序中捕获失败，写出一个输出，再让评分器为它评分。请阅读[仓库规则](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md)。

### 用模型作为评判

评分器可以调用模型。从 `skynet` 辅助模块导入 `llm`。调用会通过 Skynet 的模型网关，发送到本次运行的评分器模型。

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
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

评分器也可以使用任何兼容 OpenAI 的客户端，配合 `OPENAI_BASE_URL`、`APP_API_KEY` 和 `APP_MODEL` 环境变量。网关只接受本次运行的评分器模型。

## 模型调用和密钥

- 使用 Skynet 密钥的调用会计入运行预算。使用你自己的密钥（BYOK）时，调用计到你的提供商密钥上，Skynet 不收取模型费用。请阅读[计费](/docs/concepts/billing)。
- 评分器不会以环境变量的形式收到运行密钥。密钥只能通过 `[metric] services` 列出的 `[services.<name>]` 表，或 `[metric] mcp` 文件中的服务器，传给评分器。把密钥以 `${NAME}` 的形式写在该表的 `headers` 中。评分器把请求发送到 `SKYNET_SERVICE_<NAME>_URL` 中的 URL。Skynet 在沙箱外添加密钥。
- Skynet 会在评分器输出和日志中隐藏密钥的值。

## 限制

| 项目       | 值                      |
| ---------- | ----------------------- |
| 评分器超时 | 默认 60 秒，最多 600 秒 |

## 相关页面

- [数据集与划分](/docs/concepts/datasets)
- [引擎](/docs/concepts/engines)
- [提交运行](/docs/guides/submit)
