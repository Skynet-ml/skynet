Çalıştırma sayfası bir çalıştırmanın ilerlemesini ve sonuçlarını gösterir. Bu kılavuz çalıştırma sayfasındaki sekmeleri, puanları ve en iyi sürümün nasıl dışa aktarılacağını veya sunulacağını açıklar.

## Çalıştırma sayfasındaki sekmeler

Çalıştırma sayfası yalnızca çalıştırmaya uyan sekmeleri gösterir. Bazı sekmeler duruma ve deneyim düzeyinize bağlıdır.

| Sekme                      | Ne zaman görünür                                              | Ne içerir                                                                  |
| -------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Genel bakış**            | Her zaman                                                     | İlerleme, puanlar ve puan geçmişi                                          |
| **Kullanım**               | Deposunda `serve.py` dosyası olan başarılı bir çalıştırma     | En iyi sürümü çağırmak için bir form ve entegrasyon kodu                   |
| **Yapay nesne**            | Sonucu olan bir çalıştırma                                    | Deponuzun sürümleri ve en iyi sürüm                                        |
| **Veri**                   | Vakaları olan bitmiş bir çalıştırma                           | Eğitim, doğrulama ve test kümelerine bölünmüş veri                         |
| **Kod**                    | Çalıştırmanın gösterilecek kodu olduğunda                     | Puanlayıcı                                                                 |
| **Ödül**                   | Puanlayıcısı ödül parçaları bildiren bir çalıştırma           | Ödül bileşenleri ve ağırlıkları                                            |
| **Günlükler**              | Çoğu çalıştırma                                               | Çalıştırma günlüğü                                                         |
| **Kullanım ve maliyet**    | Oturum açtığınızda                                            | Çalıştırmanın harcadığı tutar                                              |
| **Zamanlanmış çalıştırma** | Başarılı bir çalıştırma                                       | Optimizasyonu en iyi sürümden yeniden çalıştırmak için bir zamanlama       |
| **Yapılandırma**           | Her zaman                                                     | Çalıştırma yapılandırması                                                  |

> [!NOTE]
> **Rehberli** düzeyde çalıştırma sayfası **Veri**, **Kod**, **Günlükler** ve **Yapılandırma** sekmelerini gizler. Bu sekmelerden birine doğrudan bir bağlantı yine de sekmeyi açar. Düzeyi **Ayarlar → Hesap** içinde değiştirin.

## Puanlar

Skynet sürümleri doğrulama vakaları üzerinde seçer. Nihai puanı test vakaları üzerinde raporlar. Motorlar test vakalarını hiçbir zaman görmez. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

**Genel bakış** sekmesi şunları gösterir:

- **İyileşme**: başlangıç sürümünden en iyi sürüme olan değişim.
- **Puan ilerlemesi**: çalıştırma boyunca puan.
- **Puan dökümü**: **Temel Değer**, **Optimize Edilmiş** ve **Değişim** sütunlarıyla her puan.

Çalıştırma etkinken genel bakış **Geçen süre**, **Kalan** ve **Hız** değerlerini de gösterir.

Puanlayıcınız adlandırılmış puanlar bildirdiğinde genel bakış **Adlandırılmış puanlar** bölümünü de gösterir.

### En iyi sürüm başlangıç sürümü olduğunda

Bazen hiçbir aday başlangıç sürümünü geçemez. Bu durumda Skynet başlangıç sürümünü döndürür. **Yapay nesne** sekmesi bir gerileme korumasının uygulandığını söyler.

Çalıştırma bütçesinde durduysa çalıştırma sayfası Skynet'in hangi sürümü kaydettiğini söyler. [Çalıştırmalar](/docs/concepts/runs) sayfasını okuyun.

## En iyi sürümü dışa aktarın

**Yapay nesne** sekmesi deponuzun **Sürümler** listesini gösterir. Her sürüm için şunları yapabilirsiniz:

- Sürümü kopyalayın.
- Sürümü bir dosya olarak indirin.
- Sürümü tüm deponun zip'i olarak indirin.

Bir sürümü başka bir sürümle karşılaştırabilirsiniz. Sekme bir sürümün **Puanlayıcı geri bildirimi** bölümünü de gösterir.

### Yeni bir depodaki dallar

Skynet depoyu çalıştırma için oluşturduysa her adayı kendi dalına, `skynet/candidate-<version id>` dalına gönderir. `skynet/best` dalı her zaman en iyi sürümü gösterir. Daha iyi bir sürüm çıktığında bu dal yer değiştirir.

## En iyi sürümü sunun

En iyi sürümü kendi kodunuzdan çağırabilirsiniz. Deponun kökünde `serve(input)` tanımlayan bir `serve.py` dosyası olmalıdır:

```python tab="serve.py"
def serve(input):
    return {"label": classify(input["message"])}
```

- `input`, istekteki girdi alanlarının dict'idir.
- Dönüş değeri JSON'a serileştirilebilir olmalıdır. Bir dict `outputs` olur. Başka herhangi bir değer `{"output": value}` olur.
- `serve.py`, `skynet` yardımcı modülündeki `llm` ile bir model çağırabilir. Çalıştırmanın gizli değerlerini almaz.

Skynet en iyi sürümü, çalıştırmayla aynı korumalı sandbox içinde çalıştırır. **Kullanım** sekmesinin iki bölümü vardır:

- Bir form. Girdi alanlarını doldurun ve bir sonuç almak için **Çalıştır** seçeneğini seçin.
- cURL, Python, JavaScript ve Go ile **Entegrasyon kodu**.

En iyi sürümü çağırmak için **Ayarlar → API** içinde bir API token'ı oluşturun. Ardından bir istek gönderin. `Idempotency-Key` başlığı zorunludur:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5}'
```

```python tab="Python"
import os
import uuid
import requests

response = requests.post(
    f"{os.environ['API_URL']}/serve/<optimization_id>",
    headers={
        "Authorization": f"Bearer {os.environ['APP_API_TOKEN']}",
        "Idempotency-Key": str(uuid.uuid4()),
    },
    json={"inputs": {"message": "I was charged twice."}, "max_cost_cents": 5},
)
print(response.json()["outputs"])
```

`message` yerine çalıştırmanızın girdi alanlarını yazın. **Kullanım** sekmesindeki **Entegrasyon kodu**, gerçek URL'yi ve alan adlarını sizin için doldurur.

Yanıt `outputs`, `input_fields`, `output_fields`, `model_used`, `cents_charged` ve `budget` değerlerini içerir.

> [!TIP]
> Her çağrı ayrı olarak, çağıranın bakiyesinden faturalandırılır. `max_cost_cents`, bu tek çağrının sent cinsinden en fazla ne kadar tutabileceğini belirler. Aynı `Idempotency-Key` değerini yeniden gönderirseniz Skynet çağrıyı yeniden ücretlendirmez.

Kimlik doğrulama ve diğer uç noktalar için [API](/docs/api) sayfasını okuyun.

## Sonuçları paylaşın

Bir çalıştırmayı başkalarıyla paylaşabilir veya herkese açık Keşfet galerisinde listeleyebilirsiniz. [Paylaşma ve yayımlama](/docs/guides/sharing) sayfasını okuyun.
