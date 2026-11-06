Skynet は Optimize Anything です。すべての実行はリポジトリを最適化します。エンジンがリポジトリの新しいバージョンを提案し、スコアラーが各バージョンを採点し、Skynet が最も良いバージョンを残します。

## 共通のコントラクト

すべての実行には、リポジトリとスコアラーがあります。スコアラーは Python の関数です。リポジトリの 1 つのバージョンのパスと、1 つのケースを受け取ります。

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

- `repo_path` は、セットアップコマンドの実行後の、リポジトリの 1 つのバージョンのフォルダーです。
- `case` は dict です。`case["input"]` は 1 つのケースの入力フィールドを保持します。`case["expected"]` は期待値を保持します。期待値がないケースでは `None` です。
- `score` はペアを返します。数値と、`"feedback"` 文字列を含む dict です。

リポジトリのルートには、`def serve(input)` を持つ `serve.py` ファイルがあります。スコアラーはこの関数を呼び出せます。Skynet は実行の後、この関数を使って最も良いバージョンを提供します。

> [!NOTE]
> バージョンを書くコーディングエージェントは、スコアラーのソース、期待値、実行シークレットを見ることができません。

スコアラーのコントラクトの全体については、[メトリクスとスコアラー](/docs/concepts/scorers) をお読みください。

## コードからのモデル呼び出し

リポジトリのコードとスコアラーは、Skynet のモデルゲートウェイを通じてモデルを呼び出せます。サンドボックスは、OpenAI 互換のエンドポイントを環境変数で渡します。

| 変数                 | 内容                                                                   |
| -------------------- | ---------------------------------------------------------------------- |
| `OPENAI_BASE_URL`    | ゲートウェイの URL。`APP_GATEWAY_URL` も同じ値を持ちます。             |
| `APP_API_KEY`        | この実行専用のトークン。プロバイダーキーではありません。               |
| `APP_MODEL`          | 送信するモデル名。実行のスコアラーモデルです。                         |
| `ANTHROPIC_BASE_URL` | Anthropic SDK 用のゲートウェイの URL。                                 |

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

- Skynet のキーによる呼び出しは、実行予算に対して計測されます。
- 独自のキー (BYOK) では、呼び出しはあなたのプロバイダーキーに送られます。Skynet はモデル料金を請求しません。
- ゲートウェイは、他のモデルへのリクエストを拒否します。
- 実行シークレットは、環境変数としてコードとスコアラーに届きます。変数名はシークレット名です。Skynet は出力の中のシークレットの値を隠します。

サンドボックスには、これ以外のネットワークアクセスはありません。

## エンジン

すべてのエンジンがリポジトリで動作します。

- **Auto** は既定の選択です。いくつかのエンジンを試し、その後、最も良いバージョンから GEPA を続けます。
- **GEPA** は、リフレクションによってバージョンを進化させ、パレートフロントを維持します。
- **Best-of-N** は、リフレクションモデルに独立した提案を求め、最も良いものを残します。
- **AutoResearch**、**Meta-Harness**、**AutoSaddler**、**ShinkaEvolve** は、コーディングエージェントを使ってバージョンを提案します。

エンジンは、許可したパスにあるファイルだけを変更します。各エンジンとその設定については、[エンジン](/docs/concepts/engines) をお読みください。

## Auto

**Auto** は、すべての実行で、ウィザードの既定の選択です。

1. Auto は 5 つのエンジンで探索します。GEPA、AutoResearch、Meta-Harness、AutoSaddler、ShinkaEvolve です。
2. それらが見つけた最も良いバージョンを選びます。
3. 予算がなくなるまで、そのバージョンから GEPA を続けます。

Auto はスコアラーのフィードバックテキストを読みます。

## 開始バージョンの出どころ

| 状況                                  | 使うもの                                                                                 |
| ------------------------------------- | ---------------------------------------------------------------------------------------- |
| GitHub リポジトリにコードがある       | **既存のリポジトリを使う**。選んだブランチまたはコミットのコードが開始バージョンです。         |
| タスクとデータだけがある              | **新しいリポジトリを作成**。Skynet が最初の解決策を書きます。それが開始バージョンです。  |

> [!WARNING]
> GEPA と Auto は、フィードバックの文章から学習します。フィードバックが常に同じ文字列だと、これらのオプティマイザーが使える情報が少なくなります。

## 関連ページ

- [エンジン](/docs/concepts/engines)
- [データセットと分割](/docs/concepts/datasets)
- [メトリクスとスコアラー](/docs/concepts/scorers)
- [実行を開始する](/docs/guides/submit)
