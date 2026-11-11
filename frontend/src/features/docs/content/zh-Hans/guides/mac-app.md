Skynet Mac 应用在你自己的电脑上运行 Skynet。它通过在你的 Mac 上已登录的编程 CLI 发送模型调用，因此这些调用由你的 CLI 套餐支付。你也可以把应用关联到你的托管 Skynet 账户。

## 要求

- 一台搭载 Apple 芯片的 Mac。Intel 版本的构建存在，但还没有人在 Intel 硬件上测试过。
- 正在运行的 Docker Desktop，并开启 **Use Rosetta for x86_64/amd64 emulation**。
- 一个已登录的编程 CLI：
  - Claude Code（`claude`）2.1.226 或更新版本，或
  - Codex（`codex`）0.160.0 或更新版本。

> [!WARNING]
> Codex 必须使用 ChatGPT 账户登录。使用 API 密钥登录不算数。保存在 macOS 钥匙串中的登录也不算数。

### 设置 Codex

Codex 必须把凭据保存在文件中。请在首次启动前完成以下步骤：

1. 打开 `~/.codex/config.toml` 并添加这一行：

```toml
cli_auth_credentials_store = "file"
```

2. 用你的 ChatGPT 账户登录：

```bash
codex login
```

## 安装应用

这些构建使用临时（ad hoc）签名，并且没有经过公证。在你移除隔离标记之前，macOS 会阻止它们运行。

1. 打开 `.dmg` 文件。
2. 把 **Skynet** 拖到 **Applications**（应用程序）。
3. 在终端中运行以下命令：

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. 从 **Applications**（应用程序）中打开 Skynet。

> [!NOTE]
> 首次启动需要几分钟。应用会构建本地沙箱镜像并准备数据库。

## 启动时会发生什么

应用按顺序执行以下步骤：

1. 从你的登录 shell 读取 `PATH`，以便找到 Docker 和你的 CLI。
2. 检查 Docker 和你的编程 CLI。
3. 在 Docker 中启动本地 Postgres 数据库。
4. 为 `linux/amd64` 构建沙箱镜像 `skynet-local-sandbox`。Docker 在 Rosetta 下运行它。
5. 更新数据库模式（schema）。
6. 启动本地 API 和 Web 应用，并在窗口中显示 Web 应用。

### 初始检查

应用会显示一个检查列表：**Docker**、**Coding CLI**、**Signed in**、**Usage limit**、**Local services** 和 **Account**。如果某项检查失败，应用会显示一个页面，告诉你该怎么做：

| 页面                       | 处理方法                                                 |
| -------------------------- | -------------------------------------------------------- |
| **Install Docker Desktop** | 安装 Docker Desktop。                                    |
| **Start Docker**           | 启动 Docker Desktop。                                    |
| **Install a coding CLI**   | 安装 Claude Code 或 Codex。                              |
| **Update your coding CLI** | 把 CLI 更新到上面列出的最低版本。                        |
| **Sign in to your CLI**    | 在终端中登录 CLI。                                       |
| **Usage limit reached**    | 等到重置时间。重置后可以再次启动新作业。                 |

修复问题后，点击 **Check again**。点击 **Open logs** 查看日志文件。在 **Usage limit reached** 页面上，你可以点击 **Open Skynet anyway**。

## 本地运行

在应用中，模型调用会经过你 Mac 上的 CLI。模型菜单把这种模式显示为 **本地**。提示文字为：“你的套餐支付这些调用，因此该模型不会给运行增加任何费用（$0）。”

应用中的 CLI 名称是 **Claude Code** 和 **Codex**。

> [!NOTE]
> 应用会从它启动的进程的环境中移除 API 密钥。因此 CLI 使用的是你的订阅，而不是 API 密钥。

### 在你的 CLI 上运行提议器

**在我的 CLI 上运行提议器** 设置控制提议器（proposer）智能体在哪里运行。选项为 **代理引擎（默认）** 和 **关闭（沙盒提议器）**。

**提议器权限** 设置 CLI 可以访问的范围：

- **仅限工作区** 是默认值。它依赖每个 CLI 自己的沙箱。
- **完全访问** 以你的用户权限运行 CLI。

> [!WARNING]
> 使用 **完全访问** 时，CLI 可以读取、更改和删除你有权访问的任何文件，并且可以访问网络。即使使用 **仅限工作区**，Claude 的 acceptEdits 模式也不会隔离网络。

### 在应用中标注

在 Mac 应用中使用 AI 标注需要你自己的提供商密钥。请在 **你自己的密钥** 下选择模型。请参阅[使用标注工具标注数据](/docs/guides/tagger)。

## 关联到你的 Skynet 账户

你可以在没有账户的情况下使用应用，为此请点击 **Use without signing in**。要与你的托管账户同步，请关联应用。

### 关联此 Mac

1. 在 **Sign in to Skynet** 步骤，应用会在你的默认浏览器中打开 **Link a Mac** 页面。
2. 如果浏览器要求登录，请登录 Skynet。
3. 查看 **关联后，此 Mac 可以：** 下的列表。
4. 点击 **关联此 Mac**。页面会显示“正在返回 Mac 应用…”。

如果登录失败或你取消了操作，应用会显示 **Try again** 和 **Use without signing in**。

关联后，这台 Mac 可以：

- 查看你的姓名和电子邮件。
- 查看你的余额。
- 将你的运行与账户同步。
- 使用托管模型，费用从你的余额中支付。
- 使用你已连接的数据源。
- 通过 Skynet 搜索和浏览网页。

关联使用安全的登录流程（带 PKCE 的 OAuth）。应用把令牌加密保存在你的 Mac 上。当你取消关联时，Skynet 会吊销这些令牌。

### 关联限制

| 限制                     | 值                                 |
| ------------------------ | ---------------------------------- |
| 每个账户可关联的 Mac 数  | 10                                 |
| 访问令牌有效期           | 1 小时，之后应用会刷新它           |
| 关联空闲超时             | 30 天未使用                        |
| 关联最长有效期           | 365 天                             |

达到空闲超时或最长有效期后，请重新关联这台 Mac。

### 关联状态

应用会显示以下状态之一：**本地模式**、**已关联此 Mac**、**登录以同步** 或 **已关联(离线)**。

## 本地服务与数据

所有本地服务只监听 `127.0.0.1`：

| 服务     | 端口范围       |
| -------- | -------------- |
| API      | 48100 到 48119 |
| Web 应用 | 48120 到 48139 |
| Postgres | 48140 到 48159 |

应用把数据保存在 `~/Library/Application Support/Skynet` 中。`logs/` 文件夹包含 `desktop.log`、`api.log`、`frontend.log`、`migrate.log` 和 `sandbox-build.log`。

退出应用时，它会停止 Web 应用、API、Postgres 和所有作业容器。

### 重置应用

> [!WARNING]
> 这些命令会删除所有本地数据，包括你的本地运行和数据集。

先退出应用，然后运行：

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## 后续步骤

- [提交运行](/docs/guides/submit)。
- 关于托管模型，请阅读[计费](/docs/concepts/billing)。
- 阅读[安全](/docs/security)。
