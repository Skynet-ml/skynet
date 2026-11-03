Metrik, Skynet'e bir çıktının ne kadar iyi olduğunu söyler. `metric.py` içinde bir Python fonksiyonudur. Her motor aynı fonksiyonu çağırır. Bu sayfa sözleşmeyi, dönüş değerini ve örnekleri verir.

## Sözleşme

```python
def score(input, output, expected=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argüman    | İçeriği                                                              |
| ---------- | -------------------------------------------------------------------- |
| `input`    | Vakanın girdi alanlarını içeren bir dict                             |
| `output`   | `run(artifact, input)` fonksiyonunuzun bu vaka için döndürdüğü değer |
| `expected` | Vakanın beklenen değeri; vakada yoksa `None`                         |

Fonksiyonunuz yalnızca iki argüman alıyorsa Skynet `score(input, output)` çağrısını yapar.

Bir DSPy programı için Skynet, DSPy metrik çağrısını bu aynı fonksiyona uyarlar. Her yapıt türü için tek bir metrik yazarsınız. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfasını okuyun.

## Dönüş değeri

`score` bir çift döndürmelidir (iki öğeli bir tuple veya liste):

1. **Puan.** Bir sayı. Optimize edici bu sayıyı yükseltmeye çalışır.
2. **Yan bilgi.** Bir dict. Değeri string olan bir `"feedback"` anahtarı olmalıdır.

Yan bilgide bir `"scores"` anahtarı da olabilir. Bu anahtar adları sayılara eşler, örneğin `{"format": 1.0, "accuracy": 0.5}`.

Dönüş değerinin biçimi farklıysa sözleşme kontrolü **Metriğin dönüş değeri** adımında başarısız olur.

| Dönüş değeri                                           | Geçerli mi                            |
| ------------------------------------------------------ | ------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`      | Evet                                  |
| `[1.0, {"feedback": "OK", "scores": {"format": 1.0}}]` | Evet                                  |
| `0.8`                                                  | Hayır. Yan bilgi yok.                 |
| `(0.8, "Close.")`                                      | Hayır. Yan bilgi bir dict değil.      |
| `(0.8, {})`                                            | Hayır. Geri bildirim string'i yok.    |
| `("0.8", {"feedback": "OK"})`                          | Hayır. Puan bir string.               |
| `(0.8, {"feedback": "OK", "scores": {"a": "high"}})`   | Hayır. Adlandırılmış bir puan sayı değil. |

## Faydalı geri bildirim yazın

GEPA, DSPy GEPA ve Auto, bir sonraki sürümü yazmak için geri bildirim metnini okur. Neyin yanlış olduğunu ve doğru bir çıktının neye benzediğini söyleyin.

| Zayıf geri bildirim | Faydalı geri bildirim                                                                 |
| ------------------- | ------------------------------------------------------------------------------------- |
| "Yanlış."           | "'billing' bekleniyordu, 'other' geldi. Mesaj bir ücretten söz ediyor."               |
| "Kötü biçim."       | "Çıktı geçerli bir JSON değil. Son alandan sonra fazladan bir virgül var."            |

> [!NOTE]
> Optimize edici metriğinizin kaynak kodunu hiçbir zaman görmez. Bir DSPy optimize edicisiyle yansıtma modeli yalnızca geri bildirim metnini görür. Beklenen değerleriniz metrikte kalır.

## Örnekler

### Tam eşleşme

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`expected`, anahtarı Çıktı sütununuzun adı olan bir dict'tir. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

### Birden çok kontrol

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

### Hakem olarak model

Metrik bir model çağırabilir. `skynet` yardımcı modülünden `llm` içe aktarın. Metrikten yapılan bir çağrı metrik modeline gider.

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

Metrik, modelini kendi `llm()` çağrısında belirler. Gösterilen dört argümanın tümü isteğe bağlıdır. `model=` olmadan metrik yansıtma modelini kullanır.

## Doğruluk, hız ve maliyet

Bir metrik, doğruluğu ölçülen çalışma süresi ve model maliyetiyle harmanlayabilir. Modül düzeyinde `REWARD_SPEC` tanımlayın ve `reward` değerini döndürün.

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

- `weights` içinde `accuracy`, `speed` ve `cost` bulunmalıdır. Her ağırlık 0 veya daha büyüktür ve hepsi 0 olamaz. Skynet ağırlıkları toplamları 1 olacak şekilde ölçekler.
- `speed.reference` milisaniye cinsindendir. `cost.reference` ABD doları cinsindendir. İkisi de pozitif olmalıdır.
- `accuracy.reference` olmadan doğruluk, 0 ile 1 arasında bir puandır. Bu değerle doğruluk bir hatadır ve düşük olan daha iyidir.

Süreyi ve maliyeti Skynet kendisi ölçer. Çalıştırma kodu bunları değiştiremez. Skynet süreyi veya maliyeti ölçemezse o bölüm 0 puan alır ve geri bildirim bunu belirtir.

Metrik, ölçümleri `skynet` modülündeki `observation()` ve `service_cost()` ile doğrudan da okuyabilir.

## Limitler

| Öğe                         | Değer                                     |
| --------------------------- | ----------------------------------------- |
| Metrik zaman aşımı          | Varsayılan olarak 60 saniye, en fazla 600 |
| Sözleşme kontrolü örnekleri | 3 vaka                                    |

## İlgili sayfalar

- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Motorlar](/docs/concepts/engines)
- [Çalıştırma gönderin](/docs/guides/submit)
