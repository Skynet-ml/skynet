Puanlayıcı, Skynet'e deponuzun bir sürümünün ne kadar iyi olduğunu söyler. `scorer.py` içinde bir Python fonksiyonudur. Her motor aynı fonksiyonu çağırır. Bu sayfa sözleşmeyi, dönüş değerini ve örnekleri verir.

## Sözleşme

```python
def score(repo_path, case, output=None):
    ...
    return score_value, {"feedback": "..."}
```

| Argüman     | İçeriği                                                                      |
| ----------- | ---------------------------------------------------------------------------- |
| `repo_path` | Yalnızca metrik dosyalarınızı tutan bir klasör. Başka bir depo dosyası veya sürüm kodu tutmaz |
| `case`      | Bir dict. `case["input"]` girdi alanlarını tutar. `case["expected"]` beklenen değeri veya `None` tutar |
| `output`    | `[run]` giriş noktasının vaka için döndürdüğü değer |

Deponun `optimize.toml` dosyasında bir `[run]` bölümü veya bir `[target.<name>]` aşaması olmalıdır. `[run]` giriş noktası (`entry`), bir sürümü bir vaka üzerinde kendi sandbox'ında çalıştırır. Skynet ikisi de olmayan bir dosyayı reddeder, çünkü her sürüm aynı puanı alırdı.

- Bir fonksiyon giriş noktası, örneğin `entry = "serve.py:serve"`, `case["input"]` değerini argüman olarak alır. `output`, onun döndürdüğü değerdir.
- Bir komut giriş noktası, örneğin `entry = ["python", "-m", "pytest", "-q"]`, girdiyi stdin üzerinden JSON olarak alır. `output`, `{"exit_code": ..., "stdout": "..."}` değeridir.

Puanlayıcı ayrı bir sandbox içinde çalışır. Bu sandbox yalnızca metrik dosyalarınızı tutar. Puanlayıcı sürümün kodunu içe aktaramaz veya çalıştıramaz. Sürümü yalnızca `output` üzerinden görür. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfasını okuyun.

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

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    want = case["expected"]["label"]
    if output == want:
        return 1.0, {"feedback": "Correct."}
    return 0.0, {"feedback": f"Expected {want!r}, got {output!r}."}
```

`case["expected"]`, Çıktı sütununuzun adıyla anahtarlanmış bir dict'tir. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

### Deponun testlerini çalıştırın

`[run]` giriş noktası testleri çalıştırır. Puanlayıcı çıkış kodunu ve test çıktısını okur.

```toml tab="optimize.toml"
version = 1

[run]
entry = ["python", "-m", "pytest", "-q"]
timeout = 300
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    if output["exit_code"] == 0:
        return 1.0, {"feedback": "All tests pass."}
    return 0.0, {"feedback": output["stdout"][-2000:]}
```

Bir sürümün `[run]` giriş noktası başarısız olursa Skynet o sürüme kendisi 0 puan verir. Giriş noktası başlayamazsa, zaman aşımına uğrarsa veya bir sinyal onu durdurursa geri bildirim `Run failed: …` olur ve Skynet puanlayıcıyı çağırmaz. Sıfır olmayan bir çıkış kodu başarısızlık değildir: puanlayıcı onu `output` içinde alır.

### Hakem olarak model

Puanlayıcı bir model çağırabilir. `skynet` yardımcı modülünden `llm` içe aktarın. Çağrı, Skynet'in model ağ geçidi üzerinden çalıştırmanın puanlayıcı modeline gider.

```toml tab="optimize.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
from skynet import llm


def score(repo_path, case, output=None):
    verdict = llm(
        "Answer PASS or FAIL, then give one sentence of reasons.",
        input=f"Question: {case['input']['question']}\nAnswer: {output}",
        temperature=0,
        max_tokens=512,
    )
    passed = str(verdict).strip().upper().startswith("PASS")
    return (1.0 if passed else 0.0), {"feedback": str(verdict)}
```

Puanlayıcı, `OPENAI_BASE_URL`, `APP_API_KEY` ve `APP_MODEL` ortam değişkenleriyle herhangi bir OpenAI uyumlu istemciyi de kullanabilir. Ağ geçidi yalnızca çalıştırmanın puanlayıcı modelini kabul eder.

## Model çağrıları ve gizli anahtarlar

- Skynet'in anahtarlarıyla yapılan çağrılar çalıştırma bütçesine göre ölçülür. Kendi anahtarınızla (BYOK) çağrı sağlayıcı anahtarınıza gider ve Skynet model ücreti alınmaz. [Faturalandırma](/docs/concepts/billing) sayfasını okuyun.
- Puanlayıcı, çalıştırma gizli anahtarlarını ortam değişkenleri olarak almaz. Bir gizli anahtar puanlayıcıya yalnızca `[metric] services` listesindeki bir `[services.<name>]` tablosu üzerinden ulaşır. Gizli anahtarı tablonun `headers` alanına `${NAME}` olarak yazın. Puanlayıcı isteklerini `SKYNET_SERVICE_<NAME>_URL` içindeki URL'ye gönderir. Skynet gizli anahtarı sandbox'ın dışında ekler.
- Skynet gizli değerleri puanlayıcı çıktısında ve günlüklerde gizler.

## Limitler

| Öğe                         | Değer                                     |
| --------------------------- | ----------------------------------------- |
| Puanlayıcı zaman aşımı      | Varsayılan olarak 60 saniye, en fazla 600 |

## İlgili sayfalar

- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Motorlar](/docs/concepts/engines)
- [Çalıştırma gönderin](/docs/guides/submit)
