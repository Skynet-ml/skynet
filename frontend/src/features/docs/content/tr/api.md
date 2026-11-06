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
| `POST /blackbox/run`                            | Bir GitHub deposu üzerinde çalıştırma başlatır.                       |
| `GET /optimizations`                            | Çalıştırmalarınızı listeler.                                          |
| `GET /optimizations/{optimization_id}`          | Tek bir çalıştırmayı döndürür: durum, ilerleme, günlükler ve sonuç.   |
| `GET /optimizations/{optimization_id}/artifact` | Çalıştırmanın bulduğu en iyi sürümü döndürür.                         |
| `POST /optimizations/{optimization_id}/cancel`  | Bir çalıştırmayı durdurur.                                            |
| `POST /serve/{optimization_id}`                 | En iyi sürümü yeni bir girdi üzerinde çalıştırır.                     |
| `GET /health`                                   | Hizmetin çalışıp çalışmadığını gösterir.                              |

## Bir çalıştırma başlatma

Skynet, Optimize Anything'dir: her çalıştırma bir depoyu optimize eder. `POST /blackbox/run`, gönderme sihirbazıyla aynı kurulumu alır. Örnek, destek mesajlarını etiketlere ayıran bir depoyu iyileştirir.

```bash tab="curl"
curl https://api.skynetml.com/blackbox/run \
  -H "Authorization: Bearer $SKYNET_TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: support-labels-001" \
  -d @run.json
```

```json tab="run.json"
{
  "name": "Support labels",
  "objective": "Sort each support message into billing, bug or other.",
  "recipe": "repo",
  "target": {
    "kind": "repo",
    "repo": {
      "provider": "github",
      "repository": "your-org/support-labels",
      "branch": "main",
      "editable_paths": ["serve.py", "prompts/"]
    }
  },
  "cases": [
    { "input": { "message": "I was charged twice." }, "expected": { "label": "billing" } },
    { "input": { "message": "The export button does nothing." }, "expected": { "label": "bug" } },
    {
      "input": { "message": "Do you have a Hebrew interface?" },
      "expected": { "label": "other" }
    }
  ],
  "input_fields": ["message"],
  "expected_field": "label",
  "scorer": {
    "kind": "python",
    "metric_code": "import importlib.util\n\n\ndef score(repo_path, case):\n    spec = importlib.util.spec_from_file_location(\"serve\", f\"{repo_path}/serve.py\")\n    module = importlib.util.module_from_spec(spec)\n    spec.loader.exec_module(module)\n    ok = str(module.serve(case[\"input\"])).strip().lower() == case[\"expected\"][\"label\"]\n    return (1.0 if ok else 0.0), {\"feedback\": \"Correct.\" if ok else \"Wrong label.\"}\n",
    "model": { "name": "<model from GET /models>" }
  },
  "strategy": { "mode": "auto" },
  "budget": { "max_scorer_runs": 200 },
  "reflection_model_config": { "name": "<model from GET /models>" },
  "max_cost_cents": 500
}
```

Önemli alanlar:

- `target.repo.repository`, `owner/name` biçimindeki GitHub deposudur. Önce GitHub'ı **Ayarlar → Bağlayıcılar** bölümünde bağlayın.
- `target.repo.editable_paths`, optimize edicinin değiştirebileceği dosya ve klasörleri listeler. `"."` deponun tamamı demektir.
- `target.repo.commit` isteğe bağlıdır. 40 karakterlik bir SHA'dır. Verilmezse Skynet daldaki en son commit'i kullanır.
- `target.repo.secrets` isteğe bağlıdır. Her gizli değerin bir `name` ve bir `value` alanı vardır. Kodunuza ve puanlayıcınıza bir ortam değişkeni olarak ulaşır.
- `scorer.metric_code`, `score(repo_path, case)` fonksiyonunu tanımlar. `scorer.model`, kodunuzun ve puanlayıcınızın model ağ geçidi üzerinden çağırabileceği modeldir.
- `strategy.mode` değeri `auto` (varsayılan) veya `single`'dır. `single` ile `strategy.engine` alanını `GET /blackbox/engines` tarafından döndürülen bir motor adına ayarlayın.
- `max_cost_cents`, bu çalıştırmanın ABD senti cinsinden harcama limitidir.
- `is_private` varsayılan olarak `false`'tur. Çalıştırmayı özel tutmak için `true` olarak ayarlayın.

`metric_code` kuralları için [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

### Yanıtlar

| Durum | Anlamı                                                                                                                                           |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `201` | Skynet çalıştırmayı kuyruğa aldı. Gövde `optimization_id` ve `status` içerir.                                                                    |
| `400` | Hedef bir depo değil veya puanlayıcı yüklenmiyor. |
| `202` | Kurulum kontrolü hâlâ çalışıyor. `Retry-After` başlığındaki saniye kadar bekleyin. Ardından aynı gövdeyi aynı `Idempotency-Key` ile gönderin. |
| `402` | Bakiyeniz çok düşük.                                                                                                                             |
| `409` | Bir kotaya ulaştınız veya kurulum kontrolü güncel değil.                                                                                         |
| `422` | İstek optimize ediciye uymuyor. |

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

En iyi sürümü yeni bir girdi üzerinde çalıştırmak için `Idempotency-Key` başlığıyla `POST /serve/{optimization_id}` gönderin. Depoda `def serve(input)` içeren bir `serve.py` dosyası olmalıdır. İstek, yanıt ve her puanın ne anlama geldiği için [Sonuçlar](/docs/guides/results) sayfasını okuyun.

## Limitler

API, web uygulamasıyla aynı kotaları, hız limitlerini ve faturalandırmayı kullanır. [Limitler](/docs/limits) ve [Faturalandırma](/docs/concepts/billing) sayfalarını okuyun.
