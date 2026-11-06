Skynet HTTP API की मदद से स्क्रिप्ट और टूल वह सब कर सकते हैं जो वेब ऐप करता है: रन शुरू करना, उन पर नज़र रखना, और परिणाम पढ़ना। यह पेज बताता है कि अनुरोधों पर हस्ताक्षर कैसे करें, पूरा संदर्भ कहाँ है, और एक पूरा उदाहरण देता है।

> [!TIP]
> क्या आप Claude Code, Codex या Cursor जैसा कोई AI एजेंट इस्तेमाल करते हैं? इसके बजाय उसे [MCP](/docs/mcp) से जोड़ें। तब एजेंट आपके लिए रन शुरू करता है और उन पर नज़र रखता है, हर रन के लिए खर्च सीमा के साथ।

## बेस URL

हर अनुरोध यहाँ भेजें:

```text
https://api.skynetml.com
```

## प्रमाणीकरण

API, API टोकन इस्तेमाल करता है।

1. Skynet में **सेटिंग्स** खोलें, फिर **API** टैब खोलें।
2. **टोकन जनरेट करें** पर क्लिक करें।
3. टोकन को तुरंत कॉपी करें। Skynet पूरा टोकन सिर्फ़ एक बार दिखाता है।

हर अनुरोध के `Authorization` हेडर में टोकन भेजें:

```http
Authorization: Bearer skyd_...
```

- टोकन `skyd_` से शुरू होते हैं।
- हर उपयोगकर्ता के पास ज़्यादा से ज़्यादा एक मान्य टोकन होता है। नया टोकन पुराने टोकन को बंद कर देता है।
- किसी टोकन को बंद करने के लिए **टोकन निरस्त करें** पर क्लिक करें।

> [!WARNING]
> टोकन को पासवर्ड की तरह संभालें। इसे सोर्स कोड और लॉग से बाहर रखें। Skynet टोकन कैसे सहेजता है, यह जानने के लिए [सुरक्षा](/docs/security) पढ़ें।

## इंटरैक्टिव संदर्भ

पूरा संदर्भ [api.skynetml.com/scalar](https://api.skynetml.com/scalar) पर है। इसमें हर सार्वजनिक एंडपॉइंट, उसके फ़ील्ड और उसके रिस्पॉन्स की सूची है। आप उस पेज से टेस्ट अनुरोध भेज सकते हैं।

यह संदर्भ `https://api.skynetml.com/openapi.public.json` पर मौजूद OpenAPI दस्तावेज़ पढ़ता है। अपनी भाषा में क्लाइंट जनरेट करने के लिए उस फ़ाइल का इस्तेमाल करें।

## मुख्य एंडपॉइंट

| मेथड और पाथ                                     | यह क्या करता है                                               |
| ----------------------------------------------- | ------------------------------------------------------------- |
| `GET /models`                                   | उन मॉडलों की सूची देता है जिन्हें आप इस्तेमाल कर सकते हैं।        |
| `GET /optimizers`                               | किसी सेटअप के लिए उपयुक्त ऑप्टिमाइज़रों की सूची देता है।           |
| `GET /blackbox/engines`                         | नेटिव इंजनों की सूची देता है और बताता है कि हर इंजन उपलब्ध है या नहीं। |
| `POST /optimize` | DSPy प्रोग्राम के लिए रन शुरू करता है। |
| `POST /blackbox/run`                            | किसी GitHub रिपॉज़िटरी पर रन शुरू करता है।                       |
| `GET /optimizations`                            | आपके रन की सूची देता है।                                       |
| `GET /optimizations/{optimization_id}`          | एक रन लौटाता है: स्थिति, प्रगति, लॉग और परिणाम।                  |
| `GET /optimizations/{optimization_id}/artifact` | रन को मिला सबसे अच्छा संस्करण लौटाता है।                         |
| `POST /optimizations/{optimization_id}/cancel`  | रन को रोकता है।                                               |
| `POST /serve/{optimization_id}`                 | सबसे अच्छे संस्करण को किसी नए इनपुट पर चलाता है।                  |
| `GET /health`                                   | बताता है कि सेवा चालू है या नहीं।                                |

## रन शुरू करें

`POST /optimize` वही सेटअप लेता है जो सबमिट विज़ार्ड लेता है। यह उदाहरण एक ऐसे DSPy प्रोग्राम को बेहतर बनाता है जो सपोर्ट संदेशों को लेबल में बाँटता है।

```bash tab="curl"
curl https://api.skynetml.com/optimize \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "task": {
    "objective": "Sort each support message into billing, bug or other.",
    "artifact": {
      "kind": "dspy_program",
      "starting_version": "Label this support message as billing, bug or other."
    }
  },
  "data": {
    "mode": "pairs",
    "cases": [
      { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
      { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
      {
        "input": { "message": "Do you have a Hebrew interface?" },
        "expected": { "label": "other" }
      }
    ]
  },
  "optimizer": { "choice": "auto", "budget": { "max_scorer_runs": 200 } },
  "models": {
    "task_model": { "name": "<model from GET /models>" },
    "reflection_model": { "name": "<model from GET /models>" }
  },
  "workspace": {
    "run_code": "import dspy\n\nprogram = dspy.Predict(\"message -> label\")\n\n\ndef run(artifact, input):\n    return program(message=input[\"message\"]).label\n",
    "metric_code": "def score(input, output, expected=None):\n    ok = str(output).strip().lower() == expected[\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n"
  },
  "max_cost_cents": 500
}
```

महत्वपूर्ण फ़ील्ड:

- `task.artifact.kind` का मान `dspy_program` होता है।
- `data.mode` का मान `pairs` (अपेक्षित आउटपुट वाले इनपुट) या `single` (ठीक एक केस) होता है।
- `optimizer.choice` का मान `auto` या `GET /optimizers` से मिला कोई एक ऑप्टिमाइज़र नाम होता है।
- `max_cost_cents` इस रन की खर्च सीमा है, अमेरिकी सेंट में।
- `is_private` डिफ़ॉल्ट रूप से `false` होता है। रन को निजी रखने के लिए इसे `true` पर सेट करें।

`run_code` और `metric_code` के नियमों के लिए [मेट्रिक और स्कोरर](/docs/concepts/scorers) पढ़ें।

### रिस्पॉन्स

| स्थिति | अर्थ                                                                                                                                                       |
| ------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `201`  | Skynet ने रन को कतार में डाल दिया। बॉडी में `optimization_id` और `status` होते हैं।                                                                          |
| `202`  | सेटअप जाँच अभी चल रही है। `Retry-After` हेडर में दिए गए सेकंड तक इंतज़ार करें। फिर वही बॉडी उसी `Idempotency-Key` के साथ दोबारा भेजें।                          |
| `402`  | आपका बैलेंस बहुत कम है।                                                                                                                                     |
| `409`  | आप किसी कोटा तक पहुँच गए हैं, या सेटअप जाँच पुरानी हो चुकी है।                                                                                                 |
| `422`  | अनुरोध या वर्कस्पेस कोड ऑप्टिमाइज़र के अनुरूप नहीं है।                                                                                                         |

> [!NOTE]
> हमेशा `Idempotency-Key` हेडर भेजें। अगर आप वही अनुरोध उसी कुंजी के साथ दोबारा भेजते हैं, तो Skynet पहला रन लौटाता है। वह दूसरा रन शुरू नहीं करता।

## रन पर नज़र रखें

जब तक रन की स्थिति अंतिम न हो जाए, उसे पोल करते रहें:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` फ़ील्ड इनमें से एक होता है:

| स्थिति                             | अंतिम |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | नहीं  |
| `paused`                           | नहीं  |
| `success`                          | हाँ   |
| `failed`, `cancelled`, `stopped`   | हाँ   |

हर 10 से 30 सेकंड में पोल करें। एक रन में कुछ मिनट से लेकर कई घंटे तक लग सकते हैं।

[API संदर्भ](https://api.skynetml.com/scalar) में रन रिस्पॉन्स के हर फ़ील्ड की सूची है।

## परिणाम का इस्तेमाल करें

जब स्थिति `success` हो, तो सबसे अच्छा संस्करण पाएँ:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

सबसे अच्छे संस्करण को किसी नए इनपुट पर चलाने के लिए `POST /serve/{optimization_id}` भेजें। हर स्कोर का क्या मतलब है, यह जानने के लिए [परिणाम](/docs/guides/results) पढ़ें।

## सीमाएँ

API वही कोटा, रेट सीमाएँ और बिलिंग इस्तेमाल करता है जो वेब ऐप करता है। [सीमाएँ](/docs/limits) और [बिलिंग](/docs/concepts/billing) पढ़ें।
