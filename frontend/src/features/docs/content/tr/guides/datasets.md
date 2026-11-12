Veri kümesi, bir optimizasyon çalıştırmasının öğrendiği ve puanlamada kullandığı örneklerden oluşan bir tablodur. Bu kılavuz bir dosyanın nasıl yükleneceğini, satır ve sütunların nasıl düzenleneceğini, sütun rollerinin nasıl belirleneceğini ve bir veri kümesinin nasıl paylaşılacağını gösterir. Kavramlar için [Veri kümeleri](/docs/concepts/datasets) sayfasını okuyun.

## Desteklenen dosyalar

Skynet şu dosya türlerini okur:

| Uzantı          | Skynet dosyayı nasıl okur                                                                     |
| --------------- | --------------------------------------------------------------------------------------------- |
| `.csv`          | İlk satır başlık satırıdır. Dosyada en az bir veri satırı olmalıdır.                          |
| `.json`         | Nesnelerden oluşan bir dizi veya tek bir nesne. İlk nesnenin anahtarları sütun olur.          |
| `.xlsx`, `.xls` | Skynet yalnızca ilk sayfayı okur.                                                             |

Skynet diğer uzantıları "Unsupported file format" mesajıyla reddeder.

> [!TIP]
> Beklenen çıktıyı son sütuna koyun. Gönderme sihirbazı varsayılan olarak son sütunu çıktı olarak işaretler.

### Örnek CSV

```csv
question,answer
What is the capital of France?,Paris
How many legs does a spider have?,8
```

### Örnek JSON

```json
[
  { "question": "What is the capital of France?", "answer": "Paris" },
  { "question": "How many legs does a spider have?", "answer": "8" }
]
```

## Veri kümesi yükleyin

1. **Veri kümeleri** sayfasını açın.
2. **Veri kümesi yükle** düğmesine tıklayın veya bir dosyayı sayfaya sürükleyin. Sürüklerken sayfa "Yüklemek için bırakın" mesajını gösterir.
3. "Veri kümesi kütüphanenize kaydedildi" bildirimini bekleyin.

Skynet, uzantısı olmadan dosya adını veri kümesinin adı olarak kullanır. Adı değiştirmek için veri kümesini seçin ve **Yeniden adlandır** düğmesine tıklayın.

> [!NOTE]
> Her hesabın bir depolama kotası vardır. Bir yükleme kotayı aşarsa Skynet limiti açıklayan bir iletişim kutusu açar. Bkz. [Limitler](/docs/limits).

Bir dosyayı gönderme sihirbazında da yükleyebilir ("CSV, JSON veya Excel dosyası yükleyin") veya [aracı panelinde](/docs/guides/agent-panel) ekleyebilirsiniz.

## Veri kümesi bulun

Kitaplık veri kümelerinizi listeler. Listeyi filtrelemek için **Ada göre ara** alanını kullanın. Başkalarının sizinle paylaştığı veri kümeleri **Benimle paylaşılan** altında görünür.

Bir veri kümesini seçtiğinizde şu eylemler görünür:

- **Bu veri kümesini etiketle**, [etiketleyiciyi](/docs/guides/tagger) bu veri kümesiyle açar.
- **Veriyi düzenle**, veri kümesi düzenleyicisini açar.
- **Paylaş**, paylaşım iletişim kutusunu açar.
- **Yeniden adlandır**, veri kümesinin adını değiştirir.

Diğer eylemler **Kopyasını kaydet** ve **Sil** eylemleridir.

> [!WARNING]
> Silme, veri kümesini kitaplığınızdan kalıcı olarak kaldırır. Bu işlemi geri alamazsınız. Veri kümesinden oluşturulan çalıştırmalar geçerli kalır, ancak veri kümesine olan bağlantıları kopar.

## Veri kümesini düzenleyin

Düzenleyici veri kümesini bir ızgara olarak gösterir. Her sayfada 100 satır gösterir. Sayfalar arasında geçmek için **Sonraki satırlar** ve **Önceki satırlar** düğmelerini kullanın.

### Satırları ve sütunları değiştirin

- **Satır ekle** ve **Sütun ekle**, sona yeni hücreler ekler.
- **Üste satır ekle** ve **Alta satır ekle**, seçili satırın yanına bir satır ekler.
- **Satırı çoğalt**, **Satırı sil**, **Sütunu sil** ve **Sütunu yeniden adlandır** yapıyı değiştirir.
- **Doldurmak için sürükleyin**, bir hücrenin değerini yanındaki hücrelere kopyalar.
- **Geri al** ve **Yinele**, son değişikliğinizi geri alır veya yineler.

Bir satırı tam olarak görmek için üzerine çift tıklayın.

### Kaydetme

Düzenleyici değişikliklerinizi her düzenlemeden kısa bir süre sonra otomatik olarak kaydeder. Kaydederken durum **Kaydediliyor…** gösterir. Kaydetme başarısız olursa durum **Kaydetme başarısız oldu** gösterir.

> [!NOTE]
> Düzenleyici satırı olmayan bir veri kümesini kaydedemez. Bir satır ekleyene kadar "Değişikliklerinizi kaydetmek için bir satır ekleyin" mesajını gösterir.

İşiniz bittiğinde **Kitaplığa dön** düğmesine tıklayın.

### Salt okunur erişim

Paylaşılan bir veri kümesinde Görüntüleyici rolünüz varsa düzenleyici salt okunur modda açılır. Verileri değiştirmek için sahipten Düzenleyici rolünü isteyin veya **Kopyasını kaydet** seçeneğini kullanın.

### Yapay zekâ ile etiketleyin

Düzenleyicide bir **Yapay zekâ ile etiketle** eylemi vardır. Bu eylem etiketleyiciyi açar; orada bir yapay zekâ modeli etiket eklemenize yardım eder. Bkz. [Etiketleyiciyle veri etiketleyin](/docs/guides/tagger).

## Sütun rollerini belirleyin

Bir çalıştırma, hangi sütunların girdi olduğunu ve hangi sütunun beklenen çıktı olduğunu bilmelidir. Bu rolleri veri kümesi düzenleyicisinde değil, bir çalıştırma gönderirken belirlersiniz.

Gönderme sihirbazında "Veriler nasıl okunacak" adımı her sütunu üç seçenekle gösterir:

- **Girdi**: puanlayıcınız bu değeri `case["input"]` içinde alır.
- **Çıktı**: puanlayıcının karşılaştırdığı beklenen yanıt.
- **Atla**: çalıştırma bu sütunu yok sayar.

Varsayılanlar şunlardır:

- Her sütun bir girdidir ve son sütun çıktıdır.
- Tek sütunlu bir veri kümesinde bir girdi vardır ve çıktı yoktur.

Çıktı sütunu yoksa Skynet ilk satırı tek bir girdi olarak kullanır. En az bir girdi sütunu seçmelisiniz.

Bazı sütunlar görüntü de içerebilir. Bu sütunlarda Skynet'e değerleri nasıl okuyacağını söylemek için **Görüntü** veya **Metin** seçin.

> [!TIP]
> Veri kümesi düzenleyicisi, çıktı sütunlarında bir "output" rozeti gösterir. Düzenleyicide eklediğiniz yeni bir sütun girdi olarak başlar.

Sihirbaz satırları eğitim ve değerlendirme için bölümlere de ayırır. Bölümlerin toplamı 1 olmalıdır. Toplam 1 değilse sihirbaz "Bölme oranlarının toplamı 1 olmalıdır." mesajını gösterir. Akışın tamamı için [Çalıştırma gönderin](/docs/guides/submit) sayfasını okuyun.

## Veri kümesini paylaşın

Bir veri kümesi seçin ve **Paylaş** düğmesine tıklayın. **Paylaşım ve gizlilik** iletişim kutusu açılır.

### Bağlantı erişimi

| Seçenek                       | Veri kümesini kim açabilir                                                   |
| ----------------------------- | ---------------------------------------------------------------------------- |
| **Kısıtlı**                   | Yalnızca sahip ve davet edilen kişiler.                                      |
| **Bağlantıya sahip herkes**   | Bağlantıya sahip, oturum açmış herkes; seçtiğiniz rolle.                     |

"Bağlantıya sahip herkes" ayarlı bir veri kümesi için bağlantının verdiği rolü seçin: **Görüntüleyici** veya **Düzenleyici**. URL'yi kopyalamak için **Bağlantıyı kopyala** düğmesine tıklayın.

### Kişileri davet edin

1. **Kişileri davet et** altında, **Kullanıcı adıyla kişi ekleyin** alanına bir kullanıcı adı yazın.
2. **Davet et** düğmesine tıklayın.
3. **Erişimi olan kişiler** altındaki listeyi kontrol edin. Bir kişinin rolünü değiştirmek için **Rolü değiştir** seçeneğini kullanın.

Roller şunlardır:

| Rol           | Erişim                                                                          |
| ------------- | ------------------------------------------------------------------------------- |
| Sahip         | Paylaşımı ve silmeyi yönetmek dahil tam denetim.                                |
| Düzenleyici   | Görüntüleyici erişimine ek olarak modeli çalıştırma, sohbet ve yeniden adlandırma. |
| Görüntüleyici | Veri kümesini açar. Düzenleyici salt okunurdur.                                 |

### Sahipliği devredin

Veri kümesini başka bir kişiye vermek için **Sahipliği devret** seçeneğini kullanın. Skynet sizden onay ister.

> [!WARNING]
> Devirden sonra düzenleyici olursunuz ve sahip ayrıcalıklarınızı kaybedersiniz.

## Sonraki adımlar

- Veri kümenizle [çalıştırma gönderin](/docs/guides/submit).
- Satırlarınızın henüz etiketi yoksa [etiketleyiciyle veri etiketleyin](/docs/guides/tagger).
- Skynet'in çıktıları nasıl karşılaştıracağını seçmek için [puanlayıcılar](/docs/concepts/scorers) hakkında okuyun.
