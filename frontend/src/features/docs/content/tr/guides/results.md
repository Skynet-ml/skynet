Çalıştırma sayfası bir çalıştırmanın ilerlemesini ve sonuçlarını gösterir. Bu kılavuz çalıştırma sayfasındaki sekmeleri, puanları ve en iyi sürümün nasıl dışa aktarılacağını veya sunulacağını açıklar.

## Çalıştırma sayfasındaki sekmeler

Çalıştırma sayfası yalnızca çalıştırmaya uyan sekmeleri gösterir. Bazı sekmeler yola, duruma ve deneyim düzeyinize bağlıdır.

| Sekme                         | Ne zaman görünür                                                       | Ne içerir                                                                               |
| ----------------------------- | ---------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| **Genel bakış**               | Her zaman                                                              | İlerleme, puanlar ve puan geçmişi                                                       |
| **Kullanım**                  | Skynet'in sunabileceği bir programa sahip başarılı bir çalıştırma      | Optimize edilmiş programı çalıştırmak için bir form ve entegrasyon kodu                 |
| **Yapay nesne** | Sonucu olan bir depo çalıştırması | Yapıtınızın sürümleri ve en iyi sürüm |
| **Veri**                      | Bitmiş bir DSPy programı çalıştırması                                  | Eğitim, doğrulama ve test kümelerine bölünmüş veri ve test kümesi değerlendirmesi       |
| **Kod**                       | Çalıştırmanın gösterilecek kodu olduğunda                              | Çalıştırma kodu ve metrik                                                               |
| **Ödül** | Metriği ödül parçaları bildiren bir depo çalıştırması | Ödül bileşenleri ve ağırlıkları |
| **Yapay nesne** | Bitmiş bir DSPy programı çalıştırması | Optimize edilmiş çıktı ve **Sonuçları dışa aktar** |
| **Günlükler**                 | Çoğu çalıştırma                                                        | Çalıştırma günlüğü                                                                      |
| **Kullanım ve maliyet**       | Oturum açtığınızda                                                     | Çalıştırmanın harcadığı tutar                                                           |
| **Zamanlanmış çalıştırma** | Başarılı bir DSPy programı çalıştırması | Optimizasyonu yeniden çalıştırmak için bir zamanlama |
| **Yapılandırma**              | Her zaman                                                              | Çalıştırma yapılandırması                                                               |

> [!NOTE]
> **Rehberli** düzeyde çalıştırma sayfası **Veri**, **Kod**, **Günlükler** ve **Yapılandırma** sekmelerini gizler. Bu sekmelerden birine doğrudan bir bağlantı yine de sekmeyi açar. Düzeyi **Ayarlar → Hesap** içinde değiştirin.

## Puanlar

Skynet sürümleri doğrulama vakaları üzerinde seçer. Nihai puanı test vakaları üzerinde raporlar. Motorlar test vakalarını hiçbir zaman görmez. [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

**Genel bakış** sekmesi şunları gösterir:

- **İyileşme**: başlangıç sürümünden en iyi sürüme olan değişim.
- **Puan ilerlemesi**: çalıştırma boyunca puan.
- **Puan dökümü**: **Temel Değer**, **Optimize Edilmiş** ve **Değişim** sütunlarıyla her metrik.

Çalıştırma etkinken genel bakış **Geçen süre**, **Kalan** ve **Hız** değerlerini de gösterir.

Bir depo çalıştırmasında, metriğiniz adlandırılmış puanlar bildirdiğinde genel bakış **Adlandırılmış puanlar** bölümünü de gösterir.

Bir DSPy programı çalıştırmasında **Veri** sekmesinde bir **Test kümesi değerlendirmesi** bulunur. Her test vakası için temel puanı ve optimize edilmiş puanı gösterir.

### En iyi sürüm başlangıç sürümü olduğunda

Bazen hiçbir aday başlangıç sürümünü geçemez. Bu durumda Skynet başlangıç sürümünü döndürür. **Yapay nesne** sekmesi bir gerileme korumasının uygulandığını söyler.

Çalıştırma bütçesinde durduysa çalıştırma sayfası Skynet'in hangi sürümü kaydettiğini söyler. [Çalıştırmalar](/docs/concepts/runs) sayfasını okuyun.

## En iyi sürümü dışa aktarın

### Depo çalıştırmaları

**Yapay nesne** sekmesi yapıtınızın **Sürümler** listesini gösterir. Her sürüm için şunları yapabilirsiniz:

- Sürümü kopyalayın.
- Sürümü bir dosya olarak indirin.
- Bir depo çalıştırmasında sürümü tüm deponun zip'i olarak indirin.

Bir sürümü başka bir sürümle karşılaştırabilirsiniz. Sekme bir sürümün **Puanlayıcı geri bildirimi** bölümünü de gösterir.

### DSPy programı çalıştırmaları

**Yapay nesne** sekmesinde bir **Sonuçları dışa aktar** paneli vardır. **İndir** menüsünü açın. Menü, çalıştırmada bulunan biçimleri gösterir:

| Seçenek                    | Biçim                                                   |
| -------------------------- | ------------------------------------------------------- |
| DSPy programı              | `.zip`, çalıştırabileceğiniz bağımsız bir dışa aktarım  |
| Optimize edilmiş program   | `.pkl`                                                  |
| İstem                      | `.json`                                                 |
| Optimize edilmiş kod       | `.py`                                                   |
| Günlükler                  | `.csv`                                                  |

## En iyi sürümü sunun

Optimize edilmiş bir DSPy programını kendi kodunuzdan çağırabilirsiniz. **Kullanım** sekmesinin iki bölümü vardır:

- Bir form. Girdi alanlarını doldurun ve bir tahmin almak için **Çalıştır** seçeneğini seçin.
- cURL, Python, JavaScript, Go ve DSPy ile **Entegrasyon kodu**.

Program her zaman optimize edildiği model ayarlarıyla çalışır. Tüm girdi alanlarını vermelisiniz. Skynet fazladan alanları yok sayar.

Programı çağırmak için **Ayarlar → API** içinde bir API token'ı oluşturun. Ardından bir istek gönderin:

```bash tab="cURL"
export APP_API_TOKEN=skyd_...
curl -X POST "$API_URL/serve/<optimization_id>" \
  -H "Authorization: Bearer $APP_API_TOKEN" \
  -H "Idempotency-Key: $(uuidgen)" \
  -H "Content-Type: application/json" \
  -d '{"inputs": {"question": "What is DSPy?"}}'
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
    json={"inputs": {"question": "What is DSPy?"}},
)
print(response.json()["outputs"])
```

`question` yerine programınızın girdi alanlarını yazın. **Kullanım** sekmesindeki **Entegrasyon kodu**, gerçek URL'yi ve alan adlarını sizin için doldurur.

Yanıt `outputs`, `input_fields`, `output_fields` ve `model_used` değerlerini içerir.

> [!TIP]
> İstek gövdesi `max_cost_cents` değerini de kabul eder. Bu değer, bu tek çağrının sent cinsinden en fazla ne kadar tutabileceğini belirler. Bazı çalıştırmalar bunu zorunlu tutar.

Kısmi çıktıları geldikçe almak için aynı isteği `/serve/<optimization_id>/stream` adresine gönderin. Yanıt bir server-sent events akışıdır.

Kimlik doğrulama ve diğer uç noktalar için [API](/docs/api) sayfasını okuyun.

## Sonuçları paylaşın

Bir çalıştırmayı başkalarıyla paylaşabilir veya herkese açık Keşfet galerisinde listeleyebilirsiniz. [Paylaşma ve yayımlama](/docs/guides/sharing) sayfasını okuyun.
