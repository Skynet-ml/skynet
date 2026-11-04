Motor, yapıtınızın yeni sürümlerini öneren arama yöntemidir. Skynet'te depolar için altı yerel motor, DSPy programları için üç DSPy optimize edici ve **Auto** vardır. Bu sayfa her birinin ne yaptığını, ne zaman seçileceğini ve hangi ayarlara sahip olduğunu anlatır.

Yapıtınıza hangi motorların uyduğunu bilmiyorsanız önce [Optimizasyon nasıl çalışır](/docs/concepts/optimization) sayfasını okuyun.

## Hızlı karşılaştırma

| Motor                 | Yapıt türleri      | Kodlama ajanı ayarları               | Maks. yineleme ayarı |
| --------------------- | ------------------ | ------------------------------------ | -------------------- |
| Auto | DSPy programı | Hayır | Hayır |
| GEPA | Depo | Hayır | Hayır |
| Best-of-N | Depo | Hayır | Hayır |
| AutoResearch | Depo | Evet | Hayır |
| Meta-Harness | Depo | Evet | Evet |
| AutoSaddler | Depo | Evet | Evet |
| ShinkaEvolve | Depo | Hayır. Kendi düzenleyici ayarı vardır. | Evet |
| DSPy GEPA             | DSPy programı      | Hayır                                | Hayır                |
| DSPy MIPROv2          | DSPy programı      | Hayır                                | Hayır                |
| DSPy BootstrapFewShot | DSPy programı      | Hayır                                | Hayır                |

Sihirbaz GEPA, DSPy GEPA ve Auto'yu metriğinizden gelen geri bildirim metnini kullanan optimize ediciler olarak işaretler.

## Auto

Auto varsayılandır. Ne çalıştırdığı yapıt türüne bağlıdır:

- **DSPy programı**: Auto, DSPy GEPA'yı çalıştırır.
- **Depo**: Auto kullanılamaz. Tek bir yerel motor seçin.

**Şu durumda seçin:** hangi motorun uyduğunu bilmiyorsunuz. Başlamak için en iyi yer burasıdır.

## GEPA

"Sürümlerden oluşan bir Pareto cephesiyle yansıtıcı evrim." Yansıtma modeli her vakanın puanını ve geri bildirimini okur ve daha iyi bir sürüm yazar. GEPA, her biri bazı vakalarda en iyi olan sürümlerden oluşan bir cephe tutar.

**Şu durumda seçin:** metriğiniz somut geri bildirim veriyor ve güçlü tek bir motor istiyorsunuz.

## Best-of-N

"Yansıtma modelinden bağımsız öneriler; en iyisini sakla." Her öneri diğerlerinden bağımsızdır. Skynet en iyi önerileri doğrulama vakaları üzerinde yeniden puanlar.

**Şu durumda seçin:** diğer motorlarla karşılaştırmak için basit bir temel çizgi istiyorsunuz.

## AutoResearch

Bir kodlama ajanı araştırma turları çalıştırır. Bütçe bitene kadar düzenler, puanlar ve işe yarayanı saklar.

**Şu durumda seçin:** iyi bir değişiklik, örneğin bir Kod yapıtında.

## Meta-Harness

Bir kodlama ajanı her yinelemede bir aday grubu önerir. Adayların ve puanların tüm geçmişini kullanır.

**Şu durumda seçin:** önceki tüm denemelerin geçmişinden öğrenen bir ajan istiyorsunuz. Grup boyutunu ayarlamak için **Yineleme başına aday** ayarını kullanın.

## AutoSaddler

Bir kodlama ajanı metriğinizden gelen geri bildirimi okur ve sürüme yama uygular. Skynet yalnızca yeni bir puanlayıcı çalıştırmasının doğruladığı yamaları saklar.

**Şu durumda seçin:** metrik geri bildiriminiz açık, yerel kusurlara işaret ediyor.

## ShinkaEvolve

"Birçok sürümü paralel adalarda evrimleştirir ve onları en çok hangi modellerin iyileştirdiğini öğrenir." Bir aday popülasyonu üzerinde, bir modelin yazdığı mutasyonlar ve çaprazlamalarla yapılan evrimsel bir aramadır.

**Şu durumda seçin:** geniş bir keşif istiyorsunuz ve daha büyük bir bütçeniz var.

## DSPy optimize edicileri

| Optimize edici        | Ne yapar                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| DSPy GEPA             | Metrik geri bildirimini okur ve her tahmin edicinin talimatlarını evrimleştirir.                                                 |
| DSPy MIPROv2          | Talimatları ve az örnekli demoları birlikte önerir ve kombinasyonlarını Bayesçi optimizasyonla arar.                             |
| DSPy BootstrapFewShot | Programın doğru yaptığı çözülmüş örnekleri toplar ve bunları az örnekli demolar olarak ekler. Bir yansıtma modeli kullanmaz.      |

DSPy MIPROv2 ve DSPy BootstrapFewShot çiftlere (beklenen çıktıları olan vakalara) ihtiyaç duyar.

## Ayarlar

### Bütçe

| Ayar                                  | Varsayılan | Aralık                                                          |
| ------------------------------------- | ---------- | --------------------------------------------------------------- |
| Maksimum puanlayıcı çalıştırması      | 200        | 1 ila 100000                                    |
| Şu puanda dur                         | Kapalı     | 0 veya daha fazla                                               |
| Maks. yineleme                        | Kapalı     | 1 ila 1000. Yalnızca Meta-Harness, AutoSaddler ve ShinkaEvolve. |

Bir puanlayıcı çalıştırması, metriğinizin tek bir vaka üzerinde bir kez çağrılmasıdır. Başlangıç sürümünün ve kazananın son yeniden puanlaması **Maksimum puanlayıcı çalıştırması** sınırına sayılmaz.

### Kodlama ajanı ayarları

Bu ayarlar AutoResearch, Meta-Harness ve AutoSaddler için geçerlidir.

| Ayar                                   | Varsayılan | Aralık                                            |
| -------------------------------------- | ---------- | ------------------------------------------------- |
| Kodlama ajanı                          | Codex      | Pi, Codex, OpenCode, Prime Agent, Özel komut      |
| Yineleme başına aday                   | 3          | 1 ila 8. |
| Aday başına en fazla araç çağrısı      | 100        | 1 ila 1000                                        |

### ShinkaEvolve ayarları

| Ayar                                               | Varsayılan                                                       |
| -------------------------------------------------- | ---------------------------------------------------------------- |
| Yeni sürümlerin nasıl yazıldığı                    | Tek model çağrısı. Diğer seçenek "Kodlama ajanı (Pi)"dir.        |
| Adalar                                             | 2 (1 ila 8)                                                      |
| Göç aralığı                                        | 10                                                               |
| Ebeveyn seçimi                                     | Ağırlıklı. Diğer seçenekler: Güç yasası, Işın araması.           |
| Fark düzenlemeleri / Tam yeniden yazımlar / Çaprazlamalar | %60 / %30 / %10. Toplamları %100 olmalıdır.               |
| Arşiv boyutu                                       | 40                                                               |
| Paralel değerlendirmeler                           | 2                                                                |
| Paralel öneriler                                   | 2                                                                |

## İlgili sayfalar

- [Optimizasyon nasıl çalışır](/docs/concepts/optimization)
- [Metrikler ve puanlayıcılar](/docs/concepts/scorers)
- [Çalıştırmalar](/docs/concepts/runs)
- [Faturalandırma](/docs/concepts/billing)
