メトリクスは、出力がどれだけ良いかを Skynet に伝えます。メトリクスは `metric.py` 内の Python 関数です。すべてのエンジンが同じ関数を呼び出します。このページでは、コントラクト、戻り値、例を紹介します。

## コントラクト

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| 引数       | 保持する内容                                                       |
| ---------- | ------------------------------------------------------------------ |
| `input`    | ケースの入力フィールドを持つ dict                                  |
| `output`   | そのケースで `run(artifact, input)` が返したもの                   |
| `expected` | ケースの期待値。期待値がないケースでは `None`                      |

関数が引数を 2 つしか受け取らない場合、Skynet は `score(input, output)` を呼び出します。

DSPy プログラムの場合、Skynet は DSPy のメトリクス呼び出しをこの同じ関数に適合させます。どのアーティファクトの種類でも、書くメトリクスは 1 つです。[最適化の仕組み](/docs/concepts/optimization) をお読みください。

## 戻り値

`score` はペア（タプル、または 2 要素のリスト）を返す必要があります。

1. **スコア。** 数値です。オプティマイザーはこれを高くしようとします。
2. **付加情報。** dict です。文字列値を持つ `"feedback"` キーが必要です。

付加情報には `"scores"` キーを含めることもできます。名前を数値に対応付けます。例: `{"format": 1.0, "accuracy": 0.5}`。

戻り値の形が異なる場合、コントラクトチェックは **メトリクスの戻り値** で失敗します。

| 戻り値                                                 | 有効か                                   |
| ------------------------------------------------------ | ---------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | はい                                     |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | はい                                     |
| `0.8`                                                  | いいえ。付加情報がありません。           |
| `(0.8, "Close.")`                                      | いいえ。付加情報が dict ではありません。 |
| `(0.8, {})`                                            | いいえ。フィードバックの文字列がありません。 |
| `("0.8", {"feedback": "OK"})`                          | いいえ。スコアが文字列です。             |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | いいえ。名前付きスコアが数値ではありません。 |

## 役に立つフィードバックを書く

GEPA、DSPy GEPA、Auto は、フィードバックの文章を読んで次のバージョンを書きます。何が間違っているか、正しい出力はどのようなものかを書いてください。

| 弱いフィードバック | 役に立つフィードバック                                                            |
| ------------------ | --------------------------------------------------------------------------------- |
| "Wrong."           | "Expected 'billing', got 'other'. The message mentions a charge."                 |
| "Bad format."      | "The output is not valid JSON. It has a trailing comma after the last field."     |

> [!NOTE]
> オプティマイザーがメトリクスのソースを見ることはありません。DSPy オプティマイザーでは、リフレクションモデルが見るのはフィードバックの文章だけです。期待値はメトリクスの中にとどまります。

## 例

### 完全一致

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` は、出力列の名前をキーとする dict です。[データセットと分割](/docs/concepts/datasets) をお読みください。

### 複数のチェック

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

### 審査員としてのモデル

メトリクスはモデルを呼び出せます。`skynet` ヘルパーモジュールから `llm` をインポートします。メトリクスからの呼び出しは、メトリクスのモデルに送られます。

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

メトリクスは自身の `llm()` 呼び出しでモデルを指定します。表示した4つの引数はすべて省略できます。`model=` を省略すると、メトリクスはリフレクションモデルを使います。

## 精度、速度、コスト

メトリクスでは、精度に、測定された実行時間とモデルのコストを組み合わせることができます。モジュールレベルで `REWARD_SPEC` を定義し、`reward` の値を返します。

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

- `weights` には `accuracy`、`speed`、`cost` が必要です。各重みは 0 以上で、すべてを 0 にすることはできません。Skynet は合計が 1 になるように重みを調整します。
- `speed.reference` の単位はミリ秒です。`cost.reference` の単位は米ドルです。どちらも正の値である必要があります。
- `accuracy.reference` がない場合、精度は 0〜1 のスコアです。ある場合、精度は誤差となり、低いほど良くなります。

時間とコストは Skynet 自身が測定します。実行コードがそれらを変更することはできません。Skynet が時間またはコストを測定できない場合、その部分のスコアは 0 になり、フィードバックにその旨が示されます。

メトリクスは、`skynet` の `observation()` と `service_cost()` を使って、測定値を直接読み取ることもできます。

## 制限

| 項目                       | 値                                  |
| -------------------------- | ----------------------------------- |
| メトリクスのタイムアウト   | 既定で 60 秒、最大 600 秒           |
| コントラクトチェックのサンプル     | 3 ケース                            |

## 関連ページ

- [データセットと分割](/docs/concepts/datasets)
- [エンジン](/docs/concepts/engines)
- [実行を開始する](/docs/guides/submit)
