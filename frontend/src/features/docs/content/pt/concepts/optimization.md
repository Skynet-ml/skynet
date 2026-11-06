O Skynet é o Optimize Anything: toda execução otimiza um repositório. Um motor propõe novas versões do repositório, o seu avaliador pontua cada versão e o Skynet mantém a melhor.

## O contrato comum

Toda execução tem um repositório e um avaliador. O avaliador é uma função Python que recebe o caminho de uma versão do repositório e um caso.

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

- `repo_path` é a pasta de uma versão do repositório, depois que o comando de configuração rodou.
- `case` é um dict. `case["input"]` contém os campos de entrada de um caso. `case["expected"]` contém o valor esperado, ou `None` quando o caso não tem um.
- `score` retorna um par: um número e um dict com uma string `"feedback"`.

O repositório tem um arquivo `serve.py` na raiz com `def serve(input)`. O avaliador pode chamá-lo, e o Skynet o usa para servir a melhor versão depois da execução.

> [!NOTE]
> O agente de código que escreve as versões não vê o código do avaliador, os valores esperados nem os segredos da execução.

Leia [Métricas e avaliadores](/docs/concepts/scorers) para ver o contrato completo do avaliador.

## Chamadas de modelo a partir do seu código

O código do seu repositório e o seu avaliador podem chamar um modelo pelo gateway de modelos do Skynet. A sandbox fornece um endpoint compatível com OpenAI em variáveis de ambiente:

| Variável             | O que contém                                                 |
| -------------------- | ------------------------------------------------------------ |
| `OPENAI_BASE_URL`    | A URL do gateway. `APP_GATEWAY_URL` contém o mesmo valor.    |
| `APP_API_KEY`        | Um token só para esta execução. Não é uma chave de provedor. |
| `APP_MODEL`          | O nome do modelo a enviar. É o modelo do avaliador da execução. |
| `ANTHROPIC_BASE_URL` | A URL do gateway para SDKs da Anthropic.                     |

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

- As chamadas com as chaves do Skynet são medidas no orçamento da execução.
- Com a sua própria chave (BYOK), a chamada vai para a chave do seu provedor. O Skynet não cobra taxa de modelo.
- O gateway recusa pedidos para qualquer outro modelo.
- Os segredos da execução chegam ao seu código e ao seu avaliador como variáveis de ambiente. O nome da variável é o nome do segredo. O Skynet oculta os valores dos segredos na saída.

A sandbox não tem nenhum outro acesso à rede.

## Motores

Todos os motores funcionam com repositórios:

- O **Auto** é o padrão. Ele testa vários motores e depois continua o GEPA a partir da melhor versão.
- O **GEPA** evolui versões por reflexão e mantém uma fronteira de Pareto.
- O **Best-of-N** pede ao modelo de reflexão propostas independentes e mantém a melhor.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** e **ShinkaEvolve** usam um agente de código para propor versões.

Os motores alteram apenas os arquivos nos caminhos que você permite. Leia [Motores](/docs/concepts/engines) para ver cada motor e as suas configurações.

## Auto

O **Auto** é a escolha padrão no assistente, para todas as execuções.

1. O Auto explora com cinco motores: GEPA, AutoResearch, Meta-Harness, AutoSaddler e ShinkaEvolve.
2. Ele escolhe a melhor versão que eles encontraram.
3. Ele continua o GEPA a partir dessa versão até o orçamento acabar.

O Auto lê o texto de feedback do seu avaliador.

## De onde vem a versão inicial

| A sua situação                            | Use                                                                                     |
| ----------------------------------------- | --------------------------------------------------------------------------------------- |
| Você tem código em um repositório do GitHub | **Usar um repositório**. O código no branch ou commit que você escolhe é o ponto de partida. |
| Você tem apenas uma tarefa e dados        | **Criar um repositório novo**. O Skynet escreve uma primeira solução. Esse é o ponto de partida. |

> [!WARNING]
> O GEPA e o Auto aprendem com o texto de feedback. Se o seu feedback for sempre a mesma string, esses otimizadores terão menos informação para trabalhar.

## Páginas relacionadas

- [Motores](/docs/concepts/engines)
- [Conjuntos de dados e divisões](/docs/concepts/datasets)
- [Métricas e avaliadores](/docs/concepts/scorers)
- [Iniciar uma execução](/docs/guides/submit)
