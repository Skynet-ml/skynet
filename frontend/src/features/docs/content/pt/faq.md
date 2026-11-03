Esta página responde a perguntas comuns sobre o Skynet. Cada resposta tem um link para a página com todos os detalhes.

## Geral

### O que é o Skynet?

O Skynet é uma plataforma que otimiza prompts, programas, agentes e fluxos de trabalho com base nos seus próprios dados e na sua métrica. Você envia um conjunto de dados, descreve a tarefa e inicia uma execução. A execução testa novas versões e as pontua em dados reservados. Para começar, leia o [Início rápido](/docs/quickstart).

### O que o Skynet pode otimizar?

O Skynet é o Optimize Anything. Ele otimiza prompts, código, texto e programas DSPy em um único produto. O tipo de artefato define quais motores podem trabalhar nele:

- Artefatos de Prompt, Código e Texto usam os motores nativos: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler e ShinkaEvolve.
- Programas DSPy usam os otimizadores DSPy: DSPy GEPA, DSPy MIPROv2 e DSPy BootstrapFewShot.

O **Auto** combina os motores adequados à sua tarefa. Veja [Como a otimização funciona](/docs/concepts/optimization) e [Motores](/docs/concepts/engines).

### Quais idiomas a interface suporta?

A interface tem 24 idiomas. O hebraico é o idioma base, e o layout suporta texto da direita para a esquerda. A documentação em hebraico está em [/docs/he](/docs/he).

### O Skynet é open source?

Sim. O código usa a licença AGPL-3.0. Se você oferecer uma versão modificada do Skynet como serviço, a licença exige que você compartilhe as suas alterações.

## Dados

### Quais tipos de arquivo posso enviar?

Você pode enviar arquivos CSV, JSON, `.xlsx` e `.xls`. Em arquivos Excel, o Skynet lê apenas a primeira planilha. Veja [Preparar um conjunto de dados](/docs/guides/datasets).

### Os meus dados não têm rótulos. O que posso fazer?

Use o rotulador. Você pode rotular as linhas por conta própria ou deixar um modelo de IA rotulá-las enquanto você revisa o trabalho dele. Veja [Rotular dados com o rotulador](/docs/guides/tagger).

### Quem pode ver as minhas execuções e os meus conjuntos de dados?

> [!WARNING]
> Por padrão, novas execuções têm a visibilidade **Pública**. Uma execução pública aparece na pesquisa pública, e qualquer pessoa pode navegar pelos resultados dela.

Defina **Visibilidade** como **Privada** ao enviar uma execução para mantê-la fora da pesquisa pública. Novos conjuntos de dados têm o acesso **Restrito**: só você e as pessoas que você convidar podem abri-los. Veja [Compartilhamento](/docs/guides/sharing) e [Segurança](/docs/security).

### Os provedores de modelos treinam com os meus prompts?

Por padrão, não. A opção padrão de **Privacidade dos dados dos modelos** é **Sem treino**. Ela envia prompts apenas para provedores que não treinam com eles e não os guardam. Essa configuração vale para chamadas que o Skynet cobra do seu saldo. Chamadas com a sua própria chave seguem as configurações de privacidade da sua conta OpenRouter. Veja [Segurança](/docs/security).

## Cobrança

### Como o Skynet cobra pelas execuções?

O Skynet usa um saldo pré-pago. O uso de modelos hospedados, a taxa de recarga e o uso da sandbox são medidos. Antes de o trabalho pago começar, o Skynet reserva uma margem na sua carteira e no orçamento da execução. Veja [Cobrança](/docs/concepts/billing).

### Posso usar a minha própria chave de provedor de modelos?

Sim. Com sua própria chave (BYOK), O provedor cobra os tokens do modelo na sua chave. Skynet não cobra taxa pelo modelo. O orçamento da execução cobre apenas o uso da sandbox. O Skynet criptografa a sua chave e nunca a mostra de novo. Veja [Cobrança](/docs/concepts/billing) e [Segurança](/docs/security).

### Posso usar o Skynet sem pagar pelas chamadas de modelo?

O [app para Mac](/docs/guides/mac-app) executa o Skynet no seu próprio computador. Ele envia as chamadas de modelo pelo Claude Code ou pelo Codex, com sessão iniciada no seu Mac. O plano da sua CLI paga essas chamadas, então elas somam US$ 0 à execução.

## Ferramentas e integrações

### Posso controlar o Skynet a partir do meu próprio cliente de IA?

Sim. O Skynet tem um servidor MCP em `https://mcp.skynetml.com/mcp`. Você entra com OAuth. O servidor tem estas ferramentas: `check_run`, `start_run`, `get_run`, `list_runs` e `stop_run`. Veja [MCP](/docs/mcp).

### Existe uma API?

Sim. Gere um token de API em Definições e envie-o como `Authorization: Bearer <token>`. Cada usuário tem um token válido por vez. Veja [API](/docs/api).

### O painel do agente pode excluir as minhas execuções?

Só se você permitir. No modo padrão **Perguntar**, o agente pergunta antes de cada ação. No modo **Automático**, ele ainda pergunta antes de ações destrutivas, como excluir ou enviar. Veja [Painel do agente](/docs/guides/agent-panel).
