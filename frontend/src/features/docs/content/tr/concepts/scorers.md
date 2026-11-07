Puanlayıcı, Skynet'e deponuzun bir sürümünün ne kadar iyi olduğunu söyler. `scorer.py` içinde bir Python fonksiyonudur. Her motor aynı fonksiyonu çağırır. Bu sayfa sözleşmeyi, dönüş değerini ve örnekleri verir.

## Sözleşme

```python
def score(repo_path, case):
    ...
    return score_value, {"feedback": "..."}
```

| Argüman     | İçeriği                                                                      |
| ----------- | ---------------------------------------------------------------------------- |
| `repo_path` | Kurulum komutu çalıştıktan sonra deponun bir sürümünün klasörü               |
| `case`      | Bir dict. `case["input"]` girdi alanlarını tutar. `case["expected"]` beklenen değeri veya `None` tutar |

Fonksiyonunuz yalnızca bir argüman alıyorsa Skynet `score(repo_path)` çağırır. Puanlayıcı kendi testlerini çalıştırıyorsa ve vakalara ihtiyacı yoksa bu biçimi kullanın.

Puanlayıcı sandbox içinde, sürümün klasöründe çalışır. Depodan kod içe aktarabilir, bir komut çalıştırabilir veya `serve.py` içindeki `serve()` fonksiyonunu çağırabilir. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfasını okuyun.

## Dönüş değeri

`score`, geri bildirimli bir puan döndürmelidir. Şu biçimler geçerlidir:

1. Bir çift: bir sayı ve bir geri bildirim string'i ya da `"feedback"` anahtarı olan bir dict.
2. `"score"` altında bir sayı ve `"feedback"` altında bir string olan bir dict.

Geri bildirim boş olmayan bir string olmalıdır.

Yan bilgide bir `"scores"` anahtarı da olabilir. Bu anahtar adları adlandırılmış puanlara eşler. Her adlandırılmış puanın kendi puanı ve geri bildirimi vardır, örneğin `{"format": {"score": 1.0, "feedback": "Valid JSON."}}`.

Yan bilgide aynı biçimde bir `"cases"` anahtarı da olabilir. Her girdi bir vakanın sonucudur. Her motor her vakayı ayrı bir hedef olarak kullanır.

Dönüş değerinin biçimi farklıysa kontrol başarısız olur ve nedenini gösterir.

| Dönüş değeri                                                                    | Geçerli mi                                |
| ------------------------------------------------------------------------------- | ----------------------------------------- |
| `(0.8, {"feedback": "Close. Missing the date."})`                               | Evet                                      |
| `(0.8, "Close. Missing the date.")`                                             | Evet                                      |
| `{"score": 1.0, "feedback": "OK", "scores": {"format": {"score": 1.0, "feedback": "OK"}}}` | Evet                           |
| `0.8`                                                                           | Hayır. Geri bildirim yok.                 |
| `(0.8, {})`                                                                     | Hayır. Geri bildirim string'i yok.        |
| `("0.8", {"feedback": "OK"})`                                                   | Hayır. Puan bir string.                   |
| `(0.8, {"feedback": "OK", "scores": {"a": {"score": "high", "feedback": "OK"}}})` | Hayır. Adlandırılmış bir puan sayı değil. |

## Faydalı geri bildirim yazın

GEPA ve Auto, bir sonraki sürümü yazmak için geri bildirim metnini okur. Neyin yanlış olduğunu ve doğru bir çıktının neye benzediğini söyleyin.

| Zayıf geri bildirim | Faydalı geri bildirim                                                                 |
| ------------------- | ------------------------------------------------------------------------------------- |
| "Yanlış."           | "'billing' bekleniyordu, 'other' geldi. Mesaj bir ücretten söz ediyor."               |
| "Kötü biçim."       | "Çıktı geçerli bir JSON değil. Son alandan sonra fazladan bir virgül var."            |

> [!NOTE]
> Kodlama ajanı puanlayıcınızın kaynak kodunu, beklenen değerleri veya çalıştırma gizli anahtarlarını hiçbir zaman görmez. Yalnızca puanları ve geri bildirim metnini görür.

## Örnekler

### Tam eşleşme

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = module.serve(case["input"])
    want = case["expected"]["label"]
    if got == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {got!r}."}
```

`case["expected"]`, Çıktı sütununuzun adıyla anahtarlanmış bir dict'tir. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

### Deponun testlerini çalıştırın

```python tab="scorer.py"
import subprocess


def score(repo_path):
    result = subprocess.run(["python", "-m", "pytest", "-q"], cwd=repo_path, capture_output=True, text=True)
    if result.returncode == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": result.stdout[-2000:]}
```

### Hakem olarak model

Puanlayıcı bir model çağırabilir. `skynet` yardımcı modülünden `llm` içe aktarın. Çağrı, Skynet'in model ağ geçidi üzerinden çalıştırmanın puanlayıcı modeline gider.

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
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

Puanlayıcı, `OPENAI_BASE_URL`, `APP_API_KEY` ve `APP_MODEL` ortam değişkenleriyle herhangi bir OpenAI uyumlu istemciyi de kullanabilir. Ağ geçidi yalnızca çalıştırmanın puanlayıcı modelini kabul eder.

## Model çağrıları ve gizli anahtarlar

- Skynet'in anahtarlarıyla yapılan çağrılar çalıştırma bütçesine göre ölçülür. Kendi anahtarınızla (BYOK) çağrı sağlayıcı anahtarınıza gider ve Skynet model ücreti alınmaz. [Faturalandırma](/docs/concepts/billing) sayfasını okuyun.
- Çalıştırma gizli anahtarları puanlayıcıya ortam değişkenleri olarak ulaşır. Değişken adı, gizli anahtarın adıdır, örneğin `os.environ["SEARCH_API_KEY"]`.
- Skynet gizli değerleri puanlayıcı çıktısında ve günlüklerde gizler.

## Limitler

| Öğe                         | Değer                                     |
| --------------------------- | ----------------------------------------- |
| Puanlayıcı zaman aşımı      | Varsayılan olarak 60 saniye, en fazla 600 |

## İlgili sayfalar

- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Motorlar](/docs/concepts/engines)
- [Çalıştırma gönderin](/docs/guides/submit)
