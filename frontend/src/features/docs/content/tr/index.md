Skynet, elinizde zaten olan kodu veya sizin için yazdığı kodu iyileştirir. Skynet, Optimize Anything'dir: her çalıştırma bir depoyu optimize eder. Ona bir depo, bir dizi vaka ve bir puanlayıcı verirsiniz. Skynet deponun yeni sürümlerini önerir, her birini puanlayıcınızla puanlar ve en iyi sürümü saklar.

## Skynet neyi optimize eder

Her çalıştırma bir depoyu optimize eder. İki şekilde başlayabilirsiniz:

| Başlangıç              | Ne olur                                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------------------------- |
| Bir depo kullan        | Skynet, bağladığınız bir GitHub deposunda, izin verdiğiniz yollardaki dosyaları değiştirir.          |
| Yeni depo oluştur  | Bir görev ve veri verirsiniz. Skynet hesabınızda yeni bir GitHub deposu oluşturur ve ilk çözümü yazar. |

Depodaki kod herhangi bir şey olabilir: bir istem hattı, bir ajan altyapısı, bir betik veya bir kütüphane. Herhangi bir kütüphaneyi kullanabilir.

Her çalıştırma aynı sözleşmeyi kullanır. Puanlayıcınız `score(repo_path, case)` fonksiyonunu tanımlar. Bir puan ve yazılı geri bildirim döndürür. Optimize edici, bir sonraki sürümü daha iyi yapmak için bu geri bildirimi okur.

> [!NOTE]
> Her sürüm bir sandbox içinde çalışır. Sürümleri yazan kodlama ajanı, puanlayıcının kaynak kodunu veya beklenen değerleri göremez.

## Tek ürün, birçok motor

**Auto**, her çalıştırma için varsayılandır. GEPA, AutoResearch, Meta-Harness, AutoSaddler ve ShinkaEvolve'u dener, ardından en iyi sürümden GEPA ile devam eder. Tek bir motor da seçebilirsiniz: GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler veya ShinkaEvolve. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) ve [Motorlar](/docs/concepts/engines) sayfalarını okuyun.

## Bir çalıştırma nasıl işler

1. Görevi tanımlarsınız ve bir depo seçersiniz ya da Skynet'ten bir depo oluşturmasını istersiniz.
2. Vakaları verirsiniz. Skynet bunları eğitim, doğrulama ve test kümelerine ayırır.
3. Bir optimize edici, modeller ve bir bütçe seçersiniz.
4. Puanlayıcıyı yazarsınız. Bir kurulum kontrolü onu başlangıç sürümü üzerinde çalıştırır.
5. Skynet optimizasyonu çalıştırır ve sonuçları gösterir.

Skynet sürümleri doğrulama vakaları üzerinde seçer. Nihai puanı test vakaları üzerinde raporlar. Motorlar test vakalarını hiçbir zaman görmez.

## Nereden başlamalı

| Yapmak istediğiniz                    | Okuyun                                                  |
| ------------------------------------- | ------------------------------------------------------- |
| İlk çalıştırmanızı başlatmak          | [Hızlı başlangıç](/docs/quickstart)                     |
| Optimizasyonun nasıl çalıştığını anlamak | [Optimizasyon nasıl çalışır](/docs/concepts/optimization) |
| Bir motor seçmek                      | [Motorlar](/docs/concepts/engines)                      |
| Verinizi hazırlamak                   | [Veri kümeleri ve bölmeler](/docs/concepts/datasets)    |
| Bir puanlayıcı yazmak                 | [Metrikler ve puanlayıcılar](/docs/concepts/scorers)    |
| Sihirbazı doldurmak                   | [Çalıştırma gönderme](/docs/guides/submit)              |
| Sonuçlarınızı okumak                  | [Sonuçlar](/docs/guides/results)                        |
| Maliyeti anlamak                      | [Faturalandırma](/docs/concepts/billing)                |
| Skynet'i bir ajandan kullanmak        | [MCP](/docs/mcp)                                        |
| Skynet'i koddan çağırmak              | [API](/docs/api)                                        |
| Limitleri kontrol etmek               | [Limitler](/docs/limits)                                |

> [!TIP]
> Küçük bir veri kümesi ve varsayılan ayarlarla başlayın. Puanlayıcı geri bildirimini somut yazın. İyi bir geri bildirim, optimize ediciye tek başına puandan daha fazla bilgi verir.
