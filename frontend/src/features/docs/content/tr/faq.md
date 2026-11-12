Bu sayfa Skynet hakkında sık sorulan soruları yanıtlar. Her yanıt, ayrıntıların tamamını içeren sayfaya bağlantı verir.

## Genel

### Skynet nedir?

Skynet, depoları kendi verilerinize ve puanlayıcınıza göre optimize eden bir platformdur. Bir veri kümesi yükler, görevi tanımlar ve bir çalıştırma başlatırsınız. Çalıştırma yeni sürümler dener ve bunları ayrılmış veriler üzerinde puanlar. Başlamak için [Hızlı başlangıç](/docs/quickstart) sayfasını okuyun.

### Skynet neyi optimize edebilir?

Skynet, Optimize Anything'dir: her çalıştırma bir depoyu optimize eder. Elinizde olan bir GitHub deposunu kullanın veya Skynet'in görevinizden ve verinizden yeni bir depo oluşturmasına izin verin. Kod herhangi bir kütüphaneyi kullanabilir.

Her motor depolar üzerinde çalışır: GEPA, Best-of-N, Meta-Harness, AutoResearch, AutoSaddler ve ShinkaEvolve.

**Auto** varsayılandır. Birkaç motorla keşif yapar, ardından en iyi sürümden GEPA ile devam eder. [Optimizasyon nasıl çalışır](/docs/concepts/optimization) ve [Motorlar](/docs/concepts/engines) sayfalarını okuyun.

### Arayüz hangi dilleri destekliyor?

Arayüzde 24 dil vardır. Temel dil İbranicedir ve düzen sağdan sola metni destekler. İbranice belgeler [/docs/he](/docs/he) adresindedir.

### Skynet açık kaynak mı?

Evet. Kod AGPL-3.0 lisansını kullanır. Skynet'in değiştirilmiş bir sürümünü hizmet olarak sunarsanız lisans, değişikliklerinizi paylaşmanızı gerektirir.

## Veri

### Hangi dosya türlerini yükleyebilirim?

CSV, JSON, `.xlsx` ve `.xls` dosyaları yükleyebilirsiniz. Excel dosyalarında Skynet yalnızca ilk sayfayı okur. [Veri kümesi hazırlama](/docs/guides/datasets) sayfasına bakın.

### Verilerimde etiket yok. Ne yapabilirim?

Etiketleyiciyi kullanın. Satırları kendiniz etiketleyebilir veya bir yapay zekâ modelinin etiketlemesine izin verip işini gözden geçirebilirsiniz. [Etiketleyiciyle veri etiketleme](/docs/guides/tagger) sayfasına bakın.

### Çalıştırmalarımı ve veri kümelerimi kim görebilir?

> [!WARNING]
> Yeni çalıştırmalar varsayılan olarak **Herkese açık**'tır. Herkese açık bir çalıştırma genel aramada görünür ve herkes sonuçlarına göz atabilir.

Bir çalıştırmayı genel aramanın dışında tutmak için gönderirken **Görünürlük**'ü **Özel** olarak ayarlayın. Yeni veri kümeleri **Kısıtlı**'dır: yalnızca siz ve davet ettiğiniz kişiler açabilir. [Paylaşım](/docs/guides/sharing) ve [Güvenlik](/docs/security) sayfalarına bakın.

### Model sağlayıcıları istemlerimle model eğitiyor mu?

Varsayılan olarak hayır. Varsayılan **Model veri gizliliği** seçeneği **Eğitim yok**'tur. İstemleri yalnızca bunlarla eğitim yapmayan ve bunları saklamayan sağlayıcılara gönderir. Bu ayar, Skynet'in bakiyenizden faturalandırdığı çağrılar için geçerlidir. OpenRouter üzerinden BYOK çağrıları OpenRouter hesabınızın gizlilik ayarlarına uyar. Doğrudan sağlayıcıya yapılan BYOK çağrıları sağlayıcının veri politikasına uyar. [Güvenlik](/docs/security) sayfasına bakın.

## Faturalandırma

### Skynet çalıştırmalar için nasıl ücret alır?

Skynet ön ödemeli bir bakiye kullanır. Yönetilen model kullanımı ve ek ücreti ile sandbox kullanımı ölçülür. Bakiye yüklemede ücret alınır. Ücretli iş başlamadan önce Skynet cüzdanınızda ve çalıştırma bütçesinde pay ayırır. [Faturalandırma](/docs/concepts/billing) sayfasına bakın.

### Kendi model sağlayıcı anahtarımı kullanabilir miyim?

Evet. Kendi anahtarınızla (BYOK) sağlayıcınız model belirteçlerini kendi anahtarınıza faturalandırır. Skynet model ücreti almaz. Çalışma bütçesi yalnızca sandbox kullanımını kapsar. Skynet anahtarınızı şifreler ve bir daha asla göstermez. [Faturalandırma](/docs/concepts/billing) ve [Güvenlik](/docs/security) sayfalarına bakın.

### Skynet'i model çağrılarına ödeme yapmadan çalıştırabilir miyim?

[Mac uygulaması](/docs/guides/mac-app), Skynet'i kendi bilgisayarınızda çalıştırır. Model çağrılarını Mac'inizde oturum açılmış Claude Code veya Codex üzerinden gönderir. Bu çağrıları CLI planınız öder; bu yüzden çalıştırmaya 0 $ eklerler.

## Araçlar ve entegrasyonlar

### Skynet'i kendi yapay zekâ istemcimden yönetebilir miyim?

Evet. Skynet'in `https://mcp.skynetml.com/mcp` adresinde bir MCP sunucusu vardır. OAuth ile giriş yaparsınız. Sunucuda şu araçlar vardır: `check_run`, `start_run`, `get_run`, `list_runs` ve `stop_run`. [MCP](/docs/mcp) sayfasına bakın.

### Bir API var mı?

Evet. Ayarlar'da bir API belirteci oluşturun, ardından bunu `Authorization: Bearer <token>` olarak gönderin. Her kullanıcının aynı anda yalnızca bir geçerli belirteci olur. [API](/docs/api) sayfasına bakın.

### Aracı paneli çalıştırmalarımı silebilir mi?

Yalnızca siz izin verirseniz. Varsayılan **Sor** modunda ajan her işlemden önce sorar. **Otomatik** modda da silme veya gönderme gibi yıkıcı işlemlerden önce yine sorar. [Aracı paneli](/docs/guides/agent-panel) sayfasına bakın.
