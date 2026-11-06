Bu sayfa, ilk çalıştırmanızı yaklaşık on dakikada nasıl başlatacağınızı gösterir. Giriş yapar, küçük bir veri kümesi yükler, sihirbazın dört adımını doldurur ve çalıştırmayı başlatırsınız. Örnekte Skynet, destek mesajlarını etiketlere ayıran yeni bir depo oluşturur ve ardından onu iyileştirir.

## Başlamadan önce

Şunlara ihtiyacınız var:

- Bir Skynet hesabı.
- Bir GitHub hesabı. Skynet depoyu bu hesapta oluşturur.
- Bir CSV, JSON veya Excel dosyası (`.csv`, `.json`, `.xlsx` veya `.xls`). Her satır bir vakadır.
- Bir veya daha fazla girdi sütunu, örneğin `message`.
- Bir beklenen çıktı sütunu, örneğin `label`.

> [!TIP]
> En az 5 satır kullanın. 5'ten az çift olduğunda Skynet test kümesi ayırmaz; bu durumda nihai puan, optimize edicinin kullandığı vakalardan gelir.

## 1. Giriş yapın

Skynet'e giriş yapın. GitHub'ı **Ayarlar → Bağlayıcılar** bölümünde bağlayın. Ardından `/submit` adresindeki gönderme sayfasını açın.

> [!NOTE]
> Bu sayfa **Standart** seviyenin etiketlerini kullanır. **Rehberli** seviye daha basit etiketler kullanır, Optimize edici adımını atlar ve sizin için **Auto** seçer. Seviyeyi Ayarlar'dan değiştirin.

## 2. Görev

İlk adım **Görev**'dir. "Neyi optimize etmek istiyorsunuz?" diye sorar.

1. **Amaç** alanına iyi bir sonucun ne olduğunu yazın. Sade bir dil kullanın, örneğin "Her destek mesajını faturalandırma, hata veya diğer olarak sınıflandır."
2. İsteğe bağlı: **Arka plan** alanına optimize ediciye yardımcı olacak bağlamı ekleyin.
3. **Yeni depo oluştur** seçeneğini seçin. Bir ad girin, örneğin `support-labels`, ve **Özel** veya **Herkese açık** seçeneğini seçin.

Skynet depoyu GitHub hesabınızda oluşturur. Asistan, görevinizden ve verinizden ilk çözümü yazar. Bu çözüm başlangıç sürümüdür. Zaten kodunuz varsa bunun yerine **Bir depo kullan** seçeneğini seçin. [Çalıştırma gönderme](/docs/guides/submit) sayfasını okuyun.

## 3. Girdiler

İkinci adım **Girdiler**'dir.

1. **CSV, JSON veya Excel dosyası yükleyin**'i ya da kayıtlı bir veri kümesini kullanmak için **Kütüphaneden seç**'i seçin.
2. **Veriler nasıl okunacak** altında her sütuna bir rol verin:
   - **Girdi**: Kodunuz bu sütunu alır.
   - **Çıktı**: Beklenen değerdir. Puanlayıcı bunu alır.
   - **Atla**: Skynet bu sütunu kullanmaz.
3. Bölme için **Öneriyi kullan** seçeneğini koruyun. Çoğu veri kümesi için bölme %70 eğitim, %15 doğrulama ve %15 testtir.

Kuralların tamamı için [Veri kümeleri ve bölmeler](/docs/concepts/datasets) sayfasını okuyun.

## 4. Optimize edici

Üçüncü adım **Optimize edici**'dir ("Optimize edici, modeller ve bütçe").

- Optimize ediciyi **Auto** olarak bırakın.
- Bir **Yansıtma modeli** seçin. Optimize edici yeni sürümleri yazmak için bu modeli kullanır. Bu model zorunludur.
- **Bütçe** altında **Maksimum puanlayıcı çalıştırması** değerini varsayılan 200'de bırakın.

Belirli bir motor seçmek için [Motorlar](/docs/concepts/engines) sayfasını okuyun.

## 5. Metrik ve çalıştırma

Son adım **Metrik ve çalıştırma**'dır. Asistan puanlayıcıyı sizin için yazar. Onu düzenleyebilirsiniz. Puanlayıcı, deponun bir sürümünün yolunu ve bir vakayı alır:

```python tab="scorer.py"
import importlib.util


def score(repo_path, case):
    spec = importlib.util.spec_from_file_location("serve", f"{repo_path}/serve.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    got = str(module.serve(case["input"])).strip().lower()
    want = case["expected"]["label"].strip().lower()
    if got == want:
        return 1.0, {"feedback": "Correct label."}
    return 0.0, {"feedback": f"Expected '{want}', got '{got}'."}
```

`case["input"]` bir vakanın Girdi sütunlarını tutar. `case["expected"]` Çıktı sütununu, sütun adıyla anahtarlanmış olarak tutar. Depoda `def serve(input)` içeren bir `serve.py` dosyası vardır. Skynet, çalıştırmadan sonra en iyi sürümü sunmak için aynı fonksiyonu kullanır.

Ardından kontrolü çalıştırın. Kontrol, kurulumunuzu ve puanlayıcınızı sandbox içinde başlangıç sürümü üzerinde çalıştırır. Çalıştırmayı ancak kontrol geçtikten sonra başlatabilirsiniz.

## 6. Başlatın ve izleyin

Özet ekranında "Çalıştırmaya hazır" yazar. Sekmeleri Genel, Veri, Modeller, Bütçe ve Ödül'dür. Kurulumu kontrol edin, ardından optimizasyonu başlatın.

Çalıştırmayı izlemek için optimizasyonlar listenizden açın. Skynet her adayı yeni deponuzda kendi dalına gönderir. `skynet/best` dalı her zaman en iyi sürümü gösterir. Puanları ve kazanan sürümü anlamak için [Sonuçlar](/docs/guides/results) sayfasını okuyun.

## Sonraki adımlar

- Daha iyi bir puanlayıcı yazın: [Metrikler ve puanlayıcılar](/docs/concepts/scorers).
- Motorların nasıl çalıştığını öğrenin: [Optimizasyon nasıl çalışır](/docs/concepts/optimization).
- Bir çalıştırmanın maliyetini öğrenin: [Faturalandırma](/docs/concepts/billing).
