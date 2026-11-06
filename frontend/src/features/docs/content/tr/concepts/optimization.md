Skynet, Optimize Anything'dir: bir DSPy programını veya bir depoyu metriğinize göre iyileştiren tek bir ürün. Yapıt türü, hangi motorların onun üzerinde çalışabileceğini belirler.

## Ortak sözleşme

Her çalıştırmanın iki dosyası vardır. `run.py` yapıtı tek bir vaka üzerinde çalıştırır. `metric.py` çıktıyı puanlar.

```python tab="run.py"
from skynet import llm


def run(artifact, input):
    return llm(artifact, input=input)
```

```python tab="metric.py"
def score(input, output, expected=None):
    ok = output == expected
    return (1.0 if ok else 0.0), {"feedback": "Match." if ok else "No match."}
```

- `artifact`, optimize edicinin önerdiği mevcut sürümdür.
- `input`, bir vakanın girdi alanlarını tutan bir dict'tir.
- `expected`, vakanın beklenen değeridir; vakanın beklenen değeri yoksa `None` olur.
- `score` bir çift döndürür: bir sayı ve `"feedback"` dizesi içeren bir dict.

`skynet` yardımcı modülü `llm`, `Image` ve `log` sağlar. Çalıştırma kodundan yapılan bir `llm` çağrısı görev modeline gider. Metrikten yapılan bir `llm` çağrısı metrik modeline gider.

> [!NOTE]
> Çalıştırma kodu sandbox içinde kendi sürecinde çalışır. Metriğin kaynak kodunu, beklenen değeri veya metrik anahtarını göremez.

Metrik sözleşmesinin tamamı için [Metrikler ve puanlayıcılar](/docs/concepts/scorers) sayfasını okuyun.

## Yapıt türleri ve motorlar

| Yapıt türü    | Motorlar                                                                     |
| ------------- | ---------------------------------------------------------------------------- |
| Depo | GEPA, Best-of-N, AutoResearch, Meta-Harness, AutoSaddler, ShinkaEvolve |
| DSPy programı | Auto, DSPy GEPA, DSPy MIPROv2, DSPy BootstrapFewShot                         |

Motorlar iki aileye ayrılır:

- **Yerel motorlar** yapıta kara kutu olarak davranır. Depolar üzerinde çalışırlar.
- **DSPy optimize edicileri** bir DSPy programının içinde çalışır. Yalnızca DSPy programı yapıtları üzerinde çalışırlar.

DSPy MIPROv2 ve DSPy BootstrapFewShot çiftlere (beklenen çıktıları olan vakalara) ihtiyaç duyar. Tek bir girdiyle bir DSPy programı yalnızca Auto ve DSPy GEPA'yı kullanabilir.

## Depolar

Yerel motorlar, izin verdiğiniz yollardaki dosyaları değiştirir. Metriğiniz deponun her sürümünü puanlar.

Altı yerel motor farklı arama yöntemleri kullanır:

- **GEPA**, sürümleri yansıtma yoluyla evrimleştirir ve bir Pareto cephesi tutar.
- **Best-of-N**, yansıtma modelinden bağımsız öneriler ister ve en iyisini saklar.
- **AutoResearch**, **Meta-Harness**, **AutoSaddler** ve **ShinkaEvolve**, sürüm önermek için bir kodlama ajanı kullanır.

Her motor ve ayarları için [Motorlar](/docs/concepts/engines) sayfasını okuyun.

## DSPy programı yapıtları

DSPy programı bir yapıt türüdür. `run.py` dosyanız modül düzeyinde bir `dspy.Module` oluşturur ve onu `run` içinde çağırır. Dosyada birden fazla modül varsa optimize edilecek olanı `program` olarak adlandırın.

Bir DSPy optimize edicisi çalıştığında şunlar olur:

1. Skynet modülünüzü bulur ve `run` fonksiyonunuzu onun etrafına sarar. Her vaka sizin kendi `run` fonksiyonunuzdan geçer.
2. DSPy optimize edicisi programı eğitim vakaları üzerinde derler.
3. Seçimi doğrulama vakaları üzerinde yapar.
4. Kazanan, her tahmin edicinin talimat metni ile modülün derlenmiş durumunun tamamıdır. Durum, az örnekli (few-shot) demoları da içerir.

Nihai puanlar, Skynet'in kaydettiği programın aynısını ölçer.

| Optimize edici        | Geri bildirim metnini okur |
| --------------------- | -------------------------- |
| DSPy GEPA             | Evet                       |
| DSPy MIPROv2          | Hayır, yalnızca puan       |
| DSPy BootstrapFewShot | Hayır, yalnızca puan       |

DSPy BootstrapFewShot bir yansıtma modeli kullanmaz.

> [!TIP]
> DSPy yansıtma modeli yalnızca metriğinizin döndürdüğü geri bildirim metnini görür. Metriğin kaynak kodunu hiçbir zaman görmez.

## Auto

**Auto**, sihirbazdaki varsayılan seçimdir.

- **Depo**: Auto kullanılamaz. Tek bir yerel motor seçin.
- Bir DSPy programında Auto, DSPy GEPA'yı çalıştırır.

Auto, metriğinizin geri bildirim metnini okur.

## Hangi motoru seçmeli

| Durumunuz                                                     | Kullanın                                                                 |
| ------------------------------------------------------------- | ------------------------------------------------------------------------ |
| Bir GitHub deposunda kodunuz var | Bir depo. Tek bir yerel motor seçin. |
| Bir DSPy programınız var                                      | Bir DSPy programı yapıtı. Auto ile başlayın.                             |
| Verinizden seçilmiş az örnekli demolar istiyorsunuz           | DSPy BootstrapFewShot veya DSPy MIPROv2 ile bir DSPy programı yapıtı      |

> [!WARNING]
> GEPA, DSPy GEPA ve Auto geri bildirim metninden öğrenir. Geri bildiriminiz her zaman aynı dizeyse bu optimize edicilerin çalışacağı bilgi azalır.

## İlgili sayfalar

- [Motorlar](/docs/concepts/engines)
- [Veri kümeleri ve bölmeler](/docs/concepts/datasets)
- [Metrikler ve puanlayıcılar](/docs/concepts/scorers)
- [Çalıştırma gönderme](/docs/guides/submit)
