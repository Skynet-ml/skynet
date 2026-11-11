Skynet Mac 應用程式喺你自己部電腦上面運行 Skynet。佢會經一個喺你部 Mac 已登入嘅編程 CLI 發出模型呼叫，所以呢啲呼叫由你嘅 CLI 計劃支付。你亦可以將應用程式連結到你嘅雲端 Skynet 帳戶。

## 要求

- 一部用 Apple silicon 嘅 Mac。有 Intel 版本，但係未有人喺 Intel 硬件上面測試過。
- Docker Desktop，要運行緊，而且開咗 **Use Rosetta for x86_64/amd64 emulation**。
- 一個已登入嘅編程 CLI：
  - Claude Code (`claude`) 2.1.226 或者更新版本，或者
  - Codex (`codex`) 0.160.0 或者更新版本。

> [!WARNING]
> Codex 一定要用 ChatGPT 登入。用 API key 登入唔計。保存喺 macOS 鑰匙圈嘅登入都唔計。

### 設定 Codex

Codex 一定要將憑證保存喺檔案入面。第一次啟動之前，做以下步驟：

1. 打開 `~/.codex/config.toml`，加入呢一行：

```toml
cli_auth_credentials_store = "file"
```

2. 用你嘅 ChatGPT 帳戶登入：

```bash
codex login
```

## 安裝應用程式

呢啲版本用 ad hoc 簽署，冇經過公證 (notarization)。喺你移除隔離標記之前，macOS 會封鎖佢哋。

1. 打開 `.dmg` 檔案。
2. 將 **Skynet** 拖去 **Applications**。
3. 喺 Terminal 運行呢個指令：

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. 喺 **Applications** 打開 Skynet。

> [!NOTE]
> 第一次啟動要幾分鐘。應用程式會建立一個本機沙盒映像，同埋準備佢嘅數據庫。

## 啟動時會發生乜事

應用程式會按次序做以下步驟：

1. 佢會由你嘅登入 shell 讀取 `PATH`，等佢搵到 Docker 同你嘅 CLI。
2. 佢會檢查 Docker 同你嘅編程 CLI。
3. 佢會喺 Docker 入面啟動一個本機 Postgres 數據庫。
4. 佢會為 `linux/amd64` 建立沙盒映像 `skynet-local-sandbox`。Docker 會喺 Rosetta 下面運行佢。
5. 佢會更新數據庫結構 (schema)。
6. 佢會啟動本機 API 同網頁應用程式，再喺視窗入面顯示網頁應用程式。

### 初次設定檢查

應用程式會顯示一個檢查清單：**Docker**、**Coding CLI**、**Signed in**、**Usage limit**、**Local services** 同 **Account**。如果有一項檢查失敗，應用程式會顯示一個畫面，話你知要做乜：

| 畫面                       | 要做乜                                                       |
| -------------------------- | ------------------------------------------------------------ |
| **Install Docker Desktop** | 安裝 Docker Desktop。                                        |
| **Start Docker**           | 啟動 Docker Desktop。                                        |
| **Install a coding CLI**   | 安裝 Claude Code 或者 Codex。                                |
| **Update your coding CLI** | 將 CLI 更新到上面嘅最低版本。                                |
| **Sign in to your CLI**    | 喺 Terminal 登入 CLI。                                       |
| **Usage limit reached**    | 等到重設時間。重設之後，新工作可以再開始。                   |

修好問題之後，撳 **Check again**。撳 **Open logs** 睇記錄檔。喺 **Usage limit reached** 畫面，你可以撳 **Open Skynet anyway**。

## 本機運行

喺應用程式入面，模型呼叫會經過你部 Mac 上面嘅 CLI。模型選單會將呢個模式顯示為 **本機**。提示會話：「調用費用由你嘅計劃支付，所以呢個模型為運行加 $0。」

應用程式入面嘅 CLI 名稱係 **Claude Code** 同 **Codex**。

> [!NOTE]
> 應用程式會喺佢啟動嘅程序嘅環境入面移除 API key。所以 CLI 會用你嘅訂閱，而唔係 API key。

### 喺你嘅 CLI 運行提議器

**喺我嘅 CLI 上運行提議器** 呢個設定控制提議器代理喺邊度運行。選擇有 **代理引擎（預設）** 同 **停用（沙盒提議器）**。

**提議器權限** 設定 CLI 可以接觸到乜嘢：

- **只限工作區** 係預設值。佢依賴每個 CLI 自己嘅沙盒。
- **完全存取** 會用你嘅用戶權限運行 CLI。

> [!WARNING]
> 用 **完全存取** 嘅時候，CLI 可以讀取、修改同刪除任何你可以處理嘅檔案，亦可以連上網絡。Claude 嘅 acceptEdits 模式唔會隔離網絡，就算用 **只限工作區** 都係。

### 喺應用程式入面標註

喺 Mac 應用程式用 AI 標註，需要你自己嘅供應商金鑰。喺 **自備金鑰** 下面揀一個模型。睇 [用標註工具標註數據](/docs/guides/tagger)。

## 連結到你嘅 Skynet 帳戶

你唔使帳戶都可以用應用程式。撳 **Use without signing in** 就得。想同你嘅雲端帳戶同步，就要連結應用程式。

### 連結呢部 Mac

1. 去到 **Sign in to Skynet** 呢一步，應用程式會喺你嘅預設瀏覽器打開 **Link a Mac** 頁面。
2. 如果瀏覽器要求，就喺瀏覽器登入 Skynet。
3. 檢查 **連結之後，呢部 Mac 可以：** 下面嘅清單。
4. 撳 **連結呢部 Mac**。頁面會顯示「返緊去 Mac app…」。

如果登入失敗或者你取消，應用程式會顯示 **Try again** 同 **Use without signing in**。

連結之後，呢部 Mac 可以：

- 睇到你嘅名同電郵。
- 睇到你嘅結餘。
- 將你嘅運行同你嘅帳戶同步。
- 用雲端模型，費用由你嘅結餘支付。
- 用你已連接嘅資料來源。
- 經 Skynet 搜尋同瀏覽網頁。

連結用一個安全嘅登入流程（OAuth 加 PKCE）。應用程式會將佢嘅 token 加密保存喺你部 Mac。當你解除連結，Skynet 會撤銷呢啲 token。

### 連結限制

| 限制                       | 數值                               |
| -------------------------- | ---------------------------------- |
| 每個帳戶可連結嘅 Mac       | 10                                 |
| 存取 token 有效期          | 1 小時，之後應用程式會更新佢       |
| 連結閒置逾時               | 30 日冇用                          |
| 連結最長期限               | 365 日                             |

閒置逾時或者到咗最長期限之後，要再連結一次部 Mac。

### 連結狀態

應用程式會顯示以下其中一個狀態：**本機模式**、**已連結呢部 Mac**、**登入嚟同步** 或者 **已連結(離線)**。

## 本機服務同數據

所有本機服務都只係監聽 `127.0.0.1`：

| 服務           | 埠範圍         |
| -------------- | -------------- |
| API            | 48100 至 48119 |
| 網頁應用程式   | 48120 至 48139 |
| Postgres       | 48140 至 48159 |

應用程式將佢嘅數據保存喺 `~/Library/Application Support/Skynet`。`logs/` 資料夾裝住 `desktop.log`、`api.log`、`frontend.log`、`migrate.log` 同 `sandbox-build.log`。

當你結束應用程式，佢會停止網頁應用程式、API、Postgres 同所有工作容器。

### 重設應用程式

> [!WARNING]
> 呢啲指令會刪除所有本機數據，包括你嘅本機運行同數據集。

先結束應用程式。然後運行：

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## 下一步

- [提交運行](/docs/guides/submit)。
- 了解雲端模型嘅 [收費](/docs/concepts/billing)。
- 睇 [安全](/docs/security)。
