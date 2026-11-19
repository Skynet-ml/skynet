Esta página mostra como iniciar sua primeira execução em cerca de dez minutos. Você entra na sua conta, envia um conjunto de dados pequeno, preenche as quatro etapas do assistente e inicia a execução. No exemplo, o Skynet cria um novo repositório que classifica mensagens de suporte em rótulos e depois o melhora.

## Antes de começar

Você precisa de:

- Uma conta no Skynet.
- Uma conta no GitHub. O Skynet cria o repositório nesta conta.
- Um arquivo CSV, JSON ou Excel (`.csv`, `.json`, `.xlsx` ou `.xls`). Cada linha é um caso.
- Uma ou mais colunas de entrada, por exemplo `message`.
- Uma coluna de saída esperada, por exemplo `label`.

> [!TIP]
> Use pelo menos 5 linhas. Com menos de 5 pares, o Skynet não mantém um conjunto de teste, e a pontuação final vem de casos que o otimizador usou.

## 1. Entrar

Entre no Skynet. Conecte o GitHub em **Definições → Conectores**. Depois abra a página de envio em `/submit`.

> [!NOTE]
> Esta página usa os rótulos do nível **Padrão**. O nível **Guiado** usa rótulos mais simples, pula a etapa Otimizador e seleciona **Auto** para você. Altere o nível em Definições.

## 2. Tarefa

A primeira etapa é **Tarefa**. Ela pergunta "O que quer otimizar?"

1. Em **Objetivo**, escreva o que é um bom resultado. Use palavras simples, por exemplo "Classifique cada mensagem de suporte como billing, bug ou other."
2. Opcional: em **Contexto**, adicione informações que ajudam o otimizador.
3. Selecione **Criar um repositório novo**. Digite um nome, por exemplo `support-labels`, e selecione **Privado** ou **Público**.

O Skynet cria o repositório na sua conta do GitHub. O assistente escreve uma primeira solução a partir da sua tarefa e dos seus dados. Essa solução é a versão inicial. Se você já tem código, selecione **Usar um repositório**. Leia [Iniciar uma execução](/docs/guides/submit).

## 3. Entradas

A segunda etapa é **Entradas**.

1. Selecione **Carregue um ficheiro CSV, JSON ou Excel**, ou **Escolher da biblioteca** para usar um conjunto de dados salvo.
2. Em **Como ler os dados**, atribua um papel a cada coluna:
   - **Entrada**: o seu código recebe esta coluna.
   - **Saída**: o valor esperado. O avaliador o recebe.
   - **Ignorar**: o Skynet não usa esta coluna.
3. Mantenha **Utilizar recomendação** para a divisão. Para a maioria dos conjuntos de dados, a divisão é 70% treino, 15% validação e 15% teste.

Leia [Conjuntos de dados e divisões](/docs/concepts/datasets) para ver as regras completas.

## 4. Otimizador

A terceira etapa é **Otimizador** ("Otimizador, modelos e orçamento").

- Mantenha o otimizador em **Auto**.
- Selecione um **Modelo de reflexão**. O otimizador o usa para escrever novas versões. Este modelo é obrigatório.
- Em **Orçamento**, mantenha **Máx. de execuções do avaliador** no valor padrão de 200.

Leia [Motores](/docs/concepts/engines) para escolher um motor específico.

## 5. Métrica e execução

A última etapa é **Métrica e execução**. O assistente escreve o avaliador e o arquivo `optimize.toml` para você. Você pode editá-los. A entrada `[run]` executa uma versão do repositório em um caso. O avaliador recebe a saída da entrada e o caso:

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    got = str(output).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return {"score": 1.0, "feedback": "Correct label."}
    return {"score": 0.0, "feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` contém as colunas de Entrada de um caso. `case["expected"]` contém a coluna de Saída, indexada pelo nome da coluna. O repositório tem um arquivo `serve.py` com `def serve(input)`. A entrada `[run]` o chama, e `output` é o valor que ele retorna. O avaliador roda em outra sandbox que contém apenas os seus arquivos de métrica e nenhum código da versão, então não pode importar `serve.py`. O Skynet usa a mesma função para servir a melhor versão depois da execução.

Depois execute a verificação. A verificação executa a sua configuração, a entrada `[run]` e o seu avaliador na versão inicial, na sandbox. Você só pode iniciar a execução depois que a verificação passar.

## 6. Iniciar e acompanhar

A tela de resumo mostra "Pronto para executar". Suas abas são Geral, Dados, Modelos, Orçamento e Recompensa. Confira a configuração e inicie a otimização.

Abra a execução na sua lista de otimizações para acompanhá-la. O Skynet envia cada candidato para um branch próprio no seu novo repositório. O branch `skynet/best` sempre aponta para a melhor versão. Leia [Resultados](/docs/guides/results) para entender as pontuações e a versão vencedora.

## Próximos passos

- Escreva um avaliador melhor: [Métricas e avaliadores](/docs/concepts/scorers).
- Saiba como os motores funcionam: [Como a otimização funciona](/docs/concepts/optimization).
- Saiba quanto custa uma execução: [Cobrança](/docs/concepts/billing).
