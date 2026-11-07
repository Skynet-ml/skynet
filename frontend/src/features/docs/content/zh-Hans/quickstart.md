本页介绍如何在大约十分钟内开始你的第一次运行。你需要登录、上传一个小数据集、填写向导的四个步骤，然后开始运行。在示例中，Skynet 会创建一个新仓库，用来把客服消息归类到标签，然后改进它。

## 开始之前

你需要：

- 一个 Skynet 账户。
- 一个 GitHub 账户。Skynet 会在这个账户中创建仓库。
- 一个 CSV、JSON 或 Excel 文件（`.csv`、`.json`、`.xlsx` 或 `.xls`）。每一行是一个用例。
- 一个或多个输入列，例如 `message`。
- 一个期望输出列，例如 `label`。

> [!TIP]
> 至少使用 5 行。如果少于 5 对，Skynet 不会保留测试集，最终分数就会来自优化器用过的用例。

## 1. 登录

登录 Skynet。在 **设置 → 连接器** 中连接 GitHub。然后打开提交页面 `/submit`。

> [!NOTE]
> 本页使用 **标准** 级别的标签。**引导** 级别使用更简单的标签，跳过“优化器”步骤，并为你选择 **Auto**。你可以在“设置”中更改级别。

## 2. 任务

第一步是 **任务**。它会问“你想优化什么？”

1. 在 **目标** 中写明什么是好的结果。用通俗的话，例如“把每条客服消息归类为 billing、bug 或 other。”
2. 可选：在 **背景** 中添加有助于优化器的上下文。
3. 选择 **创建新仓库**。输入一个名称，例如 `support-labels`，然后选择 **私有** 或 **公开**。

Skynet 会在你的 GitHub 账户中创建这个仓库。助手根据你的任务和数据编写第一个解决方案。这个解决方案就是起始版本。如果你已经有代码，请改为选择 **使用现有仓库**。请阅读[提交运行](/docs/guides/submit)。

## 3. 输入

第二步是 **输入**。

1. 选择 **上传 CSV、JSON 或 Excel 文件**，或选择 **从库中选择** 来使用已保存的数据集。
2. 在 **如何读取数据** 下，为每一列指定角色：
   - **输入**：你的代码会收到这一列。
   - **输出**：期望值。评分器会收到它。
   - **跳过**：Skynet 不使用这一列。
3. 划分方式保持 **使用推荐**。对大多数数据集，划分为 70% 训练、15% 验证和 15% 测试。

完整规则请阅读[数据集与划分](/docs/concepts/datasets)。

## 4. 优化器

第三步是 **优化器**（“优化器、模型和预算”）。

- 优化器保持为 **Auto**。
- 选择一个 **反思模型**。优化器用它来编写新版本。此模型为必填项。
- 在 **预算** 下，**最大评分器运行次数** 保持默认值 200。

如需选择特定引擎，请阅读[引擎](/docs/concepts/engines)。

## 5. 指标和运行

最后一步是 **指标和运行**。助手会为你编写评分器。你可以编辑它。评分器会收到仓库某个版本的路径和一个用例：

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` 包含一个用例的输入列。`case["expected"]` 包含输出列，以列名作为键。仓库中有一个 `serve.py` 文件，其中定义了 `def serve(input)`。运行结束后，Skynet 用同一个函数来部署最好的版本。

然后运行检查。检查会在沙箱中，在起始版本上运行你的安装步骤和评分器。只有检查通过后，你才能开始运行。

## 6. 开始并观察

摘要页面显示“准备运行”。它的标签页有“常规”、“数据”、“模型”、“预算”和“奖励”。检查设置，然后开始优化。

从你的运行列表中打开这次运行即可观察进度。Skynet 会把每个候选版本推送到你新仓库中它自己的分支。`skynet/best` 分支始终指向最好的版本。请阅读[结果](/docs/guides/results)来理解分数和胜出的版本。

## 后续步骤

- 编写更好的评分器：[指标与评分器](/docs/concepts/scorers)。
- 了解引擎如何工作：[优化的工作原理](/docs/concepts/optimization)。
- 了解一次运行的费用：[计费](/docs/concepts/billing)。
