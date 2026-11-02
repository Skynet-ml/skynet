/**
 * Setup steps for connecting MCP clients to the hosted Skynet MCP server.
 *
 * This is the one source for every per-client snippet. The public page
 * /docs/mcp (and its Markdown twin) reads it today; the in-app Settings > MCP
 * page should import it too, so the two never drift. Check each client
 * against its own docs (`source`) and move MCP_CLIENTS_LAST_CHECKED when you
 * do. A client with `verified: false` was not checked against official docs.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */

/** The public MCP endpoint. Clients save this URL, so it must never change. */
export const MCP_SERVER_URL = "https://mcp.skynetml.com/mcp";

/** The day someone last checked every client below against its own docs. */
export const MCP_CLIENTS_LAST_CHECKED = "2026-11-01";

/** The server name that the commands and config files register. */
export const MCP_SERVER_NAME = "skynet";

/** A docs locale code. Text without a translation for it falls back to English. */
export type McpLocale = string;
type Localized<T> = { en: T; he: T } & Partial<Record<McpLocale, T>>;
type Text = Localized<string>;

/** A text in a locale, or in English when it has no translation. */
export function mcpText(text: Text, locale: McpLocale): string {
  return text[locale] ?? text.en;
}

export interface McpSnippet {
  label: Text;
  language: "bash" | "json" | "toml" | "text";
  code: string;
}

export interface McpClient {
  id: string;
  name: Text;
  /** False when the steps come from third-party docs, not the client's own. */
  verified: boolean;
  /** The client's own MCP setup docs. */
  source?: string;
  /** A one-click install link, when the client has one. */
  installUrl?: string;
  steps: Text[];
  snippets: McpSnippet[];
  note?: Text;
}

/** Base64 of a UTF-8 string, in Node and in the browser. */
function base64(text: string): string {
  return btoa(String.fromCharCode(...new TextEncoder().encode(text)));
}

/** Cursor's install link: the config is base64 JSON, then URL-encoded. */
export function cursorInstallUrl(url: string = MCP_SERVER_URL): string {
  const config = encodeURIComponent(base64(JSON.stringify({ url })));
  return `cursor://anysphere.cursor-deeplink/mcp/install?name=${MCP_SERVER_NAME}&config=${config}`;
}

/** VS Code's install link: the config is URL-encoded JSON. */
export function vscodeInstallUrl(url: string = MCP_SERVER_URL): string {
  const config = encodeURIComponent(JSON.stringify({ type: "http", url }));
  return `https://vscode.dev/redirect/mcp/install?name=${MCP_SERVER_NAME}&config=${config}`;
}

const json = (value: unknown) => JSON.stringify(value, null, 2);

const RTL_LOCALES = new Set(["ar", "fa"]);
const TRANSLATED_LOCALES = ["ar", "de", "es", "fa", "fr", "hi", "it", "ja", "ko", "pt", "ru", "tr", "uk", "yue", "zh-Hans"];

/** Text that every locale writes the same, like a brand name or a file path. RTL locales get `rtl`. */
function sameText(value: string, rtl: string = value): Text {
  const text: Text = { en: value, he: rtl };
  for (const locale of TRANSLATED_LOCALES) text[locale] = RTL_LOCALES.has(locale) ? rtl : value;
  return text;
}

export const MCP_CLIENTS: McpClient[] = [
  {
    id: "claude-code",
    name: sameText("Claude Code"),
    verified: true,
    source: "https://code.claude.com/docs/en/mcp",
    steps: [
      {
        en: "Run this command in your terminal.",
        he: "הרץ/י את הפקודה הזו בטרמינל.",
        ar: "شغّل هذا الأمر في الطرفية.",
        de: "Führen Sie diesen Befehl in Ihrem Terminal aus.",
        es: "Ejecuta este comando en tu terminal.",
        fa: "این فرمان را در ترمینال اجرا کنید.",
        fr: "Exécutez cette commande dans votre terminal.",
        hi: "यह कमांड अपने टर्मिनल में चलाएँ।",
        it: "Esegui questo comando nel terminale.",
        ja: "ターミナルでこのコマンドを実行します。",
        ko: "터미널에서 이 명령을 실행합니다.",
        pt: "Execute este comando no seu terminal.",
        ru: "Выполните эту команду в терминале.",
        tr: "Bu komutu terminalinizde çalıştırın.",
        uk: "Виконайте цю команду в терміналі.",
        yue: "喺終端機執行呢個指令。",
        "zh-Hans": "在终端中运行此命令。",
      },
      {
        en: "In Claude Code, run /mcp, select skynet, and sign in to Skynet in the browser.",
        he: "ב-Claude Code, הרץ/י ‎/mcp, בחר/י ב-skynet והתחבר/י ל-Skynet בדפדפן.",
        ar: "في Claude Code، شغّل ‎/mcp، واختر skynet، ثم سجّل الدخول إلى Skynet في المتصفح.",
        de: "Führen Sie in Claude Code /mcp aus, wählen Sie skynet und melden Sie sich im Browser bei Skynet an.",
        es: "En Claude Code, ejecuta /mcp, selecciona skynet e inicia sesión en Skynet en el navegador.",
        fa: "در Claude Code، ‎/mcp را اجرا کنید، skynet را انتخاب کنید و در مرورگر وارد Skynet شوید.",
        fr: "Dans Claude Code, exécutez /mcp, sélectionnez skynet et connectez-vous à Skynet dans le navigateur.",
        hi: "Claude Code में /mcp चलाएँ, skynet चुनें, और ब्राउज़र में Skynet में साइन इन करें।",
        it: "In Claude Code, esegui /mcp, seleziona skynet e accedi a Skynet nel browser.",
        ja: "Claude Code で /mcp を実行し、skynet を選択して、ブラウザーで Skynet にサインインします。",
        ko: "Claude Code에서 /mcp를 실행하고 skynet을 선택한 다음 브라우저에서 Skynet에 로그인합니다.",
        pt: "No Claude Code, execute /mcp, selecione skynet e entre no Skynet pelo navegador.",
        ru: "В Claude Code выполните /mcp, выберите skynet и войдите в Skynet в браузере.",
        tr: "Claude Code'da /mcp komutunu çalıştırın, skynet'i seçin ve tarayıcıda Skynet'e giriş yapın.",
        uk: "У Claude Code виконайте /mcp, виберіть skynet і увійдіть у Skynet у браузері.",
        yue: "喺 Claude Code 執行 /mcp，揀 skynet，然後喺瀏覽器登入 Skynet。",
        "zh-Hans": "在 Claude Code 中运行 /mcp，选择 skynet，然后在浏览器中登录 Skynet。",
      },
    ],
    snippets: [
      {
        label: {
          en: "Add the server",
          he: "הוספת השרת",
          ar: "إضافة الخادم",
          de: "Server hinzufügen",
          es: "Añadir el servidor",
          fa: "افزودن سرور",
          fr: "Ajouter le serveur",
          hi: "सर्वर जोड़ें",
          it: "Aggiungi il server",
          ja: "サーバーを追加",
          ko: "서버 추가",
          pt: "Adicionar o servidor",
          ru: "Добавление сервера",
          tr: "Sunucu ekleme",
          uk: "Додавання сервера",
          yue: "加入伺服器",
          "zh-Hans": "添加服务器",
        },
        language: "bash",
        code: `claude mcp add --transport http ${MCP_SERVER_NAME} ${MCP_SERVER_URL}`,
      },
      {
        label: {
          en: "Add it for every project",
          he: "הוספה לכל הפרויקטים",
          ar: "الإضافة لكل المشاريع",
          de: "Für alle Projekte hinzufügen",
          es: "Añadirlo a todos los proyectos",
          fa: "افزودن برای همه پروژه‌ها",
          fr: "L'ajouter à tous les projets",
          hi: "हर प्रोजेक्ट के लिए जोड़ें",
          it: "Aggiungilo a tutti i progetti",
          ja: "すべてのプロジェクトに追加",
          ko: "모든 프로젝트에 추가",
          pt: "Adicionar a todos os projetos",
          ru: "Добавление для всех проектов",
          tr: "Tüm projeler için ekleme",
          uk: "Додавання для всіх проєктів",
          yue: "加入所有項目",
          "zh-Hans": "为所有项目添加",
        },
        language: "bash",
        code: `claude mcp add --transport http --scope user ${MCP_SERVER_NAME} ${MCP_SERVER_URL}`,
      },
    ],
  },
  {
    id: "claude",
    name: {
      en: "Claude (web and desktop)",
      he: "Claude (אתר ואפליקציה)",
      ar: "Claude (الويب وسطح المكتب)",
      de: "Claude (Web und Desktop)",
      es: "Claude (web y escritorio)",
      fa: "Claude (وب و دسکتاپ)",
      fr: "Claude (web et bureau)",
      hi: "Claude (वेब और डेस्कटॉप)",
      it: "Claude (web e desktop)",
      ja: "Claude（Web とデスクトップ）",
      ko: "Claude(웹 및 데스크톱)",
      pt: "Claude (web e desktop)",
      ru: "Claude (веб и десктоп)",
      tr: "Claude (web ve masaüstü)",
      uk: "Claude (веб і десктоп)",
      yue: "Claude（網頁版同桌面版）",
      "zh-Hans": "Claude（网页版和桌面版）",
    },
    verified: true,
    source: "https://support.claude.com/en/articles/11175166",
    steps: [
      {
        en: "Open Customize, then Connectors.",
        he: "פתח/י את Customize ואז את Connectors.",
        ar: "افتح Customize، ثم Connectors.",
        de: "Öffnen Sie Customize und dann Connectors.",
        es: "Abre Customize y luego Connectors.",
        fa: "Customize و سپس Connectors را باز کنید.",
        fr: "Ouvrez Customize, puis Connectors.",
        hi: "Customize खोलें, फिर Connectors खोलें।",
        it: "Apri Customize, poi Connectors.",
        ja: "Customize を開き、次に Connectors を開きます。",
        ko: "Customize를 연 다음 Connectors를 엽니다.",
        pt: "Abra Customize e depois Connectors.",
        ru: "Откройте Customize, затем Connectors.",
        tr: "Customize'ı, ardından Connectors'ı açın.",
        uk: "Відкрийте Customize, потім Connectors.",
        yue: "打開 Customize，然後打開 Connectors。",
        "zh-Hans": "打开 Customize，然后打开 Connectors。",
      },
      {
        en: "Select +, then Add custom connector.",
        he: "בחר/י ב-+ ואז ב-Add custom connector.",
        ar: "اختر +، ثم Add custom connector.",
        de: "Wählen Sie + und dann Add custom connector.",
        es: "Selecciona + y luego Add custom connector.",
        fa: "+ و سپس Add custom connector را انتخاب کنید.",
        fr: "Sélectionnez +, puis Add custom connector.",
        hi: "+ चुनें, फिर Add custom connector चुनें।",
        it: "Seleziona +, poi Add custom connector.",
        ja: "+ を選択し、次に Add custom connector を選択します。",
        ko: "+를 선택한 다음 Add custom connector를 선택합니다.",
        pt: "Selecione + e depois Add custom connector.",
        ru: "Выберите +, затем Add custom connector.",
        tr: "+ simgesini, ardından Add custom connector seçeneğini seçin.",
        uk: "Виберіть +, потім Add custom connector.",
        yue: "揀 +，然後揀 Add custom connector。",
        "zh-Hans": "选择 +，然后选择 Add custom connector。",
      },
      {
        en: `Enter the name Skynet and the URL ${MCP_SERVER_URL}, then select Add.`,
        he: `הזן/הזיני את השם Skynet ואת הכתובת ${MCP_SERVER_URL}, ואז בחר/י ב-Add.`,
        ar: `أدخل الاسم Skynet والعنوان <${MCP_SERVER_URL}>، ثم اختر Add.`,
        de: `Geben Sie den Namen Skynet und die URL ${MCP_SERVER_URL} ein und wählen Sie dann Add.`,
        es: `Introduce el nombre Skynet y la URL ${MCP_SERVER_URL}, y luego selecciona Add.`,
        fa: `نام Skynet و نشانی ${MCP_SERVER_URL} را وارد کنید، سپس Add را انتخاب کنید.`,
        fr: `Saisissez le nom Skynet et l'URL ${MCP_SERVER_URL}, puis sélectionnez Add.`,
        hi: `नाम Skynet और URL ${MCP_SERVER_URL} दर्ज करें, फिर Add चुनें।`,
        it: `Inserisci il nome Skynet e l'URL ${MCP_SERVER_URL}, poi seleziona Add.`,
        ja: `名前に Skynet、URL に ${MCP_SERVER_URL} を入力し、Add を選択します。`,
        ko: `이름에 Skynet, URL에 <${MCP_SERVER_URL}>을 입력한 다음 Add를 선택합니다.`,
        pt: `Digite o nome Skynet e a URL ${MCP_SERVER_URL} e depois selecione Add.`,
        ru: `Введите имя Skynet и URL ${MCP_SERVER_URL}, затем выберите Add.`,
        tr: `Ad olarak Skynet'i, URL olarak ${MCP_SERVER_URL} adresini girin, ardından Add'i seçin.`,
        uk: `Введіть назву Skynet і URL-адресу ${MCP_SERVER_URL}, потім виберіть Add.`,
        yue: `輸入名稱 Skynet 同 URL <${MCP_SERVER_URL}>，然後揀 Add。`,
        "zh-Hans": `输入名称 Skynet 和 URL <${MCP_SERVER_URL}>，然后选择 Add。`,
      },
      {
        en: "Select Connect and sign in to Skynet.",
        he: "בחר/י ב-Connect והתחבר/י ל-Skynet.",
        ar: "اختر Connect وسجّل الدخول إلى Skynet.",
        de: "Wählen Sie Connect und melden Sie sich bei Skynet an.",
        es: "Selecciona Connect e inicia sesión en Skynet.",
        fa: "Connect را انتخاب کنید و وارد Skynet شوید.",
        fr: "Sélectionnez Connect et connectez-vous à Skynet.",
        hi: "Connect चुनें और Skynet में साइन इन करें।",
        it: "Seleziona Connect e accedi a Skynet.",
        ja: "Connect を選択し、Skynet にサインインします。",
        ko: "Connect를 선택하고 Skynet에 로그인합니다.",
        pt: "Selecione Connect e entre no Skynet.",
        ru: "Выберите Connect и войдите в Skynet.",
        tr: "Connect'i seçin ve Skynet'e giriş yapın.",
        uk: "Виберіть Connect і увійдіть у Skynet.",
        yue: "揀 Connect，然後登入 Skynet。",
        "zh-Hans": "选择 Connect 并登录 Skynet。",
      },
    ],
    snippets: [],
    note: {
      en: "On Team and Enterprise plans, an owner adds the connector in Organization settings, then Connectors. The Free plan allows one custom connector.",
      he: "בתוכניות Team ו-Enterprise, בעל הארגון מוסיף את המחבר ב-Organization settings ואז ב-Connectors. בתוכנית Free אפשר להוסיף מחבר מותאם אחד.",
      ar: "في خطتَي Team وEnterprise، يضيف المالك الموصّل من Organization settings، ثم Connectors. تسمح خطة Free بموصّل مخصّص واحد.",
      de: "In den Tarifen Team und Enterprise fügt ein Owner den Connector unter Organization settings und dann Connectors hinzu. Der Free-Tarif erlaubt einen benutzerdefinierten Connector.",
      es: "En los planes Team y Enterprise, un propietario añade el conector en Organization settings y luego en Connectors. El plan Free permite un conector personalizado.",
      fa: "در طرح‌های Team و Enterprise، مالک سازمان رابط را در Organization settings و سپس Connectors اضافه می‌کند. طرح Free یک رابط سفارشی را مجاز می‌کند.",
      fr: "Avec les offres Team et Enterprise, un propriétaire ajoute le connecteur dans Organization settings, puis Connectors. L'offre Free permet un seul connecteur personnalisé.",
      hi: "Team और Enterprise प्लान में, ओनर Organization settings में, फिर Connectors में कनेक्टर जोड़ता है। Free प्लान में एक कस्टम कनेक्टर जोड़ा जा सकता है।",
      it: "Nei piani Team ed Enterprise, un proprietario aggiunge il connettore in Organization settings, poi Connectors. Il piano Free consente un connettore personalizzato.",
      ja: "Team プランと Enterprise プランでは、オーナーが Organization settings の Connectors でコネクタを追加します。Free プランではカスタムコネクタを 1 つ追加できます。",
      ko: "Team 및 Enterprise 플랜에서는 소유자가 Organization settings의 Connectors에서 커넥터를 추가합니다. Free 플랜에서는 사용자 지정 커넥터를 하나 추가할 수 있습니다.",
      pt: "Nos planos Team e Enterprise, um proprietário adiciona o conector em Organization settings e depois em Connectors. O plano Free permite um conector personalizado.",
      ru: "В тарифах Team и Enterprise владелец добавляет коннектор в Organization settings, затем в Connectors. Тариф Free позволяет добавить один пользовательский коннектор.",
      tr: "Team ve Enterprise planlarında bağlayıcıyı bir sahip Organization settings, ardından Connectors bölümünden ekler. Free plan bir özel bağlayıcıya izin verir.",
      uk: "У тарифах Team і Enterprise власник додає конектор в Organization settings, потім у Connectors. Тариф Free дозволяє один користувацький конектор.",
      yue: "喺 Team 同 Enterprise 計劃，擁有者會喺 Organization settings 再入 Connectors 加入連接器。Free 計劃可以加一個自訂連接器。",
      "zh-Hans": "在 Team 和 Enterprise 套餐中，由所有者在 Organization settings 的 Connectors 中添加连接器。Free 套餐允许添加一个自定义连接器。",
    },
  },
  {
    id: "chatgpt",
    name: sameText("ChatGPT"),
    verified: true,
    source: "https://help.openai.com/en/articles/12584461",
    steps: [
      {
        en: "Open ChatGPT on the web, then open Plugins.",
        he: "פתח/י את ChatGPT באתר, ואז את Plugins.",
        ar: "افتح ChatGPT على الويب، ثم افتح Plugins.",
        de: "Öffnen Sie ChatGPT im Web und dann Plugins.",
        es: "Abre ChatGPT en la web y luego abre Plugins.",
        fa: "ChatGPT را در وب باز کنید، سپس Plugins را باز کنید.",
        fr: "Ouvrez ChatGPT sur le web, puis ouvrez Plugins.",
        hi: "वेब पर ChatGPT खोलें, फिर Plugins खोलें।",
        it: "Apri ChatGPT sul web, poi apri Plugins.",
        ja: "Web 版の ChatGPT を開き、次に Plugins を開きます。",
        ko: "웹에서 ChatGPT를 연 다음 Plugins를 엽니다.",
        pt: "Abra o ChatGPT na web e depois abra Plugins.",
        ru: "Откройте веб-версию ChatGPT, затем Plugins.",
        tr: "ChatGPT'yi web'de açın, ardından Plugins'i açın.",
        uk: "Відкрийте вебверсію ChatGPT, потім Plugins.",
        yue: "喺網頁打開 ChatGPT，然後打開 Plugins。",
        "zh-Hans": "在网页上打开 ChatGPT，然后打开 Plugins。",
      },
      {
        en: "Select +, then Add custom MCP server.",
        he: "בחר/י ב-+ ואז ב-Add custom MCP server.",
        ar: "اختر +، ثم Add custom MCP server.",
        de: "Wählen Sie + und dann Add custom MCP server.",
        es: "Selecciona + y luego Add custom MCP server.",
        fa: "+ و سپس Add custom MCP server را انتخاب کنید.",
        fr: "Sélectionnez +, puis Add custom MCP server.",
        hi: "+ चुनें, फिर Add custom MCP server चुनें।",
        it: "Seleziona +, poi Add custom MCP server.",
        ja: "+ を選択し、次に Add custom MCP server を選択します。",
        ko: "+를 선택한 다음 Add custom MCP server를 선택합니다.",
        pt: "Selecione + e depois Add custom MCP server.",
        ru: "Выберите +, затем Add custom MCP server.",
        tr: "+ simgesini, ardından Add custom MCP server seçeneğini seçin.",
        uk: "Виберіть +, потім Add custom MCP server.",
        yue: "揀 +，然後揀 Add custom MCP server。",
        "zh-Hans": "选择 +，然后选择 Add custom MCP server。",
      },
      {
        en: `Enter the URL ${MCP_SERVER_URL}, choose OAuth, and sign in to Skynet.`,
        he: `הזן/הזיני את הכתובת ${MCP_SERVER_URL}, בחר/י ב-OAuth והתחבר/י ל-Skynet.`,
        ar: `أدخل العنوان <${MCP_SERVER_URL}>، واختر OAuth، ثم سجّل الدخول إلى Skynet.`,
        de: `Geben Sie die URL ${MCP_SERVER_URL} ein, wählen Sie OAuth und melden Sie sich bei Skynet an.`,
        es: `Introduce la URL ${MCP_SERVER_URL}, elige OAuth e inicia sesión en Skynet.`,
        fa: `نشانی ${MCP_SERVER_URL} را وارد کنید، OAuth را انتخاب کنید و وارد Skynet شوید.`,
        fr: `Saisissez l'URL ${MCP_SERVER_URL}, choisissez OAuth et connectez-vous à Skynet.`,
        hi: `URL ${MCP_SERVER_URL} दर्ज करें, OAuth चुनें, और Skynet में साइन इन करें।`,
        it: `Inserisci l'URL ${MCP_SERVER_URL}, scegli OAuth e accedi a Skynet.`,
        ja: `URL に ${MCP_SERVER_URL} を入力し、OAuth を選んで、Skynet にサインインします。`,
        ko: `URL에 <${MCP_SERVER_URL}>을 입력하고 OAuth를 선택한 다음 Skynet에 로그인합니다.`,
        pt: `Digite a URL ${MCP_SERVER_URL}, escolha OAuth e entre no Skynet.`,
        ru: `Введите URL ${MCP_SERVER_URL}, выберите OAuth и войдите в Skynet.`,
        tr: `${MCP_SERVER_URL} URL'sini girin, OAuth'u seçin ve Skynet'e giriş yapın.`,
        uk: `Введіть URL-адресу ${MCP_SERVER_URL}, виберіть OAuth і увійдіть у Skynet.`,
        yue: `輸入 URL <${MCP_SERVER_URL}>，揀 OAuth，然後登入 Skynet。`,
        "zh-Hans": `输入 URL <${MCP_SERVER_URL}>，选择 OAuth，然后登录 Skynet。`,
      },
      {
        en: "In a chat, type @ and select Skynet.",
        he: "בצ'אט, הקלד/י @ ובחר/י ב-Skynet.",
        ar: "في محادثة، اكتب @ واختر Skynet.",
        de: "Geben Sie in einem Chat @ ein und wählen Sie Skynet.",
        es: "En un chat, escribe @ y selecciona Skynet.",
        fa: "در یک گفتگو، @ را تایپ کنید و Skynet را انتخاب کنید.",
        fr: "Dans une conversation, tapez @ et sélectionnez Skynet.",
        hi: "किसी चैट में @ टाइप करें और Skynet चुनें।",
        it: "In una chat, digita @ e seleziona Skynet.",
        ja: "チャットで @ を入力し、Skynet を選択します。",
        ko: "채팅에서 @를 입력하고 Skynet을 선택합니다.",
        pt: "Em um chat, digite @ e selecione Skynet.",
        ru: "В чате введите @ и выберите Skynet.",
        tr: "Bir sohbette @ yazın ve Skynet'i seçin.",
        uk: "У чаті введіть @ і виберіть Skynet.",
        yue: "喺對話入面輸入 @，然後揀 Skynet。",
        "zh-Hans": "在聊天中输入 @，然后选择 Skynet。",
      },
    ],
    snippets: [],
    note: {
      en: "Custom MCP servers depend on your ChatGPT plan. In Business workspaces, only admins can add them.",
      he: "שרתי MCP מותאמים תלויים בתוכנית ה-ChatGPT שלך. בסביבות Business, רק מנהלים יכולים להוסיף אותם.",
      ar: "تعتمد خوادم MCP المخصّصة على خطة ChatGPT لديك. في مساحات عمل Business، يمكن للمسؤولين فقط إضافتها.",
      de: "Ob benutzerdefinierte MCP-Server verfügbar sind, hängt von Ihrem ChatGPT-Tarif ab. In Business-Workspaces können nur Admins sie hinzufügen.",
      es: "Los servidores MCP personalizados dependen de tu plan de ChatGPT. En los espacios de trabajo Business, solo los administradores pueden añadirlos.",
      fa: "سرورهای MCP سفارشی به طرح ChatGPT شما بستگی دارند. در فضاهای کاری Business، فقط مدیران می‌توانند آن‌ها را اضافه کنند.",
      fr: "Les serveurs MCP personnalisés dépendent de votre offre ChatGPT. Dans les espaces de travail Business, seuls les administrateurs peuvent les ajouter.",
      hi: "कस्टम MCP सर्वर आपके ChatGPT प्लान पर निर्भर करते हैं। Business वर्कस्पेस में केवल एडमिन उन्हें जोड़ सकते हैं।",
      it: "I server MCP personalizzati dipendono dal tuo piano ChatGPT. Negli spazi di lavoro Business, solo gli amministratori possono aggiungerli.",
      ja: "カスタム MCP サーバーを使えるかどうかは、ChatGPT のプランによって異なります。Business ワークスペースでは、管理者だけが追加できます。",
      ko: "사용자 지정 MCP 서버는 ChatGPT 플랜에 따라 다릅니다. Business 워크스페이스에서는 관리자만 추가할 수 있습니다.",
      pt: "Os servidores MCP personalizados dependem do seu plano do ChatGPT. Em workspaces Business, só administradores podem adicioná-los.",
      ru: "Доступность пользовательских MCP-серверов зависит от вашего тарифа ChatGPT. В рабочих пространствах Business добавлять их могут только администраторы.",
      tr: "Özel MCP sunucuları ChatGPT planınıza bağlıdır. Business çalışma alanlarında yalnızca yöneticiler bunları ekleyebilir.",
      uk: "Доступність користувацьких MCP-серверів залежить від вашого тарифу ChatGPT. У робочих просторах Business додавати їх можуть лише адміністратори.",
      yue: "可唔可以用自訂 MCP 伺服器，要睇你嘅 ChatGPT 計劃。喺 Business 工作區，只有管理員可以加。",
      "zh-Hans": "能否使用自定义 MCP 服务器取决于你的 ChatGPT 套餐。在 Business 工作区中，只有管理员可以添加。",
    },
  },
  {
    id: "codex",
    name: sameText("Codex"),
    verified: true,
    source: "https://learn.chatgpt.com/docs/extend/mcp?surface=cli",
    steps: [
      {
        en: "Add the server, then sign in.",
        he: "הוסף/י את השרת ואז התחבר/י.",
        ar: "أضف الخادم، ثم سجّل الدخول.",
        de: "Fügen Sie den Server hinzu und melden Sie sich dann an.",
        es: "Añade el servidor y luego inicia sesión.",
        fa: "سرور را اضافه کنید، سپس وارد شوید.",
        fr: "Ajoutez le serveur, puis connectez-vous.",
        hi: "सर्वर जोड़ें, फिर साइन इन करें।",
        it: "Aggiungi il server, poi accedi.",
        ja: "サーバーを追加してから、サインインします。",
        ko: "서버를 추가한 다음 로그인합니다.",
        pt: "Adicione o servidor e depois entre.",
        ru: "Добавьте сервер, затем войдите.",
        tr: "Sunucuyu ekleyin, ardından giriş yapın.",
        uk: "Додайте сервер, потім увійдіть.",
        yue: "加入伺服器，然後登入。",
        "zh-Hans": "添加服务器，然后登录。",
      },
      {
        en: "Or add the server to ~/.codex/config.toml yourself.",
        he: "או הוסף/י את השרת בעצמך לקובץ ‎~/.codex/config.toml.",
        ar: "أو أضف الخادم بنفسك إلى ‎~/.codex/config.toml.",
        de: "Oder fügen Sie den Server selbst zu ~/.codex/config.toml hinzu.",
        es: "O añade tú mismo el servidor a ~/.codex/config.toml.",
        fa: "یا خودتان سرور را به ‎~/.codex/config.toml اضافه کنید.",
        fr: "Ou ajoutez vous-même le serveur à ~/.codex/config.toml.",
        hi: "या सर्वर को खुद ~/.codex/config.toml में जोड़ें।",
        it: "Oppure aggiungi tu stesso il server a ~/.codex/config.toml.",
        ja: "または、~/.codex/config.toml にサーバーを手動で追加します。",
        ko: "또는 ~/.codex/config.toml에 서버를 직접 추가합니다.",
        pt: "Ou adicione você mesmo o servidor ao ~/.codex/config.toml.",
        ru: "Или добавьте сервер в ~/.codex/config.toml вручную.",
        tr: "Ya da sunucuyu ~/.codex/config.toml dosyasına kendiniz ekleyin.",
        uk: "Або додайте сервер у ~/.codex/config.toml вручну.",
        yue: "或者自己將伺服器加入 ~/.codex/config.toml。",
        "zh-Hans": "或者自行将服务器添加到 ~/.codex/config.toml。",
      },
    ],
    snippets: [
      {
        label: {
          en: "Add and sign in",
          he: "הוספה והתחברות",
          ar: "الإضافة وتسجيل الدخول",
          de: "Hinzufügen und anmelden",
          es: "Añadir e iniciar sesión",
          fa: "افزودن و ورود",
          fr: "Ajouter et se connecter",
          hi: "जोड़ें और साइन इन करें",
          it: "Aggiungi e accedi",
          ja: "追加してサインイン",
          ko: "추가 및 로그인",
          pt: "Adicionar e entrar",
          ru: "Добавление и вход",
          tr: "Ekleme ve giriş",
          uk: "Додавання і вхід",
          yue: "加入同登入",
          "zh-Hans": "添加并登录",
        },
        language: "bash",
        code: `codex mcp add ${MCP_SERVER_NAME} --url ${MCP_SERVER_URL}\ncodex mcp login ${MCP_SERVER_NAME}`,
      },
      {
        label: sameText("~/.codex/config.toml", "‎~/.codex/config.toml"),
        language: "toml",
        code: `[mcp_servers.${MCP_SERVER_NAME}]\nurl = "${MCP_SERVER_URL}"`,
      },
    ],
  },
  {
    id: "cursor",
    name: sameText("Cursor"),
    verified: true,
    source: "https://cursor.com/docs/context/mcp/install-links",
    installUrl: cursorInstallUrl(),
    steps: [
      {
        en: "Use the install link, or add the server to ~/.cursor/mcp.json.",
        he: "השתמש/י בקישור ההתקנה, או הוסף/י את השרת לקובץ ‎~/.cursor/mcp.json.",
        ar: "استخدم رابط التثبيت، أو أضف الخادم إلى ‎~/.cursor/mcp.json.",
        de: "Verwenden Sie den Installationslink oder fügen Sie den Server zu ~/.cursor/mcp.json hinzu.",
        es: "Usa el enlace de instalación o añade el servidor a ~/.cursor/mcp.json.",
        fa: "از پیوند نصب استفاده کنید، یا سرور را به ‎~/.cursor/mcp.json اضافه کنید.",
        fr: "Utilisez le lien d'installation ou ajoutez le serveur à ~/.cursor/mcp.json.",
        hi: "इंस्टॉल लिंक का उपयोग करें, या सर्वर को ~/.cursor/mcp.json में जोड़ें।",
        it: "Usa il link di installazione oppure aggiungi il server a ~/.cursor/mcp.json.",
        ja: "インストールリンクを使用するか、~/.cursor/mcp.json にサーバーを追加します。",
        ko: "설치 링크를 사용하거나 ~/.cursor/mcp.json에 서버를 추가합니다.",
        pt: "Use o link de instalação ou adicione o servidor ao ~/.cursor/mcp.json.",
        ru: "Используйте ссылку для установки или добавьте сервер в ~/.cursor/mcp.json.",
        tr: "Kurulum bağlantısını kullanın ya da sunucuyu ~/.cursor/mcp.json dosyasına ekleyin.",
        uk: "Скористайтеся посиланням для встановлення або додайте сервер у ~/.cursor/mcp.json.",
        yue: "用安裝連結，或者將伺服器加入 ~/.cursor/mcp.json。",
        "zh-Hans": "使用安装链接，或将服务器添加到 ~/.cursor/mcp.json。",
      },
      {
        en: "Cursor asks you to sign in to Skynet.",
        he: "Cursor יבקש ממך להתחבר ל-Skynet.",
        ar: "يطلب منك Cursor تسجيل الدخول إلى Skynet.",
        de: "Cursor fordert Sie auf, sich bei Skynet anzumelden.",
        es: "Cursor te pide que inicies sesión en Skynet.",
        fa: "Cursor از شما می‌خواهد وارد Skynet شوید.",
        fr: "Cursor vous demande de vous connecter à Skynet.",
        hi: "Cursor आपसे Skynet में साइन इन करने को कहता है।",
        it: "Cursor ti chiede di accedere a Skynet.",
        ja: "Cursor から Skynet へのサインインを求められます。",
        ko: "Cursor가 Skynet에 로그인하라고 요청합니다.",
        pt: "O Cursor pede que você entre no Skynet.",
        ru: "Cursor предложит вам войти в Skynet.",
        tr: "Cursor sizden Skynet'e giriş yapmanızı ister.",
        uk: "Cursor попросить вас увійти в Skynet.",
        yue: "Cursor 會叫你登入 Skynet。",
        "zh-Hans": "Cursor 会提示你登录 Skynet。",
      },
    ],
    snippets: [
      {
        label: sameText("~/.cursor/mcp.json", "‎~/.cursor/mcp.json"),
        language: "json",
        code: json({ mcpServers: { [MCP_SERVER_NAME]: { url: MCP_SERVER_URL } } }),
      },
    ],
  },
  {
    id: "vscode",
    name: sameText("VS Code"),
    verified: true,
    source: "https://code.visualstudio.com/docs/agents/reference/mcp-configuration",
    installUrl: vscodeInstallUrl(),
    steps: [
      {
        en: "Use the install link, the command line, or .vscode/mcp.json in your workspace.",
        he: "השתמש/י בקישור ההתקנה, בשורת הפקודה, או בקובץ ‎.vscode/mcp.json בסביבת העבודה.",
        ar: "استخدم رابط التثبيت، أو سطر الأوامر، أو ‎.vscode/mcp.json في مساحة العمل.",
        de: "Verwenden Sie den Installationslink, die Befehlszeile oder .vscode/mcp.json in Ihrem Workspace.",
        es: "Usa el enlace de instalación, la línea de comandos o .vscode/mcp.json en tu espacio de trabajo.",
        fa: "از پیوند نصب، خط فرمان یا ‎.vscode/mcp.json در فضای کاری استفاده کنید.",
        fr: "Utilisez le lien d'installation, la ligne de commande ou .vscode/mcp.json dans votre espace de travail.",
        hi: "इंस्टॉल लिंक, कमांड लाइन, या अपने वर्कस्पेस में .vscode/mcp.json का उपयोग करें।",
        it: "Usa il link di installazione, la riga di comando oppure .vscode/mcp.json nel workspace.",
        ja: "インストールリンク、コマンドライン、またはワークスペースの .vscode/mcp.json を使用します。",
        ko: "설치 링크, 명령줄 또는 워크스페이스의 .vscode/mcp.json을 사용합니다.",
        pt: "Use o link de instalação, a linha de comando ou o .vscode/mcp.json no seu workspace.",
        ru: "Используйте ссылку для установки, командную строку или файл .vscode/mcp.json в рабочей области.",
        tr: "Kurulum bağlantısını, komut satırını ya da çalışma alanınızdaki .vscode/mcp.json dosyasını kullanın.",
        uk: "Скористайтеся посиланням для встановлення, командним рядком або файлом .vscode/mcp.json у робочій області.",
        yue: "用安裝連結、命令列，或者工作區入面嘅 .vscode/mcp.json。",
        "zh-Hans": "使用安装链接、命令行或工作区中的 .vscode/mcp.json。",
      },
      {
        en: "VS Code asks you to sign in to Skynet when the server starts.",
        he: "VS Code יבקש ממך להתחבר ל-Skynet כשהשרת עולה.",
        ar: "يطلب منك VS Code تسجيل الدخول إلى Skynet عند بدء تشغيل الخادم.",
        de: "VS Code fordert Sie beim Start des Servers auf, sich bei Skynet anzumelden.",
        es: "VS Code te pide que inicies sesión en Skynet cuando se inicia el servidor.",
        fa: "VS Code هنگام شروع سرور از شما می‌خواهد وارد Skynet شوید.",
        fr: "VS Code vous demande de vous connecter à Skynet au démarrage du serveur.",
        hi: "सर्वर शुरू होने पर VS Code आपसे Skynet में साइन इन करने को कहता है।",
        it: "VS Code ti chiede di accedere a Skynet quando il server si avvia.",
        ja: "サーバーの起動時に、VS Code から Skynet へのサインインを求められます。",
        ko: "서버가 시작되면 VS Code가 Skynet에 로그인하라고 요청합니다.",
        pt: "O VS Code pede que você entre no Skynet quando o servidor inicia.",
        ru: "При запуске сервера VS Code предложит вам войти в Skynet.",
        tr: "Sunucu başladığında VS Code sizden Skynet'e giriş yapmanızı ister.",
        uk: "Коли сервер запускається, VS Code попросить вас увійти в Skynet.",
        yue: "伺服器啟動嗰陣，VS Code 會叫你登入 Skynet。",
        "zh-Hans": "服务器启动时，VS Code 会提示你登录 Skynet。",
      },
    ],
    snippets: [
      {
        label: {
          en: "Command line",
          he: "שורת פקודה",
          ar: "سطر الأوامر",
          de: "Befehlszeile",
          es: "Línea de comandos",
          fa: "خط فرمان",
          fr: "Ligne de commande",
          hi: "कमांड लाइन",
          it: "Riga di comando",
          ja: "コマンドライン",
          ko: "명령줄",
          pt: "Linha de comando",
          ru: "Командная строка",
          tr: "Komut satırı",
          uk: "Командний рядок",
          yue: "命令列",
          "zh-Hans": "命令行",
        },
        language: "bash",
        code: `code --add-mcp '${JSON.stringify({ name: MCP_SERVER_NAME, type: "http", url: MCP_SERVER_URL })}'`,
      },
      {
        label: sameText(".vscode/mcp.json", "‎.vscode/mcp.json"),
        language: "json",
        code: json({ servers: { [MCP_SERVER_NAME]: { type: "http", url: MCP_SERVER_URL } } }),
      },
    ],
  },
  {
    id: "gemini-cli",
    name: sameText("Gemini CLI"),
    verified: true,
    source: "https://geminicli.com/docs/tools/mcp-server/",
    steps: [
      {
        en: "Add the server.",
        he: "הוסף/י את השרת.",
        ar: "أضف الخادم.",
        de: "Fügen Sie den Server hinzu.",
        es: "Añade el servidor.",
        fa: "سرور را اضافه کنید.",
        fr: "Ajoutez le serveur.",
        hi: "सर्वर जोड़ें।",
        it: "Aggiungi il server.",
        ja: "サーバーを追加します。",
        ko: "서버를 추가합니다.",
        pt: "Adicione o servidor.",
        ru: "Добавьте сервер.",
        tr: "Sunucuyu ekleyin.",
        uk: "Додайте сервер.",
        yue: "加入伺服器。",
        "zh-Hans": "添加服务器。",
      },
      {
        en: "In Gemini CLI, run /mcp auth skynet and sign in.",
        he: "ב-Gemini CLI, הרץ/י ‎/mcp auth skynet והתחבר/י.",
        ar: "في Gemini CLI، شغّل ‎/mcp auth skynet وسجّل الدخول.",
        de: "Führen Sie in Gemini CLI /mcp auth skynet aus und melden Sie sich an.",
        es: "En Gemini CLI, ejecuta /mcp auth skynet e inicia sesión.",
        fa: "در Gemini CLI، ‎/mcp auth skynet را اجرا کنید و وارد شوید.",
        fr: "Dans Gemini CLI, exécutez /mcp auth skynet et connectez-vous.",
        hi: "Gemini CLI में /mcp auth skynet चलाएँ और साइन इन करें।",
        it: "In Gemini CLI, esegui /mcp auth skynet e accedi.",
        ja: "Gemini CLI で /mcp auth skynet を実行し、サインインします。",
        ko: "Gemini CLI에서 /mcp auth skynet을 실행하고 로그인합니다.",
        pt: "No Gemini CLI, execute /mcp auth skynet e entre.",
        ru: "В Gemini CLI выполните /mcp auth skynet и войдите.",
        tr: "Gemini CLI'da /mcp auth skynet komutunu çalıştırın ve giriş yapın.",
        uk: "У Gemini CLI виконайте /mcp auth skynet і увійдіть.",
        yue: "喺 Gemini CLI 執行 /mcp auth skynet，然後登入。",
        "zh-Hans": "在 Gemini CLI 中运行 /mcp auth skynet，然后登录。",
      },
    ],
    snippets: [
      {
        label: {
          en: "Add the server",
          he: "הוספת השרת",
          ar: "إضافة الخادم",
          de: "Server hinzufügen",
          es: "Añadir el servidor",
          fa: "افزودن سرور",
          fr: "Ajouter le serveur",
          hi: "सर्वर जोड़ें",
          it: "Aggiungi il server",
          ja: "サーバーを追加",
          ko: "서버 추가",
          pt: "Adicionar o servidor",
          ru: "Добавление сервера",
          tr: "Sunucu ekleme",
          uk: "Додавання сервера",
          yue: "加入伺服器",
          "zh-Hans": "添加服务器",
        },
        language: "bash",
        code: `gemini mcp add --transport http ${MCP_SERVER_NAME} ${MCP_SERVER_URL}`,
      },
      {
        label: sameText("settings.json"),
        language: "json",
        code: json({ mcpServers: { [MCP_SERVER_NAME]: { httpUrl: MCP_SERVER_URL } } }),
      },
    ],
  },
  {
    id: "windsurf",
    name: sameText("Windsurf"),
    verified: false,
    steps: [
      {
        en: "Add the server to mcp_config.json.",
        he: "הוסף/י את השרת לקובץ mcp_config.json.",
        ar: "أضف الخادم إلى mcp_config.json.",
        de: "Fügen Sie den Server zu mcp_config.json hinzu.",
        es: "Añade el servidor a mcp_config.json.",
        fa: "سرور را به mcp_config.json اضافه کنید.",
        fr: "Ajoutez le serveur à mcp_config.json.",
        hi: "सर्वर को mcp_config.json में जोड़ें।",
        it: "Aggiungi il server a mcp_config.json.",
        ja: "mcp_config.json にサーバーを追加します。",
        ko: "mcp_config.json에 서버를 추가합니다.",
        pt: "Adicione o servidor ao mcp_config.json.",
        ru: "Добавьте сервер в mcp_config.json.",
        tr: "Sunucuyu mcp_config.json dosyasına ekleyin.",
        uk: "Додайте сервер у mcp_config.json.",
        yue: "將伺服器加入 mcp_config.json。",
        "zh-Hans": "将服务器添加到 mcp_config.json。",
      },
    ],
    snippets: [
      {
        label: sameText("mcp_config.json"),
        language: "json",
        code: json({ mcpServers: { [MCP_SERVER_NAME]: { serverUrl: MCP_SERVER_URL } } }),
      },
    ],
  },
  {
    id: "other",
    name: {
      en: "Other clients",
      he: "לקוחות אחרים",
      ar: "عملاء آخرون",
      de: "Andere Clients",
      es: "Otros clientes",
      fa: "کلاینت‌های دیگر",
      fr: "Autres clients",
      hi: "अन्य क्लाइंट",
      it: "Altri client",
      ja: "その他のクライアント",
      ko: "기타 클라이언트",
      pt: "Outros clientes",
      ru: "Другие клиенты",
      tr: "Diğer istemciler",
      uk: "Інші клієнти",
      yue: "其他用戶端",
      "zh-Hans": "其他客户端",
    },
    verified: true,
    steps: [
      {
        en: "Any client that supports remote MCP servers over Streamable HTTP with OAuth can connect. Give it the server URL. Do not give it an API key.",
        he: "כל לקוח שתומך בשרתי MCP מרוחקים ב-Streamable HTTP עם OAuth יכול להתחבר. יש לתת לו את כתובת השרת. אין לתת לו מפתח API.",
        ar: "يمكن لأي عميل يدعم خوادم MCP البعيدة عبر Streamable HTTP مع OAuth أن يتصل. أعطه عنوان URL للخادم. لا تعطه مفتاح API.",
        de: "Jeder Client, der entfernte MCP-Server über Streamable HTTP mit OAuth unterstützt, kann sich verbinden. Geben Sie ihm die Server-URL. Geben Sie ihm keinen API-Schlüssel.",
        es: "Cualquier cliente que admita servidores MCP remotos mediante Streamable HTTP con OAuth puede conectarse. Dale la URL del servidor. No le des una clave de API.",
        fa: "هر کلاینتی که از سرورهای MCP راه دور از طریق Streamable HTTP با OAuth پشتیبانی کند، می‌تواند وصل شود. نشانی سرور را به آن بدهید. کلید API به آن ندهید.",
        fr: "Tout client qui prend en charge les serveurs MCP distants via Streamable HTTP avec OAuth peut se connecter. Donnez-lui l'URL du serveur. Ne lui donnez pas de clé API.",
        hi: "कोई भी क्लाइंट जो OAuth के साथ Streamable HTTP पर रिमोट MCP सर्वर सपोर्ट करता है, कनेक्ट कर सकता है। उसे सर्वर URL दें। उसे API कुंजी न दें।",
        it: "Qualsiasi client che supporta server MCP remoti tramite Streamable HTTP con OAuth può collegarsi. Fornisci l'URL del server. Non fornire una chiave API.",
        ja: "OAuth を使った Streamable HTTP でリモート MCP サーバーに対応しているクライアントなら、どれでも接続できます。サーバー URL を指定します。API キーは指定しないでください。",
        ko: "OAuth를 사용하는 Streamable HTTP로 원격 MCP 서버를 지원하는 클라이언트는 모두 연결할 수 있습니다. 서버 URL을 입력합니다. API 키는 입력하지 마세요.",
        pt: "Qualquer cliente que ofereça suporte a servidores MCP remotos via Streamable HTTP com OAuth pode se conectar. Informe a URL do servidor. Não informe uma chave de API.",
        ru: "Подключиться может любой клиент, который поддерживает удалённые MCP-серверы через Streamable HTTP с OAuth. Укажите ему URL сервера. Не указывайте ключ API.",
        tr: "OAuth ile Streamable HTTP üzerinden uzak MCP sunucularını destekleyen her istemci bağlanabilir. İstemciye sunucu URL'sini verin. API anahtarı vermeyin.",
        uk: "Підключитися може будь-який клієнт, що підтримує віддалені MCP-сервери через Streamable HTTP з OAuth. Вкажіть йому URL-адресу сервера. Не вказуйте ключ API.",
        yue: "任何支援以 Streamable HTTP 加 OAuth 連接遠端 MCP 伺服器嘅用戶端都可以連線。俾佢伺服器 URL。唔好俾佢 API 金鑰。",
        "zh-Hans": "任何支持通过 Streamable HTTP 和 OAuth 连接远程 MCP 服务器的客户端都可以连接。向它提供服务器 URL。不要向它提供 API 密钥。",
      },
    ],
    snippets: [
      { label: {
        en: "Server URL",
        he: "כתובת השרת",
        ar: "عنوان URL للخادم",
        de: "Server-URL",
        es: "URL del servidor",
        fa: "نشانی سرور",
        fr: "URL du serveur",
        hi: "सर्वर URL",
        it: "URL del server",
        ja: "サーバー URL",
        ko: "서버 URL",
        pt: "URL do servidor",
        ru: "URL сервера",
        tr: "Sunucu URL'si",
        uk: "URL-адреса сервера",
        yue: "伺服器 URL",
        "zh-Hans": "服务器 URL",
      }, language: "text", code: MCP_SERVER_URL },
    ],
  },
];

const MD_TEXT: Localized<{ install: string; unverified: string; source: string; checked: string }> = {
  en: {
    checked: "Last checked against each client's docs: {date}.",
    install: "Install with one click",
    unverified:
      "Not checked against this client's own docs. If these steps fail, use the client's MCP docs with the server URL.",
    source: "Official setup docs",
  },
  he: {
    checked: "נבדק לאחרונה מול התיעוד של כל לקוח: {date}.",
    install: "התקנה בלחיצה אחת",
    unverified:
      "השלבים לא נבדקו מול התיעוד של הלקוח עצמו. אם הם לא עובדים, השתמש/י בתיעוד ה-MCP של הלקוח עם כתובת השרת.",
    source: "תיעוד ההתקנה הרשמי",
  },
  ar: {
    checked: "آخر تحقق من التوثيق الخاص بكل عميل: {date}.",
    install: "التثبيت بنقرة واحدة",
    unverified:
      "لم يُتحقق من هذه الخطوات مقابل توثيق هذا العميل. إذا فشلت، فاستخدم توثيق MCP الخاص بالعميل مع عنوان URL للخادم.",
    source: "توثيق الإعداد الرسمي",
  },
  de: {
    checked: "Zuletzt mit der Dokumentation jedes Clients abgeglichen: {date}.",
    install: "Mit einem Klick installieren",
    unverified:
      "Nicht mit der Dokumentation dieses Clients abgeglichen. Wenn diese Schritte nicht funktionieren, verwenden Sie die MCP-Dokumentation des Clients mit der Server-URL.",
    source: "Offizielle Einrichtungsdokumentation",
  },
  es: {
    checked: "Última comprobación con la documentación de cada cliente: {date}.",
    install: "Instalar con un clic",
    unverified:
      "No se comprobó con la documentación de este cliente. Si estos pasos fallan, usa la documentación de MCP del cliente con la URL del servidor.",
    source: "Documentación oficial de configuración",
  },
  fa: {
    checked: "آخرین بررسی با مستندات هر کلاینت: {date}.",
    install: "نصب با یک کلیک",
    unverified:
      "این مراحل با مستندات خود این کلاینت بررسی نشده‌اند. اگر کار نکردند، از مستندات MCP کلاینت با نشانی سرور استفاده کنید.",
    source: "مستندات رسمی راه‌اندازی",
  },
  fr: {
    checked: "Dernière vérification avec la documentation de chaque client : {date}.",
    install: "Installer en un clic",
    unverified:
      "Non vérifié avec la documentation de ce client. Si ces étapes échouent, utilisez la documentation MCP du client avec l'URL du serveur.",
    source: "Documentation officielle de configuration",
  },
  hi: {
    checked: "हर क्लाइंट के दस्तावेज़ों से आखिरी जाँच: {date}।",
    install: "एक क्लिक में इंस्टॉल करें",
    unverified:
      "इस क्लाइंट के अपने दस्तावेज़ों से जाँच नहीं की गई। अगर ये चरण काम न करें, तो सर्वर URL के साथ क्लाइंट के MCP दस्तावेज़ों का उपयोग करें।",
    source: "आधिकारिक सेटअप दस्तावेज़",
  },
  it: {
    checked: "Ultimo controllo con la documentazione di ogni client: {date}.",
    install: "Installa con un clic",
    unverified:
      "Non verificato con la documentazione di questo client. Se questi passaggi non funzionano, usa la documentazione MCP del client con l'URL del server.",
    source: "Documentazione ufficiale di configurazione",
  },
  ja: {
    checked: "各クライアントのドキュメントとの最終確認日: {date}。",
    install: "ワンクリックでインストール",
    unverified:
      "このクライアント自身のドキュメントでは確認していません。この手順でうまくいかない場合は、サーバー URL を使ってクライアントの MCP ドキュメントに従ってください。",
    source: "公式のセットアップドキュメント",
  },
  ko: {
    checked: "각 클라이언트 문서와 마지막으로 확인한 날짜: {date}.",
    install: "한 번 클릭으로 설치",
    unverified:
      "이 클라이언트의 자체 문서로 확인하지 않았습니다. 이 단계가 실패하면 서버 URL과 함께 클라이언트의 MCP 문서를 사용하세요.",
    source: "공식 설정 문서",
  },
  pt: {
    checked: "Última verificação com a documentação de cada cliente: {date}.",
    install: "Instalar com um clique",
    unverified:
      "Não verificado com a documentação deste cliente. Se estas etapas falharem, use a documentação de MCP do cliente com a URL do servidor.",
    source: "Documentação oficial de configuração",
  },
  ru: {
    checked: "Последняя сверка с документацией каждого клиента: {date}.",
    install: "Установить в один клик",
    unverified:
      "Не сверено с документацией этого клиента. Если эти шаги не работают, используйте документацию MCP клиента и URL сервера.",
    source: "Официальная документация по настройке",
  },
  tr: {
    checked: "Her istemcinin belgeleriyle son kontrol: {date}.",
    install: "Tek tıkla kurun",
    unverified:
      "Bu istemcinin kendi belgeleriyle kontrol edilmedi. Bu adımlar işe yaramazsa istemcinin MCP belgelerini sunucu URL'siyle kullanın.",
    source: "Resmî kurulum belgeleri",
  },
  uk: {
    checked: "Остання звірка з документацією кожного клієнта: {date}.",
    install: "Установити одним клацанням",
    unverified:
      "Не звірено з документацією цього клієнта. Якщо ці кроки не працюють, скористайтеся документацією MCP клієнта й URL-адресою сервера.",
    source: "Офіційна документація з налаштування",
  },
  yue: {
    checked: "最後一次對照每個用戶端嘅文件檢查：{date}。",
    install: "一撳安裝",
    unverified:
      "未對照呢個用戶端自己嘅文件檢查。如果呢啲步驟唔得，就用伺服器 URL 跟住用戶端嘅 MCP 文件做。",
    source: "官方設定文件",
  },
  "zh-Hans": {
    checked: "最后一次对照各客户端文档检查：{date}。",
    install: "一键安装",
    unverified:
      "未对照该客户端自己的文档检查。如果这些步骤失败，请使用客户端的 MCP 文档和服务器 URL。",
    source: "官方设置文档",
  },
};

/** A client's install link for the MCP server at `url`. */
function installUrlFor(client: McpClient, url: string): string | undefined {
  if (client.installUrl === cursorInstallUrl()) return cursorInstallUrl(url);
  if (client.installUrl === vscodeInstallUrl()) return vscodeInstallUrl(url);
  return client.installUrl;
}

/**
 * One client's setup steps as Markdown, without a heading. `url` is the MCP
 * server the steps name: staging and PR previews pass their own server.
 */
export function mcpClientMarkdown(client: McpClient, locale: McpLocale, url: string = MCP_SERVER_URL): string {
  const t = MD_TEXT[locale] ?? MD_TEXT.en;
  const parts: string[] = [];
  if (!client.verified) parts.push(`> [!WARNING]\n> ${t.unverified}`);
  parts.push(client.steps.map((step, i) => `${i + 1}. ${mcpText(step, locale)}`).join("\n"));
  const installUrl = installUrlFor(client, url);
  if (installUrl) parts.push(`[${t.install}](${installUrl})`);
  for (const snippet of client.snippets) {
    parts.push(`**${mcpText(snippet.label, locale)}**\n\n\`\`\`${snippet.language}\n${snippet.code}\n\`\`\``);
  }
  if (client.note) parts.push(`> [!NOTE]\n> ${mcpText(client.note, locale)}`);
  if (client.source) parts.push(`[${t.source}](${client.source})`);
  return parts.join("\n\n").split(MCP_SERVER_URL).join(url);
}

/** The "last checked" line shown above the client steps. */
export function mcpClientsCheckedLine(locale: McpLocale): string {
  return (MD_TEXT[locale] ?? MD_TEXT.en).checked.replace("{date}", MCP_CLIENTS_LAST_CHECKED);
}

/** Every client's setup steps as Markdown, one level-3 section per client. */
export function mcpClientsMarkdown(locale: McpLocale, url: string = MCP_SERVER_URL): string {
  const sections = MCP_CLIENTS.map((client) => `### ${mcpText(client.name, locale)}\n\n${mcpClientMarkdown(client, locale, url)}`);
  return [`_${mcpClientsCheckedLine(locale)}_`, ...sections].join("\n\n");
}
