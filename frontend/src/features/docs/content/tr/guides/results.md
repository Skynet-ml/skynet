Çalıştırma sayfası bir çalıştırmanın ilerlemesini ve sonuçlarını gösterir. Bu kılavuz çalıştırma sayfasındaki sekmeleri, puanları ve en iyi sürümün nasıl dışa aktarılacağını açıklar.

## Çalıştırma sayfasındaki sekmeler

Çalıştırma sayfası yalnızca çalıştırmaya uyan sekmeleri gösterir. Bazı sekmeler duruma ve deneyim düzeyinize bağlıdır.

| Sekme                      | Ne zaman görünür                                              | Ne içerir                                                                  |
| -------------------------- | ------------------------------------------------------------- | -------------------------------------------------------------------------- |
| **Genel bakış**            | Her zaman                                                     | İlerleme, puanlar ve puan geçmişi                                          |
| **Yapay nesne**            | Sonucu olan bir çalıştırma                                    | Deponuzun sürümleri ve en iyi sürüm                                        |
| **Veri**                   | Vakaları olan bitmiş bir çalıştırma                           | Eğitim, doğrulama ve test kümelerine bölünmüş veri                         |
| **Kod**                    | Çalıştırmanın gösterilecek kodu olduğunda                     | Puanlayıcı                                                                 |
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

Skynet depoyu çalıştırma için GitHub'da oluşturduysa her adayı kendi dalına, `skynet/candidate-<version id>` dalına gönderir. `skynet/best` dalı her zaman en iyi sürümü gösterir. Daha iyi bir sürüm çıktığında bu dal yer değiştirir.

## Sonuçları paylaşın

Bir çalıştırmayı başkalarıyla paylaşabilir veya herkese açık Keşfet galerisinde listeleyebilirsiniz. [Paylaşma ve yayımlama](/docs/guides/sharing) sayfasını okuyun.
