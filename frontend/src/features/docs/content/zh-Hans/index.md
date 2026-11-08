Skynet 用来改进你已有的代码，或者它为你编写的代码。Skynet 就是 Optimize Anything：每次运行都优化一个仓库。你提供一个仓库、一组用例和一个评分器。Skynet 会提出仓库的新版本，用你的评分器为每个版本打分，并保留最好的版本。

## Skynet 能优化什么

每次运行都优化一个仓库。你可以用两种方式开始：

| 开始方式   | 会发生什么                                                       |
| ---------- | ---------------------------------------------------------------- |
| 使用现有仓库   | Skynet 修改你连接的 GitHub 仓库中、你允许修改的路径里的文件。     |
| 创建新仓库 | 你提供任务和数据。Skynet 在你的账户中创建一个新的 GitHub 仓库，并编写第一个解决方案。 |

仓库中的代码可以是任何东西：提示流水线、智能体框架（harness）、脚本或库。它可以使用任何库。

每次运行都遵循同一个契约。你的评分器定义 `score(repo_path, case)`。它返回一个分数和一段文字反馈。优化器读取反馈，让下一个版本更好。

> [!NOTE]
> 每个版本都在沙箱中运行。编写版本的编码智能体看不到评分器的源代码，也看不到期望值。

## 一个产品，多个引擎

**Auto** 是每次运行的默认选项。它会尝试 GEPA、AutoResearch、Meta-Harness、AutoSaddler 和 ShinkaEvolve，然后从最好的版本继续运行 GEPA。你也可以选择单个引擎：GEPA、Best-of-N、AutoResearch、Meta-Harness、AutoSaddler 或 ShinkaEvolve。请阅读 [优化的工作原理](/docs/concepts/optimization) 和 [引擎](/docs/concepts/engines)。

## 一次运行如何进行

1. 你描述任务，然后选择一个仓库，或者让 Skynet 创建一个仓库。
2. 你提供用例。Skynet 把它们划分为训练集、验证集和测试集。
3. 你选择优化器、模型和预算。
4. 你编写评分器。设置检查会在起始版本上运行它。
5. Skynet 执行优化并展示结果。

Skynet 根据验证用例选择版本，并在测试用例上报告最终分数。引擎永远看不到测试用例。

## 从哪里开始

| 如果你想                 | 请阅读                                          |
| ------------------------ | ----------------------------------------------- |
| 开始你的第一次运行       | [快速入门](/docs/quickstart)                    |
| 了解优化的工作原理       | [优化的工作原理](/docs/concepts/optimization)   |
| 选择引擎                 | [引擎](/docs/concepts/engines)                  |
| 准备数据                 | [数据集与划分](/docs/concepts/datasets)         |
| 编写评分器               | [指标与评分器](/docs/concepts/scorers)          |
| 填写向导                 | [提交运行](/docs/guides/submit)                 |
| 查看结果                 | [结果](/docs/guides/results)                    |
| 了解费用                 | [计费](/docs/concepts/billing)                  |
| 从智能体中使用 Skynet    | [MCP](/docs/mcp)                                |
| 通过代码调用 Skynet      | [API](/docs/api)                                |
| 查看限制                 | [限制](/docs/limits)                            |

> [!TIP]
> 先用小数据集和默认设置。让评分器反馈尽量具体。好的反馈比单纯的分数能给优化器提供更多信息。
