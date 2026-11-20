O avaliador diz ao Skynet quão boa é uma versão do seu repositório. Ele é uma função Python em `scorer.py`. Todos os motores chamam a mesma função. Esta página apresenta o contrato, o valor de retorno e exemplos.

## O contrato

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argumento   | O que contém                                                                     |
| ----------- | -------------------------------------------------------------------------------- |
| `repo_path` | Uma pasta que contém apenas os seus arquivos de métrica. Ela não contém nenhum outro arquivo do repositório nem código da versão |
| `case`      | Um dict. `case["input"]` contém os campos de entrada. `case["expected"]` contém o valor esperado, ou `None` |
| `output`    | O que a entrada `[run]` retornou para o caso, ou `None` se o arquivo não tiver `[run]` |

O arquivo `ascent.toml` do repositório precisa ter uma seção `[run]` ou uma etapa `[target.<name>]`. A entrada `[run]` (`entry`) executa uma versão em um caso, na sua própria sandbox. O Skynet recusa um arquivo sem nenhuma das duas, porque todas as versões receberiam a mesma pontuação.

- Uma entrada de função, por exemplo `entry = "serve.py:serve"`, recebe `case["input"]` como argumento. `output` é o valor que ela retorna.
- Uma entrada de comando, por exemplo `entry = ["python", "run_tests.py"]`, lê `{"input": ...}` do arquivo JSON no caminho de `ASCENT_INPUT`. Ela escreve `{"output": ...}` no caminho de `ASCENT_OUTPUT`. `output` é o valor em `"output"`. O que o comando imprime vai só para os logs.

O avaliador roda em outra sandbox. Essa sandbox contém apenas os seus arquivos de métrica. O avaliador não pode importar nem executar o código da versão. Ele vê a versão apenas por meio de `output`, ou por meio de `call()` para uma etapa `[target.<name>]`. Importe `call` com `from openascent import call`. Leia [Como a otimização funciona](/docs/concepts/optimization).

## O valor de retorno

`score` retorna um objeto:

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

- `"score"` é obrigatório. É um número finito. Só esta pontuação ordena as versões.
- `"feedback"` é um texto opcional. Ele é recomendado, porque GEPA e Auto aprendem com ele. Sem feedback, os motores veem só as pontuações, e o log da execução avisa isso uma vez.
- `"scores"` é opcional. Ele mapeia nomes para pontuações nomeadas. Cada pontuação nomeada é um objeto com o seu próprio `"score"` e um `"feedback"` opcional. Todo motor usa cada pontuação nomeada como um objetivo próprio.

Qualquer outro formato ou chave é um erro. A verificação mostra a parte errada e o formato válido.

| Valor de retorno | Válido |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Sim |
| `{"score": 0.8}` | Sim. Não há feedback. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Sim |
| `0.8` | Não. O valor não é um objeto. |
| `(0.8, "Close. Missing the date.")` | Não. O valor não é um objeto. |
| `{"score": "0.8"}` | Não. A pontuação é uma string. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | Não. Uma pontuação nomeada deve ser um objeto. |
| `{"score": 0.8, "feedbak": "OK"}` | Não. A chave `feedbak` é desconhecida. |

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

```toml tab="ascent.toml"
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

`case["expected"]` é um dict indexado pelo nome da sua coluna de Saída. Leia [Conjuntos de dados e divisões](/docs/concepts/datasets).

### Rodar os testes do repositório

A entrada `[run]` roda os testes. O `pytest` sai com um código diferente de zero quando um teste falha, e um código de saída diferente de zero faz o caso falhar. Por isso o wrapper `run_tests.py` escreve o resultado em `ASCENT_OUTPUT` e sai com 0. O avaliador lê esse resultado.

```toml tab="ascent.toml"
version = 1

[run]
entry = ["python", "run_tests.py"]
timeout = 300
```

```python tab="run_tests.py"
import json
import os
import subprocess

result = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True)
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"output": {"passed": result.returncode == 0, "log": result.stdout[-2000:]}}, f)
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["passed"]:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["log"]}
```

Quando a entrada `[run]` falha em um caso, esse caso não tem nota. Se a entrada não inicia, excede o tempo limite ou um sinal a para, ou se uma entrada de função lança uma exceção ou sai antes de retornar um valor, o feedback é `Run failed: …`, e o Skynet não chama o avaliador. Uma entrada de comando também falha quando sai com um código diferente de zero ou não escreve um arquivo de saída válido. Uma versão com um caso assim não tem nota. Ela não pode ser a melhor versão, não atinge nenhum `[outcome] target` e não abre nenhum pull request. Se uma falha precisa contar como um número, capture a falha no seu programa, escreva uma saída e deixe o avaliador dar uma nota a ela. Leia as [regras do repositório](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md).

### Modelo como juiz

O avaliador pode chamar um modelo. Importe `llm` do módulo auxiliar `openascent`. A chamada passa pelo gateway de modelos do Skynet e vai para o modelo do avaliador da execução.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from openascent import llm


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

O avaliador também pode usar qualquer cliente compatível com OpenAI com as variáveis de ambiente `OPENAI_BASE_URL`, `APP_API_KEY` e `APP_MODEL`. O gateway aceita apenas o modelo do avaliador da execução.

## Chamadas de modelo e segredos

- As chamadas com as chaves do Skynet são medidas no orçamento da execução. Com a sua própria chave (BYOK), a chamada vai para a chave do seu provedor, sem taxa de modelo do Skynet. Leia [Cobrança](/docs/concepts/billing).
- O avaliador não recebe os segredos da execução como variáveis de ambiente. Um segredo chega ao avaliador só por uma tabela `[services.<name>]` que `[metric] services` lista, ou por um servidor do arquivo `[metric] mcp`. Escreva o segredo nos `headers` da tabela como `${NAME}`. O avaliador envia as requisições para a URL em `ASCENT_SERVICE_<NAME>_URL`. O Skynet adiciona o segredo fora da sandbox.
- O Skynet oculta os valores dos segredos na saída do avaliador e nos logs.

## Limites

| Item                        | Valor                                  |
| --------------------------- | -------------------------------------- |
| Tempo limite do avaliador   | 60 segundos por padrão, no máximo 600  |

## Páginas relacionadas

- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Motores](/docs/concepts/engines)
- [Iniciar uma execução](/docs/guides/submit)
