Skynet, Optimize Anything'dir: her çalıştırma bir depoyu optimize eder. Bir motor deponun yeni sürümlerini önerir, puanlayıcınız her sürümü puanlar ve Skynet en iyisini saklar.

## Ortak sözleşme

Her çalıştırmanın bir deposu, `[run]` giriş noktası veya `[target.<name>]` aşaması içeren bir `ascent.toml` dosyası ve bir puanlayıcısı vardır. `[run]` giriş noktası, deponun bir sürümünü bir vaka üzerinde çalıştırır. Puanlayıcı bir Python fonksiyonudur. Giriş noktasının çıktısını ve vakayı alır.

```toml tab="ascent.toml"
version = 1

[run]
entry = "serve.py:serve"
```

```python tab="scorer.py"
def score(repo_path, case, output=None):
    ok = output == case["expected"]
    return {"score": 1.0 if ok else 0.0, "feedback": "Match." if ok else "No match."}
```

- `output`, `[run]` giriş noktasının vaka için döndürdüğü değerdir.
- `case` bir dict'tir. `case["input"]` bir vakanın girdi alanlarını tutar. `case["expected"]` beklenen değeri tutar; vakanın beklenen değeri yoksa `None` olur.
- `repo_path`, yalnızca metrik dosyalarınızı tutan bir klasördür.
- `score` tek bir nesne döndürür: `"score"` altında bir sayı ve isteğe bağlı bir `"feedback"` dizesi.

Deponun kökünde `def serve(input)` içeren bir `serve.py` dosyası vardır. `[run]` giriş noktası onu kendi sandbox'ında çağırır. Skynet, çalıştırmadan sonra en iyi sürümü sunmak için onu kullanır.

Puanlayıcı ayrı bir sandbox içinde çalışır. Bu sandbox yalnızca metrik dosyalarınızı tutar, sürüm kodunu tutmaz, bu yüzden puanlayıcı `serve.py` dosyasını içe aktaramaz. Sürümü yalnızca `output` üzerinden veya bir `[target.<name>]` aşamasına yapılan `call()` üzerinden görür. `call` fonksiyonunu `from openascent import call` ile içe aktarın. `[run]` giriş noktası veya `[target.<name>]` aşaması olmadan Skynet `ascent.toml` dosyasını reddeder.

> [!NOTE]
> Sürümleri yazan kodlama ajanı, puanlayıcının kaynak kodunu, beklenen değerleri veya çalıştırma gizli anahtarlarını göremez.

Puanlayıcı sözleşmesinin tamamı için [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

## Kodunuzdan model çağrıları

Depo kodunuz ve puanlayıcınız, Skynet'in model ağ geçidi üzerinden bir model çağırabilir. Sandbox, ortam değişkenlerinde OpenAI uyumlu bir uç nokta verir:

| Değişken             | Ne tutar                                                           |
| -------------------- | ------------------------------------------------------------------ |
| `OPENAI_BASE_URL`    | Ağ geçidi URL'si. `ASCENT_MODEL_URL` aynı değeri tutar.            |
| `ASCENT_MODEL_KEY`   | Yalnızca bu çalıştırma için bir belirteç. Sağlayıcı anahtarı değildir. |
| `ASCENT_MODEL`       | Gönderilecek model adı. Çalıştırmanın puanlayıcı modelidir.        |
| `ANTHROPIC_BASE_URL` | Anthropic SDK'ları için ağ geçidi URL'si.                          |

```python tab="pipeline.py"
import os

from openai import OpenAI

client = OpenAI(base_url=os.environ["OPENAI_BASE_URL"], api_key=os.environ["ASCENT_MODEL_KEY"])


def label(message):
    response = client.chat.completions.create(
        model=os.environ["ASCENT_MODEL"],
        messages=[{"role": "user", "content": f"Label as billing, bug or other: {message}"}],
    )
    return response.choices[0].message.content
```

- Skynet'in anahtarlarıyla yapılan çağrılar çalıştırma bütçesine göre ölçülür.
- Kendi anahtarınızla (BYOK) çağrı sağlayıcı anahtarınıza gider. Skynet model ücreti almaz.
- Ağ geçidi başka herhangi bir model için gelen istekleri reddeder.
- Kodunuz ve puanlayıcınız çalıştırma gizli anahtarlarını ortam değişkenleri olarak almaz. Bir gizli anahtar bir role yalnızca rolün `[run] services` veya `[metric] services` içinde listelediği bir `[services.<name>]` tablosu veya rolün `mcp` dosyasındaki bir sunucu üzerinden ulaşır. Gizli anahtarı tablonun `headers` alanına `${NAME}` olarak yazın. Skynet gizli anahtarı sandbox'ın dışında ekler ve gizli değerleri çıktıda gizler.

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
| Bir GitHub, GitLab veya Bitbucket deposunda kodunuz var     | **Bir depo kullan**. Seçtiğiniz dal veya commit'teki kod başlangıçtır.                    |
| Yalnızca bir göreviniz ve veriniz var | **Yeni depo oluştur**. Skynet ilk çözümü yazar. Bu çözüm başlangıçtır.               |

> [!WARNING]
> GEPA ve Auto geri bildirim metninden öğrenir. Geri bildiriminiz her zaman aynı dizeyse bu optimize edicilerin çalışacağı bilgi azalır.

## İlgili sayfalar

- [Motorlar](/docs/concepts/engines)
- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Metrikler ve puanlayıcılar](/docs/concepts/scorers)
- [Çalıştırma gönderme](/docs/guides/submit)
