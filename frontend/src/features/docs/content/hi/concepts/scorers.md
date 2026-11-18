स्कोरर Skynet को बताता है कि आपकी रिपॉज़िटरी का एक संस्करण कितना अच्छा है। यह `scorer.py` में लिखा एक Python फ़ंक्शन है। हर इंजन इसी एक फ़ंक्शन को कॉल करता है। यह पेज कॉन्ट्रैक्ट, रिटर्न मान और उदाहरण बताता है।

## कॉन्ट्रैक्ट

```python
def score(repo_path, case):
    ...
    return {"score": score_value, "feedback": "..."}
```

| आर्गुमेंट    | इसमें क्या होता है                                                              |
| ----------- | ------------------------------------------------------------------------------ |
| `repo_path` | रिपॉज़िटरी के एक संस्करण का फ़ोल्डर, सेटअप कमांड चलने के बाद                       |
| `case`      | एक dict। `case["input"]` में इनपुट फ़ील्ड होते हैं। `case["expected"]` में अपेक्षित मान होता है, या `None` |

अगर आपका फ़ंक्शन केवल एक आर्गुमेंट लेता है, तो Skynet `score(repo_path)` को कॉल करता है। यह रूप तब इस्तेमाल करें जब स्कोरर अपने खुद के टेस्ट चलाता है और उसे केसों की ज़रूरत नहीं है।

स्कोरर सैंडबॉक्स में, संस्करण के फ़ोल्डर में चलता है। यह रिपॉज़िटरी से कोड इम्पोर्ट कर सकता है, कोई कमांड चला सकता है, या `serve.py` से `serve()` को कॉल कर सकता है। [ऑप्टिमाइज़ेशन कैसे काम करता है](/docs/concepts/optimization) पढ़ें।

## रिटर्न मान

`score` एक object लौटाता है:

```python
{
    "score": 0.78,
    "feedback": "Misses short names.",
    "scores": {
        "precision": {"score": 0.90},
        "recall": {"score": 0.69, "feedback": "Misses names under 4 letters."},
    },
}
```

- `"score"` ज़रूरी है। यह एक सीमित संख्या है। सिर्फ़ यही स्कोर संस्करणों का क्रम तय करता है।
- `"feedback"` वैकल्पिक टेक्स्ट है। इसे देने की सलाह दी जाती है, क्योंकि GEPA और Auto इससे सीखते हैं। फ़ीडबैक के बिना इंजन सिर्फ़ स्कोर देखते हैं, और रन लॉग यह बात एक बार बताता है।
- `"scores"` वैकल्पिक है। यह नामों को नामित स्कोर से जोड़ती है। हर नामित स्कोर एक object है, जिसमें उसका अपना `"score"` और एक वैकल्पिक `"feedback"` होता है। हर इंजन हर नामित स्कोर को अपने अलग लक्ष्य के रूप में इस्तेमाल करता है।

कोई भी दूसरा रूप या key एक त्रुटि है। जाँच गलत हिस्सा और मान्य रूप दिखाती है।

| रिटर्न मान | मान्य |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | हाँ |
| `{"score": 0.8}` | हाँ। कोई फ़ीडबैक नहीं है। |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | हाँ |
| `0.8` | नहीं। मान एक object नहीं है। |
| `(0.8, "Close. Missing the date.")` | नहीं। मान एक object नहीं है। |
| `{"score": "0.8"}` | नहीं। स्कोर एक string है। |
| `{"score": 0.8, "scores": {"format": 1.0}}` | नहीं। नामित स्कोर एक object होना चाहिए। |
| `{"score": 0.8, "feedbak": "OK"}` | नहीं। `feedbak` key अज्ञात है। |

## उपयोगी फ़ीडबैक लिखें

GEPA और Auto अगला संस्करण लिखने के लिए फ़ीडबैक टेक्स्ट पढ़ते हैं। बताएँ कि क्या गलत है और सही आउटपुट कैसा दिखता है।

| कमज़ोर फ़ीडबैक | उपयोगी फ़ीडबैक                                                                   |
| ------------- | ------------------------------------------------------------------------------- |
| "गलत।"        | "अपेक्षित 'billing' था, मिला 'other'। संदेश में एक शुल्क का ज़िक्र है।"               |
| "खराब फ़ॉर्मैट।" | "आउटपुट मान्य JSON नहीं है। आखिरी फ़ील्ड के बाद एक अतिरिक्त कॉमा है।"               |

> [!NOTE]
> कोडिंग एजेंट आपके स्कोरर का सोर्स, अपेक्षित मान या रन के सीक्रेट कभी नहीं देखता। वह सिर्फ़ स्कोर और फ़ीडबैक टेक्स्ट देखता है।

## उदाहरण

### सटीक मिलान

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]` एक dict है, जिसकी key आपके आउटपुट कॉलम का नाम है। [डेटासेट और विभाजन](/docs/concepts/datasets) पढ़ें।

### रिपॉज़िटरी के टेस्ट चलाएँ

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": result.stdout[-2000:]}
```

### जज के रूप में मॉडल

स्कोरर किसी मॉडल को कॉल कर सकता है। `skynet` हेल्पर मॉड्यूल से `llm` इम्पोर्ट करें। कॉल Skynet के मॉडल गेटवे से होकर रन के स्कोरर मॉडल तक जाती है।

```python tab="scorer.py"
import importlib.util

from skynet import llm


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    answer = module.serve(case["input"])
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {answer}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

स्कोरर `OPENAI_BASE_URL`, `APP_API_KEY` और `APP_MODEL` एनवायरनमेंट वेरिएबल के साथ कोई भी OpenAI-संगत क्लाइंट भी इस्तेमाल कर सकता है। गेटवे सिर्फ़ रन का स्कोरर मॉडल स्वीकार करता है।

## मॉडल कॉल और सीक्रेट

- Skynet की कुंजियों पर हुई कॉल रन के बजट से मापी जाती हैं। आपकी अपनी कुंजी (BYOK) के साथ, कॉल आपके प्रदाता की कुंजी पर जाती है, और Skynet कोई मॉडल शुल्क नहीं लेता। [बिलिंग](/docs/concepts/billing) पढ़ें।
- रन के सीक्रेट स्कोरर तक एनवायरनमेंट वेरिएबल के रूप में पहुँचते हैं। वेरिएबल का नाम सीक्रेट का नाम है, उदाहरण के लिए `os.environ["SEARCH_API_KEY"]`।
- Skynet स्कोरर के आउटपुट और लॉग में सीक्रेट मान छिपाता है।

## सीमाएँ

| आइटम          | मान                                       |
| ------------- | ----------------------------------------- |
| स्कोरर टाइमआउट | डिफ़ॉल्ट रूप से 60 सेकंड, अधिकतम 600         |

## संबंधित पेज

- [डेटासेट और विभाजन](/docs/concepts/datasets)
- [इंजन](/docs/concepts/engines)
- [रन सबमिट करें](/docs/guides/submit)
