Um conjunto de dados é uma tabela de exemplos com os quais uma execução de otimização aprende e é pontuada. Este guia mostra como enviar um arquivo, editar linhas e colunas, definir os papéis das colunas e compartilhar um conjunto de dados. Para os conceitos, leia [Conjuntos de dados](/docs/concepts/datasets).

## Arquivos suportados

O Skynet lê estes tipos de arquivo:

| Extensão        | Como o Skynet o lê                                                                       |
| --------------- | ---------------------------------------------------------------------------------------- |
| `.csv`          | A primeira linha é a linha de cabeçalho. O arquivo precisa de pelo menos uma linha de dados. |
| `.json`         | Um array de objetos ou um único objeto. As chaves do primeiro objeto viram as colunas.   |
| `.xlsx`, `.xls` | O Skynet lê apenas a primeira planilha.                                                  |

O Skynet rejeita outras extensões com a mensagem "Unsupported file format".

> [!TIP]
> Coloque a saída esperada na última coluna. O assistente de envio marca a última coluna como saída por padrão.

### Exemplo de CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Exemplo de JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Enviar um conjunto de dados

1. Abra **Conjuntos de dados**.
2. Clique em **Carregar conjunto de dados** ou arraste um arquivo para a página. Enquanto você arrasta, a página mostra "Largue para carregar".
3. Aguarde a notificação "Conjunto de dados guardado na sua biblioteca".

O Skynet usa o nome do arquivo sem a extensão como nome do conjunto de dados. Para alterá-lo, selecione o conjunto de dados e clique em **Mudar o nome**.

> [!NOTE]
> Cada conta tem uma cota de armazenamento. Se um envio ultrapassar a cota, o Skynet abre uma caixa de diálogo que explica o limite. Veja [Limites](/docs/limits).

Você também pode enviar um arquivo no assistente de envio ("Carregue um ficheiro CSV, JSON ou Excel") ou anexar um no [painel do agente](/docs/guides/agent-panel).

## Encontrar um conjunto de dados

A biblioteca lista os seus conjuntos de dados. Use **Pesquisar por nome** para filtrar a lista. Os conjuntos de dados que outras pessoas compartilham com você aparecem em **Partilhado comigo**.

Quando você seleciona um conjunto de dados, aparecem estas ações:

- **Marcar este conjunto de dados** abre o [rotulador](/docs/guides/tagger) com este conjunto de dados.
- **Editar dados** abre o editor de conjuntos de dados.
- **Partilhar** abre a caixa de diálogo de compartilhamento.
- **Mudar o nome** altera o nome do conjunto de dados.

Outras ações são **Guardar uma cópia** e **Eliminar**.

> [!WARNING]
> Eliminar remove o conjunto de dados da sua biblioteca permanentemente. Não é possível desfazer. As execuções criadas a partir do conjunto de dados continuam válidas, mas o link delas para ele deixa de funcionar.

## Editar um conjunto de dados

O editor mostra o conjunto de dados como uma grade. Ele mostra 100 linhas por página. Use **Próximas linhas** e **Linhas anteriores** para navegar entre as páginas.

### Alterar linhas e colunas

- **Adicionar linha** e **Adicionar coluna** adicionam novas células no final.
- **Inserir linha acima** e **Inserir linha abaixo** adicionam uma linha junto à linha selecionada.
- **Duplicar linha**, **Eliminar linha**, **Eliminar coluna** e **Renomear coluna** alteram a estrutura.
- **Arrastar para preencher** copia o valor de uma célula para as células vizinhas.
- **Desfazer** e **Refazer** revertem ou repetem a sua última alteração.

Clique duas vezes em uma linha para vê-la por completo.

### Salvamento

O editor salva as suas alterações automaticamente, logo após cada edição. Durante o salvamento, o status mostra **A guardar…**. Se o salvamento falhar, o status mostra **Falha ao guardar**.

> [!NOTE]
> O editor não consegue salvar um conjunto de dados sem linhas. Ele mostra "Adicione uma linha para guardar as alterações" até você adicionar uma.

Clique em **Voltar à biblioteca** quando terminar.

### Acesso somente leitura

Se você tem a função Visualizador em um conjunto de dados compartilhado, o editor abre no modo somente leitura. Para alterar os dados, peça ao proprietário a função Editor ou use **Guardar uma cópia**.

### Etiquetar com IA

O editor tem uma ação **Etiquetar com IA**. Ela abre o rotulador, onde um modelo de IA ajuda você a adicionar rótulos. Veja [Rotular dados com o rotulador](/docs/guides/tagger).

## Definir os papéis das colunas

Uma execução precisa saber quais colunas são entradas e qual coluna é a saída esperada. Você define esses papéis ao enviar uma execução, e não no editor de conjuntos de dados.

No assistente de envio, a etapa "Como ler os dados" mostra cada coluna com três opções:

- **Entrada**: o seu avaliador recebe este valor em `case["input"]`.
- **Saída**: a resposta esperada, com a qual o avaliador compara.
- **Ignorar**: a execução ignora esta coluna.

Os padrões são:

- Todas as colunas são entradas, e a última coluna é a saída.
- Um conjunto de dados com uma única coluna tem uma entrada e nenhuma saída.

Sem coluna de saída, o Skynet usa a primeira linha como uma única entrada. Você deve escolher pelo menos uma coluna de entrada.

Algumas colunas também podem conter imagens. Para essas colunas, escolha **Imagem** ou **Texto** para dizer ao Skynet como ler os valores.

> [!TIP]
> O editor de conjuntos de dados mostra um selo "saída" nas colunas de saída. Uma nova coluna que você adiciona no editor começa como entrada.

O assistente também divide as linhas em partes para treino e avaliação. As partes devem somar 1. Se não somarem, o assistente mostra "A divisão tem de somar 1.". Para o fluxo completo, leia [Iniciar uma execução](/docs/guides/submit).

## Compartilhar um conjunto de dados

Selecione um conjunto de dados e clique em **Partilhar**. A caixa de diálogo **Partilha e privacidade** é aberta.

### Acesso por link

| Opção                                | Quem pode abrir o conjunto de dados                                     |
| ------------------------------------ | ----------------------------------------------------------------------- |
| **Restrito**                         | Apenas o proprietário e as pessoas convidadas.                          |
| **Qualquer pessoa com a ligação**    | Qualquer pessoa com sessão iniciada que tenha o link, com a função que você escolher. |

Para um conjunto de dados com "Qualquer pessoa com a ligação", escolha a função que o link concede: **Visualizador** ou **Editor**. Clique em **Copiar ligação** para copiar a URL.

### Convidar pessoas

1. Em **Convidar pessoas**, digite um nome de usuário em **Adicionar pessoas por nome de utilizador**.
2. Clique em **Convidar**.
3. Confira a lista em **Pessoas com acesso**. Use **Alterar função** para mudar a função de uma pessoa.

As funções são:

| Função       | Acesso                                                                      |
| ------------ | --------------------------------------------------------------------------- |
| Proprietário | Controle total, incluindo gerenciar o compartilhamento e a exclusão.        |
| Editor       | Acesso de visualizador, mais executar o modelo, conversar e renomear.       |
| Visualizador | Abre o conjunto de dados. O editor fica em modo somente leitura.            |

### Transferir a propriedade

Use **Transferir propriedade** para entregar o conjunto de dados a outra pessoa. O Skynet pede que você confirme.

> [!WARNING]
> Depois da transferência, você passa a ser editor e perde os privilégios de proprietário.

## Próximos passos

- [Inicie uma execução](/docs/guides/submit) com o seu conjunto de dados.
- [Rotule dados com o rotulador](/docs/guides/tagger) se as suas linhas ainda não tiverem rótulos.
- Leia sobre os [avaliadores](/docs/concepts/scorers) para escolher como o Skynet compara as saídas.
