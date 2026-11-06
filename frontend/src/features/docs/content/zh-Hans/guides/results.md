运行页面显示运行的进度和结果。本指南说明运行页面上的标签页、各项分数，以及如何导出或部署最佳版本。

## 运行页面上的标签页

运行页面只显示适用于该运行的标签页。部分标签页取决于状态和你的体验级别。

| 标签页             | 何时显示                                       | 包含的内容                                         |
| ------------------ | ---------------------------------------------- | -------------------------------------------------- |
| **概览**           | 始终                                           | 进度、分数和分数历史                               |
| **用量**           | 成功的运行，且其仓库有 `serve.py` 文件         | 用于调用最佳版本的表单，以及集成代码               |
| **产物**           | 有结果的运行                                   | 你的仓库的各个版本，以及最佳版本                   |
| **数据**           | 已完成且有用例的运行                           | 划分为训练集、验证集和测试集的数据                 |
| **代码**           | 运行有可显示的代码时                           | 评分器                                             |
| **奖励**           | 评分器声明了奖励部分的运行                     | 奖励组成部分及其权重                               |
| **日志**           | 大多数运行                                     | 运行日志                                           |
| **用量与费用**     | 你已登录时                                     | 运行的花费                                         |
| **定时运行**       | 成功的运行                                     | 从最佳版本再次运行优化的计划                       |
| **配置**           | 始终                                           | 运行配置                                           |

> [!NOTE]
> 在 **引导** 级别下，运行页面会隐藏 **数据**、**代码**、**日志** 和 **配置** 标签页。通过直接链接仍然可以打开这些标签页。你可以在 **设置 → 账户** 中更改级别。

## 分数

Skynet 根据验证用例选择版本，并在测试用例上报告最终分数。引擎永远看不到测试用例。请阅读[数据集与划分](/docs/concepts/datasets)。

**概览** 标签页显示：

- **提升**：从起始版本到最佳版本的变化。
- **分数变化**：运行过程中的分数。
- **分数明细**：每项分数，包含 **基线**、**已优化** 和 **变化** 列。

运行进行中时，概览还会显示 **已用时**、**剩余** 和 **速率**。

当你的评分器报告具名分数时，概览还会显示 **命名分数**。

### 当最佳版本就是起始版本时

有时没有任何候选能超过起始版本。这时 Skynet 会返回起始版本。**产物** 标签页会告诉你已应用回归保护。

如果运行因达到预算而停止，运行页面会告诉你 Skynet 保存了哪个版本。请阅读[运行](/docs/concepts/runs)。

## 导出最佳版本

**产物** 标签页列出了你的仓库的 **版本**。对于每个版本，你可以：

- 复制该版本。
- 将该版本下载为文件。
- 将该版本下载为包含整个仓库的 zip 文件。

你可以把一个版本与另一个版本进行比较。该标签页还会显示某个版本的 **评分器反馈**。

### 新仓库中的分支

当 Skynet 为本次运行创建了仓库时，它会把每个候选推送到各自的分支 `skynet/candidate-<version id>`。`skynet/best` 分支始终指向最佳版本。出现更好的版本时，它会随之移动。

## 部署最佳版本

你可以从自己的代码中调用最佳版本。仓库的根目录必须有一个定义了 `serve(input)` 的 `serve.py` 文件：

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input` 是请求中输入字段的 dict。
- 返回值必须可以序列化为 JSON。dict 会成为 `outputs`。其他任何值会成为 `{"output": value}`。
- `serve.py` 可以使用 `skynet` 辅助模块中的 `llm` 调用模型。它不会获得运行的密钥。

Skynet 在与运行相同的受保护沙箱中运行最佳版本。**用量** 标签页包含两个部分：

- 一个表单。输入各输入字段并选择 **运行**，即可得到结果。
- cURL、Python、JavaScript 和 Go 的 **集成代码**。

要调用最佳版本，请在 **设置 → API** 中创建 API 令牌。然后发送请求。`Idempotency-Key` 请求头是必需的：

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

把 `message` 替换为你的运行的输入字段。**用量** 标签页上的 **集成代码** 会为你填好真实的 URL 和字段名称。

响应包含 `outputs`、`input_fields`、`output_fields`、`model_used`、`cents_charged` 和 `budget`。

> [!TIP]
> 每次调用单独计费，从调用者的余额中扣除。`max_cost_cents` 设置这一次调用最多可以花费的金额，单位为美分。如果你再次发送相同的 `Idempotency-Key`，Skynet 不会再次对该调用收费。

关于身份验证和其他端点，请阅读 [API](/docs/api)。

## 分享结果

你可以把运行分享给其他人，或把它列入公开的“探索”展示区。请阅读[分享与发布](/docs/guides/sharing)。
