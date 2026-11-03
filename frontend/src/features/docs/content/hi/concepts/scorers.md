मेट्रिक Skynet को बताता है कि कोई आउटपुट कितना अच्छा है। यह `metric.py` में लिखा एक Python फ़ंक्शन है। हर इंजन इसी एक फ़ंक्शन को कॉल करता है। यह पेज कॉन्ट्रैक्ट, रिटर्न मान और उदाहरण बताता है।

## कॉन्ट्रैक्ट

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| आर्गुमेंट   | इसमें क्या होता है                                                  |
| ---------- | ----------------------------------------------------------------- |
| `input`    | केस के इनपुट फ़ील्ड वाली एक dict                                     |
| `output`   | केस के लिए आपके `run(artifact, input)` ने जो लौटाया                    |
| `expected` | केस का अपेक्षित मान, या `None` जब केस में अपेक्षित मान न हो             |

अगर आपका फ़ंक्शन केवल दो आर्गुमेंट लेता है, तो Skynet `score(input, output)` को कॉल करता है।

DSPy प्रोग्राम के लिए, Skynet DSPy मेट्रिक कॉल को इसी फ़ंक्शन के अनुसार ढाल देता है। आप हर तरह के आर्टिफ़ैक्ट के लिए एक ही मेट्रिक लिखते हैं। [ऑप्टिमाइज़ेशन कैसे काम करता है](/docs/concepts/optimization) पढ़ें।

## रिटर्न मान

`score` को एक जोड़ी लौटानी होगी (दो आइटम वाला tuple या list):

1. **स्कोर।** एक संख्या। ऑप्टिमाइज़र इसे बढ़ाने की कोशिश करता है।
2. **साइड इन्फ़ो।** एक dict। इसमें `"feedback"` key होनी चाहिए, जिसका मान एक string हो।

साइड इन्फ़ो में `"scores"` key भी हो सकती है। यह नामों को संख्याओं से जोड़ती है, उदाहरण के लिए `{"format": 1.0, "accuracy": 0.5}`।

अगर रिटर्न मान का आकार अलग है, तो कॉन्ट्रैक्ट जाँच **मेट्रिक का रिटर्न मान** पर विफल होती है।

| रिटर्न मान                                              | मान्य                                  |
| ------------------------------------------------------ | -------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | हाँ                                     |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | हाँ                                     |
| `0.8`                                                  | नहीं। कोई साइड इन्फ़ो नहीं है।              |
| `(0.8, "Close.")`                                      | नहीं। साइड इन्फ़ो dict नहीं है।              |
| `(0.8, {})`                                            | नहीं। कोई feedback string नहीं है।          |
| `("0.8", {"feedback": "OK"})`                          | नहीं। स्कोर एक string है।                   |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | नहीं। एक नामित स्कोर संख्या नहीं है।          |

## उपयोगी फ़ीडबैक लिखें

GEPA, DSPy GEPA और Auto अगला संस्करण लिखने के लिए फ़ीडबैक टेक्स्ट पढ़ते हैं। बताएँ कि क्या गलत है और सही आउटपुट कैसा दिखता है।

| कमज़ोर फ़ीडबैक | उपयोगी फ़ीडबैक                                                                  |
| ------------- | ----------------------------------------------------------------------------- |
| "Wrong."      | "Expected 'billing', got 'other'. The message mentions a charge."             |
| "Bad format." | "The output is not valid JSON. It has a trailing comma after the last field." |

> [!NOTE]
> ऑप्टिमाइज़र आपके मेट्रिक का सोर्स कभी नहीं देखता। DSPy ऑप्टिमाइज़र में, रिफ़्लेक्शन मॉडल केवल फ़ीडबैक टेक्स्ट देखता है। आपके अपेक्षित मान मेट्रिक के पास ही रहते हैं।

## उदाहरण

### सटीक मिलान

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected` एक dict है, जिसकी keys आपके आउटपुट कॉलम के नाम हैं। [डेटासेट और विभाजन](/docs/concepts/datasets) पढ़ें।

### कई जाँचें

```python tab="metric.py"
import json


def score(input, output, expected=None):
    try:
        data = json.loads(output)
    except ValueError as exc:
        return 0.0, {"feedback": f"Output is not valid JSON: {exc}", "scores": {"format": 0.0, "fields": 0.0}}
    want = expected["fields"]
    found = [key for key in want if key in data]
    fields = len(found) / len(want) if want else 1.0
    missing = [key for key in want if key not in data]
    feedback = "All fields present." if not missing else f"Missing fields: {', '.join(missing)}."
    return 0.5 + 0.5 * fields, {"feedback": feedback, "scores": {"format": 1.0, "fields": fields}}
```

### जज के रूप में मॉडल

मेट्रिक किसी मॉडल को कॉल कर सकता है। `skynet` हेल्पर मॉड्यूल से `llm` इम्पोर्ट करें। मेट्रिक से की गई कॉल मेट्रिक मॉडल पर जाती है।

```python tab="metric.py"
from skynet import llm


def score(input, output, expected=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {input['question']}\nAnswer: {output}",
        model="openai/gpt-4.1-mini",
        temperature=0,
        max_tokens=512,
        reasoning_effort="low",
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

मेट्रिक अपने मॉडल को अपनी `llm()` कॉल में तय करता है। दिखाए गए चारों आर्ग्युमेंट वैकल्पिक हैं। `model=` के बिना मेट्रिक रिफ़्लेक्शन मॉडल का उपयोग करता है।

## सटीकता, गति और लागत

मेट्रिक सटीकता को मापे गए रन समय और मॉडल लागत के साथ मिला सकता है। मॉड्यूल स्तर पर `REWARD_SPEC` परिभाषित करें और `reward` का मान लौटाएँ।

```python tab="metric.py"
from skynet import reward

REWARD_SPEC = {
    "weights": {"accuracy": 0.7, "speed": 0.2, "cost": 0.1},
    "accuracy": {},
    "speed": {"reference": 2000},
    "cost": {"reference": 0.01},
}


def score(input, output, expected=None):
    ok = output == expected["label"]
    return reward(REWARD_SPEC, 1.0 if ok else 0.0, "Correct." if ok else "Wrong label.")
```

- `weights` में `accuracy`, `speed` और `cost` होने चाहिए। हर weight 0 या उससे अधिक होता है, और सभी 0 नहीं हो सकते। Skynet उन्हें इस तरह स्केल करता है कि उनका योग 1 हो।
- `speed.reference` मिलीसेकंड में है। `cost.reference` अमेरिकी डॉलर में है। दोनों धनात्मक होने चाहिए।
- `accuracy.reference` के बिना, सटीकता 0 से 1 तक का स्कोर है। इसके साथ, सटीकता एक त्रुटि है, और कम मान बेहतर है।

Skynet समय और लागत खुद मापता है। रन कोड उन्हें बदल नहीं सकता। अगर Skynet समय या लागत नहीं माप पाता, तो उस हिस्से का स्कोर 0 होता है और फ़ीडबैक में यह बताया जाता है।

मेट्रिक `skynet` के `observation()` और `service_cost()` से मापे गए मान सीधे भी पढ़ सकता है।

## सीमाएँ

| आइटम                   | मान                                 |
| ---------------------- | ---------------------------------- |
| मेट्रिक टाइमआउट          | डिफ़ॉल्ट रूप से 60 सेकंड, अधिकतम 600   |
| कॉन्ट्रैक्ट जाँच के सैंपल   | 3 केस                               |

## संबंधित पेज

- [डेटासेट और विभाजन](/docs/concepts/datasets)
- [इंजन](/docs/concepts/engines)
- [रन सबमिट करें](/docs/guides/submit)
