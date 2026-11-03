"""Deterministic starting state for the Skynet agent-benchmark world.

:func:`base_state` returns a fresh, fully deterministic state dict on every call
(no wall-clock reads, no randomness): "now" is fixed at ``2026-09-20T09:00:00Z``.
The state mirrors the real Skynet backend the agent tools act on -- jobs, the
dataset library and sample catalog, the model catalog, wallet, the
public-search corpus, blackbox engines, discovery endpoints, tagging sessions and
user preferences.

:func:`make_job` builds one job dict with sensible defaults; :func:`add_job`
stores one into a live world during a task's ``setup()``. Both are documented for
task authors in ``WORLD.md``.
"""

from __future__ import annotations

from typing import Any

NOW = "2026-09-20T09:00:00Z"
DANA = "dana"
NOA = "noa"
ADMIN = "admin"

REGISTRY_MODULES = ["cot", "flex", "predict", "react"]
REGISTRY_METRICS: list[str] = []
REGISTRY_OPTIMIZERS = ["gepa"]

GPT4O_MINI = "openrouter/openai/gpt-4o-mini"
GPT4O = "openrouter/openai/gpt-4o"
GPT5 = "openrouter/openai/gpt-5"
O3_MINI = "openrouter/openai/o3-mini"
CLAUDE_HAIKU = "openrouter/anthropic/claude-haiku-4.5"
CLAUDE_SONNET = "openrouter/anthropic/claude-sonnet-4.5"
CLAUDE_OPUS = "openrouter/anthropic/claude-opus-4.5"
GEMINI_PRO = "openrouter/google/gemini-2.5-pro"
GEMINI_FLASH = "openrouter/google/gemini-2.5-flash"
GROK4 = "openrouter/x-ai/grok-4"
DEEPSEEK = "openrouter/deepseek/deepseek-chat"
LLAMA_SCOUT = "openrouter/meta-llama/llama-4-scout"


def _oid(n: int) -> str:
    """Return a deterministic uuid-shaped optimization id for fixture slot ``n``."""
    return f"00000000-0000-4000-8000-{n:012d}"


# --- Sample dataset catalog (the real bundled Hebrew samples) --------------

SENTIMENT_ROWS = [
    {"text": "השירות היה מעולה והמשלוח הגיע מהר", "label": "חיובי"},
    {"text": "המוצר הגיע שבור ואף אחד לא ענה לי", "label": "שלילי"},
    {"text": "הזמנתי ועדיין מחכה, בסדר גמור בינתיים", "label": "נייטרלי"},
    {"text": "פשוט מושלם, אקנה שוב בלי היסוס", "label": "חיובי"},
    {"text": "אכזבה גדולה, לא ממליץ לאף אחד", "label": "שלילי"},
    {"text": "האריזה תקינה והמחיר סביר", "label": "נייטרלי"},
    {"text": "צוות התמיכה עזר לי מיד, כל הכבוד", "label": "חיובי"},
    {"text": "חיכיתי שעה בטלפון ולא נעניתי", "label": "שלילי"},
    {"text": "קיבלתי את ההזמנה, הכול לפי התיאור", "label": "נייטרלי"},
    {"text": "איכות יוצאת דופן, שווה כל שקל", "label": "חיובי"},
    {"text": "נשלח מוצר שגוי ולא הוחזר לי הכסף", "label": "שלילי"},
    {"text": "זמן אספקה סטנדרטי, אין תלונות מיוחדות", "label": "נייטרלי"},
]

EMAIL_ROWS = [
    {
        "subject": "בעיה בהתחברות",
        "body": "אני לא מצליח להיכנס לחשבון שלי",
        "category": "תמיכה",
    },
    {"subject": "חיוב כפול", "body": "חויבתי פעמיים על אותה הזמנה", "category": "חיוב"},
    {
        "subject": "הצעת מחיר",
        "body": "אשמח לקבל הצעת מחיר לחבילה העסקית",
        "category": "מכירות",
    },
    {
        "subject": "האפליקציה קורסת",
        "body": "האפליקציה נסגרת בכל פעם שאני פותח אותה",
        "category": "תמיכה",
    },
    {"subject": "החזר כספי", "body": "מתי אקבל את ההחזר על הביטול", "category": "חיוב"},
    {
        "subject": "שדרוג מנוי",
        "body": "אני מעוניין לשדרג לתוכנית הפרימיום",
        "category": "מכירות",
    },
    {
        "subject": "איפוס סיסמה",
        "body": "לא קיבלתי את מייל איפוס הסיסמה",
        "category": "תמיכה",
    },
    {
        "subject": "חשבונית מס",
        "body": "אפשר לקבל חשבונית על התשלום האחרון",
        "category": "חיוב",
    },
    {
        "subject": "רישוי לצוות",
        "body": "כמה עולה רישיון לעשרה משתמשים",
        "category": "מכירות",
    },
    {
        "subject": "שגיאת מערכת",
        "body": "אני מקבל שגיאה 500 כשאני שומר",
        "category": "תמיכה",
    },
]

QA_ROWS = [
    {"question": "מהי בירת צרפת?", "answer": "פריז"},
    {"question": "כמה יבשות יש בעולם?", "answer": "שבע"},
    {"question": "מי כתב את המחזה המלט?", "answer": "שייקספיר"},
    {"question": "מהו היסוד הכימי O?", "answer": "חמצן"},
    {"question": "באיזו שנה נחתו על הירח?", "answer": "1969"},
    {"question": "מהי החיה הגדולה ביותר?", "answer": "לווייתן כחול"},
    {"question": "כמה צבעים יש בקשת?", "answer": "שבעה"},
    {"question": "מהי בירת יפן?", "answer": "טוקיו"},
    {"question": "מיהו מחבר תורת היחסות?", "answer": "איינשטיין"},
    {"question": "מהו הנהר הארוך בעולם?", "answer": "הנילוס"},
    {"question": "כמה רגליים יש לעכביש?", "answer": "שמונה"},
    {"question": "מהי בירת איטליה?", "answer": "רומא"},
]

_SENTIMENT_SIGNATURE = '''import dspy


class ClassifySentiment(dspy.Signature):
    """סווג ביקורת לקוח לחיובי, שלילי או נייטרלי."""

    text: str = dspy.InputField(desc="טקסט הביקורת")
    label: str = dspy.OutputField(desc="חיובי / שלילי / נייטרלי")
'''

_SENTIMENT_METRIC = '''def metric(example, prediction, trace=None):
    """החזר 1.0 אם התווית שהתקבלה תואמת לתווית הצפויה."""
    expected = str(example.label).strip().lower()
    got = str(getattr(prediction, "label", "")).strip().lower()
    return 1.0 if got == expected else 0.0
'''

_EMAIL_SIGNATURE = '''import dspy


class TriageEmail(dspy.Signature):
    """מיין פנייה בדוא"ל לתמיכה, חיוב או מכירות."""

    subject: str = dspy.InputField(desc="נושא הפנייה")
    body: str = dspy.InputField(desc="גוף הפנייה")
    category: str = dspy.OutputField(desc="תמיכה / חיוב / מכירות")
'''

_EMAIL_METRIC = '''def metric(example, prediction, trace=None):
    """החזר 1.0 כאשר הקטגוריה שהתקבלה תואמת."""
    expected = str(example.category).strip()
    got = str(getattr(prediction, "category", "")).strip()
    return 1.0 if got == expected else 0.0
'''

_QA_SIGNATURE = '''import dspy


class AnswerQuestion(dspy.Signature):
    """ענה בקצרה על שאלת טריוויה בעברית."""

    question: str = dspy.InputField(desc="השאלה")
    answer: str = dspy.OutputField(desc="תשובה קצרה")
'''

_QA_METRIC = '''def metric(example, prediction, trace=None):
    """החזר 1.0 כאשר התשובה הצפויה מוכלת בתשובה שהתקבלה."""
    expected = str(example.answer).strip().lower()
    got = str(getattr(prediction, "answer", "")).strip().lower()
    return 1.0 if expected and expected in got else 0.0
'''


def _samples() -> list[dict[str, Any]]:
    """Return the three bundled Hebrew sample datasets, full rows included."""
    return [
        {
            "sample_id": "sentiment-he",
            "name": "ניתוח רגש בעברית",
            "description": "סיווג ביקורות לקוחות לחיובי, שלילי או נייטרלי. 12 דוגמאות.",
            "task_type": "classification",
            "dataset_filename": "sentiment-he.csv",
            "input_columns": ["text"],
            "output_columns": ["label"],
            "signature_code": _SENTIMENT_SIGNATURE,
            "metric_code": _SENTIMENT_METRIC,
            "rows": [dict(r) for r in SENTIMENT_ROWS],
        },
        {
            "sample_id": "email-triage-he",
            "name": "מיון פניות בדוא״ל",
            "description": "סיווג פניות דוא״ל לתמיכה, חיוב או מכירות. 10 דוגמאות.",
            "task_type": "classification",
            "dataset_filename": "email-triage-he.csv",
            "input_columns": ["subject", "body"],
            "output_columns": ["category"],
            "signature_code": _EMAIL_SIGNATURE,
            "metric_code": _EMAIL_METRIC,
            "rows": [dict(r) for r in EMAIL_ROWS],
        },
        {
            "sample_id": "qa-general-he",
            "name": "שאלות ותשובות כלליות",
            "description": "מענה לשאלות טריוויה קצרות בעברית. 12 דוגמאות.",
            "task_type": "qa",
            "dataset_filename": "qa-general-he.csv",
            "input_columns": ["question"],
            "output_columns": ["answer"],
            "signature_code": _QA_SIGNATURE,
            "metric_code": _QA_METRIC,
            "rows": [dict(r) for r in QA_ROWS],
        },
    ]


# --- Dataset library -------------------------------------------------------


def _datasets() -> list[dict[str, Any]]:
    """Return the dataset-library entries visible to the user (dana + one shared)."""
    return [
        {
            "id": "ds_support_tickets",
            "name": "support-tickets",
            "source": "upload",
            "row_count": 120,
            "column_count": 3,
            "byte_size": 48210,
            "content_hash": "sha256:1a2b3c4d",
            "owner_username": DANA,
            "created_at": "2026-08-09T08:30:00Z",
            "updated_at": "2026-08-09T08:30:00Z",
        },
        {
            "id": "ds_reviews_he",
            "name": "ביקורות לקוחות",
            "source": "upload",
            "row_count": 200,
            "column_count": 2,
            "byte_size": 61040,
            "content_hash": "sha256:5e6f7a8b",
            "owner_username": DANA,
            "created_at": "2026-08-13T10:00:00Z",
            "updated_at": "2026-08-14T09:00:00Z",
        },
        {
            "id": "ds_email_triage",
            "name": "email-triage",
            "source": "upload",
            "row_count": 90,
            "column_count": 3,
            "byte_size": 33110,
            "content_hash": "sha256:9c0d1e2f",
            "owner_username": DANA,
            "created_at": "2026-08-17T14:20:00Z",
            "updated_at": "2026-08-17T14:20:00Z",
        },
        {
            "id": "ds_tiny_eval",
            "name": "tiny-eval",
            "source": "upload",
            "row_count": 8,
            "column_count": 2,
            "byte_size": 2040,
            "content_hash": "sha256:aa11bb22",
            "owner_username": DANA,
            "created_at": "2026-09-01T09:00:00Z",
            "updated_at": "2026-09-01T09:00:00Z",
        },
        {
            "id": "ds_noa_catalog",
            "name": "noa-product-catalog",
            "source": "upload",
            "row_count": 150,
            "column_count": 4,
            "byte_size": 72500,
            "content_hash": "sha256:cc33dd44",
            "owner_username": NOA,
            "created_at": "2026-08-28T11:00:00Z",
            "updated_at": "2026-08-28T11:00:00Z",
            "shared_with": {DANA: "viewer"},
        },
    ]


# --- Model catalog ---------------------------------------------------------


def _models() -> list[dict[str, Any]]:
    """Return the internal model catalog (agent view drops unavailable rows)."""
    return [
        {
            "name": GPT4O_MINI,
            "provider": "openrouter",
            "supports_thinking": False,
            "supports_vision": True,
            "max_input_tokens": 128000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": GPT4O,
            "provider": "openrouter",
            "supports_thinking": False,
            "supports_vision": True,
            "max_input_tokens": 128000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": GPT5,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 400000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": O3_MINI,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": False,
            "max_input_tokens": 200000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": CLAUDE_HAIKU,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 200000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": CLAUDE_SONNET,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 1000000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": CLAUDE_OPUS,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 200000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": GEMINI_PRO,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 1048576,
            "available": True,
            "needs_key": False,
        },
        {
            "name": GEMINI_FLASH,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": True,
            "max_input_tokens": 1048576,
            "available": True,
            "needs_key": False,
        },
        {
            "name": GROK4,
            "provider": "openrouter",
            "supports_thinking": True,
            "supports_vision": False,
            "max_input_tokens": 256000,
            "available": True,
            "needs_key": False,
        },
        {
            "name": DEEPSEEK,
            "provider": "openrouter",
            "supports_thinking": False,
            "supports_vision": False,
            "max_input_tokens": 163840,
            "available": True,
            "needs_key": False,
        },
        {
            "name": LLAMA_SCOUT,
            "provider": "openrouter",
            "supports_thinking": False,
            "supports_vision": True,
            "max_input_tokens": 1310720,
            "available": False,
            "needs_key": True,
            "unavailable_reason": "No provider key configured for meta-llama.",
        },
    ]


# --- Discovery endpoints (for discover_models) -----------------------------


def _endpoints() -> dict[str, dict[str, Any]]:
    """Return probe endpoints keyed by normalized base_url."""
    return {
        "https://api.openai.com/v1": {
            "models": ["gpt-4o", "gpt-4o-mini", "gpt-5", "o3", "o3-mini"],
            "needs_key": True,
            "error": None,
        },
        "https://litellm.internal:4000": {
            "models": [GPT4O_MINI, CLAUDE_HAIKU, GEMINI_FLASH],
            "needs_key": False,
            "error": None,
        },
        "https://broken.example.com": {
            "models": [],
            "needs_key": False,
            "error": "HTTP 502",
        },
    }


# --- Wallet ----------------------------------------------------------------


def _wallet() -> dict[str, Any]:
    """Return the wallet: paid balance + free grant + a self-consistent ledger.

    Invariant: ``paid_balance_cents + free_grant.cents_remaining ==`` the sum
    of all ledger ``credits`` deltas (2000 + 180 == 2180).
    """
    ledger = [
        {
            "id": "led_0001",
            "at": "2026-08-09T09:00:00Z",
            "label": "מענק הצטרפות חד-פעמי",
            "model": None,
            "cents": 500,
            "kind": "grant",
        },
        {
            "id": "led_0002",
            "at": "2026-08-09T10:00:00Z",
            "label": "Top-up",
            "model": None,
            "cents": 1500,
            "kind": "topup",
        },
        {
            "id": "led_0003",
            "at": "2026-08-10T12:30:00Z",
            "label": "support-tickets v2",
            "model": GPT4O_MINI,
            "cents": -80,
            "kind": "run",
        },
        {
            "id": "led_0004",
            "at": "2026-08-11T09:45:00Z",
            "label": "support-tickets v2 (copy)",
            "model": GPT4O_MINI,
            "cents": -60,
            "kind": "run",
        },
        {
            "id": "led_0005",
            "at": "2026-08-14T16:20:00Z",
            "label": "ניתוח רגש בעברית",
            "model": CLAUDE_HAIKU,
            "cents": -50,
            "kind": "run",
        },
        {
            "id": "led_0006",
            "at": "2026-08-26T11:10:00Z",
            "label": "regression-risk run",
            "model": GPT4O_MINI,
            "cents": -40,
            "kind": "run",
        },
        {
            "id": "led_0007",
            "at": "2026-08-30T13:00:00Z",
            "label": "grid: reasoning effort",
            "model": GPT4O,
            "cents": -40,
            "kind": "run",
        },
        {
            "id": "led_0008",
            "at": "2026-09-02T10:05:00Z",
            "label": "prompt-opt: cold email",
            "model": CLAUDE_SONNET,
            "cents": -30,
            "kind": "run",
        },
        {
            "id": "led_0009",
            "at": "2026-09-08T15:30:00Z",
            "label": "Top-up",
            "model": None,
            "cents": 500,
            "kind": "topup",
        },
        {
            "id": "led_0010",
            "at": "2026-09-19T08:15:00Z",
            "label": "live sentiment sweep",
            "model": GPT4O_MINI,
            "cents": -20,
            "kind": "run",
        },
    ]
    return {
        "paid_balance_cents": 2000,
        "free_grant": {"cents_remaining": 180, "cents_total": 500},
        "ledger": ledger,
    }


# --- Public search corpus --------------------------------------------------


def _search_corpus() -> list[dict[str, Any]]:
    """Return ~14 public jobs for lexical dashboard search (plus 2 private)."""
    corpus = [
        {
            "optimization_id": "pub_0001",
            "name": "support ticket classifier",
            "task_name": "support ticket triage",
            "module_name": "cot",
            "optimizer_name": "gepa",
            "winning_model": GPT4O_MINI,
            "optimization_type": "run",
            "baseline_metric": 0.61,
            "optimized_metric": 0.82,
            "summary_text": "Classify support tickets into billing, technical and account buckets.",
            "description": "A chain-of-thought classifier for support tickets.",
            "created_at": "2026-08-05T09:00:00Z",
            "is_private": False,
            "owner_username": "maya",
        },
        {
            "optimization_id": "pub_0002",
            "name": "sentiment analysis hebrew",
            "task_name": "hebrew sentiment",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": CLAUDE_HAIKU,
            "optimization_type": "run",
            "baseline_metric": 0.55,
            "optimized_metric": 0.78,
            "summary_text": "Hebrew customer-review sentiment into positive, negative, neutral.",
            "description": "Sentiment classification over Hebrew reviews.",
            "created_at": "2026-08-08T09:00:00Z",
            "is_private": False,
            "owner_username": "maya",
        },
        {
            "optimization_id": "pub_0003",
            "name": "email triage router",
            "task_name": "email routing",
            "module_name": "cot",
            "optimizer_name": "gepa",
            "winning_model": GPT4O,
            "optimization_type": "run",
            "baseline_metric": 0.60,
            "optimized_metric": 0.80,
            "summary_text": "Route inbound email to support, billing or sales queues.",
            "description": "Email triage with chain-of-thought.",
            "created_at": "2026-08-12T09:00:00Z",
            "is_private": False,
            "owner_username": "noa",
        },
        {
            "optimization_id": "pub_0004",
            "name": "question answering trivia",
            "task_name": "trivia qa",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": GEMINI_FLASH,
            "optimization_type": "run",
            "baseline_metric": 0.48,
            "optimized_metric": 0.69,
            "summary_text": "Short-answer trivia question answering.",
            "description": "General knowledge question answering.",
            "created_at": "2026-08-15T09:00:00Z",
            "is_private": False,
            "owner_username": "ravid",
        },
        {
            "optimization_id": "pub_0005",
            "name": "document summarization",
            "task_name": "summarization",
            "module_name": "cot",
            "optimizer_name": "dspy.teleprompt.MIPROv2",
            "winning_model": CLAUDE_SONNET,
            "optimization_type": "run",
            "baseline_metric": 0.52,
            "optimized_metric": 0.71,
            "summary_text": "Abstractive summarization of long support threads.",
            "description": "Summarize multi-turn threads.",
            "created_at": "2026-08-18T09:00:00Z",
            "is_private": False,
            "owner_username": "ravid",
        },
        {
            "optimization_id": "pub_0006",
            "name": "named entity extraction",
            "task_name": "entity extraction",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": GPT4O_MINI,
            "optimization_type": "run",
            "baseline_metric": 0.64,
            "optimized_metric": 0.83,
            "summary_text": "Extract product and org names from reviews.",
            "description": "NER over review text.",
            "created_at": "2026-08-20T09:00:00Z",
            "is_private": False,
            "owner_username": "maya",
        },
        {
            "optimization_id": "pub_0007",
            "name": "intent detection grid",
            "task_name": "intent detection",
            "module_name": "cot",
            "optimizer_name": "gepa",
            "winning_model": CLAUDE_HAIKU,
            "optimization_type": "grid_search",
            "baseline_metric": 0.58,
            "optimized_metric": 0.79,
            "summary_text": "Grid search over generation and reflection models for intent detection.",
            "description": "Intent classification bake-off.",
            "created_at": "2026-08-23T09:00:00Z",
            "is_private": False,
            "owner_username": "noa",
        },
        {
            "optimization_id": "pub_0008",
            "name": "cold email prompt",
            "task_name": "prompt optimization",
            "module_name": "blackbox",
            "optimizer_name": "gepa",
            "winning_model": CLAUDE_SONNET,
            "optimization_type": "blackbox",
            "baseline_metric": 0.40,
            "optimized_metric": 0.72,
            "summary_text": "Blackbox optimization of a cold-email opener prompt.",
            "description": "Blackbox prompt tuning for outreach email.",
            "created_at": "2026-08-26T09:00:00Z",
            "is_private": False,
            "owner_username": "ravid",
        },
        {
            "optimization_id": "pub_0009",
            "name": "toxicity filter",
            "task_name": "content moderation",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": GPT4O_MINI,
            "optimization_type": "run",
            "baseline_metric": 0.70,
            "optimized_metric": 0.88,
            "summary_text": "Flag toxic comments for moderation review.",
            "description": "Toxicity classification.",
            "created_at": "2026-08-29T09:00:00Z",
            "is_private": False,
            "owner_username": "maya",
        },
        {
            "optimization_id": "pub_0010",
            "name": "product recommendation ranker",
            "task_name": "recommendation",
            "module_name": "cot",
            "optimizer_name": "dspy.teleprompt.BootstrapFewShot",
            "winning_model": GEMINI_PRO,
            "optimization_type": "run",
            "baseline_metric": 0.51,
            "optimized_metric": 0.66,
            "summary_text": "Rank products for a shopping query.",
            "description": "Recommendation ranking.",
            "created_at": "2026-09-01T09:00:00Z",
            "is_private": False,
            "owner_username": "ravid",
        },
        {
            "optimization_id": "pub_0011",
            "name": "language detection",
            "task_name": "language id",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": DEEPSEEK,
            "optimization_type": "run",
            "baseline_metric": 0.82,
            "optimized_metric": 0.94,
            "summary_text": "Detect the language of a short message.",
            "description": "Language identification.",
            "created_at": "2026-09-04T09:00:00Z",
            "is_private": False,
            "owner_username": "noa",
        },
        {
            "optimization_id": "pub_0012",
            "name": "invoice field extraction",
            "task_name": "document extraction",
            "module_name": "cot",
            "optimizer_name": "gepa",
            "winning_model": GPT4O,
            "optimization_type": "run",
            "baseline_metric": 0.59,
            "optimized_metric": 0.77,
            "summary_text": "Extract totals and dates from invoice text.",
            "description": "Invoice field extraction.",
            "created_at": "2026-09-07T09:00:00Z",
            "is_private": False,
            "owner_username": "maya",
        },
        {
            "optimization_id": "pub_0013",
            "name": "chatbot faq matcher",
            "task_name": "faq matching",
            "module_name": "react",
            "optimizer_name": "gepa",
            "winning_model": CLAUDE_HAIKU,
            "optimization_type": "run",
            "baseline_metric": 0.63,
            "optimized_metric": 0.81,
            "summary_text": "Match user questions to FAQ entries with tool use.",
            "description": "ReAct FAQ matcher.",
            "created_at": "2026-09-10T09:00:00Z",
            "is_private": False,
            "owner_username": "ravid",
        },
        {
            "optimization_id": "pub_0014",
            "name": "spam classifier",
            "task_name": "spam detection",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": GPT4O_MINI,
            "optimization_type": "run",
            "baseline_metric": 0.75,
            "optimized_metric": 0.91,
            "summary_text": "Classify messages as spam or ham.",
            "description": "Spam detection classifier.",
            "created_at": "2026-09-13T09:00:00Z",
            "is_private": False,
            "owner_username": "noa",
        },
        {
            "optimization_id": "pub_0015",
            "name": "dana private sentiment",
            "task_name": "hebrew sentiment",
            "module_name": "predict",
            "optimizer_name": "gepa",
            "winning_model": CLAUDE_HAIKU,
            "optimization_type": "run",
            "baseline_metric": 0.55,
            "optimized_metric": 0.74,
            "summary_text": "Private Hebrew sentiment run owned by dana.",
            "description": "Private run, excluded from public search.",
            "created_at": "2026-09-14T09:00:00Z",
            "is_private": True,
            "owner_username": DANA,
        },
        {
            "optimization_id": "pub_0016",
            "name": "noa private extraction",
            "task_name": "document extraction",
            "module_name": "cot",
            "optimizer_name": "gepa",
            "winning_model": GPT4O,
            "optimization_type": "run",
            "baseline_metric": 0.60,
            "optimized_metric": 0.78,
            "summary_text": "Private extraction run owned by noa.",
            "description": "Private run, excluded from public search.",
            "created_at": "2026-09-15T09:00:00Z",
            "is_private": True,
            "owner_username": NOA,
        },
    ]
    return corpus


# --- Blackbox engines ------------------------------------------------------


def _blackbox() -> dict[str, Any]:
    """Return the blackbox engine catalog; AutoSaddler is the one unavailable engine."""
    engines = [
        {
            "id": "gepa",
            "label": "GEPA",
            "description": "Reflective evolution with a Pareto front of versions.",
            "available": True,
            "unavailable_reason": None,
            "requires_agent_target": False,
            "supports_parts": True,
            "checkpoint_recovery_supported": True,
            "checkpoint_recovery_reason": None,
        },
        {
            "id": "best_of_n",
            "label": "Best-of-N",
            "description": "Independent proposals from the reflection model; keep the best.",
            "available": True,
            "unavailable_reason": None,
            "requires_agent_target": False,
            "supports_parts": False,
            "checkpoint_recovery_supported": False,
            "checkpoint_recovery_reason": "This pinned engine does not expose a compatible checkpoint restore contract.",
        },
        {
            "id": "autoresearch",
            "label": "AutoResearch",
            "description": "A coding agent runs research rounds over a Pareto frontier of versions.",
            "available": True,
            "unavailable_reason": None,
            "requires_agent_target": False,
            "supports_parts": False,
            "checkpoint_recovery_supported": False,
            "checkpoint_recovery_reason": "This pinned engine does not expose a compatible checkpoint restore contract.",
        },
        {
            "id": "meta_harness",
            "label": "Meta-Harness",
            "description": "A coding-agent proposer searches harness code using candidate and evaluation history.",
            "available": True,
            "unavailable_reason": None,
            "requires_agent_target": False,
            "supports_parts": False,
            "checkpoint_recovery_supported": False,
            "checkpoint_recovery_reason": "This pinned engine does not expose a compatible checkpoint restore contract.",
        },
        {
            "id": "autosaddler",
            "label": "AutoSaddler",
            "description": "A coding agent diagnoses training failures and patches the version; only patches confirmed on held-out development cases are kept.",
            "available": False,
            "unavailable_reason": "AutoSaddler is not enabled on this deployment.",
            "requires_agent_target": False,
            "supports_parts": True,
            "checkpoint_recovery_supported": False,
            "checkpoint_recovery_reason": "This pinned engine does not expose a compatible checkpoint restore contract.",
        },
    ]
    return {
        "sandbox_available": True,
        "sandbox_reason": None,
        "auto_engines": ["gepa", "autoresearch", "meta_harness"],
        "auto_available": True,
        "auto_unavailable_reason": None,
        "auto_checkpoint_recovery_supported": False,
        "auto_checkpoint_recovery_reason": "The Auto recipe cannot restore its multi-engine search from one checkpoint.",
        "proposer_runtimes": [
            {
                "id": "vercel",
                "label": "Vercel Sandbox",
                "available": True,
                "unavailable_reason": None,
            }
        ],
        "upstream_revision": "gepa-2026.08",
        "engines": engines,
    }


# --- Tagging sessions ------------------------------------------------------


def _tagging_sessions() -> list[dict[str, Any]]:
    """Return the tagging sessions (two owned by dana, one shared in by noa)."""
    return [
        {
            "id": "tag_0001",
            "name": "support-tickets labeling",
            "phase": "annotating",
            "row_count": 120,
            "tagged_count": 84,
            "pinned": True,
            "created_at": "2026-08-09T08:00:00Z",
            "updated_at": "2026-09-18T12:00:00Z",
            "mode": "auto",
            "source_name": "support-tickets.csv",
            "owner_username": DANA,
        },
        {
            "id": "tag_0002",
            "name": "sentiment gold set",
            "phase": "review",
            "row_count": 200,
            "tagged_count": 200,
            "pinned": False,
            "created_at": "2026-08-13T09:00:00Z",
            "updated_at": "2026-08-30T16:00:00Z",
            "mode": "manual",
            "source_name": "ביקורות-לקוחות.csv",
            "owner_username": DANA,
        },
        {
            "id": "tag_0003",
            "name": "noa triage set",
            "phase": "annotating",
            "row_count": 90,
            "tagged_count": 30,
            "pinned": False,
            "created_at": "2026-08-28T09:00:00Z",
            "updated_at": "2026-09-05T10:00:00Z",
            "mode": "auto",
            "source_name": "noa-triage.csv",
            "owner_username": NOA,
            "shared_with": {DANA: "viewer"},
        },
    ]


# --- Per-example test results & results ------------------------------------


def _binary_examples(
    n: int,
    base_correct: int,
    opt_correct: int,
    start_index: int,
    gold_a: str,
    gold_b: str,
) -> tuple[list[dict], list[dict]]:
    """Build (baseline, optimized) per-example test records with global indices.

    The first ``base_correct``/``opt_correct`` examples score 1.0; the rest 0.0,
    so the split means equal ``base_correct/n`` and ``opt_correct/n``.
    """
    baseline, optimized = [], []
    for i in range(n):
        gold = gold_a if i % 2 == 0 else gold_b
        other = gold_b if gold == gold_a else gold_a
        idx = start_index + i
        text = f"דוגמה מספר {idx}"
        base_ok = i < base_correct
        opt_ok = i < opt_correct
        baseline.append(
            {
                "index": idx,
                "inputs": {"text": text},
                "expected": gold,
                "prediction": gold if base_ok else other,
                "score": 1.0 if base_ok else 0.0,
            }
        )
        optimized.append(
            {
                "index": idx,
                "inputs": {"text": text},
                "expected": gold,
                "prediction": gold if opt_ok else other,
                "score": 1.0 if opt_ok else 0.0,
            }
        )
    return baseline, optimized


def _run_result(
    job: dict[str, Any],
    base_ex: list[dict],
    opt_ex: list[dict],
    runtime: float,
    tokens: int,
) -> dict[str, Any]:
    """Assemble a single-run RunResponse-shaped result dict for a success job."""
    model = job.get("model_name") or GPT4O_MINI
    return {
        "module_name": job["module_name"],
        "optimizer_name": job["optimizer_name"],
        "metric_name": job.get("metric_name", "accuracy"),
        "split_counts": {"train": 60, "val": 20, "test": len(opt_ex)},
        "baseline_test_metric": job["baseline_test_metric"],
        "optimized_test_metric": job["optimized_test_metric"],
        "metric_improvement": job["metric_improvement"],
        "optimization_metadata": {},
        "details": {},
        "program_artifact_path": None,
        "program_artifact": {
            "optimized_prompt": {
                "input_fields": list(job["column_mapping"]["inputs"].keys()),
                "output_fields": list(job["column_mapping"]["outputs"].keys()),
                "instructions": "סווג את הקלט לפי התווית הנכונה, בהתבסס על הדוגמאות.",
                "demos": [dict(base_ex[0])] if base_ex else [],
            }
        },
        "runtime_seconds": runtime,
        "num_lm_calls": tokens // 400,
        "total_tokens": tokens,
        "usage_by_model": [
            {
                "model": model,
                "input_tokens": int(tokens * 0.7),
                "output_tokens": int(tokens * 0.3),
            }
        ],
        "avg_response_time_ms": 820.0,
        "lm_activity": None,
        "run_log": [],
        "baseline_test_results": base_ex,
        "optimized_test_results": opt_ex,
        "baseline_logged_metrics": {"accuracy": job["baseline_test_metric"]},
        "optimized_logged_metrics": {"accuracy": job["optimized_test_metric"]},
    }


def _serve_block(job: dict[str, Any], demos: list[dict]) -> dict[str, Any]:
    """Build the serve-info block a served success job exposes."""
    inputs = list(job["column_mapping"]["inputs"].keys())
    outputs = list(job["column_mapping"]["outputs"].keys())
    sample = {inputs[0]: "השירות היה מצוין ומהיר"} if inputs else {}
    return {
        "input_fields": inputs,
        "output_fields": outputs,
        "instructions": "סווג את הקלט לפי התווית הנכונה, בהתבסס על הדוגמאות.",
        "demo_count": len(demos),
        "sample_inputs": sample,
        "model_name": job.get("model_name") or GPT4O_MINI,
    }


def _log(
    ts: str,
    level: str,
    message: str,
    logger: str = "worker",
    pair_index: int | None = None,
) -> dict[str, Any]:
    """Build one JobLogEntry."""
    return {
        "timestamp": ts,
        "level": level,
        "logger": logger,
        "message": message,
        "pair_index": pair_index,
    }


# --- Job factory -----------------------------------------------------------

_JOB_DEFAULTS: dict[str, Any] = {
    "optimization_type": "run",
    "composition": "single",
    "status": "success",
    "message": None,
    "stop_reason": None,
    "result_availability": None,
    "terminal_evidence": None,
    "execution_budget": None,
    "recovery": None,
    "name": None,
    "description": None,
    "pinned": False,
    "created_at": NOW,
    "started_at": None,
    "completed_at": None,
    "elapsed": None,
    "elapsed_seconds": None,
    "estimated_remaining": None,
    "username": DANA,
    "module_name": "cot",
    "module_kwargs": {},
    "optimizer_name": "gepa",
    "optimizer_kwargs": {},
    "compile_kwargs": {},
    "column_mapping": None,
    "dataset_rows": 100,
    "stored_bytes": 0,
    "source_dataset_id": None,
    "resumable": False,
    "pausable": False,
    "latest_metrics": {},
    "model_name": GPT4O_MINI,
    "model_settings": None,
    "reflection_model_name": None,
    "task_model_name": None,
    "total_pairs": None,
    "completed_pairs": None,
    "failed_pairs": None,
    "generation_models": None,
    "reflection_models": None,
    "split_fractions": {"train": 0.6, "val": 0.2, "test": 0.2},
    "shuffle": True,
    "seed": 42,
    "baseline_test_metric": None,
    "optimized_test_metric": None,
    "metric_improvement": None,
    "best_pair_label": None,
    "summary_text": None,
    "metric_name": "accuracy",
    "role": None,
    "grants": None,
    "logs": None,
    "result": None,
    "grid_result": None,
    "serve": None,
    "payload": None,
    "cost_cents": 0,
}


def make_job(**overrides: Any) -> dict[str, Any]:
    """Return one job dict with defaults, applying ``overrides``.

    Defaults describe a successful single ``run`` owned by ``dana`` on gpt-4o-mini.
    ``metric_improvement`` is auto-filled as ``optimized - baseline`` (rounded to
    6 dp) when both metrics are given and improvement was not passed explicitly.
    ``column_mapping`` defaults to ``text -> label``. Pass ``optimization_id`` to
    fix the id; otherwise the caller (e.g. :func:`add_job`) must set it.

    Args:
        **overrides: Any job field to override.

    Returns:
        A fresh job dict ready to place in ``world.s["jobs"]``.
    """
    job = {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
        for k, v in _JOB_DEFAULTS.items()
    }
    job["optimization_id"] = overrides.get("optimization_id", "")
    if job["column_mapping"] is None:
        job["column_mapping"] = {
            "inputs": {"text": "text"},
            "outputs": {"label": "label"},
        }
    if job["model_settings"] is None:
        job["model_settings"] = (
            {"name": job["model_name"], "temperature": 0.0}
            if job["model_name"]
            else None
        )
    if job["grants"] is None:
        job["grants"] = {}
    if job["logs"] is None:
        job["logs"] = []
    job.update(overrides)
    improvement_given = "metric_improvement" in overrides
    if (
        not improvement_given
        and job.get("baseline_test_metric") is not None
        and job.get("optimized_test_metric") is not None
    ):
        job["metric_improvement"] = round(
            job["optimized_test_metric"] - job["baseline_test_metric"], 6
        )
    return job


def add_job(world: Any, **overrides: Any) -> dict[str, Any]:
    """Build a job via :func:`make_job` and store it in a live ``world``.

    Assigns a deterministic id (``11111111-...`` slot based on the current job
    count) when ``optimization_id`` is omitted, so setup order fixes the id.

    Args:
        world: The world to mutate (its ``s["jobs"]`` dict).
        **overrides: Job fields to override.

    Returns:
        The stored job dict.
    """
    jobs = world.s.setdefault("jobs", {})
    if "optimization_id" not in overrides:
        overrides["optimization_id"] = f"11111111-0000-4000-8000-{len(jobs):012d}"
    job = make_job(**overrides)
    jobs[job["optimization_id"]] = job
    return job


def _jobs() -> dict[str, Any]:
    """Return the ~18 seeded jobs keyed by optimization id."""
    jobs: list[dict[str, Any]] = []

    b1, o1 = _binary_examples(10, 6, 8, 0, "חיובי", "שלילי")
    j1 = make_job(
        optimization_id=_oid(1),
        name="support-tickets v2",
        pinned=True,
        status="success",
        module_name="cot",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=120,
        source_dataset_id="ds_support_tickets",
        created_at="2026-08-10T09:00:00Z",
        started_at="2026-08-10T09:00:10Z",
        completed_at="2026-08-10T09:12:30Z",
        elapsed="12m 20s",
        elapsed_seconds=740.0,
        baseline_test_metric=0.6,
        optimized_test_metric=0.8,
        summary_text="Solid gain on support tickets.",
        logs=[
            _log(
                "2026-08-10T09:00:10Z",
                "INFO",
                "Run started: module=cot optimizer=gepa model=openrouter/openai/gpt-4o-mini",
            ),
            _log(
                "2026-08-10T09:01:00Z",
                "INFO",
                "Loaded 120 rows; split train=72 val=24 test=24",
            ),
            _log(
                "2026-08-10T09:03:00Z",
                "INFO",
                "Baseline evaluation complete: accuracy=0.60",
            ),
            _log(
                "2026-08-10T09:11:00Z", "INFO", "Optimization complete: accuracy=0.80"
            ),
            _log("2026-08-10T09:12:30Z", "INFO", "Run finished successfully"),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
            "source_dataset_id": "ds_support_tickets",
            "signature_code": _SENTIMENT_SIGNATURE,
            "metric_code": _SENTIMENT_METRIC,
        },
        cost_cents=80,
    )
    j1["result"] = _run_result(j1, b1, o1, 740.0, 120000)
    j1["serve"] = _serve_block(
        j1, j1["result"]["program_artifact"]["optimized_prompt"]["demos"]
    )
    jobs.append(j1)

    j2 = make_job(
        optimization_id=_oid(2),
        name="support-tickets v2 (copy)",
        status="success",
        module_name="cot",
        optimizer_name="dspy.teleprompt.MIPROv2",
        model_name=GPT4O_MINI,
        dataset_rows=120,
        source_dataset_id="ds_support_tickets",
        created_at="2026-08-11T09:00:00Z",
        started_at="2026-08-11T09:00:10Z",
        completed_at="2026-08-11T09:13:00Z",
        elapsed="12m 50s",
        elapsed_seconds=770.0,
        baseline_test_metric=0.62,
        optimized_test_metric=0.79,
        payload={
            "module_name": "cot",
            "optimizer_name": "dspy.teleprompt.MIPROv2",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
            "source_dataset_id": "ds_support_tickets",
        },
        cost_cents=60,
    )
    b2, o2 = _binary_examples(10, 6, 8, 0, "חיובי", "שלילי")
    j2["result"] = _run_result(j2, b2, o2, 770.0, 118000)
    j2["serve"] = _serve_block(
        j2, j2["result"]["program_artifact"]["optimized_prompt"]["demos"]
    )
    jobs.append(j2)

    b3, o3 = _binary_examples(12, 7, 9, 0, "חיובי", "שלילי")
    j3 = make_job(
        optimization_id=_oid(3),
        name="ניתוח רגש בעברית",
        status="success",
        module_name="predict",
        optimizer_name="gepa",
        model_name=CLAUDE_HAIKU,
        dataset_rows=200,
        source_dataset_id="ds_reviews_he",
        created_at="2026-08-14T09:00:00Z",
        started_at="2026-08-14T09:00:10Z",
        completed_at="2026-08-14T09:20:00Z",
        elapsed="19m 50s",
        elapsed_seconds=1190.0,
        baseline_test_metric=round(7 / 12, 4),
        optimized_test_metric=0.75,
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        payload={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": CLAUDE_HAIKU},
            "source_dataset_id": "ds_reviews_he",
            "signature_code": _SENTIMENT_SIGNATURE,
            "metric_code": _SENTIMENT_METRIC,
        },
        cost_cents=50,
    )
    j3["result"] = _run_result(j3, b3, o3, 1190.0, 205000)
    j3["serve"] = _serve_block(
        j3, j3["result"]["program_artifact"]["optimized_prompt"]["demos"]
    )
    jobs.append(j3)

    j4 = make_job(
        optimization_id=_oid(4),
        name="email-triage nightly",
        status="failed",
        module_name="cot",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=90,
        source_dataset_id="ds_email_triage",
        created_at="2026-08-18T09:00:00Z",
        started_at="2026-08-18T09:00:10Z",
        completed_at="2026-08-18T09:01:40Z",
        elapsed="1m 30s",
        elapsed_seconds=90.0,
        stop_reason="error",
        result_availability="none",
        message="Metric function raised KeyError: 'label'",
        column_mapping={
            "inputs": {"subject": "subject", "body": "body"},
            "outputs": {"category": "category"},
        },
        logs=[
            _log(
                "2026-08-18T09:00:10Z", "INFO", "Run started: module=cot optimizer=gepa"
            ),
            _log(
                "2026-08-18T09:00:40Z",
                "INFO",
                "Loaded 90 rows; split train=54 val=18 test=18",
            ),
            _log(
                "2026-08-18T09:01:35Z",
                "WARNING",
                "Metric evaluation returned no score for 18 examples",
            ),
            _log(
                "2026-08-18T09:01:40Z",
                "ERROR",
                "Metric function raised KeyError: 'label' - the metric referenced a column that is not in the dataset (columns: subject, body, category). Fix the metric code and retry.",
            ),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"subject": "subject", "body": "body"},
                "outputs": {"category": "category"},
            },
            "model_config": {"name": GPT4O_MINI},
            "source_dataset_id": "ds_email_triage",
            "metric_code": "def metric(example, prediction, trace=None):\n    return 1.0 if prediction.label == example.category else 0.0\n",
        },
        cost_cents=0,
    )
    jobs.append(j4)

    j5 = make_job(
        optimization_id=_oid(5),
        name="qa-bot tuning",
        status="failed",
        module_name="react",
        optimizer_name="gepa",
        model_name=GROK4,
        dataset_rows=140,
        created_at="2026-08-20T09:00:00Z",
        started_at="2026-08-20T09:00:10Z",
        completed_at="2026-08-20T09:05:00Z",
        elapsed="4m 50s",
        elapsed_seconds=290.0,
        stop_reason="error",
        result_availability="none",
        resumable=False,
        message="Provider error: rate limit exceeded (HTTP 429)",
        column_mapping={
            "inputs": {"question": "question"},
            "outputs": {"answer": "answer"},
        },
        logs=[
            _log(
                "2026-08-20T09:00:10Z",
                "INFO",
                "Run started: module=react optimizer=gepa model=openrouter/x-ai/grok-4",
            ),
            _log(
                "2026-08-20T09:02:00Z",
                "WARNING",
                "Provider returned HTTP 429, retrying (attempt 1/5)",
            ),
            _log(
                "2026-08-20T09:03:30Z",
                "WARNING",
                "Provider returned HTTP 429, retrying (attempt 4/5)",
            ),
            _log(
                "2026-08-20T09:05:00Z",
                "ERROR",
                "RateLimitError: provider rate limit exceeded after 5 retries (HTTP 429). This is a transient provider error; retry later.",
            ),
        ],
        payload={
            "module_name": "react",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"question": "question"},
                "outputs": {"answer": "answer"},
            },
            "model_config": {"name": GROK4},
        },
        cost_cents=0,
    )
    jobs.append(j5)

    j6 = make_job(
        optimization_id=_oid(6),
        name="long-run experiment",
        status="cancelled",
        module_name="cot",
        optimizer_name="gepa",
        model_name=GEMINI_PRO,
        dataset_rows=300,
        created_at="2026-08-24T09:00:00Z",
        started_at="2026-08-24T09:00:10Z",
        completed_at="2026-08-24T09:40:00Z",
        elapsed="39m 50s",
        elapsed_seconds=2390.0,
        stop_reason="cancelled",
        resumable=True,
        message="Cancelled by user",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log(
                "2026-08-24T09:00:10Z", "INFO", "Run started: module=cot optimizer=gepa"
            ),
            _log("2026-08-24T09:20:00Z", "INFO", "Checkpoint saved at iteration 12"),
            _log(
                "2026-08-24T09:40:00Z",
                "WARNING",
                "Run cancelled by user; checkpoint retained",
            ),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GEMINI_PRO},
        },
        cost_cents=40,
    )
    jobs.append(j6)

    j7 = make_job(
        optimization_id=_oid(7),
        name="live sentiment sweep",
        status="running",
        module_name="predict",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=200,
        source_dataset_id="ds_reviews_he",
        created_at="2026-09-19T09:00:00Z",
        started_at="2026-09-19T09:00:10Z",
        elapsed="—",
        estimated_remaining="3m 20s",
        pausable=True,
        resumable=False,
        latest_metrics={"iteration": 8, "best_so_far": 0.71, "completed_so_far": 8},
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log(
                "2026-09-19T09:00:10Z",
                "INFO",
                "Run started: module=predict optimizer=gepa",
            ),
            _log("2026-09-19T09:03:00Z", "INFO", "Checkpoint saved at iteration 4"),
            _log("2026-09-20T08:50:00Z", "INFO", "Iteration 8 best_so_far=0.71"),
        ],
        payload={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
            "source_dataset_id": "ds_reviews_he",
        },
        cost_cents=20,
    )
    jobs.append(j7)

    j8 = make_job(
        optimization_id=_oid(8),
        name="paused-tuning",
        status="paused",
        module_name="cot",
        optimizer_name="gepa",
        model_name=CLAUDE_SONNET,
        dataset_rows=180,
        created_at="2026-09-15T09:00:00Z",
        started_at="2026-09-15T09:00:10Z",
        elapsed="8m 00s",
        elapsed_seconds=480.0,
        stop_reason="paused",
        resumable=True,
        latest_metrics={"iteration": 6, "best_so_far": 0.68},
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log(
                "2026-09-15T09:00:10Z", "INFO", "Run started: module=cot optimizer=gepa"
            ),
            _log("2026-09-15T09:06:00Z", "INFO", "Checkpoint saved at iteration 6"),
            _log(
                "2026-09-15T09:08:00Z", "INFO", "Run paused by user; resume point saved"
            ),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": CLAUDE_SONNET},
        },
        cost_cents=30,
    )
    jobs.append(j8)

    j9 = make_job(
        optimization_id=_oid(9),
        name="queued-run",
        status="pending",
        module_name="predict",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=100,
        created_at="2026-09-20T08:45:00Z",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[_log("2026-09-20T08:45:00Z", "INFO", "Run queued")],
        payload={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
        },
    )
    jobs.append(j9)

    j10 = make_job(
        optimization_id=_oid(10),
        name="validating-run",
        status="validating",
        module_name="cot",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=110,
        created_at="2026-09-20T08:00:00Z",
        started_at="2026-09-20T08:00:05Z",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log("2026-09-20T08:00:05Z", "INFO", "Validating payload and dataset"),
            _log("2026-09-20T08:01:00Z", "INFO", "Signature and metric compiled OK"),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
        },
    )
    jobs.append(j10)

    j11 = _grid_bakeoff()
    jobs.append(j11)
    j12 = _grid_reasoning()
    jobs.append(j12)

    j13 = make_job(
        optimization_id=_oid(13),
        name="regression-risk run",
        status="success",
        module_name="predict",
        optimizer_name="gepa",
        model_name=GPT4O_MINI,
        dataset_rows=150,
        created_at="2026-08-26T09:00:00Z",
        started_at="2026-08-26T09:00:10Z",
        completed_at="2026-08-26T09:11:00Z",
        elapsed="10m 50s",
        elapsed_seconds=650.0,
        baseline_test_metric=0.7,
        optimized_test_metric=0.66,
        summary_text="Optimized program scored below baseline.",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        payload={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O_MINI},
        },
        cost_cents=40,
    )
    b13, o13 = _binary_examples(10, 7, 6, 0, "חיובי", "שלילי")
    j13["result"] = _run_result(j13, b13, o13, 650.0, 90000)
    j13["serve"] = _serve_block(
        j13, j13["result"]["program_artifact"]["optimized_prompt"]["demos"]
    )
    jobs.append(j13)

    j14 = make_job(
        optimization_id=_oid(14),
        name="prompt-opt: cold email",
        status="success",
        optimization_type="blackbox",
        module_name="blackbox",
        optimizer_name="gepa",
        model_name=CLAUDE_SONNET,
        reflection_model_name=CLAUDE_SONNET,
        dataset_rows=40,
        created_at="2026-09-02T09:00:00Z",
        started_at="2026-09-02T09:00:10Z",
        completed_at="2026-09-02T09:18:00Z",
        elapsed="17m 50s",
        elapsed_seconds=1070.0,
        baseline_test_metric=0.4,
        optimized_test_metric=0.72,
        column_mapping=None,
        summary_text="Blackbox lifted a cold-email prompt from 0.40 to 0.72.",
        logs=[
            _log("2026-09-02T09:00:10Z", "INFO", "Blackbox run started: engine=gepa"),
            _log("2026-09-02T09:02:00Z", "INFO", "Baseline candidate scored 0.40"),
            _log("2026-09-02T09:18:00Z", "INFO", "Best candidate scored 0.72"),
        ],
        payload={
            "objective": "Write a compelling cold-email opener.",
            "scorer": {
                "kind": "python",
                "metric_code": "def score(candidate, case):\n    return len(candidate) / 200.0\n",
            },
            "reflection_model_config": {"name": CLAUDE_SONNET},
            "strategy": {"mode": "single", "engine": "gepa"},
        },
        cost_cents=30,
    )
    j14["blackbox_result"] = {
        "optimizer_name": "gepa",
        "strategy_mode": "single",
        "engine_used": "gepa",
        "split_counts": {"train": 24, "val": 8, "test": 8},
        "baseline_test_metric": 0.4,
        "optimized_test_metric": 0.72,
        "metric_improvement": 0.32,
        "seed_candidate": "Hi {name}, I noticed your team is scaling fast.",
        "best_candidate": "Hi {name} - saw {company} just shipped {feature}; here is a 10-minute idea to cut onboarding time in half.",
        "regression_guard_applied": False,
        "lanes": [
            {
                "engine": "gepa",
                "phase": "single",
                "status": "completed",
                "best_score": 0.72,
                "scorer_runs": 40,
                "error": None,
            }
        ],
        "versions": [
            {
                "candidate": "Hi {name}, I noticed your team is scaling fast.",
                "score": 0.4,
                "mean_score": 0.4,
                "evals": 8,
                "first_run": 0,
                "side_info": {},
            },
            {
                "candidate": "Hi {name} - saw {company} just shipped {feature}; here is a 10-minute idea to cut onboarding time in half.",
                "score": 0.72,
                "mean_score": 0.72,
                "evals": 8,
                "first_run": 1,
                "side_info": {},
            },
        ],
        "candidate_tree": [],
        "total_scorer_runs": 40,
        "runtime_seconds": 1070.0,
        "num_lm_calls": 96,
        "total_tokens": 88000,
        "usage_by_model": [
            {"model": CLAUDE_SONNET, "input_tokens": 61000, "output_tokens": 27000}
        ],
        "lm_activity": None,
        "optimization_metadata": {},
        "details": {},
    }
    jobs.append(j14)

    j15 = make_job(
        optimization_id=_oid(15),
        name="blackbox: scorer crash",
        status="failed",
        optimization_type="blackbox",
        module_name="blackbox",
        optimizer_name="best_of_n",
        model_name=CLAUDE_HAIKU,
        reflection_model_name=CLAUDE_HAIKU,
        dataset_rows=30,
        created_at="2026-09-05T09:00:00Z",
        started_at="2026-09-05T09:00:10Z",
        completed_at="2026-09-05T09:02:00Z",
        elapsed="1m 50s",
        elapsed_seconds=110.0,
        stop_reason="error",
        result_availability="none",
        message="Scorer raised ZeroDivisionError",
        column_mapping=None,
        logs=[
            _log(
                "2026-09-05T09:00:10Z", "INFO", "Blackbox run started: engine=best_of_n"
            ),
            _log(
                "2026-09-05T09:02:00Z",
                "ERROR",
                "Scorer raised ZeroDivisionError in metric_code line 8 (division by zero). Fix the scorer and retry.",
            ),
        ],
        payload={
            "objective": "Optimize a tagline.",
            "scorer": {
                "kind": "python",
                "metric_code": "def score(candidate, case):\n    return 1.0 / (len(candidate) - len(candidate))\n",
            },
            "reflection_model_config": {"name": CLAUDE_HAIKU},
            "strategy": {"mode": "single", "engine": "best_of_n"},
        },
        cost_cents=0,
    )
    jobs.append(j15)

    b16, o16 = _binary_examples(10, 6, 8, 0, "חיובי", "שלילי")
    j16 = make_job(
        optimization_id=_oid(16),
        name="noa shared classifier",
        status="success",
        username=NOA,
        grants={DANA: "viewer"},
        module_name="cot",
        optimizer_name="gepa",
        model_name=CLAUDE_SONNET,
        dataset_rows=150,
        created_at="2026-09-06T09:00:00Z",
        started_at="2026-09-06T09:00:10Z",
        completed_at="2026-09-06T09:15:00Z",
        elapsed="14m 50s",
        elapsed_seconds=890.0,
        baseline_test_metric=0.6,
        optimized_test_metric=0.77,
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": CLAUDE_SONNET},
        },
        cost_cents=70,
    )
    j16["result"] = _run_result(j16, b16, o16, 890.0, 140000)
    j16["serve"] = _serve_block(
        j16, j16["result"]["program_artifact"]["optimized_prompt"]["demos"]
    )
    jobs.append(j16)

    j17 = make_job(
        optimization_id=_oid(17),
        name="noa private classifier",
        status="success",
        username=NOA,
        grants={},
        module_name="predict",
        optimizer_name="gepa",
        model_name=GPT4O,
        dataset_rows=120,
        created_at="2026-09-01T09:00:00Z",
        started_at="2026-09-01T09:00:10Z",
        completed_at="2026-09-01T09:14:00Z",
        elapsed="13m 50s",
        elapsed_seconds=830.0,
        baseline_test_metric=0.58,
        optimized_test_metric=0.79,
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        payload={
            "module_name": "predict",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GPT4O},
        },
        cost_cents=65,
    )
    jobs.append(j17)

    j18 = make_job(
        optimization_id=_oid(18),
        name="budget-capped run",
        status="stopped",
        module_name="cot",
        optimizer_name="gepa",
        model_name=GEMINI_PRO,
        dataset_rows=260,
        created_at="2026-09-08T09:00:00Z",
        started_at="2026-09-08T09:00:10Z",
        completed_at="2026-09-08T09:30:00Z",
        elapsed="29m 50s",
        elapsed_seconds=1790.0,
        stop_reason="budget_reached",
        resumable=True,
        message="Stopped: spending limit reached",
        execution_budget={
            "limit_cents": 50,
            "spent_cents": 50,
            "budget_id": "bud_0001",
            "revision": 1,
        },
        latest_metrics={"iteration": 14, "best_so_far": 0.73},
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log(
                "2026-09-08T09:00:10Z", "INFO", "Run started: module=cot optimizer=gepa"
            ),
            _log("2026-09-08T09:15:00Z", "INFO", "Checkpoint saved at iteration 10"),
            _log(
                "2026-09-08T09:30:00Z",
                "WARNING",
                "Stopped: spending limit of 50 credits reached; resume to continue",
            ),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "model_config": {"name": GEMINI_PRO},
        },
        cost_cents=50,
    )
    jobs.append(j18)

    return {job["optimization_id"]: job for job in jobs}


def _grid_bakeoff() -> dict[str, Any]:
    """Build the 6-pair grid-search job (best pair + one failed pair)."""
    gen = [CLAUDE_HAIKU, GPT4O_MINI]
    refl = [GPT4O, CLAUDE_SONNET, GEMINI_PRO]
    specs = [
        (GPT4O_MINI, GPT4O, 0.5, 0.62, None),
        (GPT4O_MINI, CLAUDE_SONNET, 0.5, 0.71, None),
        (GPT4O_MINI, GEMINI_PRO, 0.5, 0.58, None),
        (CLAUDE_HAIKU, GPT4O, 0.5, 0.8, None),
        (CLAUDE_HAIKU, CLAUDE_SONNET, 0.5, 0.77, None),
        (
            CLAUDE_HAIKU,
            GEMINI_PRO,
            None,
            None,
            "RateLimitError: provider returned HTTP 429 for this pair",
        ),
    ]
    pairs = []
    for idx, (g, r, base, opt, err) in enumerate(specs):
        pair = {
            "pair_index": idx,
            "generation_model": g,
            "reflection_model": r,
            "generation_reasoning_effort": None,
            "reflection_reasoning_effort": None,
            "baseline_test_metric": base,
            "optimized_test_metric": opt,
            "metric_improvement": round(opt - base, 6)
            if base is not None and opt is not None
            else None,
            "target_score": None,
            "target_score_reached": None,
            "stop_reason": "error" if err else None,
            "result_availability": "none" if err else "evaluated",
            "terminal_evidence": None,
            "runtime_seconds": 120.0 if not err else 30.0,
            "num_lm_calls": 60 if not err else 5,
            "total_tokens": 24000 if not err else 1500,
            "usage_by_model": [
                {"model": g, "input_tokens": 16000, "output_tokens": 8000}
            ]
            if not err
            else [],
            "avg_response_time_ms": 780.0,
            "lm_activity": None,
            "program_artifact": None,
            "error": err,
            "baseline_test_results": [],
            "optimized_test_results": [],
            "baseline_logged_metrics": {},
            "optimized_logged_metrics": {},
        }
        pairs.append(pair)
    bb, oo = _binary_examples(10, 5, 8, 0, "חיובי", "שלילי")
    pairs[3]["baseline_test_results"] = bb
    pairs[3]["optimized_test_results"] = oo
    pairs[3]["baseline_logged_metrics"] = {"accuracy": 0.5}
    pairs[3]["optimized_logged_metrics"] = {"accuracy": 0.8}
    best = pairs[3]
    job = make_job(
        optimization_id=_oid(11),
        name="grid: model bake-off",
        status="success",
        optimization_type="grid_search",
        module_name="cot",
        optimizer_name="gepa",
        model_name=None,
        model_settings=None,
        generation_models=[{"name": m} for m in gen],
        reflection_models=[{"name": m} for m in refl],
        total_pairs=6,
        completed_pairs=5,
        failed_pairs=1,
        dataset_rows=180,
        source_dataset_id="ds_support_tickets",
        created_at="2026-08-28T09:00:00Z",
        started_at="2026-08-28T09:00:10Z",
        completed_at="2026-08-28T09:45:00Z",
        elapsed="44m 50s",
        elapsed_seconds=2690.0,
        baseline_test_metric=best["baseline_test_metric"],
        optimized_test_metric=best["optimized_test_metric"],
        best_pair_label=f"{best['generation_model']} + {best['reflection_model']}",
        summary_text="Best pair: claude-haiku-4.5 + gpt-4o at +0.30.",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        logs=[
            _log("2026-08-28T09:00:10Z", "INFO", "Grid search started: 6 pairs"),
            _log(
                "2026-08-28T09:30:00Z",
                "INFO",
                "Pair 3 (claude-haiku-4.5 + gpt-4o) best so far: 0.80",
                pair_index=3,
            ),
            _log(
                "2026-08-28T09:40:00Z",
                "ERROR",
                "Pair 5 failed: RateLimitError HTTP 429",
                pair_index=5,
            ),
            _log(
                "2026-08-28T09:45:00Z",
                "INFO",
                "Grid search complete: 5 completed, 1 failed",
            ),
        ],
        payload={
            "module_name": "cot",
            "optimizer_name": "gepa",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "generation_models": [{"name": m} for m in gen],
            "reflection_models": [{"name": m} for m in refl],
            "source_dataset_id": "ds_support_tickets",
        },
        cost_cents=220,
    )
    job["metric_improvement"] = round(
        best["optimized_test_metric"] - best["baseline_test_metric"], 6
    )
    job["grid_result"] = {
        "module_name": "cot",
        "optimizer_name": "gepa",
        "metric_name": "accuracy",
        "split_counts": {"train": 108, "val": 36, "test": 36},
        "total_pairs": 6,
        "completed_pairs": 5,
        "failed_pairs": 1,
        "stopped_pairs": 0,
        "pair_results": pairs,
        "best_pair": best,
        "runtime_seconds": 2690.0,
        "total_tokens": 122000,
        "usage_by_model": [
            {"model": CLAUDE_HAIKU, "input_tokens": 60000, "output_tokens": 30000},
            {"model": GPT4O, "input_tokens": 20000, "output_tokens": 12000},
        ],
    }
    job["serve"] = {
        "input_fields": ["text"],
        "output_fields": ["label"],
        "instructions": "סווג את הקלט לפי התווית הנכונה.",
        "demo_count": 3,
        "sample_inputs": {"text": "השירות היה מצוין ומהיר"},
        "model_name": best["generation_model"],
    }
    return job


def _grid_reasoning() -> dict[str, Any]:
    """Build the 4-pair grid-search job (all pairs completed)."""
    gen = [GPT4O_MINI, GPT4O]
    refl = [CLAUDE_HAIKU, CLAUDE_SONNET]
    specs = [
        (GPT4O_MINI, CLAUDE_HAIKU, 0.55, 0.6),
        (GPT4O_MINI, CLAUDE_SONNET, 0.55, 0.68),
        (GPT4O, CLAUDE_HAIKU, 0.55, 0.64),
        (GPT4O, CLAUDE_SONNET, 0.55, 0.7),
    ]
    pairs = []
    for idx, (g, r, base, opt) in enumerate(specs):
        pairs.append(
            {
                "pair_index": idx,
                "generation_model": g,
                "reflection_model": r,
                "generation_reasoning_effort": "medium",
                "reflection_reasoning_effort": "high",
                "baseline_test_metric": base,
                "optimized_test_metric": opt,
                "metric_improvement": round(opt - base, 6),
                "target_score": None,
                "target_score_reached": None,
                "stop_reason": None,
                "result_availability": "evaluated",
                "terminal_evidence": None,
                "runtime_seconds": 150.0,
                "num_lm_calls": 70,
                "total_tokens": 26000,
                "usage_by_model": [
                    {"model": g, "input_tokens": 17000, "output_tokens": 9000}
                ],
                "avg_response_time_ms": 800.0,
                "lm_activity": None,
                "program_artifact": None,
                "error": None,
                "baseline_test_results": [],
                "optimized_test_results": [],
                "baseline_logged_metrics": {},
                "optimized_logged_metrics": {},
            }
        )
    best = pairs[3]
    job = make_job(
        optimization_id=_oid(12),
        name="grid: reasoning effort",
        status="success",
        optimization_type="grid_search",
        module_name="predict",
        optimizer_name="dspy.teleprompt.BootstrapFewShot",
        model_name=None,
        model_settings=None,
        generation_models=[{"name": m} for m in gen],
        reflection_models=[{"name": m} for m in refl],
        total_pairs=4,
        completed_pairs=4,
        failed_pairs=0,
        dataset_rows=160,
        created_at="2026-08-30T09:00:00Z",
        started_at="2026-08-30T09:00:10Z",
        completed_at="2026-08-30T09:35:00Z",
        elapsed="34m 50s",
        elapsed_seconds=2090.0,
        baseline_test_metric=best["baseline_test_metric"],
        optimized_test_metric=best["optimized_test_metric"],
        best_pair_label=f"{best['generation_model']} + {best['reflection_model']}",
        summary_text="Best pair: gpt-4o + claude-sonnet-4.5 at 0.70.",
        column_mapping={"inputs": {"text": "text"}, "outputs": {"label": "label"}},
        payload={
            "module_name": "predict",
            "optimizer_name": "dspy.teleprompt.BootstrapFewShot",
            "column_mapping": {
                "inputs": {"text": "text"},
                "outputs": {"label": "label"},
            },
            "generation_models": [{"name": m} for m in gen],
            "reflection_models": [{"name": m} for m in refl],
        },
        cost_cents=180,
    )
    job["metric_improvement"] = round(
        best["optimized_test_metric"] - best["baseline_test_metric"], 6
    )
    job["grid_result"] = {
        "module_name": "predict",
        "optimizer_name": "dspy.teleprompt.BootstrapFewShot",
        "metric_name": "accuracy",
        "split_counts": {"train": 96, "val": 32, "test": 32},
        "total_pairs": 4,
        "completed_pairs": 4,
        "failed_pairs": 0,
        "stopped_pairs": 0,
        "pair_results": pairs,
        "best_pair": best,
        "runtime_seconds": 2090.0,
        "total_tokens": 104000,
        "usage_by_model": [
            {"model": GPT4O, "input_tokens": 52000, "output_tokens": 24000}
        ],
    }
    job["serve"] = {
        "input_fields": ["text"],
        "output_fields": ["label"],
        "instructions": "סווג את הקלט לפי התווית הנכונה.",
        "demo_count": 2,
        "sample_inputs": {"text": "השירות היה מצוין ומהיר"},
        "model_name": best["generation_model"],
    }
    return job


def base_state() -> dict[str, Any]:
    """Return a fresh, fully deterministic starting state for one task's world.

    Returns:
        A dict with all top-level sections the tools read and write: ``user``,
        ``users``, ``jobs``, ``datasets``, ``samples``, ``staged``, ``models``,
        ``endpoints``, ``registry``, ``wallet``, ``search_corpus``,
        ``blackbox``, ``tagging_sessions``, ``preferences``, ``wizard``,
        ``ui_cards`` and ``faults``. No wall-clock or random values are used.
    """
    return {
        "now": NOW,
        "user": {"username": DANA, "is_admin": False},
        "users": {
            DANA: {"username": DANA, "is_admin": False},
            NOA: {"username": NOA, "is_admin": False},
            ADMIN: {"username": ADMIN, "is_admin": True},
        },
        "jobs": _jobs(),
        "datasets": _datasets(),
        "samples": _samples(),
        "staged": {},
        "models": _models(),
        "endpoints": _endpoints(),
        "registry": {
            "modules": list(REGISTRY_MODULES),
            "metrics": list(REGISTRY_METRICS),
            "optimizers": list(REGISTRY_OPTIMIZERS),
        },
        "wallet": _wallet(),
        "search_corpus": _search_corpus(),
        "blackbox": _blackbox(),
        "tagging_sessions": _tagging_sessions(),
        "preferences": {
            "advanced_mode": False,
            "expand_advanced": False,
            "lite_mode": False,
            "wizard_code_assist": "auto",
            "wizard_split_mode": "auto",
            "tagger_assist": True,
        },
        "storage": {"used_bytes": 219400, "quota_bytes": 1073741824},
        "wizard": {},
        "ui_cards": [],
        "faults": {},
    }
