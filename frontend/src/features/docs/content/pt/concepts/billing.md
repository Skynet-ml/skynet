O Skynet usa um saldo pré-pago. Você adiciona fundos ao saldo. Cada execução tem um limite de gastos, e o Skynet cobra do seu saldo o uso real da execução. Esta página explica o saldo, os orçamentos de execução, as reservas, as taxas e as suas próprias chaves de provedor.

> [!NOTE]
> Os números desta página são os padrões atuais. O app mostra os valores atualizados. Abra **Definições → Faturação** para ver o seu saldo, a taxa de recarga e o seu histórico de transações.

## Saldo

O seu saldo é em dólares americanos. O Skynet o mantém em centavos.

Por padrão, o Skynet não dá saldo inicial gratuito. Adicione fundos antes de iniciar a sua primeira execução paga.

Você adiciona fundos em **Definições → Faturação**. Você pode escolher um valor fixo ou inserir um valor personalizado:

| Recarga            | Valor adicionado ao seu saldo    |
| ------------------ | -------------------------------- |
| Valores fixos      | US$ 5, US$ 20 ou US$ 50          |
| Personalizado      | Qualquer valor de US$ 5 a US$ 1.000 |

O valor que você escolhe é o valor que entra no seu saldo. A taxa de recarga é cobrada à parte. Veja as taxas abaixo.

**Definições → Uso** mostra o que você gastou. O histórico de transações em **Definições → Faturação** lista as suas recargas e as cobranças das execuções.

## Orçamentos de execução

Cada execução tem um limite de gastos. O limite é o máximo que a execução pode gastar. Ele cobre a configuração e a otimização.

Uma execução não tem uma estimativa de custo antes de começar. Ela gasta até o seu limite, para no orçamento e mantém a melhor versão. Leia [Execuções](/docs/concepts/runs) para ver como o Skynet decide quando parar.

Você define o limite na tela **Orçamento** do assistente de envio, no campo **Limite de gastos**:

- **Definir um limite** é o modo padrão. O limite começa em US$ 5. Se a execução precisar de mais de US$ 5 para começar, o limite começa nesse valor.
- **Sem limite** permite que a execução use o seu saldo até terminar. A execução nunca pausa. Se o seu saldo acabar, a execução para e mantém o que produziu.

### O valor necessário para começar

O assistente mostra o valor **Necessário para começar**. Esse valor é:

- as retenções para as sandboxes que a execução abre antes da primeira chamada de modelo, mais
- uma margem extra para as primeiras chamadas de modelo. A margem extra é de US$ 1, ou o custo da chamada individual mais cara dos modelos selecionados, se for maior.

O assistente não deixa você continuar quando o limite está abaixo desse valor. Ele também bloqueia você quando o seu saldo está abaixo desse valor. Aumente o limite ou adicione fundos para continuar.

> [!TIP]
> Quando uma execução para no limite do orçamento, use **Aumentar o limite e continuar** na página da execução. A execução continua a partir do último checkpoint.

## Reservas

Antes de o Skynet fazer trabalho pago, ele reserva parte do limite da execução. O Skynet libera a reserva quando o trabalho é liquidado e cobra apenas o custo real.

Quando um orçamento já tem atividade, o cartão **Limite de gastos** no assistente mostra o seu extrato:

| Rótulo                      | Significado                                                                         |
| --------------------------- | ----------------------------------------------------------------------------------- |
| **Restante neste limite**   | A parte do limite que resta.                                                        |
| **Gasto até agora**         | O custo já liquidado.                                                               |
| **Reservado**               | Dinheiro retido para trabalho em andamento. O Skynet o libera quando o trabalho é liquidado. |

## Taxas

O Skynet tem duas cobranças além do custo do provedor.

| Cobrança                              | Padrão atual                              | Aplica-se a                                                  |
| ------------------------------------- | ----------------------------------------- | ------------------------------------------------------------ |
| Taxa de recarga                       | 12,5% do valor, mais US$ 0,35             | Cada recarga                                                 |
| Acréscimo sobre o uso                 | Custo do provedor × 1,15 (custo mais 15%) | Chamadas de modelo com as chaves do Skynet e computação da sandbox |

### Taxa de recarga

O Skynet arredonda a parte percentual para cima, até o próximo centavo, e depois soma US$ 0,35. Por exemplo, uma recarga de US$ 20 tem uma taxa de US$ 2,85. A tela de recarga mostra a taxa antes de você pagar.

### Acréscimo sobre o uso

O Skynet multiplica o custo do provedor pelo acréscimo sobre o uso. O acréscimo se aplica às chamadas de modelo que o Skynet paga e à computação da sandbox. As reservas, o valor necessário para começar e os preços dos modelos no app já incluem o acréscimo.

O Skynet encontra o custo do provedor de uma chamada de modelo nesta ordem:

1. O custo que o OpenRouter informa para a chamada.
2. A tabela de preços atualizada do OpenRouter para o modelo.
3. A tabela de preços do LiteLLM.
4. Uma taxa alternativa alta de US$ 15 por milhão de tokens de entrada e US$ 75 por milhão de tokens de saída. Essa taxa garante que o Skynet nunca cobre um modelo desconhecido abaixo do custo.

## As suas próprias chaves de provedor (BYOK)

Você pode fazer chamadas de modelo com a sua própria chave de provedor. Adicione chaves em **Definições → Provedores**. Para o OpenRouter, você também pode usar **Continuar com OpenRouter**.

Quando uma chamada de modelo usa a sua chave:

- O seu provedor cobra os tokens do modelo na sua chave.
- Skynet não cobra taxa pelo modelo.
- O orçamento da execução cobre apenas o uso da sandbox.

Quaisquer taxas de um avaliador externo ou de um provedor MCP vão diretamente para esse provedor. Elas não fazem parte do limite de gastos da execução.

## Chamadas de modelo a partir do seu código

O código do seu repositório e o seu avaliador podem chamar um modelo pelo gateway de modelos do Skynet. Leia [Como a otimização funciona](/docs/concepts/optimization).

- Uma chamada com as chaves do Skynet é medida no orçamento da execução, como qualquer outra chamada de modelo. O acréscimo sobre o uso se aplica.
- Uma chamada com a sua própria chave (BYOK) vai para a chave do seu provedor. O Skynet não cobra taxa de modelo por ela.

## Uso da sandbox

O código do seu repositório e o seu avaliador rodam em uma sandbox. O Skynet mede a computação da sandbox:

1. Antes de uma sandbox iniciar, o Skynet reserva um valor do limite da execução.
2. Quando a sandbox termina, o Skynet cobra o uso final que o provedor da sandbox informa.
3. O acréscimo sobre o uso se aplica à computação da sandbox.

A computação da sandbox faz parte do limite de gastos da execução, mesmo quando as chamadas de modelo usam a sua própria chave.

## Serviço

Cada chamada ao endpoint de serviço de uma execução concluída é cobrada separadamente. Quem chama paga com o próprio saldo. Defina `max_cost_cents` na requisição para limitar uma chamada. Leia [Resultados](/docs/guides/results).

## Planos

Há dois planos: Free e Pro. O Pro aumenta alguns limites, como execuções simultâneas e armazenamento. Se a renovação do Pro falhar, a conta mantém os limites do Pro por 7 dias. Depois disso, ela usa os limites do Free até o pagamento ser concluído. Veja [Limites](/docs/limits).

## Proteções da plataforma

- Um turno interativo de modelo, como uma conversa com o assistente, precisa de um saldo de pelo menos US$ 0,05 antes de começar.
- O assistente do formulário é gratuito. Cada conta pode iniciar 50 turnos do assistente do formulário por dia UTC.

## Páginas relacionadas

- [Execuções](/docs/concepts/runs)
- [Iniciar uma execução com o assistente](/docs/guides/submit)
- [Limites](/docs/limits)
