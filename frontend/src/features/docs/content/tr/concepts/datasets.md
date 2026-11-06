Veri kümesi, vakalardan oluşan bir tablodur. Her satır bir vakadır. Skynet deponuzun her sürümünü bu vakalar üzerinde puanlayıcınızla puanlar ve sürümleri karşılaştırmak için bu puanları kullanır. Bu sayfa, Skynet'in vakaları nasıl okuduğunu, sütun rollerinin ne işe yaradığını ve vakaları eğitim, doğrulama ve test kümelerine nasıl böldüğünü açıklar.

## Verileri yükleyin

Sihirbazın **Girdiler** adımında iki seçeneğiniz vardır:

- **CSV, JSON veya Excel dosyası yükleyin**. Skynet `.csv`, `.json`, `.xlsx` ve `.xls` dosyalarını kabul eder.
- **Kitaplıktan seç**. Daha önce kaydettiğiniz bir veri kümesini kullanın.

Bir çalıştırmada en fazla 200.000 vaka olabilir.

Kayıtlı veri kümelerini yönetmek için [Veri kümeleri](/docs/guides/datasets) sayfasını okuyun.

## Sütun rolleri

**Veriler nasıl okunacak** bölümünde her sütuna bir rol verin:

| Rol    | Skynet'in sütunla yaptığı                                     |
| ------ | ------------------------------------------------------------- |
| Girdi  | Değeri, puanlayıcınızın aldığı `case["input"]` içine koyar       |
| Çıktı  | Değeri, puanlayıcınızın aldığı `case["expected"]` içine koyar            |
| Atla   | Sütunu kullanmaz                                              |

Her sütunun ayrıca bir türü vardır: **Metin** veya **Görüntü**.

Yalnızca bir sütun Çıktı sütunu olabilir. İkinci bir sütunu Çıktı yaparsanız, ilk sütun Atla olarak değişir.

### Kodunuzun aldığı veriler

Skynet her satır için bir vaka oluşturur:

- `input` bir dict'tir. Her Girdi sütunu için bir anahtarı vardır.
- `expected` tek anahtarlı bir dict'tir: anahtar, Çıktı sütununun adıdır.

Şu tablo için:

| message             | label   | id  |
| ------------------- | ------- | --- |
| I was charged twice | billing | 17  |

`message` = Girdi, `label` = Çıktı ve `id` = Atla rolleriyle kodunuz şunları alır:

```python tab="input"
{"message": "I was charged twice"}
```

```python tab="expected"
{"label": "billing"}
```

> [!TIP]
> Puanlayıcınızda beklenen değeri sütun adıyla okuyun, örneğin `case["expected"]["label"]`.

## Veri modları

Roller, çalıştırmanın veri modunu belirler:

| Mod   | Ne zaman             | Ne olur                                                                                              |
| ----- | -------------------- | ---------------------------------------------------------------------------------------------------- |
| Çift  | Bir sütun Çıktı      | Her satır, bir girdisi ve bir beklenen değeri olan bir vakadır. En az 2 satır gerekir.               |
| Tekil | Hiçbir sütun Çıktı değil | Çalıştırma yalnızca ilk satırın Girdi sütunlarını kullanır. Beklenen değeri olmayan tek bir vakadır. |

Tekil modda Skynet bu tek vakayı eğitim, seçim ve rapor için kullanır. Hiçbir vaka ayrı tutulmaz.

## Bölme

Çift modunda Skynet vakaları üç kümeye böler:

| Küme                 | Ne işe yarar                                                   |
| -------------------- | -------------------------------------------------------------- |
| Eğitim               | Motor, bu vakalar üzerinde sürümler önerir ve test eder.       |
| Doğrulama (val)      | Skynet, en iyi sürümü bu vakalar üzerinde seçer.               |
| Test                 | Skynet, nihai puanı bu vakalar üzerinde raporlar.              |

> [!NOTE]
> Test vakaları hiçbir zaman bir motora, bir önericiye veya sözleşme kontrolüne ulaşmaz. Nihai puan, optimize edicinin görmediği vakalar üzerindedir.

### Varsayılan bölme

| Çift sayısı      | Eğitim | Doğrulama | Test |
| ---------------- | ------ | --------- | ---- |
| 5 veya daha fazla | %70    | %15       | %15  |
| 5'ten az         | %85    | %15       | %0   |

5'ten az çift olduğunda Skynet test kümesi ayırmaz.

### Skynet vakaları nasıl sayar

Skynet oranları, en büyük kalan yöntemiyle tam vaka sayılarına çevirir. Sayıların toplamı her zaman vaka sayısına eşittir. Oranı sıfırdan büyük olan bir küme en az bir vaka alır. Oranı sıfır olan bir küme hiç vaka almaz.

Bölmeden önce Skynet vakaları karıştırır. Karıştırma, çalıştırmanın tohum (seed) değerini kullanır. Bir tohum vermezseniz Skynet vakalardan, hedeften ve yapıttan bir tohum üretir. Bu yüzden aynı çalıştırmayı yeniden gönderirseniz aynı bölmeyi alırsınız.

### Yedek kurallar

- 5'ten az çift olduğunda Skynet test kümesini kaldırır ve eğitim ile doğrulama oranlarını toplamları 1 olacak şekilde ölçekler.
- Eğitim kümesi boşsa Skynet doğrulama vakalarını eğitim için kullanır.
- Doğrulama kümesi yoksa Skynet seçimi eğitim vakaları üzerinde yapar.
- Test kümesi yoksa Skynet doğrulama üzerinde raporlar. Doğrulama kümesi de yoksa eğitim üzerinde raporlar.

> [!WARNING]
> Skynet'in eğitim veya doğrulama üzerinde raporladığı bir puan, ayrı tutulmuş bir puan değildir. Test kümesi almak için en az 5 çift kullanın.

## Bölmeyi değiştirin

Bölme bölümünde **Önerilen bölme** görünür. İki modu vardır:

1. **Öneriyi kullan**: Skynet yukarıdaki varsayılan bölmeyi kullanır.
2. **Manuel seçim**: **Eğitim**, **Doğrulama** ve **Test** değerlerini kendiniz belirlersiniz.

Manuel seçimde her alan 0 ile 1 arasında, 0,05'lik adımlarla bir oran alır. Her alan aldığı vaka sayısını gösterir. Üç oranın toplamı 1 olmalıdır. Değilse sihirbaz "Bölme oranlarının toplamı 1 olmalıdır." mesajını gösterir.

## Sözleşme örnekleri

**Metrik ve çalıştırma** adımındaki sözleşme kontrolü, kurulumunuzu ve puanlayıcınızı başlangıç sürümünde, 3 örnek vaka üzerinde çalıştırır. Skynet bu örnekleri yalnızca eğitim ve doğrulama vakalarından alır, hiçbir zaman testten almaz.

## İlgili sayfalar

- [Veri kümeleri](/docs/guides/datasets)
- [Metrikler ve puanlayıcılar](/docs/concepts/scorers)
- [Çalıştırma gönderin](/docs/guides/submit)
- [Limitler](/docs/limits)
