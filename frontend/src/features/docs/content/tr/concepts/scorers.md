Puanlayıcı, Skynet'e deponuzun bir sürümünün ne kadar iyi olduğunu söyler. `scorer.py` içinde bir Python fonksiyonudur. Her motor aynı fonksiyonu çağırır. Bu sayfa sözleşmeyi, dönüş değerini ve örnekleri verir.

## Sözleşme

```python
def score(repo_path, case, output=None):
    ...
    return {"score": score_value, "feedback": "..."}
```

| Argüman     | İçeriği                                                                      |
| ----------- | ---------------------------------------------------------------------------- |
| `repo_path` | Yalnızca metrik dosyalarınızı tutan bir klasör. Başka bir depo dosyası veya sürüm kodu tutmaz |
| `case`      | Bir dict. `case["input"]` girdi alanlarını tutar. `case["expected"]` beklenen değeri veya `None` tutar |
| `output`    | `[run]` giriş noktasının vaka için döndürdüğü değer veya dosyada `[run]` yoksa `None` |

Deponun `ascent.toml` dosyasında bir `[run]` bölümü veya bir `[target.<name>]` aşaması olmalıdır. `[run]` giriş noktası (`entry`), bir sürümü bir vaka üzerinde kendi sandbox'ında çalıştırır. Skynet ikisi de olmayan bir dosyayı reddeder, çünkü her sürüm aynı puanı alırdı.

- Bir fonksiyon giriş noktası, örneğin `entry = "serve.py:serve"`, `case["input"]` değerini argüman olarak alır. `output`, onun döndürdüğü değerdir.
- Bir komut giriş noktası, örneğin `entry = ["python", "run_tests.py"]`, `ASCENT_INPUT` içindeki yoldaki JSON dosyasından `{"input": ...}` değerini okur. `{"output": ...}` değerini `ASCENT_OUTPUT` içindeki yola yazar. `output`, `"output"` altındaki değerdir. Komutun yazdırdıkları yalnızca günlüklere gider.

Puanlayıcı ayrı bir sandbox içinde çalışır. Bu sandbox yalnızca metrik dosyalarınızı tutar. Puanlayıcı sürümün kodunu içe aktaramaz veya çalıştıramaz. Sürümü yalnızca `output` üzerinden veya bir `[target.<name>]` aşamasına yapılan `call()` üzerinden görür. `call` fonksiyonunu `from openascent import call` ile içe aktarın. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfasını okuyun.

## Dönüş değeri

`score` tek bir nesne döndürür:

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

- `"score"` zorunludur. Sonlu bir sayıdır. Sürümleri yalnızca bu puan sıralar.
- `"feedback"` isteğe bağlı bir metindir. Önerilir, çünkü GEPA ve Auto ondan öğrenir. Geri bildirim yoksa motorlar yalnızca puanları görür ve çalıştırma günlüğü bunu bir kez söyler.
- `"scores"` isteğe bağlıdır. Adları adlandırılmış puanlara eşler. Her adlandırılmış puan, kendi `"score"` değeri ve isteğe bağlı bir `"feedback"` içeren bir nesnedir. Her motor her adlandırılmış puanı ayrı bir hedef olarak kullanır.

Başka her biçim veya anahtar bir hatadır. Kontrol yanlış kısmı ve geçerli biçimi gösterir.

| Dönüş değeri | Geçerli mi |
| --- | --- |
| `{"score": 0.8, "feedback": "Close. Missing the date."}` | Evet |
| `{"score": 0.8}` | Evet. Geri bildirim yok. |
| `{"score": 1.0, "scores": {"format": {"score": 1.0}}}` | Evet |
| `0.8` | Hayır. Değer bir nesne değil. |
| `(0.8, "Close. Missing the date.")` | Hayır. Değer bir nesne değil. |
| `{"score": "0.8"}` | Hayır. Puan bir string. |
| `{"score": 0.8, "scores": {"format": 1.0}}` | Hayır. Adlandırılmış bir puan bir nesne olmalıdır. |
| `{"score": 0.8, "feedbak": "OK"}` | Hayır. `feedbak` anahtarı bilinmiyor. |

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

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return {"score": 1.0, "feedback": "Correct."}
    return {"score": 0.0, "feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]`, Çıktı sütununuzun adıyla anahtarlanmış bir dict'tir. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

### Deponun testlerini çalıştırın

`[run]` giriş noktası testleri çalıştırır. Bir test başarısız olduğunda `pytest` sıfır olmayan bir çıkış koduyla çıkar ve sıfır olmayan bir çıkış kodu durumu başarısız yapar. Bu yüzden `run_tests.py` sarmalayıcısı sonucu `ASCENT_OUTPUT` dosyasına yazar ve 0 ile çıkar. Puanlayıcı bu sonucu okur.

```toml tab="ascent.toml"
version = 1

[run]
entry = ["python", "run_tests.py"]
timeout = 300
```

```python tab="run_tests.py"
import json
import os
import subprocess

result = subprocess.run(["python", "-m", "pytest", "-q"], capture_output=True, text=True)
with open(os.environ["ASCENT_OUTPUT"], "w") as f:
    json.dump({"output": {"passed": result.returncode == 0, "log": result.stdout[-2000:]}}, f)
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["passed"]:
        return {"score": 1.0, "feedback": "All tests pass."}
    return {"score": 0.0, "feedback": output["log"]}
```

`[run]` giriş noktası bir durumda başarısız olursa o durumun puanı olmaz. Giriş noktası başlayamazsa, zaman aşımına uğrarsa veya bir sinyal onu durdurursa ya da bir fonksiyon giriş noktası değer döndürmeden önce hata fırlatır veya çıkarsa geri bildirim `Run failed: …` olur ve Skynet puanlayıcıyı çağırmaz. Bir komut giriş noktası, sıfır olmayan bir kodla çıktığında veya geçerli bir çıktı dosyası yazmadığında da başarısız olur. Böyle bir durumu olan sürümün puanı olmaz. O sürüm en iyi sürüm olamaz, hiçbir `[outcome] target` değerine ulaşmaz ve pull request açmaz. Bir başarısızlığın sayı olarak sayılması gerekiyorsa başarısızlığı programınızda yakalayın, bir çıktı yazın ve puanlayıcının onu puanlamasına izin verin. [Depo kuralları](https://github.com/Skynet-ml/skynet/blob/main/docs/repository-constraints.md) sayfasını okuyun.

### Hakem olarak model

Puanlayıcı bir model çağırabilir. `openascent` yardımcı modülünden `llm` içe aktarın. Çağrı, Skynet'in model ağ geçidi üzerinden çalıştırmanın puanlayıcı modeline gider.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from openascent import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return {"score": 1.0 if passed else 0.0, "feedback": str(verdict)}
```

Puanlayıcı, `OPENAI_BASE_URL`, `ASCENT_MODEL_KEY` ve `ASCENT_MODEL` ortam değişkenleriyle herhangi bir OpenAI uyumlu istemciyi de kullanabilir. Ağ geçidi yalnızca çalıştırmanın puanlayıcı modelini kabul eder.

## Model çağrıları ve gizli anahtarlar

- Skynet'in anahtarlarıyla yapılan çağrılar çalıştırma bütçesine göre ölçülür. Kendi anahtarınızla (BYOK) çağrı sağlayıcı anahtarınıza gider ve Skynet model ücreti alınmaz. [Faturalandırma](/docs/concepts/billing) sayfasını okuyun.
- Puanlayıcı, çalıştırma gizli anahtarlarını ortam değişkenleri olarak almaz. Bir gizli anahtar puanlayıcıya yalnızca `[metric] services` listesindeki bir `[services.<name>]` tablosu veya `[metric] mcp` dosyasındaki bir sunucu üzerinden ulaşır. Gizli anahtarı tablonun `headers` alanına `${NAME}` olarak yazın. Puanlayıcı isteklerini `ASCENT_SERVICE_<NAME>_URL` içindeki URL'ye gönderir. Skynet gizli anahtarı sandbox'ın dışında ekler.
- Skynet gizli değerleri puanlayıcı çıktısında ve günlüklerde gizler.

## Limitler

| Öğe                         | Değer                                     |
| --------------------------- | ----------------------------------------- |
| Puanlayıcı zaman aşımı      | Varsayılan olarak 60 saniye, en fazla 600 |

## İlgili sayfalar

- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Motorlar](/docs/concepts/engines)
- [Çalıştırma gönderin](/docs/guides/submit)
