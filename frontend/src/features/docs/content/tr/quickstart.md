Bu sayfa, ilk çalıştırmanızı yaklaşık on dakikada nasıl başlatacağınızı gösterir. Giriş yapar, küçük bir veri kümesi yükler, sihirbazın dört adımını doldurur ve çalıştırmayı başlatırsınız. Örnek, destek mesajlarını etiketlere ayıran bir DSPy programını iyileştirir.

## Başlamadan önce

Şunlara ihtiyacınız var:

- Bir Skynet hesabı.
- Bir CSV, JSON veya Excel dosyası (`.csv`, `.json`, `.xlsx` veya `.xls`). Her satır bir vakadır.
- Bir veya daha fazla girdi sütunu, örneğin `message`.
- Bir beklenen çıktı sütunu, örneğin `label`.

> [!TIP]
> En az 5 satır kullanın. 5'ten az çift olduğunda Skynet test kümesi ayırmaz; bu durumda nihai puan, optimize edicinin kullandığı vakalardan gelir.

## 1. Giriş yapın

Skynet'e giriş yapın. Ardından `/submit` adresindeki gönderme sayfasını açın.

> [!NOTE]
> Bu sayfa **Standart** seviyenin etiketlerini kullanır. **Rehberli** seviye daha basit etiketler kullanır, Optimize edici adımını atlar ve sizin için **Auto** seçer. Seviyeyi Ayarlar'dan değiştirin.

## 2. Görev

İlk adım **Görev**'dir. "Neyi optimize etmek istiyorsunuz?" diye sorar.

1. **Amaç** alanına iyi bir sonucun ne olduğunu yazın. Sade bir dil kullanın, örneğin "Her destek mesajını faturalandırma, hata veya diğer olarak sınıflandır."
2. İsteğe bağlı: **Arka plan** alanına optimize ediciye yardımcı olacak bağlamı ekleyin.
3. **Neyin optimize edileceği** alanında **DSPy programı** seçeneğini seçin.
4. **Başlangıç sürümü** alanına programınızın geçerli talimatını yapıştırın.

## 3. Girdiler

İkinci adım **Girdiler**'dir.

1. **CSV, JSON veya Excel dosyası yükleyin**'i ya da kayıtlı bir veri kümesini kullanmak için **Kütüphaneden seç**'i seçin.
2. **Veriler nasıl okunacak** altında her sütuna bir rol verin:
   - **Girdi**: Çalıştırma kodu bu sütunu alır.
   - **Çıktı**: Beklenen değerdir. Metrik bunu alır.
   - **Atla**: Skynet bu sütunu kullanmaz.
3. Bölme için **Öneriyi kullan** seçeneğini koruyun. Çoğu veri kümesi için bölme %70 eğitim, %15 doğrulama ve %15 testtir.

Kuralların tamamı için [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

## 4. Optimize edici

Üçüncü adım **Optimize edici**'dir ("Optimize edici, modeller ve bütçe").

- Optimize ediciyi **Auto** olarak bırakın.
- Bir **Yansıtma modeli** seçin. Optimize edici yeni sürümleri yazmak için bu modeli kullanır. Bu model zorunludur.
- Bir **Görev modeli** seçin. Programınız onu kullanır. Bir DSPy programı için zorunludur.
- **Bütçe** altında **Maksimum puanlayıcı çalıştırması** değerini varsayılan 200'de bırakın.

Belirli bir motor seçmek için [Motorlar](/docs/concepts/engines) sayfasını okuyun.

## 5. Metrik ve çalıştırma

Son adım **Metrik ve çalıştırma**'dır ("Kod ve metrik"). İki dosya yazarsınız.

```python tab="run.py"
import dspy

program = dspy.Predict("message -> label")


def run(artifact, input):
    return program(message=input["message"]).label
```

```python tab="metric.py"
def score(input, output, expected=None):
    want = expected["label"].strip().lower()
    got = str(output).strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`artifact`, optimize edicinin önerdiği geçerli talimattır. Skynet bunu `program` üzerine uygular. `input`, bir vakanın Girdi sütunlarını tutar. `expected`, Çıktı sütununu sütun adıyla anahtarlanmış olarak tutar.

Ardından **Sözleşme kontrolü** altında **Kontrol et**'i seçin. Kontrol, kodunuzu ve metriğinizi sandbox içinde 3 örnek vaka üzerinde çalıştırır. Şunları kontrol eder:

| Kontrol                       | Ne zaman başarısız olur                                              |
| ----------------------------- | -------------------------------------------------------------------- |
| Çalıştırma kodu imzası        | `run.py`, `run(artifact, input)` tanımlamıyor                        |
| Metrik imzası                 | `metric.py`, `score(input, output, expected=None)` tanımlamıyor      |
| Metrik modeli                 | Metrik modeli kullanılamıyor                                         |
| Örnekler üzerinde çalıştırma kodu | Çalıştırma kodunuz bir örnek vakada hata veriyor                  |
| Metrik dönüş değeri           | `score` geçerli bir puan ve geri bildirim döndürmüyor                |

> [!NOTE]
> Çalıştırmayı yalnızca sözleşme kontrolü başarılı olduktan sonra başlatabilirsiniz.

## 6. Başlatın ve izleyin

Özet ekranında "Çalıştırmaya hazır" yazar. Sekmeleri Genel, Veri, Modeller, Bütçe ve Ödül'dür. Kurulumu kontrol edin, ardından optimizasyonu başlatın.

Çalıştırmayı izlemek için optimizasyonlar listenizden açın. Puanları ve kazanan sürümü anlamak için [Sonuçlar](/docs/guides/results) sayfasını okuyun.

## Sonraki adımlar

- Daha iyi bir metrik yazın: [Metrikler ve puanlayıcılar](/docs/concepts/scorers).
- Yapıtınıza hangi motorların uyduğunu öğrenin: [Optimizasyon nasıl çalışır](/docs/concepts/optimization).
- Bir çalıştırmanın maliyetini öğrenin: [Faturalandırma](/docs/concepts/billing).
