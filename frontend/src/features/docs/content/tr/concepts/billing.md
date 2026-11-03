Skynet ön ödemeli bir bakiye kullanır. Bakiyeye para eklersiniz. Her çalıştırmanın bir harcama limiti vardır ve Skynet çalıştırmanın gerçek kullanımını bakiyenizden düşer. Bu sayfa bakiyeyi, çalıştırma bütçelerini, rezervasyonları, ücretleri ve kendi sağlayıcı anahtarlarınızı açıklar.

> [!NOTE]
> Bu sayfadaki sayılar geçerli varsayılan değerlerdir. Uygulama canlı değerleri gösterir. Bakiyenizi, bakiye yükleme ücretini ve işlem geçmişinizi görmek için **Ayarlar → Faturalandırma** sayfasını açın.

## Bakiye

Bakiyeniz ABD doları cinsindendir. Skynet bakiyeyi sent cinsinden tutar.

Skynet varsayılan olarak ücretsiz bir başlangıç bakiyesi vermez. İlk ücretli çalıştırmanızı başlatmadan önce bakiye ekleyin.

Bakiyeyi **Ayarlar → Faturalandırma** bölümünden eklersiniz. Sabit bir tutar seçebilir veya özel bir tutar girebilirsiniz:

| Bakiye yükleme | Bakiyenize eklenen tutar       |
| -------------- | ------------------------------ |
| Sabit tutarlar | 5 $, 20 $ veya 50 $            |
| Özel           | 5 $ ile 1.000 $ arası her tutar |

Seçtiğiniz tutar, bakiyenize giren tutardır. Bakiye yükleme ücreti bunun üzerine eklenir. Aşağıdaki ücretlere bakın.

**Ayarlar → Kullanım** ne harcadığınızı gösterir. **Ayarlar → Faturalandırma** içindeki işlem geçmişi, bakiye yüklemelerinizi ve çalıştırma ücretlerinizi listeler.

## Çalıştırma bütçeleri

Her çalıştırmanın bir harcama limiti vardır. Limit, çalıştırmanın harcayabileceği en yüksek tutardır. Kurulumu ve optimizasyonu kapsar.

Bir çalıştırmanın başlamadan önce maliyet tahmini yoktur. Limitine kadar harcar, bütçesinde durur ve en iyi sürümünü korur. Skynet'in ne zaman durmaya karar verdiğini görmek için [Çalıştırmalar](/docs/concepts/runs) sayfasını okuyun.

Limiti, gönderme sihirbazının **Bütçe** ekranında, **Harcama limiti** alanında belirlersiniz:

- **Limit belirle** varsayılan moddur. Limit 5 $'dan başlar. Çalıştırmanın başlamak için 5 $'dan fazlasına ihtiyacı varsa limit o tutardan başlar.
- **Limit yok**, çalıştırmanın bitene kadar bakiyenizden harcamasına izin verir. Çalıştırma hiçbir zaman duraklamaz. Bakiyeniz tükenirse çalıştırma durur ve ürettiklerini korur.

### Başlamak için gereken tutar

Sihirbaz **Başlamak için gereken** tutarı gösterir. Bu tutar şunlardan oluşur:

- çalıştırmanın ilk model çağrısından önce açtığı sandbox'lar için ayrılan tutarlar, artı
- ilk model çağrıları için yedek pay. Yedek pay 1 $'dır; seçtiğiniz modellerin en pahalı tek çağrısının maliyeti daha yüksekse o maliyettir.

Limit bu tutarın altındayken sihirbaz devam etmenize izin vermez. Bakiyeniz bu tutarın altındayken de sizi durdurur. Devam etmek için limiti yükseltin veya bakiye ekleyin.

> [!TIP]
> Bir çalıştırma bütçesinde durduğunda, çalıştırma sayfasındaki **Limiti yükselt ve devam et** seçeneğini kullanın. Çalıştırma son kontrol noktasından devam eder.

## Rezervasyonlar

Skynet ücretli bir iş yapmadan önce çalıştırmanın limitinin bir bölümünü ayırır. İş kesinleştiğinde rezervasyonu serbest bırakır ve yalnızca gerçek maliyeti tahsil eder.

Bir bütçede zaten hareket varsa sihirbazdaki **Harcama limiti** kartı hesap dökümünü gösterir:

| Etiket                       | Anlamı                                                                               |
| ---------------------------- | ------------------------------------------------------------------------------------ |
| **Bu limitte kalan**         | Limitin kalan bölümü.                                                                |
| **Şimdiye kadar harcanan**   | Kesinleşen maliyet.                                                                  |
| **Ayrılan**                  | Süren iş için tutulan para. İş kesinleştiğinde Skynet bu tutarı serbest bırakır.     |

## Ücretler

Skynet, sağlayıcı maliyetinin üzerine iki ücret uygular.

| Ücret                             | Geçerli varsayılan                          | Neye uygulanır                                              |
| --------------------------------- | ------------------------------------------- | ----------------------------------------------------------- |
| Bakiye yükleme ücreti             | Tutarın %12,5'i, artı 0,35 $                | Her bakiye yükleme                                          |
| Kullanım marjı                    | Sağlayıcı maliyeti × 1,15 (maliyet artı %15) | Skynet'in anahtarlarıyla yapılan model çağrıları ve sandbox işlem gücü |

### Bakiye yükleme ücreti

Skynet yüzde kısmını bir sonraki sente yukarı yuvarlar ve ardından 0,35 $ ekler. Örneğin 20 $'lık bir bakiye yüklemenin ücreti 2,85 $'dır. Bakiye yükleme ekranı, ödeme yapmadan önce ücreti gösterir.

### Kullanım marjı

Skynet sağlayıcı maliyetini kullanım marjıyla çarpar. Marj, Skynet'in ödediği model çağrılarına ve sandbox işlem gücüne uygulanır. Rezervasyonlar, başlamak için gereken tutar ve uygulamadaki model fiyatları marjı zaten içerir.

Skynet bir model çağrısının sağlayıcı maliyetini şu sırayla bulur:

1. OpenRouter'ın çağrı için bildirdiği maliyet.
2. OpenRouter'ın model için canlı fiyat listesi.
3. LiteLLM fiyat tablosu.
4. Milyon girdi token'ı başına 15 $ ve milyon çıktı token'ı başına 75 $ olan yüksek bir yedek ücret. Bu ücret, Skynet'in bilinmeyen bir modeli hiçbir zaman maliyetinin altında ücretlendirmemesini sağlar.

## Kendi sağlayıcı anahtarlarınız (BYOK)

Model çağrılarını kendi sağlayıcı anahtarınızla çalıştırabilirsiniz. Anahtarları **Ayarlar → Sağlayıcılar** bölümünden ekleyin. OpenRouter için **OpenRouter ile devam et** seçeneğini de kullanabilirsiniz.

Bir model çağrısı anahtarınızı kullandığında:

- Sağlayıcınız model token'larını anahtarınıza faturalandırır.
- Skynet model ücreti almaz.
- Çalışma bütçesi yalnızca sandbox kullanımını kapsar.

Harici bir puanlayıcı veya MCP sağlayıcısının ücretleri doğrudan o sağlayıcıya gider. Bunlar çalıştırmanın harcama limitine dahil değildir.

## Sandbox kullanımı

Çalıştırma kodunuz ve metriğiniz bir sandbox içinde çalışır. Skynet sandbox işlem gücünü ölçer:

1. Bir sandbox başlamadan önce Skynet, çalıştırmanın limitinden bir tutar ayırır.
2. Sandbox bittiğinde Skynet, sandbox sağlayıcısının bildirdiği nihai kullanımı tahsil eder.
3. Kullanım marjı sandbox işlem gücüne uygulanır.

Sandbox işlem gücü, model çağrıları kendi anahtarınızı kullandığında da çalıştırmanın harcama limitine dahildir.

## Planlar

İki plan vardır: Free ve Pro. Pro, eşzamanlı çalıştırmalar ve depolama gibi bazı limitleri yükseltir. Bir Pro yenilemesi başarısız olursa hesap 7 gün boyunca Pro limitlerini korur. Bundan sonra ödeme başarılı olana kadar Free limitlerini kullanır. Bkz. [Limitler](/docs/limits).

## Platform güvenceleri

- Asistanla sohbet gibi etkileşimli bir model turu, başlamadan önce en az 0,05 $ bakiye gerektirir.
- Sihirbaz asistanı ücretsizdir. Her hesap, UTC günü başına 50 sihirbaz asistanı turu başlatabilir.

## İlgili sayfalar

- [Çalıştırmalar](/docs/concepts/runs)
- [Sihirbazla çalıştırma başlatın](/docs/guides/submit)
- [Limitler](/docs/limits)
