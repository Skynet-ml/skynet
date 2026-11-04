Skynet 用来改进你已有的产物。你提供一个起始版本、一组用例和一个指标。Skynet 会提出新版本，在你的用例上运行每个版本，用你的指标为输出打分，并保留最好的版本。

## Skynet 能优化什么

Skynet 支持两种产物类型：

| 产物类型     | 改动的内容                                  |
| ------------ | ------------------------------------------- |
| 仓库 | 你允许修改的路径中的文件 |
| DSPy 程序    | `dspy.Module` 中各预测器（predictor）的指令 |

每次运行都遵循同一个契约。你的运行代码定义 `run(artifact, input)`。你的指标定义 `score(input, output, expected=None)`。指标返回一个分数和一段文字反馈。优化器读取反馈，让下一个版本更好。

> [!NOTE]
> 你的运行代码和指标在沙箱中运行，并且位于不同的进程。运行代码看不到指标的源代码，也看不到期望值。

## 一个产品，多个引擎

Skynet 就是 Optimize Anything。产物类型决定了哪些引擎可以处理它：

- 仓库使用原生引擎：GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler 和 ShinkaEvolve。
- DSPy 程序产物使用 DSPy 优化器：DSPy GEPA、DSPy MIPROv2 和 DSPy BootstrapFewShot。

对于 DSPy 程序，**Auto** 使用 DSPy GEPA。对于仓库，你需要选择一个原生引擎。请阅读 [优化的工作原理](/docs/concepts/optimization) 和 [引擎](/docs/concepts/engines)。

## 一次运行如何进行

1. 你描述任务并设置起始版本。
2. 你提供用例。Skynet 把它们划分为训练集、验证集和测试集。
3. 你选择优化器、模型和预算。
4. 你编写运行代码和指标。契约检查会在样例用例上运行它们。
5. Skynet 执行优化并展示结果。

Skynet 根据验证用例选择版本，并在测试用例上报告最终分数。引擎永远看不到测试用例。

## 从哪里开始

| 如果你想                 | 请阅读                                          |
| ------------------------ | ----------------------------------------------- |
| 开始你的第一次运行       | [快速入门](/docs/quickstart)                    |
| 了解优化的工作原理       | [优化的工作原理](/docs/concepts/optimization)   |
| 选择引擎                 | [引擎](/docs/concepts/engines)                  |
| 准备数据                 | [数据集与划分](/docs/concepts/datasets)         |
| 编写指标                 | [指标与评分器](/docs/concepts/scorers)          |
| 填写向导                 | [提交运行](/docs/guides/submit)                 |
| 查看结果                 | [结果](/docs/guides/results)                    |
| 了解费用                 | [计费](/docs/concepts/billing)                  |
| 从智能体中使用 Skynet    | [MCP](/docs/mcp)                                |
| 通过代码调用 Skynet      | [API](/docs/api)                                |
| 查看限制                 | [限制](/docs/limits)                            |

> [!TIP]
> 先用小数据集和默认设置。让指标反馈尽量具体。好的反馈比单纯的分数能给优化器提供更多信息。
