Skynet, Optimize Anything'dir: her çalıştırma bir depoyu optimize eder. Bir motor deponun yeni sürümlerini önerir, puanlayıcınız her sürümü puanlar ve Skynet en iyisini saklar.

## Ortak sözleşme

Her çalıştırmanın bir deposu ve bir puanlayıcısı vardır. Puanlayıcı bir Python fonksiyonudur. Deponun bir sürümünün yolunu ve bir vakayı alır.

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.serve(case["input"])
    ok = output == case["expected"]
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `repo_path`, kurulum komutu çalıştıktan sonra deponun bir sürümünün klasörüdür.
- `case` bir dict'tir. `case["input"]` bir vakanın girdi alanlarını tutar. `case["expected"]` beklenen değeri tutar; vakanın beklenen değeri yoksa `None` olur.
- `score` bir çift döndürür: bir sayı ve `"feedback"` dizesi içeren bir dict.

Deponun kökünde `def serve(input)` içeren bir `serve.py` dosyası vardır. Puanlayıcı bu fonksiyonu çağırabilir. Skynet, çalıştırmadan sonra en iyi sürümü sunmak için onu kullanır.

> [!NOTE]
> Sürümleri yazan kodlama ajanı, puanlayıcının kaynak kodunu, beklenen değerleri veya çalıştırma gizli anahtarlarını göremez.

Puanlayıcı sözleşmesinin tamamı için [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

## Kodunuzdan model çağrıları

Depo kodunuz ve puanlayıcınız, Skynet'in model ağ geçidi üzerinden bir model çağırabilir. Sandbox, ortam değişkenlerinde OpenAI uyumlu bir uç nokta verir:

| Değişken             | Ne tutar                                                           |
| -------------------- | ------------------------------------------------------------------ |
| `OPENAI_BASE_URL`    | Ağ geçidi URL'si. `APP_GATEWAY_URL` aynı değeri tutar.             |
| `APP_API_KEY`        | Yalnızca bu çalıştırma için bir belirteç. Sağlayıcı anahtarı değildir. |
| `APP_MODEL`          | Gönderilecek model adı. Çalıştırmanın puanlayıcı modelidir.        |
| `ANTHROPIC_BASE_URL` | Anthropic SDK'ları için ağ geçidi URL'si.                          |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["APP_API_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["APP_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Skynet'in anahtarlarıyla yapılan çağrılar çalıştırma bütçesine göre ölçülür.
- Kendi anahtarınızla (BYOK) çağrı sağlayıcı anahtarınıza gider. Skynet model ücreti almaz.
- Ağ geçidi başka herhangi bir model için gelen istekleri reddeder.
- Çalıştırma gizli anahtarları kodunuza ve puanlayıcınıza ortam değişkenleri olarak ulaşır. Değişken adı, gizli anahtarın adıdır. Skynet gizli değerleri çıktıda gizler.

Sandbox'ın başka bir ağ erişimi yoktur.

## Motorlar

Her motor depolarla çalışır:

- **Auto** varsayılandır. Birkaç motoru dener, ardından en iyi sürümden GEPA ile devam eder.
- **GEPA**, sürümleri yansıtma yoluyla evrimleştirir ve bir Pareto cephesi tutar.
- **Best-of-N**, yansıtma modelinden bağımsız öneriler ister ve en iyisini saklar.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** ve **ShinkaEvolve** sürüm önermek için bir kodlama ajanı kullanır.

Motorlar yalnızca izin verdiğiniz yollardaki dosyaları değiştirir. Her motor ve ayarları için [Motorlar](/docs/concepts/engines) sayfasını okuyun.

## Auto

**Auto**, her çalıştırma için sihirbazdaki varsayılan seçimdir.

1. Auto beş motorla keşfeder: GEPA, AutoResearch, Meta-Harness, AutoSaddler ve ShinkaEvolve.
2. Bu motorların bulduğu en iyi sürümü seçer.
3. Bütçe bitene kadar o sürümden GEPA ile devam eder.

Auto, puanlayıcınızın geri bildirim metnini okur.

## Başlangıç sürümü nereden gelir

| Durumunuz                            | Kullanın                                                                                  |
| ------------------------------------ | ----------------------------------------------------------------------------------------- |
| Bir GitHub deposunda kodunuz var     | **Bir depo kullan**. Seçtiğiniz dal veya commit'teki kod başlangıçtır.                    |
| Yalnızca bir göreviniz ve veriniz var | **Yeni depo oluştur**. Skynet ilk çözümü yazar. Bu çözüm başlangıçtır.               |

> [!WARNING]
> GEPA ve Auto geri bildirim metninden öğrenir. Geri bildiriminiz her zaman aynı dizeyse bu optimize edicilerin çalışacağı bilgi azalır.

## İlgili sayfalar

- [Motorlar](/docs/concepts/engines)
- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Metrikler ve puanlayıcılar](/docs/concepts/scorers)
- [Çalıştırma gönderme](/docs/guides/submit)
