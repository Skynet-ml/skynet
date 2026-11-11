Skynet Mac uygulaması Skynet'i kendi bilgisayarınızda çalıştırır. Model çağrılarını Mac'inizde oturum açılmış bir kodlama CLI'ı üzerinden gönderir; bu yüzden bu çağrıları CLI planınız öder. Uygulamayı barındırılan Skynet hesabınıza da bağlayabilirsiniz.

## Gereksinimler

- Apple silicon işlemcili bir Mac. Intel derlemeleri vardır, ancak kimse onları Intel donanımında test etmedi.
- Çalışır durumda ve **Use Rosetta for x86_64/amd64 emulation** seçeneği açık olan Docker Desktop.
- Oturum açılmış bir kodlama CLI'ı:
  - Claude Code (`claude`) 2.1.226 veya daha yenisi, ya da
  - Codex (`codex`) 0.160.0 veya daha yenisi.

> [!WARNING]
> Codex bir ChatGPT oturumu kullanmalıdır. API anahtarıyla açılan oturum geçerli sayılmaz. macOS Anahtar Zinciri'nde tutulan bir oturum da geçerli sayılmaz.

### Codex'i kurun

Codex kimlik bilgilerini bir dosyada tutmalıdır. İlk açılıştan önce şu adımları uygulayın:

1. `~/.codex/config.toml` dosyasını açın ve şu satırı ekleyin:

```toml
cli_auth_credentials_store = "file"
```

2. ChatGPT hesabınızla oturum açın:

```bash
codex login
```

## Uygulamayı yükleyin

Derlemeler ad hoc olarak imzalanır ve noter onaylı (notarized) değildir. Karantina işaretini kaldırana kadar macOS onları engeller.

1. `.dmg` dosyasını açın.
2. **Skynet** uygulamasını **Applications** klasörüne sürükleyin.
3. Terminal'de şu komutu çalıştırın:

```bash
xattr -dr com.apple.quarantine /Applications/Skynet.app
```

4. Skynet'i **Applications** klasöründen açın.

> [!NOTE]
> İlk açılış birkaç dakika sürer. Uygulama yerel bir sandbox imajı oluşturur ve veritabanını hazırlar.

## Açılışta ne olur

Uygulama şu adımları sırayla uygular:

1. Docker'ı ve CLI'ınızı bulabilmek için oturum açma kabuğunuzdan `PATH` değerini okur.
2. Docker'ı ve kodlama CLI'ınızı kontrol eder.
3. Docker içinde yerel bir Postgres veritabanı başlatır.
4. `linux/amd64` için `skynet-local-sandbox` sandbox imajını oluşturur. Docker bu imajı Rosetta altında çalıştırır.
5. Veritabanı şemasını günceller.
6. Yerel API'yi ve web uygulamasını başlatır ve web uygulamasını pencerede gösterir.

### Başlangıç kontrolleri

Uygulama bir kontrol listesi gösterir: **Docker**, **Coding CLI**, **Signed in**, **Usage limit**, **Local services** ve **Account**. Bir kontrol başarısız olursa uygulama ne yapmanız gerektiğini söyleyen bir ekran gösterir:

| Ekran                      | Ne yapmalı                                                                        |
| -------------------------- | --------------------------------------------------------------------------------- |
| **Install Docker Desktop** | Docker Desktop'ı yükleyin.                                                        |
| **Start Docker**           | Docker Desktop'ı başlatın.                                                        |
| **Install a coding CLI**   | Claude Code veya Codex yükleyin.                                                  |
| **Update your coding CLI** | CLI'ı yukarıdaki en düşük sürüme güncelleyin.                                     |
| **Sign in to your CLI**    | Terminal'de CLI'a giriş yapın.                                                    |
| **Usage limit reached**    | Sıfırlanma zamanını bekleyin. Yeni işler sıfırlamadan sonra yeniden başlayabilir. |

Sorunu düzelttikten sonra **Check again** düğmesine tıklayın. Günlük dosyalarını görmek için **Open logs** düğmesine tıklayın. **Usage limit reached** ekranında **Open Skynet anyway** düğmesine tıklayabilirsiniz.

## Yerel çalıştırmalar

Uygulamada model çağrıları Mac'inizdeki CLI üzerinden gider. Model menüsü bu modu **Yerel** olarak gösterir. İpucu şöyle der: "Çağrıları planınız öder, bu yüzden bu model çalıştırmaya 0 $ ekler."

Uygulamadaki CLI adları **Claude Code** ve **Codex**'tir.

> [!NOTE]
> Uygulama, başlattığı işlemlerin ortamından API anahtarlarını kaldırır. Bu yüzden CLI bir API anahtarını değil, aboneliğinizi kullanır.

### Önericiyi CLI'ınızda çalıştırın

**Önericiyi kendi CLI'ımda çalıştır** ayarı, önerici ajanın nerede çalışacağını belirler. Seçenekler **Ajan motoru (varsayılan)** ve **Kapalı (yalıtılmış önerici)** seçenekleridir.

**Önerici erişimi**, CLI'ın nelere erişebileceğini belirler:

- **Yalnızca çalışma alanı** varsayılandır. Her CLI'ın kendi sandbox'ına dayanır.
- **Tam erişim**, CLI'ı kullanıcı ayrıcalıklarınızla çalıştırır.

> [!WARNING]
> **Tam erişim** ile CLI, sizin erişebildiğiniz her dosyayı okuyabilir, değiştirebilir ve silebilir, ayrıca ağa erişebilir. Claude'un acceptEdits modu, **Yalnızca çalışma alanı** seçiliyken bile ağı yalıtmaz.

### Uygulamada etiketleme

Mac uygulamasında yapay zekâ ile etiketleme kendi sağlayıcı anahtarınızı gerektirir. **Kendi anahtarınız** altında bir model seçin. Bkz. [Etiketleyiciyle veri etiketleyin](/docs/guides/tagger).

## Skynet hesabınıza bağlayın

Uygulamayı bir hesap olmadan kullanabilirsiniz. Bunun için **Use without signing in** düğmesine tıklayın. Barındırılan hesabınızla eşitlemek için uygulamayı bağlayın.

### Bu Mac'i bağlayın

1. **Sign in to Skynet** adımında uygulama, varsayılan tarayıcınızda **Link a Mac** sayfasını açar.
2. Tarayıcı isterse Skynet'te oturum açın.
3. **Bağlandıktan sonra bu Mac şunları yapabilir:** altındaki listeyi kontrol edin.
4. **Bu Mac’i bağla** düğmesine tıklayın. Sayfa "Mac uygulamasına dönülüyor…" mesajını gösterir.

Oturum açma başarısız olursa veya iptal ederseniz uygulama **Try again** ve **Use without signing in** seçeneklerini gösterir.

Bağlandıktan sonra Mac şunları yapabilir:

- Adınızı ve e-posta adresinizi görmek.
- Bakiyenizi görmek.
- Çalıştırmalarınızı hesabınızla eşitlemek.
- Bakiyenizden ödenen barındırılan modelleri kullanmak.
- Bağlı veri kaynaklarınızı kullanmak.
- Skynet üzerinden web'de arama yapmak ve gezinmek.

Bağlantı güvenli bir oturum açma akışı (PKCE ile OAuth) kullanır. Uygulama token'larını Mac'inizde şifreli olarak saklar. Bağlantıyı kaldırdığınızda Skynet token'ları iptal eder.

### Bağlantı limitleri

| Limit                            | Değer                                     |
| -------------------------------- | ----------------------------------------- |
| Hesap başına bağlı Mac sayısı    | 10                                        |
| Erişim token'ı ömrü              | 1 saat, ardından uygulama onu yeniler     |
| Bağlantı boşta kalma süresi      | 30 gün kullanılmama                       |
| Bağlantının en uzun ömrü         | 365 gün                                   |

Boşta kalma süresi veya en uzun ömür dolduktan sonra Mac'i yeniden bağlayın.

### Bağlantı durumu

Uygulama şu durumlardan birini gösterir: **Yerel mod**, **Bu Mac’e bağlı**, **Eşitlemek için giriş yapın** veya **Bağlı (çevrimdışı)**.

## Yerel hizmetler ve veriler

Tüm yerel hizmetler yalnızca `127.0.0.1` üzerinde dinler:

| Hizmet        | Port aralığı      |
| ------------- | ----------------- |
| API           | 48100 ile 48119   |
| Web uygulaması | 48120 ile 48139  |
| Postgres      | 48140 ile 48159   |

Uygulama verilerini `~/Library/Application Support/Skynet` içinde tutar. `logs/` klasörü `desktop.log`, `api.log`, `frontend.log`, `migrate.log` ve `sandbox-build.log` dosyalarını içerir.

Uygulamadan çıktığınızda uygulama web uygulamasını, API'yi, Postgres'i ve tüm iş konteynerlerini durdurur.

### Uygulamayı sıfırlayın

> [!WARNING]
> Bu komutlar, yerel çalıştırmalarınız ve veri kümeleriniz dahil tüm yerel verileri siler.

Önce uygulamadan çıkın. Ardından şunu çalıştırın:

```bash
docker volume rm skynet-local-pgdata
rm -rf ~/Library/Application\ Support/Skynet
```

## Sonraki adımlar

- [Çalıştırma gönderin](/docs/guides/submit).
- Barındırılan modeller için [faturalandırma](/docs/concepts/billing) hakkında okuyun.
- [Güvenlik](/docs/security) sayfasını okuyun.
