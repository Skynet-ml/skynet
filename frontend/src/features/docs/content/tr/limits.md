Skynet; çalıştırmalara, dosyalara, depolamaya ve istek hızlarına limitler koyar. Bu limitler platformu kararlı tutar ve bakiyenizi korur. Bu sayfa varsayılan değerleri listeler. Bir yönetici bir hesap için bazı limitleri değiştirebilir.

> [!NOTE]
> Bu sayfadaki değerler güncel varsayılanlardır. Bir limite ulaştığınızda uygulama veya API, isteği hangi limitin durdurduğunu size söyler.

## Çalıştırmalar

| Limit                          | Free | Pro   |
| ------------------------------ | ---- | ----- |
| Aynı anda etkin çalıştırmalar  | 5    | 20    |
| Kayıtlı çalıştırmalar          | 100  | 1.000 |

Etkin çalıştırma, durumu `pending`, `validating`, `running` veya `paused` olan bir çalıştırmadır. En fazla sayıda etkin çalıştırmanız olduğunda Skynet, bunlardan biri bitene kadar yeni çalıştırmaları reddeder.

Kayıtlı çalıştırma limiti tüm çalıştırmalarınızı sayar. Yenilerine yer açmak için ihtiyacınız olmayan çalıştırmaları silin.

Bir Pro yenilemesi başarısız olursa hesap Pro limitlerini 7 gün boyunca korur. Bundan sonra ödeme başarılı olana kadar Free limitlerini kullanır.

| Limit                                          | Değer                                                                  |
| ---------------------------------------------- | ---------------------------------------------------------------------- |
| Tek bir API klonlama isteğindeki kopya sayısı  | 1 ila 5                                                                |
| Bir çalıştırma için otomatik kurtarma denemesi | 3                                                                      |
| Sözleşme kontrolündeki örnek vakalar           | 3                                                                      |
| Her çalıştırma için saklanan ilerleme olayları | 5.000. Skynet önce en eski olayları kaldırır.                          |
| Bir sandbox'ın ömrü                            | 45 dakika. Daha fazla zamana ihtiyaç duyan bir çalıştırma yeni bir sandbox açar. |

Kurtarma denemelerinin nasıl çalıştığını öğrenmek için [Çalıştırmalar](/docs/concepts/runs) sayfasını okuyun.

## Depolama

| Limit            | Free    | Pro    |
| ---------------- | ------- | ------ |
| Toplam depolama  | 500 MiB | 20 GiB |

Toplam depolama; çalıştırmalarınızı, veri kümelerinizi, günlükleri, ajan sohbetlerini, hazırlanan yüklemeleri ve arama verilerini içerir. Skynet, depolama limitinizi aşacak bir yazma işlemini reddeder.

## Dosyalar ve veri kümeleri

| Limit                                              | Değer                                     |
| -------------------------------------------------- | ----------------------------------------- |
| Kütüphanenize kaydedilen veri kümesi dosyası       | Dosya başına 50 MiB, sıkıştırmadan sonra  |
| Kayıtları içe aktaran bir bağlayıcıdan gelen satırlar | 20.000                                 |
| Bir ajan sohbetine eklenen dosya                   | 20 MiB                                    |
| Tek bir sohbet ekinden okunan metin                | 50.000 karakter                           |
| Dikte için ses kaydı                               | 25 MB                                     |
| GitHub deposu arşivi                               | 200 MiB, sıkıştırılmış                    |
| Çalıştırma sayfasında açılan depo dosyası          | 1 MiB                                     |

Kayıtları içe aktaran bağlayıcılar arasında veritabanı tabloları, LLM izleri ve Notion veritabanları vardır. [Veri kümesi hazırlama](/docs/guides/datasets) sayfasını okuyun.

### Sihirbazdaki çalışma alanı dosyaları

| Limit                              | Değer                                                                   |
| ---------------------------------- | ----------------------------------------------------------------------- |
| Çalışma alanındaki ek dosyalar     | 20                                                                      |
| Dosya adı uzunluğu                 | 120 karakter                                                            |
| Klasör derinliği                   | 3 klasör                                                                |
| Dosya türleri                      | `.py`, `.txt`, `.md`, `.json`, `.yaml`, `.csv`, `.toml`, `.ini`, `.cfg` |

`run.py` ve `metric.py` bir çalıştırmanın giriş dosyalarıdır. Bunları silemez, yeniden adlandıramaz veya taşıyamazsınız.

## İstek hızları

| Limit                                                            | Değer                                         |
| ---------------------------------------------------------------- | --------------------------------------------- |
| Çalıştırma gönderimleri                                          | Kayan bir dakikada hesap başına 30            |
| Dikte kayıtları                                                  | Bir saatte hesap başına 120                   |
| Ajan sohbetleri için okunan dosyalar                             | Bir saatte hesap başına 300                   |
| Hesap istekleri (kayıt, parola sıfırlama, e-posta doğrulama)     | Kayan bir saatte e-posta adresi başına 20     |
| Sihirbaz asistanı turları                                        | Bir UTC gününde hesap başına 50               |
| Arama ve dizinleme belirteçleri (token)                          | Bir ayda hesap başına 2.000.000               |

Bir hız limitini aştığınızda API, HTTP durum kodu `429` döndürür. Bekleyin, ardından isteği yeniden gönderin.

Bir hesap aylık arama ve dizinleme belirteçlerini aştığında Skynet o hesap için yeni öğeleri dizinlemeyi durdurur. Arama bu durumda anlamsal arama kullanılamıyormuş gibi çalışır.

## Para

| Limit                                                   | Değer                                                                         |
| ------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Yeni bir çalıştırma için varsayılan harcama limiti      | 5 $ veya daha fazlaysa başlatmak için gereken tutar                           |
| Başlatmak için gereken tutardaki yedek pay              | 1 $ veya daha fazlaysa en pahalı tek model çağrısının maliyeti                |
| Özel bakiye yükleme                                     | 5 $ ila 1.000 $                                                               |
| Etkileşimli bir model turunu başlatmak için gereken bakiye | 0,05 $                                                                     |

Ücretler ve rezervasyonlar için [Bütçeler ve faturalandırma](/docs/concepts/billing) sayfasını okuyun.

## İlgili sayfalar

- [Çalıştırmalar](/docs/concepts/runs)
- [Bütçeler ve faturalandırma](/docs/concepts/billing)
- [SSS](/docs/faq)
