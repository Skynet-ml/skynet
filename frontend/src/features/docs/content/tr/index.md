Skynet, elinizde zaten olan bir yapıtı iyileştirir. Ona bir başlangıç sürümü, bir dizi örnek vaka ve bir metrik verirsiniz. Skynet yeni sürümler önerir, her birini vakalarınız üzerinde çalıştırır, çıktıyı metriğinizle puanlar ve en iyi sürümü saklar.

## Skynet neyi optimize eder

Skynet iki yapıt türüyle çalışır:

| Yapıt türü    | Ne değişir                                                   |
| ------------- | ------------------------------------------------------------ |
| Depo | Değiştirilmesine izin verdiğiniz yollardaki dosyalar |
| DSPy programı | Bir `dspy.Module` içindeki tahmin edicilerin talimatları      |

Her çalıştırma aynı sözleşmeyi kullanır. Çalıştırma kodunuz `run(artifact, input)` fonksiyonunu tanımlar. Metriğiniz `score(input, output, expected=None)` fonksiyonunu tanımlar. Metrik bir puan ve yazılı geri bildirim döndürür. Optimize edici, bir sonraki sürümü daha iyi yapmak için bu geri bildirimi okur.

> [!NOTE]
> Çalıştırma kodunuz ve metriğiniz bir sandbox içinde, ayrı süreçlerde çalışır. Çalıştırma kodu metriğin kaynak kodunu veya beklenen değerleri göremez.

## Tek ürün, birçok motor

Skynet, Optimize Anything'dir. Hangi motorların bir yapıt üzerinde çalışabileceğine yapıt türü karar verir:

- Depolar yerel motorları kullanır: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler ve ShinkaEvolve.
- DSPy programı yapıtları DSPy optimize edicilerini kullanır: DSPy GEPA, DSPy MIPROv2 ve DSPy BootstrapFewShot.

Bir DSPy programında **Auto**, DSPy GEPA'yı kullanır. Bir depoda tek bir yerel motor seçersiniz. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) ve [Motorlar](/docs/concepts/engines) sayfalarını okuyun.

## Bir çalıştırma nasıl işler

1. Görevi tanımlar ve başlangıç sürümünü belirlersiniz.
2. Vakaları verirsiniz. Skynet bunları eğitim, doğrulama ve test kümelerine ayırır.
3. Bir optimize edici, modeller ve bir bütçe seçersiniz.
4. Çalıştırma kodunu ve metriği yazarsınız. Bir sözleşme kontrolü bunları örnek vakalar üzerinde çalıştırır.
5. Skynet optimizasyonu çalıştırır ve sonuçları gösterir.

Skynet sürümleri doğrulama vakaları üzerinde seçer. Nihai puanı test vakaları üzerinde raporlar. Motorlar test vakalarını hiçbir zaman görmez.

## Nereden başlamalı

| Yapmak istediğiniz                    | Okuyun                                                  |
| ------------------------------------- | ------------------------------------------------------- |
| İlk çalıştırmanızı başlatmak          | [Hızlı başlangıç](/docs/quickstart)                     |
| Optimizasyonun nasıl çalıştığını anlamak | [Optimizasyon nasıl çalışır](/docs/concepts/optimization) |
| Bir motor seçmek                      | [Motorlar](/docs/concepts/engines)                      |
| Verinizi hazırlamak                   | [Veri kümeleri ve bölmeler](/docs/concepts/datasets)    |
| Bir metrik yazmak                     | [Metrikler ve puanlayıcılar](/docs/concepts/scorers)    |
| Sihirbazı doldurmak                   | [Çalıştırma gönderme](/docs/guides/submit)              |
| Sonuçlarınızı okumak                  | [Sonuçlar](/docs/guides/results)                        |
| Maliyeti anlamak                      | [Faturalandırma](/docs/concepts/billing)                |
| Skynet'i bir ajandan kullanmak        | [MCP](/docs/mcp)                                        |
| Skynet'i koddan çağırmak              | [API](/docs/api)                                        |
| Limitleri kontrol etmek               | [Limitler](/docs/limits)                                |

> [!TIP]
> Küçük bir veri kümesi ve varsayılan ayarlarla başlayın. Metrik geri bildirimini somut yazın. İyi bir geri bildirim, optimize ediciye tek başına puandan daha fazla bilgi verir.
