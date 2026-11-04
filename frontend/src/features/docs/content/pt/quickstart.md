Esta página mostra como iniciar sua primeira execução em cerca de dez minutos. Você entra na sua conta, envia um conjunto de dados pequeno, preenche as quatro etapas do assistente e inicia a execução. O exemplo melhora um programa DSPy que classifica mensagens de suporte em rótulos.

## Antes de começar

Você precisa de:

- Uma conta no Skynet.
- Um arquivo CSV, JSON ou Excel (`.csv`, `.json`, `.xlsx` ou `.xls`). Cada linha é um caso.
- Uma ou mais colunas de entrada, por exemplo `message`.
- Uma coluna de saída esperada, por exemplo `label`.

> [!TIP]
> Use pelo menos 5 linhas. Com menos de 5 pares, o Skynet não mantém um conjunto de teste, e a pontuação final vem de casos que o otimizador usou.

## 1. Entrar

Entre no Skynet. Depois abra a página de envio em `/submit`.

> [!NOTE]
> Esta página usa os rótulos do nível **Padrão**. O nível **Guiado** usa rótulos mais simples, pula a etapa Otimizador e seleciona **Auto** para você. Altere o nível em Definições.

## 2. Tarefa

A primeira etapa é **Tarefa**. Ela pergunta "O que quer otimizar?"

1. Em **Objetivo**, escreva o que é um bom resultado. Use palavras simples, por exemplo "Classifique cada mensagem de suporte como billing, bug ou other."
2. Opcional: em **Contexto**, adicione informações que ajudam o otimizador.
3. Em **O que será otimizado**, selecione **Programa DSPy**.
4. Em **Versão inicial**, cole a instrução atual do seu programa.

## 3. Entradas

A segunda etapa é **Entradas**.

1. Selecione **Carregue um ficheiro CSV, JSON ou Excel**, ou **Escolher da biblioteca** para usar um conjunto de dados salvo.
2. Em **Como ler os dados**, atribua um papel a cada coluna:
   - **Entrada**: o código de execução recebe esta coluna.
   - **Saída**: o valor esperado. A métrica o recebe.
   - **Ignorar**: o Skynet não usa esta coluna.
3. Mantenha **Utilizar recomendação** para a divisão. Para a maioria dos conjuntos de dados, a divisão é 70% treino, 15% validação e 15% teste.

Leia [Conjuntos de dados e divisões](/docs/concepts/datasets) para ver as regras completas.

## 4. Otimizador

A terceira etapa é **Otimizador** ("Otimizador, modelos e orçamento").

- Mantenha o otimizador em **Auto**.
- Selecione um **Modelo de reflexão**. O otimizador o usa para escrever novas versões. Este modelo é obrigatório.
- Selecione um **Modelo da tarefa**. Seu programa o usa. Um programa DSPy precisa de um.
- Em **Orçamento**, mantenha **Máx. de execuções do avaliador** no valor padrão de 200.

Leia [Motores](/docs/concepts/engines) para escolher um motor específico.

## 5. Métrica e execução

A última etapa é **Métrica e execução** ("Código e métrica"). Você escreve dois arquivos.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact` é a instrução atual que o otimizador propõe. O Skynet a aplica a `program`. `input` contém as colunas de Entrada de um caso. `expected` contém a coluna de Saída, indexada pelo nome da coluna.

Depois selecione **Verificar** em **Verificação do contrato**. A verificação executa o seu código e a sua métrica na sandbox em 3 casos de amostra. Ela verifica:

| Verificação                     | O que falha                                                        |
| ------------------------------- | ------------------------------------------------------------------ |
| Assinatura do código de execução | `run.py` não define `run(artifact, input)`                        |
| Assinatura da métrica           | `metric.py` não define `score(input, output, expected=None)`       |
| Modelo da métrica               | O modelo da métrica não pode ser usado                             |
| Código de execução nas amostras | O seu código de execução lança um erro em um caso de amostra       |
| Valor devolvido pela métrica    | `score` não retorna uma pontuação e um feedback válidos            |

> [!NOTE]
> Você só pode iniciar a execução depois que a verificação do contrato passar.

## 6. Iniciar e acompanhar

A tela de resumo mostra "Pronto para executar". Suas abas são Geral, Dados, Modelos, Orçamento e Recompensa. Confira a configuração e inicie a otimização.

Abra a execução na sua lista de otimizações para acompanhá-la. Leia [Resultados](/docs/guides/results) para entender as pontuações e a versão vencedora.

## Próximos passos

- Escreva uma métrica melhor: [Métricas e avaliadores](/docs/concepts/scorers).
- Saiba quais motores servem para o seu artefato: [Como a otimização funciona](/docs/concepts/optimization).
- Saiba quanto custa uma execução: [Cobrança](/docs/concepts/billing).
