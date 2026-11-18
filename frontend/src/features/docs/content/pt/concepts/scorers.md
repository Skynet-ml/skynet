O avaliador diz ao Skynet quão boa é uma versão do seu repositório. Ele é uma função Python em `scorer.py`. Todos os motores chamam a mesma função. Esta página apresenta o contrato, o valor de retorno e exemplos.

## O contrato

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argumento   | O que contém                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Uma pasta que contém apenas os seus arquivos de métrica. Ela não contém nenhum outro arquivo do repositório nem código da versão |
| `case`      | Um dict. `case["input"]` contém os campos de entrada. `case["expected"]` contém o valor esperado, ou `None` |
| `output`    | O que a entrada `[run]` retornou para o caso |

O arquivo `optimize.toml` do repositório precisa ter uma seção `[run]` ou uma etapa `[target.<name>]`. A entrada `[run]` (`entry`) executa uma versão em um caso, na sua própria sandbox. O Skynet recusa um arquivo sem nenhuma das duas, porque todas as versões receberiam a mesma pontuação.

- Uma entrada de função, por exemplo `entry = "serve.py:serve"`, recebe `case["input"]` como argumento. `output` é o valor que ela retorna.
- Uma entrada de comando, por exemplo `entry = ["python", "-m", "pytest", "-q"]`, recebe a entrada do caso como JSON no stdin. `output` é `{"exit_code": ..., "stdout": "..."}`.

O avaliador roda em outra sandbox. Essa sandbox contém apenas os seus arquivos de métrica. O avaliador não pode importar nem executar o código da versão. Ele vê a versão apenas por meio de `output`. Leia [Como a otimização funciona](/docs/concepts/optimization).

## O valor de retorno

`score` deve retornar uma pontuação com feedback. Estes formatos são válidos:

1. Um par: um número e uma string de feedback ou um dict com uma chave `"feedback"`.
2. Um dict com um número em `"score"` e uma string em `"feedback"`.

O feedback deve ser uma string que não esteja vazia.

As informações adicionais também podem ter uma chave `"scores"`. Ela mapeia nomes para pontuações nomeadas. Cada pontuação nomeada tem a sua própria pontuação e o seu próprio feedback, por exemplo `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

As informações adicionais também podem ter uma chave `"cases"` com o mesmo formato. Cada entrada é o resultado de um caso. Todo motor usa cada caso como um objetivo próprio.

Se o valor de retorno tiver outro formato, a verificação falha e mostra o motivo.

| Valor de retorno                                                                | Válido                                       |
| ------------------------------------------------------------------------------- | -------------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Sim                                          |
| `(0.8, "Close. Missing the date.")`                                             | Sim                                          |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Sim                               |
| `0.8`                                                                           | Não. Não há feedback.                        |
| `(0.8, {})`                                                                     | Não. Não há string de feedback.              |
| `("0.8", {"feedback": "OK"})`                                                   | Não. A pontuação é uma string.               |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | Não. Uma pontuação nomeada não é um número. |

## Escrever um feedback útil

O GEPA e o Auto leem o texto de feedback para escrever a próxima versão. Diga o que está errado e como é uma saída correta.

| Feedback fraco  | Feedback útil                                                                       |
| --------------- | ----------------------------------------------------------------------------------- |
| "Errado."       | "Esperado 'billing', obtido 'other'. A mensagem menciona uma cobrança."             |
| "Formato ruim." | "A saída não é um JSON válido. Há uma vírgula sobrando depois do último campo."     |

> [!NOTE]
> O agente de código nunca vê o código do seu avaliador, os valores esperados nem os segredos da execução. Ele vê apenas as pontuações e o texto de feedback.

## Exemplos

### Correspondência exata

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]` é um dict indexado pelo nome da sua coluna de Saída. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

### Rodar os testes do repositório

A entrada `[run]` roda os testes. O avaliador lê o código de saída e a saída dos testes.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

O Skynet dá ele mesmo nota 0 a uma versão quando a entrada `[run]` dela falha. Se a entrada não inicia, excede o tempo limite ou um sinal a para, o feedback é `Run failed: …`, e o Skynet não chama o avaliador. Um código de saída diferente de zero não é uma falha: o avaliador o recebe em `output`.

### Modelo como juiz

O avaliador pode chamar um modelo. Importe `llm` do módulo auxiliar `skynet`. A chamada passa pelo gateway de modelos do Skynet e vai para o modelo do avaliador da execução.

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
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

O avaliador também pode usar qualquer cliente compatível com OpenAI com as variáveis de ambiente `OPENAI_BASE_URL`, `APP_API_KEY` e `APP_MODEL`. O gateway aceita apenas o modelo do avaliador da execução.

## Chamadas de modelo e segredos

- As chamadas com as chaves do Skynet são medidas no orçamento da execução. Com a sua própria chave (BYOK), a chamada vai para a chave do seu provedor, sem taxa de modelo do Skynet. Leia [Cobrança](/docs/concepts/billing).
- O avaliador não recebe os segredos da execução como variáveis de ambiente. Um segredo chega ao avaliador só por uma tabela `[services.<name>]` que `[metric] services` lista. Escreva o segredo nos `headers` da tabela como `${NAME}`. O avaliador envia as requisições para a URL em `SKYNET_SERVICE_<NAME>_URL`. O Skynet adiciona o segredo fora da sandbox.
- O Skynet oculta os valores dos segredos na saída do avaliador e nos logs.

## Limites

| Item                        | Valor                                  |
| --------------------------- | -------------------------------------- |
| Tempo limite do avaliador   | 60 segundos por padrão, no máximo 600  |

## Páginas relacionadas

- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar uma execução](/docs/guides/submit)
