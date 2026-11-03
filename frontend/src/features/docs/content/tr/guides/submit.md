Gönderme sihirbazı bir çalıştırmayı kurar ve başlatır. Dört adımı vardır: **Görev**, **Girdiler**, **Optimize edici** ve **Metrik ve çalıştırma**. Her adımda bir veya daha fazla ekran bulunur. Bu kılavuz, görevden sözleşme kontrolüne ve çalıştırmanın başlamasına kadar her ekranı açıklar.

> [!NOTE]
> Sihirbaz her deneyim düzeyi için daha fazla veya daha az ayar gösterir: **Rehberli**, **Standart** veya **Uzman**. Varsayılan Rehberli düzeyidir. Düzeyi **Ayarlar → Hesap** içinde, **Ne kadar gösterilsin** bölümünde değiştirin. Rehberli düzeyde bazı etiketler daha basittir. Bu kılavuz Standart ve Uzman etiketlerini kullanır ve farklı olduğunda Rehberli etiketini parantez içinde verir.

## 1. adım: Görev

### Neyi optimize etmek istiyorsunuz?

Görevinizi **Görev görüşmesi** sohbetinde asistana anlatın. Asistan sizin için iki alan yazar:

- **Amaç** (Rehberli: **Hedef**): optimize edilmiş yapıtın iyi yapması gereken şey.
- **Arka plan** (Rehberli: **Ek bağlam**): bağlam, kısıtlar ve iyi bir sonucun neye benzediği.

İki alanı da düzenleyebilirsiniz. Asistan düzenlemelerinizin üzerine yazmaz.

### Ne optimize edilecek

**Yapıt türü** seçin:

| Yapıt türü    | Ne değişir                                         |
| ------------- | -------------------------------------------------- |
| İstem         | Bir dil modeline gönderilen talimatlar             |
| Kod           | Bir programın kaynak kodu                          |
| DSPy programı | Bir DSPy modülünün tahminci (predictor) talimatları |
| Metin         | Belge veya şablon gibi başka herhangi bir metin    |

Ardından **Başlangıç sürümü** (Rehberli: **Nereden başlıyoruz**) değerini verin. Geçerli sürümü yapıştırın veya asistanın bir taslak yazmasına izin verin.

### Bir depoyu optimize edin

Bir depodaki kodu da optimize edebilirsiniz. Şu alanları doldurun:

- **Depo**: bir depo seçin veya `owner/name` biçiminde yazın.
- **Commit**: 40 karakterlik bir SHA. Daldaki en son commit'i kullanmak için boş bırakın.
- **Optimize edicinin değiştirebileceği yollar**: her satıra bir dosya veya klasör. Nokta, deponun tamamı anlamına gelir.
- **Kurulum komutu**: puanlamadan önce her yeni checkout'ta çalışır. Boş bırakırsanız Skynet komutu kendisi bulur.

## 2. adım: Girdiler

### Veri

Çalıştırma için vakaları ekleyin. Şunları yapabilirsiniz:

- Bir CSV, JSON veya Excel dosyası yükleyin.
- Kitaplığınızdan bir veri kümesi seçin.

[Veri kümeleri ve bölmeler](/docs/concepts/datasets) ile [Veri kümesi hazırlayın](/docs/guides/datasets) sayfalarını okuyun.

### Veriler nasıl okunacak

Asistan verileri okur ve nasıl kullanılacağını önerir. Seçimlerini kontrol edin:

- Her sütunu girdi, beklenen çıktı veya yok sayılacak olarak işaretleyin.
- Çıktı sütunu yoksa Skynet ilk satırı tek bir girdi olarak kullanır.
- Eğitim, doğrulama ve test kümelerine bölmeyi belirleyin. Bölmenin toplamı 1 olmalıdır.

> [!WARNING]
> **Girdiler** adımında bir sorun varken sonraki adımları açamazsınız. Sihirbazın gösterdiği sorunları düzeltin, ardından devam edin.

## 3. adım: Optimize edici

Standart düzeyde bu adımın tek ekranı vardır. Ekran optimize ediciyi, bütçeyi ve gerektiğinde modelleri gösterir. Rehberli ve Uzman düzeylerinde adımın üç ekranı vardır: **Optimize edici**, **Modeller** ve **Bütçe**.

### Optimize edici

Sihirbaz yalnızca görevinize ve girdilerinize uyan optimize edicileri listeler. **Auto** sizin için seçilidir. İsterseniz başka bir optimize edici seçin. [Motorlar](/docs/concepts/engines) ve [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfalarını okuyun.

### Modeller

- **Görev modeli**: çalıştırma kodunuzun çağırdığı model. Çalıştırma kodunuz bir model çağırmıyorsa boş bırakın. Bir DSPy programının görev modeli olmalıdır.
- **Yansıtma modeli**: puanları ve geri bildirimi okuyup sonraki adayları yazan model. Kodlama ajanı kullanan bir optimize edicide bu, ajanın modelidir.

### Bütçe

Çalıştırmanın ne kadar süre denemeye devam edebileceğini ve ne kadar harcayabileceğini belirleyin:

- **Maks. puanlayıcı çalıştırması** (Rehberli: **Maks. deneme**): Skynet'in puanladığı en fazla aday sayısı. Bazı optimize ediciler en az belirli bir sayı gerektirir.
- **Durma puanı** (Rehberli: **Puan şuna ulaşınca erken dur**): bir sürüm bu puana ulaştığında çalıştırma durur.
- **Maks. yineleme** (Rehberli: **Maks. tur**): bunu destekleyen optimize ediciler için gösterilir.
- **Harcama limiti**: çalıştırmanın harcayabileceği en yüksek tutar. Varsayılan 5 $'dır; **Başlamak için gereken** tutar daha yüksekse o tutardır. **Limit yok** seçeneğini de seçebilirsiniz.

Limit veya bakiyeniz başlamak için gereken tutarın altındaysa sihirbaz devam etmenize izin vermez. [Bütçeler ve faturalandırma](/docs/concepts/billing) sayfasını okuyun.

## 4. adım: Metrik ve çalıştırma

### Kod ve metrik

İki dosya yazın:

- `run.py`: yapıtınızı bir vaka üzerinde çalıştıran çalıştırma kodu.
- `metric.py`: çıktıyı puanlayan ve geri bildirim yazan metrik.

**Kodlama ajanı** sizin için herhangi bir dosyayı yazabilir ve değiştirebilir. Kodunu kontrol eder ve başarısız olanı düzeltir. En fazla 20 ek dosya ekleyebilirsiniz. [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

Metrik, modelini kendi `llm()` çağrısında belirler. `model=` olmadan metrik yansıtma modelini kullanır.

### Sözleşme kontrolü

**Sözleşme kontrolü** (Rehberli: **Deneme çalıştırması**), kodunuzu ve metriğinizi sandbox içinde 3 örnek vaka üzerinde çalıştırır. Şunları kontrol eder:

- çalıştırma kodunun imzası,
- metriğin imzası,
- metrik modeli,
- çalıştırma kodunun örnekler üzerindeki davranışı,
- metriğin dönüş değeri.

**Kontrol et** (Rehberli: **Test et**) seçeneğini seçin. Kontrol geçene kadar çalıştırmayı başlatamazsınız. Kontrol geçtikten sonra kodu veya kurulumu değiştirirseniz kontrolü yeniden çalıştırın.

> [!TIP]
> Girdisini, çıktısını ve geri bildirimini görmek için her örneği açın. İyi bir geri bildirim, optimize ediciye tek başına puandan daha fazla çalışma malzemesi verir.

### Zamanlanmış çalıştırma

Optimizasyon başarılı olduktan sonra onu bir zamanlamayla yeniden çalıştırabilirsiniz. Varsayılan **Kapalı** değeridir.

### Çalıştırmaya hazır

Son ekran beş sekmede bir özet gösterir: **Genel**, **Veri**, **Modeller**, **Bütçe** ve **Ödül**. Kurulumu kontrol edin, ardından optimizasyonu başlatın.

Özet, çalıştırmanın görünürlüğünü de gösterir. Yeni çalıştırmalar varsayılan olarak herkese açıktır. Bunu daha sonra çalıştırma sayfasındaki **Paylaşım ve gizlilik** iletişim kutusunda değiştirin. [Paylaşma ve yayımlama](/docs/guides/sharing) sayfasını okuyun.

> [!NOTE]
> Bir depo çalıştırmasında bu adımda yalnızca **Kod ve metrik** ekranı ve özet bulunur. Skynet, çalıştırmayı başlattığınızda kurulumu depoya göre kontrol eder.

## Başlattıktan sonra

Çalıştırma sayfası açılır. [Çalıştırmalar](/docs/concepts/runs) ve [Sonuçları okuyun](/docs/guides/results) sayfalarını okuyun.
