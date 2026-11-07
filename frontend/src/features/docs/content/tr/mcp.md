Skynet MCP sunucusu, bir yapay zekâ ajanının Skynet'i sizin adınıza kullanmasını sağlar. Claude, ChatGPT, Codex, Cursor ve diğer MCP istemcileri bir çalıştırma kurulumunu kontrol edebilir, çalıştırmaları başlatabilir, izleyebilir ve durdurabilir. Bağlantıyı tarayıcınızda bir kez onaylarsınız. Ajan her çalıştırma için bir harcama limiti belirler.

## Sunucu URL'si

Her istemci aynı URL'yi kullanır:

```text
https://mcp.skynetml.com/mcp
```

Sunucu Streamable HTTP kullanır. Giriş OAuth 2.1 ile yapılır. İstemciye bir belirteç veya API anahtarı kopyalamazsınız.

## İstemcinizi bağlayın

İstemcinizi seçin. Adımlarını izleyin, ardından [Bağlantıyı onaylayın](#bağlantıyı-onaylayın) bölümüne gidin.

```skynet-mcp-clients

```

## Bağlantıyı onaylayın

Ajan Skynet'i ilk kez çağırdığında tarayıcınızda bir Skynet sayfası açılır.

1. Giriş yapmadıysanız Skynet'e giriş yapın.
2. Sayfayı okuyun. Sayfa ajanın adını, geri döneceği siteyi ve hesabınızı gösterir.
3. Ajanın neler yapabileceğini okuyun:
   - Optimizasyon çalıştırmalarınızı ve sonuçlarını görmek.
   - Bakiyenizden ödenen optimizasyon çalıştırmalarını başlatmak ve durdurmak.
4. Bağlanmak için **Allow**'a tıklayın. Durdurmak için **Deny**'a tıklayın.

**Allow**'a tıkladıktan sonra tarayıcı ajana geri döner. Ajan artık aşağıdaki araçları çağırabilir.

> [!NOTE]
> Ajan her çalıştırma için bir harcama limiti belirler. Bakiye yükleyemez.

## Araçlar

| Araç        | Ne yapar                                                                                          | Erişim |
| ----------- | ------------------------------------------------------------------------------------------------- | ------ |
| `check_run` | Bir çalıştırma kurulumunu, çalıştırmadan ve sizden ücret almadan kontrol eder. Bir harcama limiti önerir. | Okuma  |
| `start_run` | Bir harcama limitiyle bir çalıştırma başlatır.                                                    | Yazma  |
| `get_run`   | Durumu, harcamayı ve çalıştırma bittiğinde sonucu döndürür.                                       | Okuma  |
| `list_runs` | Çalıştırmalarınızı en yeniden başlayarak listeler. Duruma göre filtreleyebilir.                   | Okuma  |
| `stop_run`  | Bir çalıştırmayı durdurur. İkinci bir çağrı, çalıştırmanın zaten bittiğini söyleyen bir hata döndürür. | Yazma  |

Okuma erişimi `runs:read` kapsamını kullanır. Yazma erişimi `runs:write` kapsamını kullanır.

Hiçbir araç bakiye yüklemez, bakiyenizi değiştirmez veya faturalandırma ayarlarını değiştirmez.

## Bir ajan optimizasyonu nasıl çalıştırır

1. Ajan, çalıştırma kurulumuyla `check_run` çağrısı yapar. Skynet statik kontrolleri çalıştırır ve şunları döndürür:
   - Her kontrolün sonucu.
   - Çalıştırmanın başlayabileceği en düşük harcama limiti.
   - Önerilen bir harcama limiti. Bu limit en az 5,00 $'dır.
   - Bilinen bir fiyatı olmayan modeller.
   - Şu anda harcayabileceğiniz tutar.
2. Ajan, bir harcama limitiyle (`max_cost_cents`, 1 ile 1.000.000 ABD senti arası) `start_run` çağrısı yapar. Ayrıca bir `idempotency_key` gönderebilir; böylece yeniden deneme ikinci bir çalıştırma başlatmaz.
3. Kurulum kontrolü hâlâ çalışıyorsa `start_run`, `in_progress` ve `retry_after_seconds` döndürür. Ajan bu süreden sonra aynı girdiyle `start_run` çağrısını tekrarlar.
4. Ajan, çalıştırma bitene kadar `get_run` çağrısı yapar. Nihai durumlar `success`, `failed`, `cancelled` ve `stopped`'dur.

> [!TIP]
> Harcama limitinizi isteminizde ajana söyleyin, örneğin "Bu depoyu optimize et. En fazla 10 $ harca." Ajan bu limiti `start_run` içinde kullanır.

Çalıştırma, harcama limitine ulaştığında durur. Skynet'in bir çalıştırma için nasıl ücret aldığını öğrenmek için [Faturalandırma](/docs/concepts/billing) sayfasını okuyun.

## Güvenlik

- Her istemci kendi yetkisini alır. İstemci parolanızı hiçbir zaman görmez.
- Giriş PKCE (S256) kullanır. İstemci kendini dinamik istemci kaydı veya bir istemci kimliği meta veri belgesi ile kaydeder.
- Erişim belirteçlerinin süresi 1 saat sonra dolar. İstemci bunları bir yenileme belirteciyle yeniler.
- Bir yenileme belirteci 30 gün kullanılmazsa süresi dolar. Bir yetki en fazla 90 gün sürer. Ardından bağlantıyı yeniden onaylarsınız.
- Her yenileme belirteci yalnızca bir kez çalışır. Biri eski bir yenileme belirtecini yeniden kullanırsa Skynet yetkiyi sonlandırır.
- Ajan yalnızca kendi çalıştırmalarınızı görebilir ve değiştirebilir.

Skynet'in hesabınızı ve verilerinizi nasıl koruduğunu öğrenmek için [Güvenlik](/docs/security) sayfasını okuyun.

## Bağlantıyı kesme

Bir ajanın bağlantısını kesmek için Skynet sunucusunu istemciden kaldırın. Çoğu istemci bunun ardından belirteçlerini iptal eder. Bir istemci bunları iptal etmezse yenileme belirtecinin süresi 30 gün kullanılmadıktan sonra dolar.

## Sorun giderme

| Sorun                                          | Ne yapmalı                                                                   |
| ---------------------------------------------- | ---------------------------------------------------------------------------- |
| Bağlantı hemen başarısız oluyor                | Sunucu URL'sini kontrol edin. Tam olarak `https://mcp.skynetml.com/mcp` olmalıdır. |
| "This agent can't connect"                     | Bağlantıyı istemciden yeniden başlatın.                                      |
| "Too many attempts"                            | Birkaç dakika bekleyin, ardından tekrar deneyin.                             |
| İstemci bir tarayıcı açmıyor                   | İstemciyi güncelleyin. OAuth ile uzak MCP sunucularını desteklemelidir.       |
| `start_run` bir bakiye hatasıyla başarısız oluyor | Skynet'te bakiye yükleyin veya çalıştırmayı daha düşük bir harcama limitiyle başlatın. |
| Araçlar birkaç hafta sonra çalışmayı bırakıyor | Yetkinin süresi doldu. Sunucuyu kaldırın ve yeniden bağlanın.                |

Ajan olmadan doğrudan HTTP erişimi için [API](/docs/api) sayfasını okuyun.
