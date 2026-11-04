Skynet HTTP API'si, betiklerin ve araçların web uygulamasının yaptığı işleri yapmasını sağlar: çalıştırma başlatmak, çalıştırmaları izlemek ve sonuçları okumak. Bu sayfa isteklerin nasıl imzalanacağını, tam başvuru belgesinin nerede olduğunu ve eksiksiz bir örneği gösterir.

> [!TIP]
> Claude Code, Codex veya Cursor gibi bir yapay zekâ ajanı mı kullanıyorsunuz? Onu bunun yerine [MCP](/docs/mcp) üzerinden bağlayın. Ajan bu durumda çalıştırmaları sizin için başlatır ve izler; her çalıştırma için bir harcama limiti kullanır.

## Temel URL

Her isteği şu adrese gönderin:

```text
https://api.skynetml.com
```

## Kimlik doğrulama

API, API belirteçlerini kullanır.

1. Skynet'te **Ayarlar**'ı, ardından **API** sekmesini açın.
2. **Belirteç oluştur**'a tıklayın.
3. Belirteci hemen kopyalayın. Skynet tam belirteci yalnızca bir kez gösterir.

Belirteci her isteğin `Authorization` başlığında gönderin:

```http
Authorization: Bearer skyd_...
```

- Belirteçler `skyd_` ile başlar.
- Her kullanıcının en fazla bir geçerli belirteci olur. Yeni bir belirteç eski belirteci devre dışı bırakır.
- Bir belirteci devre dışı bırakmak için **Belirteci iptal et**'e tıklayın.

> [!WARNING]
> Belirteci bir parola gibi koruyun. Kaynak kodun ve günlüklerin dışında tutun. Skynet'in belirteçleri nasıl sakladığını öğrenmek için [Güvenlik](/docs/security) sayfasını okuyun.

## Etkileşimli başvuru

Tam başvuru belgesi [api.skynetml.com/scalar](https://api.skynetml.com/scalar) adresindedir. Her genel uç noktayı, alanlarını ve yanıtlarını listeler. Bu sayfadan test istekleri gönderebilirsiniz.

Başvuru belgesi `https://api.skynetml.com/openapi.public.json` adresindeki OpenAPI belgesini okur. Kendi dilinizde bir istemci oluşturmak için bu dosyayı kullanın.

## Ana uç noktalar

| Yöntem ve yol                                   | Ne yapar                                                              |
| ----------------------------------------------- | --------------------------------------------------------------------- |
| `GET /models`                                   | Kullanabileceğiniz modelleri listeler.                                |
| `GET /optimizers`                               | Bir kuruluma uyan optimize edicileri listeler.                        |
| `GET /blackbox/engines`                         | Yerel motorları ve her birinin kullanılabilir olup olmadığını listeler. |
| `POST /optimize` | Bir DSPy programı için çalıştırma başlatır. |
| `POST /blackbox/run`                            | Bir GitHub deposu üzerinde çalıştırma başlatır.                       |
| `GET /optimizations`                            | Çalıştırmalarınızı listeler.                                          |
| `GET /optimizations/{optimization_id}`          | Tek bir çalıştırmayı döndürür: durum, ilerleme, günlükler ve sonuç.   |
| `GET /optimizations/{optimization_id}/artifact` | Çalıştırmanın bulduğu en iyi sürümü döndürür.                         |
| `POST /optimizations/{optimization_id}/cancel`  | Bir çalıştırmayı durdurur.                                            |
| `POST /serve/{optimization_id}`                 | En iyi sürümü yeni bir girdi üzerinde çalıştırır.                     |
| `GET /health`                                   | Hizmetin çalışıp çalışmadığını gösterir.                              |

## Bir çalıştırma başlatma

`POST /optimize`, gönderme sihirbazıyla aynı kurulumu alır. Örnek, destek mesajlarını etiketlere ayıran bir DSPy programını iyileştirir.

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

Önemli alanlar:

- `task.artifact.kind` değeri `dspy_program`'dır.
- `data.mode` değeri `pairs` (beklenen çıktılarla birlikte girdiler) veya `single`'dır (tam olarak bir vaka).
- `optimizer.choice` değeri `auto` veya `GET /optimizers` tarafından döndürülen bir optimize edici adıdır.
- `max_cost_cents`, bu çalıştırmanın ABD senti cinsinden harcama limitidir.
- `is_private` varsayılan olarak `false`'tur. Çalıştırmayı özel tutmak için `true` olarak ayarlayın.

`run_code` ve `metric_code` kuralları için [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

### Yanıtlar

| Durum | Anlamı                                                                                                                                           |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `201` | Skynet çalıştırmayı kuyruğa aldı. Gövde `optimization_id` ve `status` içerir.                                                                    |
| `202` | Kurulum kontrolü hâlâ çalışıyor. `Retry-After` başlığındaki saniye kadar bekleyin. Ardından aynı gövdeyi aynı `Idempotency-Key` ile gönderin. |
| `402` | Bakiyeniz çok düşük.                                                                                                                             |
| `409` | Bir kotaya ulaştınız veya kurulum kontrolü güncel değil.                                                                                         |
| `422` | İstek veya çalışma alanı kodu optimize ediciye uymuyor.                                                                                          |

> [!NOTE]
> Her zaman bir `Idempotency-Key` başlığı gönderin. Aynı isteği aynı anahtarla yeniden gönderirseniz Skynet ilk çalıştırmayı döndürür. İkinci bir çalıştırma başlatmaz.

## Bir çalıştırmayı izleme

Durumu nihai olana kadar çalıştırmayı sorgulayın:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

`status` alanı şunlardan biridir:

| Durum                              | Nihai |
| ---------------------------------- | ----- |
| `pending`, `validating`, `running` | Hayır |
| `paused`                           | Hayır |
| `success`                          | Evet  |
| `failed`, `cancelled`, `stopped`   | Evet  |

10 ila 30 saniyede bir sorgulayın. Bir çalıştırma dakikalardan saatlere kadar sürebilir.

[API başvurusu](https://api.skynetml.com/scalar), çalıştırma yanıtının her alanını listeler.

## Sonucu kullanma

Durum `success` olduğunda en iyi sürümü alın:

```bash
curl https://api.skynetml.com/optimizations/$RUN_ID/artifact \
  -H "Authorization: Bearer $SKYNET_TOKEN"
```

En iyi sürümü yeni bir girdi üzerinde çalıştırmak için `POST /serve/{optimization_id}` gönderin. Her puanın ne anlama geldiği için [Sonuçlar](/docs/guides/results) sayfasını okuyun.

## Limitler

API, web uygulamasıyla aynı kotaları, hız limitlerini ve faturalandırmayı kullanır. [Limitler](/docs/limits) ve [Faturalandırma](/docs/concepts/billing) sayfalarını okuyun.
