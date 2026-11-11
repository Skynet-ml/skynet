O app Skynet para Mac executa o Skynet no seu próprio computador. Ele envia as chamadas de modelo por uma CLI de programação com sessão iniciada no seu Mac, então o plano da sua CLI paga essas chamadas. Você também pode vincular o app à sua conta hospedada do Skynet.

## Requisitos

- Um Mac com Apple silicon. Existem builds para Intel, mas ninguém as testou em hardware Intel.
- Docker Desktop em execução, com **Use Rosetta for x86_64/amd64 emulation** ativado.
- Uma CLI de programação com sessão iniciada:
  - Claude Code (`claude`) 2.1.226 ou mais recente, ou
  - Codex (`codex`) 0.160.0 ou mais recente.

> [!WARNING]
> O Codex precisa usar um login do ChatGPT. Um login com chave de API não serve. Um login guardado no Keychain do macOS também não serve.

### Configurar o Codex

O Codex precisa guardar as credenciais em um arquivo. Siga estes passos antes da primeira abertura:

1. Abra `~/.codex/config.toml` e adicione esta linha:

```toml
cli_auth_credentials_store = "file"
```

2. Entre com a sua conta do ChatGPT:

```bash
codex login
```

## Instalar o app

As builds têm assinatura ad hoc e não são notarizadas. O macOS as bloqueia até você remover a marca de quarentena.

1. Abra o arquivo `.dmg`.
2. Arraste **Skynet** para **Aplicativos**.
3. Execute este comando no Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Abra o Skynet em **Aplicativos**.

> [!NOTE]
> A primeira abertura leva vários minutos. O app constrói uma imagem local da sandbox e prepara o banco de dados.

## O que acontece na abertura

O app executa estes passos, nesta ordem:

1. Lê o `PATH` do seu shell de login, para encontrar o Docker e a sua CLI.
2. Verifica o Docker e a sua CLI de programação.
3. Inicia um banco de dados Postgres local no Docker.
4. Constrói a imagem da sandbox `skynet-local-sandbox` para `linux/amd64`. O Docker a executa com o Rosetta.
5. Atualiza o esquema do banco de dados.
6. Inicia a API local e o app web, e mostra o app web na janela.

### Verificações iniciais

O app mostra uma lista de verificações: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** e **Account**. Se uma verificação falhar, o app mostra uma tela que explica o que fazer:

| Tela                                        | O que fazer                                                                    |
| ------------------------------------------- | ------------------------------------------------------------------------------ |
| **Install Docker Desktop**                | Instale o Docker Desktop.                                                      |
| **Start Docker**                         | Inicie o Docker Desktop.                                                       |
| **Install a coding CLI**          | Instale o Claude Code ou o Codex.                                              |
| **Update your coding CLI**       | Atualize a CLI para a versão mínima indicada acima.                            |
| **Sign in to your CLI**                        | Entre na CLI pelo Terminal.                                                    |
| **Usage limit reached**                  | Aguarde o horário de redefinição. Novos jobs podem começar depois da redefinição. |

Clique em **Check again** depois de corrigir o problema. Clique em **Open logs** para ver os arquivos de log. Na tela **Usage limit reached**, você pode clicar em **Open Skynet anyway**.

## Execuções locais

No app, as chamadas de modelo passam pela CLI no seu Mac. O menu de modelos mostra esse modo como **Local**. A dica diz: "O seu plano paga as chamadas, então este modelo adiciona US$ 0 à execução."

Os nomes das CLIs no app são **Claude Code** e **Codex**.

> [!NOTE]
> O app remove as chaves de API do ambiente dos processos que ele inicia. Assim, a CLI usa a sua assinatura, e não uma chave de API.

### Executar o proponente na sua CLI

A configuração **Executar o proponente na minha CLI** controla onde o agente proponente roda. As opções são **Motor do agente (padrão)** e **Desligado (proponente isolado)**.

**Acesso do proponente** define o que a CLI pode acessar:

- **Apenas área de trabalho** é o padrão. Ele depende da sandbox própria de cada CLI.
- **Acesso total** executa a CLI com os seus privilégios de usuário.

> [!WARNING]
> Com **Acesso total**, a CLI pode ler, alterar e excluir qualquer arquivo que você pode, e acessar a rede. O modo acceptEdits do Claude não isola a rede, mesmo com **Apenas área de trabalho**.

### Rotulagem no app

A rotulagem com IA no app para Mac precisa da sua própria chave de provedor. Escolha um modelo em **A sua própria chave**. Veja [Rotular dados com o rotulador](/docs/guides/tagger).

## Vincular à sua conta Skynet

Você pode usar o app sem uma conta. Para isso, clique em **Use without signing in**. Para sincronizar com a sua conta hospedada, vincule o app.

### Vincular este Mac

1. Na etapa **Sign in to Skynet**, o app abre a página **Link a Mac** no seu navegador padrão.
2. Entre no Skynet pelo navegador, se ele pedir.
3. Confira a lista em **Depois de vinculado, este Mac poderá:**.
4. Clique em **Vincular este Mac**. A página mostra "Voltando ao app do Mac…".

Se o login falhar ou você cancelar, o app mostra **Try again** e **Use without signing in**.

Quando vinculado, o Mac pode:

- Ver o seu nome e o seu e-mail.
- Ver o seu saldo.
- Sincronizar as suas execuções com a sua conta.
- Usar modelos hospedados, pagos com o seu saldo.
- Usar as suas fontes de dados conectadas.
- Pesquisar e navegar na web pelo Skynet.

O vínculo usa um fluxo de login seguro (OAuth com PKCE). O app guarda os tokens criptografados no seu Mac. Quando você desvincula, o Skynet revoga os tokens.

### Limites do vínculo

| Limite                              | Valor                                      |
| ----------------------------------- | ------------------------------------------ |
| Macs vinculados por conta           | 10                                         |
| Validade do token de acesso         | 1 hora; depois o app o renova              |
| Tempo limite de inatividade         | 30 dias sem uso                            |
| Idade máxima do vínculo             | 365 dias                                   |

Depois do tempo limite de inatividade ou da idade máxima, vincule o Mac de novo.

### Status do vínculo

O app mostra um destes estados: **Modo local**, **Vinculado a este Mac**, **Entre para sincronizar** ou **Vinculado (offline)**.

## Serviços e dados locais

Todos os serviços locais escutam apenas em `127.0.0.1`:

| Serviço   | Faixa de portas  |
| --------- | ---------------- |
| API       | 48100 a 48119    |
| App web   | 48120 a 48139    |
| Postgres  | 48140 a 48159    |

O app guarda os dados em `~/Library/Application Support/Skynet`. A pasta `logs/` contém `desktop.log`, `api.log`, `frontend.log`, `migrate.log` e `sandbox-build.log`.

Quando você fecha o app, ele para o app web, a API, o Postgres e todos os contêineres de jobs.

### Redefinir o app

> [!WARNING]
> Estes comandos excluem todos os dados locais, incluindo as suas execuções e os seus conjuntos de dados locais.

Feche o app primeiro. Depois execute:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Próximos passos

- [Inicie uma execução](/docs/guides/submit).
- Leia sobre a [cobrança](/docs/concepts/billing) dos modelos hospedados.
- Leia [Segurança](/docs/security).
