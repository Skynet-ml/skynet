Skynet 就是 Optimize Anything：每次运行都优化一个仓库。引擎提出仓库的新版本，你的评分器为每个版本打分，Skynet 保留最好的版本。

## 共享契约

每次运行都有一个仓库和一个评分器。评分器是一个 Python 函数，它接收仓库某个版本的路径和一个用例。

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

- `repo_path` 是仓库某个版本的文件夹，安装命令已经在其中运行过。
- `case` 是一个 dict。`case["input"]` 包含一个用例的输入字段。`case["expected"]` 包含期望值；如果该用例没有期望值，则为 `None`。
- `score` 返回一对值：一个数字，以及一个包含 `"feedback"` 字符串的 dict。

仓库根目录中有一个 `serve.py` 文件，其中定义了 `def serve(input)`。评分器可以调用它，运行结束后 Skynet 也用它来部署最好的版本。

> [!NOTE]
> 编写版本的编码智能体看不到评分器的源代码、期望值或运行密钥。

完整的评分器契约，请阅读[指标与评分器](/docs/concepts/scorers)。

## 你的代码发起的模型调用

你的仓库代码和评分器可以通过 Skynet 的模型网关调用模型。沙箱通过环境变量提供一个兼容 OpenAI 的端点：

| 变量                 | 内容                                                  |
| -------------------- | ----------------------------------------------------- |
| `OPENAI_BASE_URL`    | 网关 URL。`APP_GATEWAY_URL` 包含相同的值。            |
| `APP_API_KEY`        | 仅用于本次运行的令牌。它不是提供商密钥。              |
| `APP_MODEL`          | 要发送的模型名称。它是本次运行的评分器模型。          |
| `ANTHROPIC_BASE_URL` | 供 Anthropic SDK 使用的网关 URL。                     |

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

- 使用 Skynet 密钥的调用会计入运行预算。
- 使用你自己的密钥（BYOK）时，调用计到你的提供商密钥上。Skynet 不收取模型费用。
- 网关会拒绝对其他任何模型的请求。
- 运行密钥以环境变量的形式传给你的代码和评分器。变量名就是密钥名。Skynet 会在输出中隐藏密钥的值。

沙箱没有其他网络访问权限。

## 引擎

每个引擎都处理仓库：

- **Auto** 是默认选项。它尝试多个引擎，然后从最好的版本继续运行 GEPA。
- **GEPA** 通过反思演化版本，并维护一个 Pareto 前沿。
- **Best-of-N** 让反思模型给出相互独立的提议，并保留最好的一个。
- **AutoResearch**、**Meta-Harness**、**AutoSaddler** 和 **ShinkaEvolve** 使用编程智能体来提出版本。

引擎只修改你允许的路径中的文件。关于每个引擎及其设置，请阅读[引擎](/docs/concepts/engines)。

## Auto

**Auto** 是向导中每次运行的默认选项。

1. Auto 用五个引擎进行探索：GEPA、AutoResearch、Meta-Harness、AutoSaddler 和 ShinkaEvolve。
2. 它选出这些引擎找到的最好版本。
3. 它从该版本继续运行 GEPA，直到预算用完。

Auto 会读取你的评分器给出的反馈文本。

## 起始版本从哪里来

| 你的情况                     | 使用                                                         |
| ---------------------------- | ------------------------------------------------------------ |
| 你的代码在 GitHub 仓库中     | **使用现有仓库**。你选择的分支或提交中的代码就是起点。           |
| 你只有任务和数据             | **创建新仓库**。Skynet 编写第一个解决方案，它就是起点。      |

> [!WARNING]
> GEPA 和 Auto 会从反馈文本中学习。如果你的反馈总是同一个字符串，这些优化器能利用的信息就更少。

## 相关页面

- [引擎](/docs/concepts/engines)
- [数据集与划分](/docs/concepts/datasets)
- [指标与评分器](/docs/concepts/scorers)
- [提交运行](/docs/guides/submit)
