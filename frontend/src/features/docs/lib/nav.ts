/**
 * The public docs manifest: every page, its section, its titles and the date
 * someone last checked it against the code. The sidebar, prev/next links,
 * search, sitemap and llms.txt all read this one list, in this order.
 *
 * Kept free of `@/` imports so `node --test` can load it.
 */

/**
 * The docs locales: the app's full-translation locales (FULL_TRANSLATION_LOCALES
 * in shared/lib/locale.ts). English pages have no URL prefix; every other
 * locale lives under /docs/<locale>.
 */
export const DOCS_LOCALES = [
  "en",
  "he",
  "ar",
  "de",
  "es",
  "fa",
  "fr",
  "hi",
  "it",
  "ja",
  "ko",
  "pt",
  "ru",
  "tr",
  "uk",
  "yue",
  "zh-Hans",
] as const;
export type DocsLocale = (typeof DOCS_LOCALES)[number];

const RTL_LOCALES: ReadonlySet<DocsLocale> = new Set(["he", "ar", "fa"]);

export type DocsSectionId = "start" | "concepts" | "guides" | "integrations" | "reference";

export interface DocsPage {
  /** The page path under /docs; "" is the introduction at /docs itself. */
  slug: string;
  section: DocsSectionId;
  title: Record<DocsLocale, string>;
  description: Record<DocsLocale, string>;
  /** The day someone last checked the page against the code (YYYY-MM-DD). */
  updated: string;
}

/** Every page and its section, in reading order. */
const PAGE_SECTIONS = {
  "": "start",
  quickstart: "start",
  "concepts/optimization": "concepts",
  "concepts/engines": "concepts",
  "concepts/datasets": "concepts",
  "concepts/scorers": "concepts",
  "concepts/runs": "concepts",
  "concepts/billing": "concepts",
  "guides/datasets": "guides",
  "guides/submit": "guides",
  "guides/results": "guides",
  "guides/sharing": "guides",
  "guides/tagger": "guides",
  "guides/mac-app": "guides",
  "guides/agent-panel": "guides",
  mcp: "integrations",
  api: "integrations",
  security: "reference",
  limits: "reference",
  faq: "reference",
} as const satisfies Record<string, DocsSectionId>;

type DocsSlug = keyof typeof PAGE_SECTIONS;

const SECTION_TITLES: Record<DocsLocale, Record<DocsSectionId, string>> = {
  en: {
    start: "Get started",
    concepts: "Concepts",
    guides: "Guides",
    integrations: "Integrations",
    reference: "Reference",
  },
  he: {
    start: "תחילת עבודה",
    concepts: "מושגים",
    guides: "מדריכים",
    integrations: "חיבורים",
    reference: "עיון",
  },
  ar: {
    start: "البدء",
    concepts: "المفاهيم",
    guides: "الأدلة",
    integrations: "التكاملات",
    reference: "المرجع",
  },
  de: {
    start: "Erste Schritte",
    concepts: "Konzepte",
    guides: "Anleitungen",
    integrations: "Integrationen",
    reference: "Referenz",
  },
  es: {
    start: "Primeros pasos",
    concepts: "Conceptos",
    guides: "Guías",
    integrations: "Integraciones",
    reference: "Referencia",
  },
  fa: {
    start: "شروع کار",
    concepts: "مفاهیم",
    guides: "راهنماها",
    integrations: "یکپارچه‌سازی‌ها",
    reference: "مرجع",
  },
  fr: {
    start: "Bien démarrer",
    concepts: "Concepts",
    guides: "Guides",
    integrations: "Intégrations",
    reference: "Référence",
  },
  hi: {
    start: "शुरुआत करें",
    concepts: "अवधारणाएँ",
    guides: "गाइड",
    integrations: "इंटीग्रेशन",
    reference: "संदर्भ",
  },
  it: {
    start: "Per iniziare",
    concepts: "Concetti",
    guides: "Guide",
    integrations: "Integrazioni",
    reference: "Riferimento",
  },
  ja: {
    start: "使い始める",
    concepts: "コンセプト",
    guides: "ガイド",
    integrations: "連携",
    reference: "リファレンス",
  },
  ko: {
    start: "시작하기",
    concepts: "개념",
    guides: "가이드",
    integrations: "연동",
    reference: "참고 자료",
  },
  pt: {
    start: "Primeiros passos",
    concepts: "Conceitos",
    guides: "Guias",
    integrations: "Integrações",
    reference: "Referência",
  },
  ru: {
    start: "Начало работы",
    concepts: "Основные понятия",
    guides: "Руководства",
    integrations: "Интеграции",
    reference: "Справочник",
  },
  tr: {
    start: "Başlarken",
    concepts: "Kavramlar",
    guides: "Kılavuzlar",
    integrations: "Entegrasyonlar",
    reference: "Başvuru",
  },
  uk: {
    start: "Початок роботи",
    concepts: "Основні поняття",
    guides: "Посібники",
    integrations: "Інтеграції",
    reference: "Довідник",
  },
  yue: {
    start: "開始使用",
    concepts: "概念",
    guides: "指南",
    integrations: "整合",
    reference: "參考",
  },
  "zh-Hans": {
    start: "入门",
    concepts: "概念",
    guides: "指南",
    integrations: "集成",
    reference: "参考",
  },
};

/** Each page's title and description, per locale. */
const PAGE_TEXT: Record<
  DocsLocale,
  Record<DocsSlug, readonly [title: string, description: string]>
> = {
  en: {
    "": ["Introduction", "What Skynet is, what it optimizes, and where to start."],
    quickstart: ["Quickstart", "Start your first optimization run in about ten minutes."],
    "concepts/optimization": [
      "How optimization works",
      "The repository and scorer contract, model calls from your code, and the engines.",
    ],
    "concepts/engines": [
      "Engines",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve and Auto.",
    ],
    "concepts/datasets": [
      "Datasets and splits",
      "How Skynet reads your cases and splits them into train, validation and test.",
    ],
    "concepts/scorers": [
      "Metrics and scorers",
      "Write the score function that tells the engine what better means.",
    ],
    "concepts/runs": [
      "Runs",
      "Run states, the actions you can take on a run, and how a run stops.",
    ],
    "concepts/billing": [
      "Budgets and billing",
      "Balance, run budgets, fees, and bring-your-own-key (BYOK) model routes.",
    ],
    "guides/datasets": [
      "Prepare a dataset",
      "Upload a CSV, JSON or Excel file, check its columns, and edit rows.",
    ],
    "guides/submit": [
      "Start a run with the wizard",
      "Each step of the submit wizard, from the task to the contract check.",
    ],
    "guides/results": [
      "Read the results",
      "The run page tabs, the scores, and how to export or serve the winner.",
    ],
    "guides/sharing": [
      "Share and publish",
      "Run visibility, share links, roles, and the Explore gallery.",
    ],
    "guides/tagger": [
      "Label data with the tagger",
      "Manual, Co-pilot and Autopilot labeling, review rounds, and export.",
    ],
    "guides/mac-app": ["Mac app", "Run Skynet on your Mac with your own coding agent and Docker."],
    "guides/agent-panel": [
      "Agent panel",
      "Ask the assistant to set up, start and explain runs, and control what it may do.",
    ],
    mcp: [
      "MCP server",
      "Connect Claude, ChatGPT, Codex, Cursor, VS Code and other clients to Skynet.",
    ],
    api: ["API", "Authenticate, start a run, and read results with the HTTP API."],
    security: [
      "Security",
      "Where your code runs, how credentials are kept, and how to report a problem.",
    ],
    limits: ["Limits", "Default limits for runs, files, datasets, storage and request rates."],
    faq: ["FAQ", "Short answers to common questions."],
  },
  he: {
    "": ["מבוא", "מה זה Skynet, מה הוא משפר, ואיפה להתחיל."],
    quickstart: ["התחלה מהירה", "הפעל/י את ריצת האופטימיזציה הראשונה שלך בכעשר דקות."],
    "concepts/optimization": [
      "איך האופטימיזציה עובדת",
      "חוזה המאגר ופונקציית הניקוד, קריאות למודל מהקוד שלך, והמנועים.",
    ],
    "concepts/engines": [
      "מנועים",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve ו-Auto.",
    ],
    "concepts/datasets": [
      "דאטאסטים וחלוקה",
      "איך Skynet קורא את המקרים שלך ומחלק אותם לאימון, אימות ובדיקה.",
    ],
    "concepts/scorers": [
      "פונקציות מדידה ופונקציות ציון",
      "כתוב/כתבי את פונקציית המדידה שאומרת למנוע מה נחשב טוב יותר.",
    ],
    "concepts/runs": ["ריצות", "מצבי ריצה, הפעולות שאפשר לבצע על ריצה, ואיך ריצה נעצרת."],
    "concepts/billing": ["תקציבים וחיוב", "יתרה, תקציבי ריצה, עמלות, ומפתחות API משלך (BYOK)."],
    "guides/datasets": [
      "הכנת דאטאסט",
      "העלה/י קובץ CSV, JSON או Excel, בדוק/בדקי את העמודות וערוך/ערכי שורות.",
    ],
    "guides/submit": ["הפעלת ריצה עם האשף", "כל שלב באשף ההגשה, מהמשימה ועד בדיקת החוזה."],
    "guides/results": [
      "קריאת התוצאות",
      "לשוניות דף הריצה, הציונים, ואיך לייצא או להגיש את הגרסה המנצחת.",
    ],
    "guides/sharing": ["שיתוף ופרסום", "נראות ריצות, קישורי שיתוף, תפקידים וגלריית Explore."],
    "guides/tagger": [
      "תיוג נתונים עם כלי התיוג",
      "תיוג ידני, Co-pilot ו-Autopilot, סבבי בדיקה וייצוא.",
    ],
    "guides/mac-app": ["אפליקציית Mac", "הרץ/י את Skynet על ה-Mac שלך עם סוכן הקוד שלך ו-Docker."],
    "guides/agent-panel": [
      "פאנל הסוכן",
      "בקש/י מהעוזר להגדיר, להפעיל ולהסביר ריצות, וקבע/י מה מותר לו לעשות.",
    ],
    mcp: ["שרת MCP", "חבר/י את Claude, ChatGPT, Codex, Cursor, VS Code ולקוחות נוספים ל-Skynet."],
    api: ["API", "התחברות, הפעלת ריצה וקריאת תוצאות דרך ה-API."],
    security: ["אבטחה", "איפה הקוד שלך רץ, איך נשמרים פרטי גישה, ואיך לדווח על בעיה."],
    limits: ["מגבלות", "מגבלות ברירת המחדל לריצות, קבצים, דאטאסטים, אחסון וקצב בקשות."],
    faq: ["שאלות נפוצות", "תשובות קצרות לשאלות נפוצות."],
  },
  ar: {
    "": ["مقدمة", "ما هو Skynet، وما الذي يحسّنه، ومن أين تبدأ."],
    quickstart: ["البدء السريع", "ابدأ أول عملية تشغيل للتحسين في نحو عشر دقائق."],
    "concepts/optimization": [
      "كيف يعمل التحسين",
      "عقد المستودع والمُقيِّم، واستدعاءات النموذج من شيفرتك، والمحركات.",
    ],
    "concepts/engines": [
      "المحركات",
      "GEPA وBest-of-N وMeta-Harness وAutoResearch وAutoSaddler وShinkaEvolve وAuto.",
    ],
    "concepts/datasets": [
      "مجموعات البيانات والتقسيم",
      "كيف يقرأ Skynet حالاتك ويقسمها إلى تدريب وتحقّق واختبار.",
    ],
    "concepts/scorers": [
      "المقاييس والمُقيِّمات",
      "اكتب دالة التقييم التي تخبر المحرّك بمعنى «الأفضل».",
    ],
    "concepts/runs": [
      "عمليات التشغيل",
      "حالات التشغيل، والإجراءات التي يمكنك تنفيذها على عملية التشغيل، وكيف تتوقف.",
    ],
    "concepts/billing": [
      "الميزانيات والفوترة",
      "الرصيد، وميزانيات التشغيل، والرسوم، ومسارات النماذج بمفتاحك الخاص (BYOK).",
    ],
    "guides/datasets": [
      "تجهيز مجموعة بيانات",
      "ارفع ملف CSV أو JSON أو Excel، وتحقّق من أعمدته، وعدّل الصفوف.",
    ],
    "guides/submit": [
      "بدء التشغيل باستخدام المعالج",
      "كل خطوة في معالج الإرسال، من المهمة حتى فحص العقد.",
    ],
    "guides/results": [
      "قراءة النتائج",
      "علامات تبويب صفحة التشغيل، والدرجات، وكيفية تصدير النسخة الفائزة أو تقديمها.",
    ],
    "guides/sharing": [
      "المشاركة والنشر",
      "إمكانية رؤية عمليات التشغيل، وروابط المشاركة، والأدوار، ومعرض «استكشاف».",
    ],
    "guides/tagger": [
      "وسم البيانات بأداة الوسم",
      "الوسم اليدوي والطيّار المساعد والطيّار الآلي، وجولات المراجعة، والتصدير.",
    ],
    "guides/mac-app": [
      "تطبيق Mac",
      "شغّل Skynet على جهاز Mac باستخدام وكيل البرمجة الخاص بك وDocker.",
    ],
    "guides/agent-panel": [
      "لوحة الوكيل",
      "اطلب من المساعد إعداد عمليات التشغيل وبدأها وشرحها، وحدّد ما يُسمح له بفعله.",
    ],
    mcp: ["خادم MCP", "اربط Claude وChatGPT وCodex وCursor وVS Code وعملاء آخرين بـ Skynet."],
    api: ["API", "المصادقة، وبدء عملية تشغيل، وقراءة النتائج عبر HTTP API."],
    security: ["الأمان", "أين تعمل شيفرتك، وكيف تُحفظ بيانات الاعتماد، وكيف تبلّغ عن مشكلة."],
    limits: [
      "الحدود",
      "الحدود الافتراضية لعمليات التشغيل والملفات ومجموعات البيانات والتخزين ومعدلات الطلبات.",
    ],
    faq: ["الأسئلة الشائعة", "إجابات قصيرة عن الأسئلة الشائعة."],
  },
  de: {
    "": ["Einführung", "Was Skynet ist, was es optimiert und wo Sie anfangen."],
    quickstart: [
      "Schnellstart",
      "Starten Sie Ihren ersten Optimierungsdurchlauf in etwa zehn Minuten.",
    ],
    "concepts/optimization": [
      "So funktioniert die Optimierung",
      "Der Vertrag aus Repository und Scorer, Modellaufrufe aus Ihrem Code und die Engines.",
    ],
    "concepts/engines": [
      "Engines",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve und Auto.",
    ],
    "concepts/datasets": [
      "Datensätze und Aufteilungen",
      "Wie Skynet Ihre Fälle liest und in Training, Validierung und Test aufteilt.",
    ],
    "concepts/scorers": [
      "Metriken und Scorer",
      "Schreiben Sie die Bewertungsfunktion, die der Engine sagt, was „besser“ bedeutet.",
    ],
    "concepts/runs": [
      "Durchläufe",
      "Die Status eines Durchlaufs, die möglichen Aktionen und wie ein Durchlauf endet.",
    ],
    "concepts/billing": [
      "Budgets und Abrechnung",
      "Guthaben, Durchlaufbudgets, Gebühren und Modellrouten mit eigenem Schlüssel (BYOK).",
    ],
    "guides/datasets": [
      "Einen Datensatz vorbereiten",
      "Laden Sie eine CSV-, JSON- oder Excel-Datei hoch, prüfen Sie die Spalten und bearbeiten Sie Zeilen.",
    ],
    "guides/submit": [
      "Einen Durchlauf mit dem Assistenten starten",
      "Jeder Schritt des Assistenten, von der Aufgabe bis zur Vertragsprüfung.",
    ],
    "guides/results": [
      "Ergebnisse lesen",
      "Die Tabs der Durchlaufseite, die Scores und wie Sie den Gewinner exportieren oder bereitstellen.",
    ],
    "guides/sharing": [
      "Teilen und veröffentlichen",
      "Sichtbarkeit von Durchläufen, Freigabelinks, Rollen und die Galerie „Erkunden“.",
    ],
    "guides/tagger": [
      "Daten mit dem Tagger labeln",
      "Manuelles Labeln, Co-Pilot und Autopilot, Prüfrunden und Export.",
    ],
    "guides/mac-app": [
      "Mac-App",
      "Führen Sie Skynet auf Ihrem Mac mit Ihrem eigenen Coding-Agenten und Docker aus.",
    ],
    "guides/agent-panel": [
      "Agentenbereich",
      "Lassen Sie den Assistenten Durchläufe einrichten, starten und erklären, und legen Sie fest, was er darf.",
    ],
    mcp: [
      "MCP-Server",
      "Verbinden Sie Claude, ChatGPT, Codex, Cursor, VS Code und andere Clients mit Skynet.",
    ],
    api: [
      "API",
      "Authentifizieren, einen Durchlauf starten und Ergebnisse über die HTTP-API lesen.",
    ],
    security: [
      "Sicherheit",
      "Wo Ihr Code läuft, wie Zugangsdaten aufbewahrt werden und wie Sie ein Problem melden.",
    ],
    limits: [
      "Limits",
      "Standardlimits für Durchläufe, Dateien, Datensätze, Speicher und Anfrageraten.",
    ],
    faq: ["FAQ", "Kurze Antworten auf häufige Fragen."],
  },
  es: {
    "": ["Introducción", "Qué es Skynet, qué optimiza y por dónde empezar."],
    quickstart: [
      "Inicio rápido",
      "Inicia tu primera ejecución de optimización en unos diez minutos.",
    ],
    "concepts/optimization": [
      "Cómo funciona la optimización",
      "El contrato del repositorio y el evaluador, las llamadas al modelo desde tu código y los motores.",
    ],
    "concepts/engines": [
      "Motores",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve y Auto.",
    ],
    "concepts/datasets": [
      "Conjuntos de datos y divisiones",
      "Cómo Skynet lee tus casos y los divide en entrenamiento, validación y prueba.",
    ],
    "concepts/scorers": [
      "Métricas y evaluadores",
      "Escribe la función de puntuación que le dice al motor qué significa «mejor».",
    ],
    "concepts/runs": [
      "Ejecuciones",
      "Los estados de una ejecución, las acciones que puedes realizar y cómo se detiene.",
    ],
    "concepts/billing": [
      "Presupuestos y facturación",
      "Saldo, presupuestos de ejecución, tarifas y rutas de modelo con tu propia clave (BYOK).",
    ],
    "guides/datasets": [
      "Preparar un conjunto de datos",
      "Sube un archivo CSV, JSON o Excel, revisa sus columnas y edita filas.",
    ],
    "guides/submit": [
      "Iniciar una ejecución con el asistente",
      "Cada paso del asistente, desde la tarea hasta la comprobación del contrato.",
    ],
    "guides/results": [
      "Leer los resultados",
      "Las pestañas de la página de ejecución, las puntuaciones y cómo exportar o servir la versión ganadora.",
    ],
    "guides/sharing": [
      "Compartir y publicar",
      "Visibilidad de las ejecuciones, enlaces para compartir, roles y la galería Explorar.",
    ],
    "guides/tagger": [
      "Etiquetar datos con el etiquetador",
      "Etiquetado manual, Copiloto y Piloto automático, rondas de revisión y exportación.",
    ],
    "guides/mac-app": [
      "App para Mac",
      "Ejecuta Skynet en tu Mac con tu propio agente de programación y Docker.",
    ],
    "guides/agent-panel": [
      "Panel del agente",
      "Pide al asistente que configure, inicie y explique ejecuciones, y controla lo que puede hacer.",
    ],
    mcp: [
      "Servidor MCP",
      "Conecta Claude, ChatGPT, Codex, Cursor, VS Code y otros clientes a Skynet.",
    ],
    api: ["API", "Autentícate, inicia una ejecución y lee los resultados con la API HTTP."],
    security: [
      "Seguridad",
      "Dónde se ejecuta tu código, cómo se guardan las credenciales y cómo informar de un problema.",
    ],
    limits: [
      "Límites",
      "Límites predeterminados de ejecuciones, archivos, conjuntos de datos, almacenamiento y frecuencia de solicitudes.",
    ],
    faq: ["Preguntas frecuentes", "Respuestas breves a preguntas comunes."],
  },
  fa: {
    "": ["مقدمه", "Skynet چیست، چه چیزی را بهینه می‌کند و از کجا شروع کنید."],
    quickstart: ["شروع سریع", "اولین اجرای بهینه‌سازی خود را در حدود ده دقیقه شروع کنید."],
    "concepts/optimization": [
      "بهینه‌سازی چگونه کار می‌کند",
      "قرارداد مخزن و امتیازدهنده، فراخوانی مدل از کد شما و موتورها.",
    ],
    "concepts/engines": [
      "موتورها",
      "GEPA، Best-of-N، Meta-Harness، AutoResearch، AutoSaddler، ShinkaEvolve و Auto.",
    ],
    "concepts/datasets": [
      "مجموعه‌داده‌ها و تقسیم‌بندی",
      "Skynet چگونه نمونه‌های شما را می‌خواند و آن‌ها را به آموزش، اعتبارسنجی و آزمون تقسیم می‌کند.",
    ],
    "concepts/scorers": [
      "معیارها و امتیازدهنده‌ها",
      "تابع امتیازی را بنویسید که به موتور می‌گوید «بهتر» یعنی چه.",
    ],
    "concepts/runs": [
      "اجراها",
      "وضعیت‌های اجرا، کارهایی که می‌توانید روی یک اجرا انجام دهید و اینکه اجرا چگونه متوقف می‌شود.",
    ],
    "concepts/billing": [
      "بودجه‌ها و صورت‌حساب",
      "موجودی، بودجه‌های اجرا، کارمزدها و مسیرهای مدل با کلید خودتان (BYOK).",
    ],
    "guides/datasets": [
      "آماده‌سازی مجموعه‌داده",
      "یک فایل CSV، JSON یا Excel بارگذاری کنید، ستون‌هایش را بررسی کنید و ردیف‌ها را ویرایش کنید.",
    ],
    "guides/submit": [
      "شروع اجرا با دستیار گام‌به‌گام",
      "هر مرحلهٔ دستیار ارسال، از وظیفه تا بررسی قرارداد.",
    ],
    "guides/results": [
      "خواندن نتایج",
      "زبانه‌های صفحهٔ اجرا، امتیازها و روش خروجی گرفتن یا ارائهٔ نسخهٔ برنده.",
    ],
    "guides/sharing": [
      "اشتراک‌گذاری و انتشار",
      "نمایان‌بودن اجراها، پیوندهای اشتراک‌گذاری، نقش‌ها و گالری «جستجوی عمومی».",
    ],
    "guides/tagger": [
      "برچسب‌گذاری داده با ابزار برچسب‌گذاری",
      "برچسب‌گذاری دستی، همیار و خلبان خودکار، دورهای بازبینی و خروجی گرفتن.",
    ],
    "guides/mac-app": [
      "برنامهٔ Mac",
      "Skynet را روی Mac خود با عامل کدنویسی خودتان و Docker اجرا کنید.",
    ],
    "guides/agent-panel": [
      "پنل عامل",
      "از دستیار بخواهید اجراها را تنظیم، شروع و توضیح دهد، و تعیین کنید چه کارهایی مجاز است.",
    ],
    mcp: [
      "سرور MCP",
      "Claude، ChatGPT، Codex، Cursor، VS Code و کلاینت‌های دیگر را به Skynet وصل کنید.",
    ],
    api: ["API", "احراز هویت، شروع اجرا و خواندن نتایج با HTTP API."],
    security: [
      "امنیت",
      "کد شما کجا اجرا می‌شود، اطلاعات ورود چگونه نگهداری می‌شوند و چگونه مشکلی را گزارش کنید.",
    ],
    limits: [
      "محدودیت‌ها",
      "محدودیت‌های پیش‌فرض برای اجراها، فایل‌ها، مجموعه‌داده‌ها، فضای ذخیره‌سازی و نرخ درخواست‌ها.",
    ],
    faq: ["پرسش‌های متداول", "پاسخ‌های کوتاه به پرسش‌های رایج."],
  },
  fr: {
    "": ["Introduction", "Ce qu'est Skynet, ce qu'il optimise et par où commencer."],
    quickstart: [
      "Démarrage rapide",
      "Lancez votre première exécution d'optimisation en une dizaine de minutes.",
    ],
    "concepts/optimization": [
      "Fonctionnement de l'optimisation",
      "Le contrat du dépôt et de l'évaluateur, les appels de modèle depuis votre code et les moteurs.",
    ],
    "concepts/engines": [
      "Moteurs",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve et Auto.",
    ],
    "concepts/datasets": [
      "Jeux de données et répartitions",
      "Comment Skynet lit vos cas et les répartit en entraînement, validation et test.",
    ],
    "concepts/scorers": [
      "Métriques et évaluateurs",
      "Écrivez la fonction de score qui indique au moteur ce que « mieux » veut dire.",
    ],
    "concepts/runs": [
      "Exécutions",
      "Les états d'une exécution, les actions possibles et la façon dont elle s'arrête.",
    ],
    "concepts/billing": [
      "Budgets et facturation",
      "Solde, budgets d'exécution, frais et routes de modèles avec votre propre clé (BYOK).",
    ],
    "guides/datasets": [
      "Préparer un jeu de données",
      "Importez un fichier CSV, JSON ou Excel, vérifiez ses colonnes et modifiez des lignes.",
    ],
    "guides/submit": [
      "Lancer une exécution avec l'assistant",
      "Chaque étape de l'assistant, de la tâche à la vérification du contrat.",
    ],
    "guides/results": [
      "Lire les résultats",
      "Les onglets de la page d'exécution, les scores et comment exporter ou servir la version gagnante.",
    ],
    "guides/sharing": [
      "Partager et publier",
      "Visibilité des exécutions, liens de partage, rôles et la galerie Explorer.",
    ],
    "guides/tagger": [
      "Annoter des données avec l'outil d'annotation",
      "Annotation manuelle, Copilote et Pilote automatique, cycles de relecture et export.",
    ],
    "guides/mac-app": [
      "App Mac",
      "Exécutez Skynet sur votre Mac avec votre propre agent de code et Docker.",
    ],
    "guides/agent-panel": [
      "Panneau de l'agent",
      "Demandez à l'assistant de configurer, lancer et expliquer des exécutions, et contrôlez ce qu'il peut faire.",
    ],
    mcp: [
      "Serveur MCP",
      "Connectez Claude, ChatGPT, Codex, Cursor, VS Code et d'autres clients à Skynet.",
    ],
    api: ["API", "Authentifiez-vous, lancez une exécution et lisez les résultats avec l'API HTTP."],
    security: [
      "Sécurité",
      "Où s'exécute votre code, comment les identifiants sont conservés et comment signaler un problème.",
    ],
    limits: [
      "Limites",
      "Limites par défaut des exécutions, fichiers, jeux de données, stockage et débit de requêtes.",
    ],
    faq: ["FAQ", "Réponses courtes aux questions fréquentes."],
  },
  hi: {
    "": ["परिचय", "Skynet क्या है, यह क्या ऑप्टिमाइज़ करता है, और कहाँ से शुरू करें।"],
    quickstart: ["क्विकस्टार्ट", "लगभग दस मिनट में अपना पहला ऑप्टिमाइज़ेशन रन शुरू करें।"],
    "concepts/optimization": [
      "ऑप्टिमाइज़ेशन कैसे काम करता है",
      "रिपॉज़िटरी और स्कोरर का अनुबंध, आपके कोड से मॉडल कॉल, और इंजन।",
    ],
    "concepts/engines": [
      "इंजन",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve और Auto।",
    ],
    "concepts/datasets": [
      "डेटासेट और विभाजन",
      "Skynet आपके केस कैसे पढ़ता है और उन्हें प्रशिक्षण, मान्यकरण और परीक्षण में कैसे बाँटता है।",
    ],
    "concepts/scorers": [
      "मेट्रिक और स्कोरर",
      "वह स्कोर फ़ंक्शन लिखें जो इंजन को बताता है कि बेहतर का क्या मतलब है।",
    ],
    "concepts/runs": [
      "रन",
      "रन की स्थितियाँ, रन पर आप कौन-सी कार्रवाइयाँ कर सकते हैं, और रन कैसे रुकता है।",
    ],
    "concepts/billing": [
      "बजट और बिलिंग",
      "बैलेंस, रन बजट, शुल्क, और अपनी कुंजी (BYOK) वाले मॉडल रूट।",
    ],
    "guides/datasets": [
      "डेटासेट तैयार करें",
      "CSV, JSON या Excel फ़ाइल अपलोड करें, उसके कॉलम जाँचें, और पंक्तियाँ संपादित करें।",
    ],
    "guides/submit": [
      "विज़ार्ड से रन शुरू करें",
      "सबमिट विज़ार्ड का हर चरण, टास्क से लेकर अनुबंध की जाँच तक।",
    ],
    "guides/results": [
      "परिणाम पढ़ें",
      "रन पेज के टैब, स्कोर, और विजेता को एक्सपोर्ट या सर्व करने का तरीका।",
    ],
    "guides/sharing": [
      "साझा करें और प्रकाशित करें",
      "रन की दृश्यता, शेयर लिंक, भूमिकाएँ, और एक्सप्लोर गैलरी।",
    ],
    "guides/tagger": [
      "टैगर से डेटा लेबल करें",
      "मैन्युअल, को-पायलट और ऑटोपायलट लेबलिंग, समीक्षा राउंड, और एक्सपोर्ट।",
    ],
    "guides/mac-app": ["Mac ऐप", "अपने कोडिंग एजेंट और Docker के साथ अपने Mac पर Skynet चलाएँ।"],
    "guides/agent-panel": [
      "एजेंट पैनल",
      "सहायक से रन सेट अप करने, शुरू करने और समझाने को कहें, और तय करें कि वह क्या कर सकता है।",
    ],
    mcp: [
      "MCP सर्वर",
      "Claude, ChatGPT, Codex, Cursor, VS Code और अन्य क्लाइंट को Skynet से जोड़ें।",
    ],
    api: ["API", "HTTP API से प्रमाणीकरण करें, रन शुरू करें और परिणाम पढ़ें।"],
    security: [
      "सुरक्षा",
      "आपका कोड कहाँ चलता है, क्रेडेंशियल कैसे रखे जाते हैं, और समस्या की रिपोर्ट कैसे करें।",
    ],
    limits: ["सीमाएँ", "रन, फ़ाइलों, डेटासेट, स्टोरेज और अनुरोध दरों की डिफ़ॉल्ट सीमाएँ।"],
    faq: ["अक्सर पूछे जाने वाले प्रश्न", "सामान्य प्रश्नों के छोटे उत्तर।"],
  },
  it: {
    "": ["Introduzione", "Che cos'è Skynet, che cosa ottimizza e da dove iniziare."],
    quickstart: [
      "Avvio rapido",
      "Avvia la tua prima esecuzione di ottimizzazione in circa dieci minuti.",
    ],
    "concepts/optimization": [
      "Come funziona l'ottimizzazione",
      "Il contratto del repository e dello scorer, le chiamate al modello dal tuo codice e i motori.",
    ],
    "concepts/engines": [
      "Motori",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve e Auto.",
    ],
    "concepts/datasets": [
      "Dataset e suddivisioni",
      "Come Skynet legge i tuoi casi e li suddivide in addestramento, convalida e test.",
    ],
    "concepts/scorers": [
      "Metriche e scorer",
      "Scrivi la funzione di punteggio che indica al motore che cosa significa «migliore».",
    ],
    "concepts/runs": [
      "Esecuzioni",
      "Gli stati di un'esecuzione, le azioni possibili e come si ferma.",
    ],
    "concepts/billing": [
      "Budget e fatturazione",
      "Saldo, budget di esecuzione, commissioni e percorsi dei modelli con la tua chiave (BYOK).",
    ],
    "guides/datasets": [
      "Preparare un dataset",
      "Carica un file CSV, JSON o Excel, controlla le colonne e modifica le righe.",
    ],
    "guides/submit": [
      "Avviare un'esecuzione con la procedura guidata",
      "Ogni passaggio della procedura guidata, dall'attività al controllo del contratto.",
    ],
    "guides/results": [
      "Leggere i risultati",
      "Le schede della pagina dell'esecuzione, i punteggi e come esportare o servire la versione vincente.",
    ],
    "guides/sharing": [
      "Condividere e pubblicare",
      "Visibilità delle esecuzioni, link di condivisione, ruoli e la galleria Esplora.",
    ],
    "guides/tagger": [
      "Etichettare i dati con il tagger",
      "Etichettatura manuale, Co-pilota e Pilota automatico, cicli di revisione ed esportazione.",
    ],
    "guides/mac-app": [
      "App per Mac",
      "Esegui Skynet sul tuo Mac con il tuo agente di programmazione e Docker.",
    ],
    "guides/agent-panel": [
      "Pannello dell'agente",
      "Chiedi all'assistente di configurare, avviare e spiegare le esecuzioni, e decidi che cosa può fare.",
    ],
    mcp: ["Server MCP", "Collega Claude, ChatGPT, Codex, Cursor, VS Code e altri client a Skynet."],
    api: ["API", "Autenticati, avvia un'esecuzione e leggi i risultati con l'API HTTP."],
    security: [
      "Sicurezza",
      "Dove viene eseguito il tuo codice, come vengono conservate le credenziali e come segnalare un problema.",
    ],
    limits: [
      "Limiti",
      "Limiti predefiniti per esecuzioni, file, dataset, archiviazione e frequenza delle richieste.",
    ],
    faq: ["Domande frequenti", "Risposte brevi alle domande più comuni."],
  },
  ja: {
    "": ["はじめに", "Skynet とは何か、何を最適化するか、どこから始めるか。"],
    quickstart: ["クイックスタート", "約 10 分で最初の最適化の実行を開始します。"],
    "concepts/optimization": [
      "最適化の仕組み",
      "リポジトリとスコアラーのコントラクト、コードからのモデル呼び出し、エンジン。",
    ],
    "concepts/engines": [
      "エンジン",
      "GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler、ShinkaEvolve、Auto。",
    ],
    "concepts/datasets": [
      "データセットと分割",
      "Skynet がケースを読み取り、学習・検証・テストに分割する方法。",
    ],
    "concepts/scorers": [
      "メトリクスとスコアラー",
      "何が「より良い」かをエンジンに伝えるスコア関数を書きます。",
    ],
    "concepts/runs": ["実行", "実行の状態、実行に対して行える操作、実行が停止する仕組み。"],
    "concepts/billing": ["予算と請求", "残高、実行予算、手数料、独自キー (BYOK) のモデルルート。"],
    "guides/datasets": [
      "データセットを準備する",
      "CSV、JSON、Excel ファイルをアップロードし、列を確認して行を編集します。",
    ],
    "guides/submit": [
      "ウィザードで実行を開始する",
      "タスクからコントラクトチェックまで、送信ウィザードの各ステップ。",
    ],
    "guides/results": [
      "結果を読む",
      "実行ページのタブ、スコア、最良の結果をエクスポートまたは提供する方法。",
    ],
    "guides/sharing": ["共有と公開", "実行の公開範囲、共有リンク、ロール、探索ギャラリー。"],
    "guides/tagger": [
      "タガーでデータにラベルを付ける",
      "手動、コパイロット、オートパイロットによるラベル付け、レビューラウンド、エクスポート。",
    ],
    "guides/mac-app": [
      "Mac アプリ",
      "自分のコーディングエージェントと Docker を使って Mac で Skynet を実行します。",
    ],
    "guides/agent-panel": [
      "エージェントパネル",
      "アシスタントに実行の設定・開始・説明を任せ、許可する操作を管理します。",
    ],
    mcp: [
      "MCP サーバー",
      "Claude、ChatGPT、Codex、Cursor、VS Code などのクライアントを Skynet に接続します。",
    ],
    api: ["API", "HTTP API で認証し、実行を開始し、結果を読み取ります。"],
    security: ["セキュリティ", "コードの実行場所、認証情報の保管方法、問題の報告方法。"],
    limits: ["制限", "実行、ファイル、データセット、ストレージ、リクエストレートの既定の制限。"],
    faq: ["よくある質問", "よくある質問への短い回答。"],
  },
  ko: {
    "": ["소개", "Skynet이 무엇인지, 무엇을 최적화하는지, 어디서 시작하는지 알아봅니다."],
    quickstart: ["빠른 시작", "약 10분 만에 첫 최적화 실행을 시작합니다."],
    "concepts/optimization": [
      "최적화의 작동 방식",
      "리포지토리와 채점기 계약, 코드에서의 모델 호출, 엔진.",
    ],
    "concepts/engines": [
      "엔진",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve, Auto.",
    ],
    "concepts/datasets": [
      "데이터셋과 분할",
      "Skynet이 케이스를 읽고 학습, 검증, 테스트로 나누는 방법.",
    ],
    "concepts/scorers": [
      "지표와 채점기",
      "엔진에 무엇이 더 나은지 알려 주는 점수 함수를 작성합니다.",
    ],
    "concepts/runs": ["실행", "실행 상태, 실행에서 할 수 있는 작업, 실행이 멈추는 방식."],
    "concepts/billing": ["예산과 결제", "잔액, 실행 예산, 수수료, 자체 키(BYOK) 모델 경로."],
    "guides/datasets": [
      "데이터셋 준비",
      "CSV, JSON 또는 Excel 파일을 업로드하고 열을 확인한 뒤 행을 편집합니다.",
    ],
    "guides/submit": ["마법사로 실행 시작", "작업부터 계약 검사까지 제출 마법사의 각 단계."],
    "guides/results": ["결과 읽기", "실행 페이지 탭, 점수, 최고 버전을 내보내거나 서빙하는 방법."],
    "guides/sharing": ["공유 및 게시", "실행 공개 범위, 공유 링크, 역할, 탐색 갤러리."],
    "guides/tagger": [
      "태거로 데이터 라벨링",
      "수동, 코파일럿, 오토파일럿 라벨링, 검토 라운드, 내보내기.",
    ],
    "guides/mac-app": ["Mac 앱", "내 코딩 에이전트와 Docker로 Mac에서 Skynet을 실행합니다."],
    "guides/agent-panel": [
      "에이전트 패널",
      "어시스턴트에게 실행 설정, 시작, 설명을 요청하고 허용할 작업을 정합니다.",
    ],
    mcp: [
      "MCP 서버",
      "Claude, ChatGPT, Codex, Cursor, VS Code 및 기타 클라이언트를 Skynet에 연결합니다.",
    ],
    api: ["API", "HTTP API로 인증하고, 실행을 시작하고, 결과를 읽습니다."],
    security: ["보안", "코드가 실행되는 위치, 자격 증명 보관 방식, 문제 신고 방법."],
    limits: ["제한", "실행, 파일, 데이터셋, 스토리지, 요청 속도의 기본 제한."],
    faq: ["자주 묻는 질문", "자주 묻는 질문에 대한 짧은 답변."],
  },
  pt: {
    "": ["Introdução", "O que é o Skynet, o que ele otimiza e por onde começar."],
    quickstart: [
      "Início rápido",
      "Inicie sua primeira execução de otimização em cerca de dez minutos.",
    ],
    "concepts/optimization": [
      "Como a otimização funciona",
      "O contrato do repositório e do avaliador, as chamadas de modelo a partir do seu código e os motores.",
    ],
    "concepts/engines": [
      "Motores",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve e Auto.",
    ],
    "concepts/datasets": [
      "Conjuntos de dados e divisões",
      "Como o Skynet lê seus casos e os divide em treino, validação e teste.",
    ],
    "concepts/scorers": [
      "Métricas e avaliadores",
      'Escreva a função de pontuação que diz ao motor o que significa "melhor".',
    ],
    "concepts/runs": [
      "Execuções",
      "Os estados de uma execução, as ações possíveis e como ela termina.",
    ],
    "concepts/billing": [
      "Orçamentos e cobrança",
      "Saldo, orçamentos de execução, taxas e rotas de modelo com sua própria chave (BYOK).",
    ],
    "guides/datasets": [
      "Preparar um conjunto de dados",
      "Envie um arquivo CSV, JSON ou Excel, verifique as colunas e edite linhas.",
    ],
    "guides/submit": [
      "Iniciar uma execução com o assistente",
      "Cada etapa do assistente de envio, da tarefa à verificação do contrato.",
    ],
    "guides/results": [
      "Ler os resultados",
      "As abas da página da execução, as pontuações e como exportar ou servir a versão vencedora.",
    ],
    "guides/sharing": [
      "Compartilhar e publicar",
      "Visibilidade das execuções, links de compartilhamento, funções e a galeria Explorar.",
    ],
    "guides/tagger": [
      "Rotular dados com o rotulador",
      "Rotulagem manual, Co-piloto e Piloto automático, rodadas de revisão e exportação.",
    ],
    "guides/mac-app": [
      "App para Mac",
      "Execute o Skynet no seu Mac com seu próprio agente de programação e o Docker.",
    ],
    "guides/agent-panel": [
      "Painel do agente",
      "Peça ao assistente para configurar, iniciar e explicar execuções, e controle o que ele pode fazer.",
    ],
    mcp: [
      "Servidor MCP",
      "Conecte Claude, ChatGPT, Codex, Cursor, VS Code e outros clientes ao Skynet.",
    ],
    api: ["API", "Autentique-se, inicie uma execução e leia os resultados com a API HTTP."],
    security: [
      "Segurança",
      "Onde seu código é executado, como as credenciais são guardadas e como relatar um problema.",
    ],
    limits: [
      "Limites",
      "Limites padrão para execuções, arquivos, conjuntos de dados, armazenamento e taxa de solicitações.",
    ],
    faq: ["Perguntas frequentes", "Respostas curtas para perguntas comuns."],
  },
  ru: {
    "": ["Введение", "Что такое Skynet, что он оптимизирует и с чего начать."],
    quickstart: ["Быстрый старт", "Запустите первую оптимизацию примерно за десять минут."],
    "concepts/optimization": [
      "Как работает оптимизация",
      "Контракт репозитория и оценщика, вызовы модели из вашего кода и движки.",
    ],
    "concepts/engines": [
      "Движки",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve и Auto.",
    ],
    "concepts/datasets": [
      "Наборы данных и разбиение",
      "Как Skynet читает ваши примеры и делит их на обучающую, валидационную и тестовую выборки.",
    ],
    "concepts/scorers": [
      "Метрики и оценщики",
      "Напишите функцию оценки, которая говорит движку, что значит «лучше».",
    ],
    "concepts/runs": [
      "Запуски",
      "Состояния запуска, доступные действия и то, как запуск останавливается.",
    ],
    "concepts/billing": [
      "Бюджеты и оплата",
      "Баланс, бюджеты запусков, комиссии и маршруты моделей с собственным ключом (BYOK).",
    ],
    "guides/datasets": [
      "Подготовка набора данных",
      "Загрузите файл CSV, JSON или Excel, проверьте столбцы и отредактируйте строки.",
    ],
    "guides/submit": [
      "Запуск через мастер",
      "Каждый шаг мастера отправки, от задачи до проверки контракта.",
    ],
    "guides/results": [
      "Чтение результатов",
      "Вкладки страницы запуска, оценки и как экспортировать или развернуть лучший вариант.",
    ],
    "guides/sharing": [
      "Общий доступ и публикация",
      "Видимость запусков, ссылки для доступа, роли и галерея «Обзор».",
    ],
    "guides/tagger": [
      "Разметка данных в разметчике",
      "Ручная разметка, «Второй пилот» и «Автопилот», раунды проверки и экспорт.",
    ],
    "guides/mac-app": [
      "Приложение для Mac",
      "Запускайте Skynet на своём Mac с вашим агентом для программирования и Docker.",
    ],
    "guides/agent-panel": [
      "Панель агента",
      "Попросите ассистента настроить, запустить и объяснить запуски, и задайте, что ему разрешено.",
    ],
    mcp: [
      "MCP-сервер",
      "Подключите Claude, ChatGPT, Codex, Cursor, VS Code и другие клиенты к Skynet.",
    ],
    api: ["API", "Аутентификация, запуск и чтение результатов через HTTP API."],
    security: [
      "Безопасность",
      "Где выполняется ваш код, как хранятся учётные данные и как сообщить о проблеме.",
    ],
    limits: [
      "Лимиты",
      "Лимиты по умолчанию для запусков, файлов, наборов данных, хранилища и частоты запросов.",
    ],
    faq: ["Частые вопросы", "Короткие ответы на частые вопросы."],
  },
  tr: {
    "": ["Giriş", "Skynet nedir, neyi optimize eder ve nereden başlanır."],
    quickstart: [
      "Hızlı başlangıç",
      "İlk optimizasyon çalıştırmanızı yaklaşık on dakikada başlatın.",
    ],
    "concepts/optimization": [
      "Optimizasyon nasıl çalışır",
      "Depo ve puanlayıcı sözleşmesi, kodunuzdan model çağrıları ve motorlar.",
    ],
    "concepts/engines": [
      "Motorlar",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve ve Auto.",
    ],
    "concepts/datasets": [
      "Veri kümeleri ve bölmeler",
      "Skynet'in örneklerinizi nasıl okuduğu ve eğitim, doğrulama ve test olarak nasıl böldüğü.",
    ],
    "concepts/scorers": [
      "Metrikler ve puanlayıcılar",
      "Motora neyin daha iyi olduğunu söyleyen puan fonksiyonunu yazın.",
    ],
    "concepts/runs": [
      "Çalıştırmalar",
      "Çalıştırma durumları, bir çalıştırmada yapabileceğiniz işlemler ve çalıştırmanın nasıl durduğu.",
    ],
    "concepts/billing": [
      "Bütçeler ve faturalandırma",
      "Bakiye, çalıştırma bütçeleri, ücretler ve kendi anahtarınızla (BYOK) model yolları.",
    ],
    "guides/datasets": [
      "Veri kümesi hazırlama",
      "Bir CSV, JSON veya Excel dosyası yükleyin, sütunlarını kontrol edin ve satırları düzenleyin.",
    ],
    "guides/submit": [
      "Sihirbazla çalıştırma başlatma",
      "Görevden sözleşme denetimine kadar gönderme sihirbazının her adımı.",
    ],
    "guides/results": [
      "Sonuçları okuma",
      "Çalıştırma sayfasının sekmeleri, puanlar ve kazananı dışa aktarma veya sunma yolu.",
    ],
    "guides/sharing": [
      "Paylaşma ve yayınlama",
      "Çalıştırma görünürlüğü, paylaşım bağlantıları, roller ve Keşfet galerisi.",
    ],
    "guides/tagger": [
      "Etiketleyiciyle veri etiketleme",
      "Manuel, Yardımcı pilot ve Otomatik pilot etiketleme, inceleme turları ve dışa aktarma.",
    ],
    "guides/mac-app": [
      "Mac uygulaması",
      "Skynet'i kendi kodlama aracınız ve Docker ile Mac'inizde çalıştırın.",
    ],
    "guides/agent-panel": [
      "Aracı paneli",
      "Asistandan çalıştırmaları kurmasını, başlatmasını ve açıklamasını isteyin; neler yapabileceğini belirleyin.",
    ],
    mcp: [
      "MCP sunucusu",
      "Claude, ChatGPT, Codex, Cursor, VS Code ve diğer istemcileri Skynet'e bağlayın.",
    ],
    api: ["API", "HTTP API ile kimlik doğrulayın, çalıştırma başlatın ve sonuçları okuyun."],
    security: [
      "Güvenlik",
      "Kodunuzun nerede çalıştığı, kimlik bilgilerinin nasıl saklandığı ve bir sorunun nasıl bildirileceği.",
    ],
    limits: [
      "Sınırlar",
      "Çalıştırmalar, dosyalar, veri kümeleri, depolama ve istek hızları için varsayılan sınırlar.",
    ],
    faq: ["SSS", "Sık sorulan sorulara kısa yanıtlar."],
  },
  uk: {
    "": ["Вступ", "Що таке Skynet, що він оптимізує і з чого почати."],
    quickstart: ["Швидкий старт", "Запустіть першу оптимізацію приблизно за десять хвилин."],
    "concepts/optimization": [
      "Як працює оптимізація",
      "Контракт репозиторію й оцінювача, виклики моделі з вашого коду та рушії.",
    ],
    "concepts/engines": [
      "Рушії",
      "GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler, ShinkaEvolve і Auto.",
    ],
    "concepts/datasets": [
      "Набори даних і розбиття",
      "Як Skynet читає ваші приклади й ділить їх на навчальну, валідаційну та тестову частини.",
    ],
    "concepts/scorers": [
      "Метрики й оцінювачі",
      "Напишіть функцію оцінювання, яка підказує рушію, що означає «краще».",
    ],
    "concepts/runs": [
      "Запуски",
      "Стани запуску, дії, які можна виконати із запуском, і як запуск зупиняється.",
    ],
    "concepts/billing": [
      "Бюджети й оплата",
      "Баланс, бюджети запусків, комісії та маршрути моделей із власним ключем (BYOK).",
    ],
    "guides/datasets": [
      "Підготовка набору даних",
      "Завантажте файл CSV, JSON або Excel, перевірте стовпці та відредагуйте рядки.",
    ],
    "guides/submit": [
      "Запуск через майстер",
      "Кожен крок майстра надсилання, від завдання до перевірки контракту.",
    ],
    "guides/results": [
      "Читання результатів",
      "Вкладки сторінки запуску, оцінки та як експортувати або розгорнути найкращий варіант.",
    ],
    "guides/sharing": [
      "Спільний доступ і публікація",
      "Видимість запусків, посилання для доступу, ролі та галерея «Огляд».",
    ],
    "guides/tagger": [
      "Розмітка даних у розмітнику",
      "Ручна розмітка, «Другий пілот» і «Автопілот», раунди перевірки та експорт.",
    ],
    "guides/mac-app": [
      "Застосунок для Mac",
      "Запускайте Skynet на своєму Mac із власним агентом для програмування та Docker.",
    ],
    "guides/agent-panel": [
      "Панель агента",
      "Попросіть асистента налаштувати, запустити й пояснити запуски, і визначте, що йому дозволено.",
    ],
    mcp: [
      "Сервер MCP",
      "Підключіть Claude, ChatGPT, Codex, Cursor, VS Code та інші клієнти до Skynet.",
    ],
    api: ["API", "Автентифікація, запуск і читання результатів через HTTP API."],
    security: [
      "Безпека",
      "Де виконується ваш код, як зберігаються облікові дані та як повідомити про проблему.",
    ],
    limits: [
      "Ліміти",
      "Типові ліміти для запусків, файлів, наборів даних, сховища та частоти запитів.",
    ],
    faq: ["Поширені запитання", "Короткі відповіді на поширені запитання."],
  },
  yue: {
    "": ["簡介", "Skynet 係乜嘢、會最佳化啲乜，同埋由邊度開始。"],
    quickstart: ["快速入門", "大約十分鐘開始你第一次最佳化運行。"],
    "concepts/optimization": [
      "最佳化點樣運作",
      "倉庫同評分器嘅合約、由你嘅代碼調用模型，同埋引擎。",
    ],
    "concepts/engines": [
      "引擎",
      "GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler、ShinkaEvolve 同 Auto。",
    ],
    "concepts/datasets": ["數據集同分割", "Skynet 點樣讀取你嘅案例，再分成訓練、驗證同測試。"],
    "concepts/scorers": ["指標同評分器", "寫一個評分函數，話畀引擎知乜嘢先算更好。"],
    "concepts/runs": ["運行", "運行狀態、你可以對運行做嘅操作，同埋運行點樣停止。"],
    "concepts/billing": ["預算同收費", "結餘、運行預算、費用，同埋用自己金鑰 (BYOK) 嘅模型路由。"],
    "guides/datasets": ["準備數據集", "上載 CSV、JSON 或者 Excel 檔案，檢查欄位，再編輯資料列。"],
    "guides/submit": ["用精靈開始運行", "提交精靈嘅每一步，由任務到合約檢查。"],
    "guides/results": ["睇結果", "運行頁面嘅分頁、分數，同埋點樣匯出或者提供最佳版本。"],
    "guides/sharing": ["分享同發佈", "運行嘅瀏覽權限、分享連結、角色，同埋探索圖庫。"],
    "guides/tagger": [
      "用標註工具標註數據",
      "手動、Co-pilot 同 Autopilot 標註、審核輪次，同埋匯出。",
    ],
    "guides/mac-app": ["Mac 應用程式", "喺你部 Mac 用你自己嘅編程 agent 同 Docker 運行 Skynet。"],
    "guides/agent-panel": ["Agent 面板", "叫助手設定、開始同解釋運行，再控制佢可以做啲乜。"],
    mcp: ["MCP 伺服器", "將 Claude、ChatGPT、Codex、Cursor、VS Code 同其他用戶端連接到 Skynet。"],
    api: ["API", "用 HTTP API 驗證身份、開始運行同讀取結果。"],
    security: ["安全", "你嘅程式碼喺邊度運行、憑證點樣保存，同埋點樣報告問題。"],
    limits: ["限制", "運行、檔案、數據集、儲存空間同請求速率嘅預設限制。"],
    faq: ["常見問題", "常見問題嘅簡短答案。"],
  },
  "zh-Hans": {
    "": ["简介", "Skynet 是什么、能优化什么，以及从哪里开始。"],
    quickstart: ["快速开始", "大约十分钟内启动你的第一次优化运行。"],
    "concepts/optimization": [
      "优化的工作原理",
      "仓库与评分器契约、从你的代码调用模型，以及引擎。",
    ],
    "concepts/engines": [
      "引擎",
      "GEPA、Best-of-N、Meta-Harness、AutoResearch、AutoSaddler、ShinkaEvolve 和 Auto。",
    ],
    "concepts/datasets": [
      "数据集与划分",
      "Skynet 如何读取你的用例，并将其划分为训练集、验证集和测试集。",
    ],
    "concepts/scorers": ["指标与评分器", "编写评分函数，告诉引擎什么才算更好。"],
    "concepts/runs": ["运行", "运行状态、可对运行执行的操作，以及运行如何停止。"],
    "concepts/billing": ["预算与计费", "余额、运行预算、费用，以及自带密钥 (BYOK) 的模型路由。"],
    "guides/datasets": ["准备数据集", "上传 CSV、JSON 或 Excel 文件，检查列，并编辑行。"],
    "guides/submit": ["使用向导启动运行", "提交向导的每一步，从任务到契约检查。"],
    "guides/results": ["查看结果", "运行页面的各个标签页、分数，以及如何导出或部署最佳版本。"],
    "guides/sharing": ["分享与发布", "运行可见性、分享链接、角色，以及探索画廊。"],
    "guides/tagger": [
      "使用标注工具标注数据",
      "手动、副驾和自动驾驶标注、审核轮次，以及导出。",
    ],
    "guides/mac-app": ["Mac 应用", "在你的 Mac 上使用你自己的编程智能体和 Docker 运行 Skynet。"],
    "guides/agent-panel": ["智能体面板", "让助手设置、启动和解释运行，并控制它可以做什么。"],
    mcp: ["MCP 服务器", "将 Claude、ChatGPT、Codex、Cursor、VS Code 和其他客户端连接到 Skynet。"],
    api: ["API", "通过 HTTP API 进行身份验证、启动运行并读取结果。"],
    security: ["安全", "你的代码在哪里运行、凭据如何保存，以及如何报告问题。"],
    limits: ["限制", "运行、文件、数据集、存储和请求速率的默认限制。"],
    faq: ["常见问题", "常见问题的简短解答。"],
  },
};

/** One localized field for every docs locale. */
function perLocale(pick: (locale: DocsLocale) => string): Record<DocsLocale, string> {
  return Object.fromEntries(DOCS_LOCALES.map((locale) => [locale, pick(locale)])) as Record<
    DocsLocale,
    string
  >;
}

export const DOCS_SECTIONS: Array<{ id: DocsSectionId; title: Record<DocsLocale, string> }> = (
  ["start", "concepts", "guides", "integrations", "reference"] as const
).map((id) => ({ id, title: perLocale((locale) => SECTION_TITLES[locale][id]) }));

const UPDATED = "2026-11-01";

export const DOCS_PAGES: DocsPage[] = (Object.keys(PAGE_SECTIONS) as DocsSlug[]).map((slug) => ({
  slug,
  section: PAGE_SECTIONS[slug],
  title: perLocale((locale) => PAGE_TEXT[locale][slug][0]),
  description: perLocale((locale) => PAGE_TEXT[locale][slug][1]),
  updated: UPDATED,
}));

/** Whether a string names a docs locale. */
export function isDocsLocale(value: string | undefined): value is DocsLocale {
  return (DOCS_LOCALES as readonly string[]).includes(value ?? "");
}

/** The text direction of a docs locale. */
export function docsDir(locale: DocsLocale): "rtl" | "ltr" {
  return RTL_LOCALES.has(locale) ? "rtl" : "ltr";
}

/**
 * Split a docs URL path into its locale and page slug.
 *
 * English pages have no prefix (/docs/quickstart); other locales start with
 * /docs/<locale>. Returns null when no page matches.
 */
export function resolveDocsPath(
  segments: readonly string[],
): { locale: DocsLocale; page: DocsPage } | null {
  const [first, ...rest] = segments;
  const prefixed = first !== "en" && isDocsLocale(first);
  const locale: DocsLocale = prefixed ? first : "en";
  const slug = (prefixed ? rest : segments).join("/");
  const page = DOCS_PAGES.find((p) => p.slug === slug);
  return page ? { locale, page } : null;
}

/** The site-relative URL of a page in a locale. */
export function docsHref(slug: string, locale: DocsLocale): string {
  const base = locale === "en" ? "/docs" : `/docs/${locale}`;
  return slug ? `${base}/${slug}` : base;
}

const LOCALE_PREFIXES = DOCS_LOCALES.filter((l) => l !== "en").join("|");
const UNLOCALIZED_DOCS_LINK = new RegExp(
  `\\]\\(\\/docs(?=[/)#])(?!\\/(?:${LOCALE_PREFIXES})(?=[/)#]))`,
  "g",
);

/**
 * Point the docs links in a page body at the page's locale. Every locale's
 * pages link to `/docs/...` like the English ones, so readers stay in their
 * language.
 */
export function localizeDocsLinks(markdown: string, locale: DocsLocale): string {
  if (locale === "en") return markdown;
  return markdown.replace(UNLOCALIZED_DOCS_LINK, `](/docs/${locale}`);
}

/** The page path segments from the Markdown twin route's catch-all param. */
export function docsMarkdownSegments(parts: readonly string[]): string[] {
  return parts.flatMap((part) => part.split("/")).filter(Boolean);
}

/** The site-relative URL of a page's Markdown twin. */
export function docsMarkdownHref(slug: string, locale: DocsLocale): string {
  return `${docsHref(slug, locale)}.md`;
}

/** The pages before and after a page in reading order. */
export function docsNeighbors(slug: string): { prev: DocsPage | null; next: DocsPage | null } {
  const i = DOCS_PAGES.findIndex((p) => p.slug === slug);
  if (i < 0) return { prev: null, next: null };
  return { prev: DOCS_PAGES[i - 1] ?? null, next: DOCS_PAGES[i + 1] ?? null };
}
