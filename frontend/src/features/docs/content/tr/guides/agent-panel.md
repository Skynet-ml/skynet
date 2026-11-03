Aracı paneli, Skynet içindeki bir sohbet asistanıdır. Çalıştırmalarınızı, veri kümelerinizi ve bakiyenizi okuyabilir; bir çalıştırmayı yeniden adlandırmak veya iptal etmek gibi işleri sizin yerinize yapabilir. Hangi eylemlerin onayınızı gerektirdiğini siz belirlersiniz.

## Paneli açın

Panel uygulamanın yan tarafında açılır. Paneli küçülttüğünüzde ekranda **Ajanı aç** etiketli bir hap düğmesi kalır. Paneli yeniden açmak için bu düğmeye tıklayın.

Ajanın araçlarının listesini görmek için **Neler yapabilirim** düğmesine tıklayın.

### Konuşmalar

- **Yeni konuşma**, boş bir sohbet başlatır.
- **Geçmiş konuşmalar**, eski sohbetlerinizi listeler. Bunları arayabilir, sabitleyebilir, yeniden adlandırabilir ve silebilirsiniz.

### Mesaj gönderin

Mesajınızı yazın ve Enter tuşuna basın. **Dikte et** düğmesine tıklayıp mesajınızı sesli olarak da söyleyebilirsiniz.

Ajan çalışırken mesaj alanını kullanmaya devam edebilirsiniz:

- Destekleniyorsa **Enter** mevcut işi yönlendirir. Ajan mesajı bir sonraki adımda okur. **Tab** yeni bir turu kuyruğa ekler. Yönlendirme yoksa Enter da kuyruğa ekler.
- **Bekleyen mesajlar** bölümünde mesajları düzenleyebilir, silebilir ve sürükleyerek sıralayabilirsiniz. **Yönlendir**, kullanılabiliyorsa kuyruktaki mesajı mevcut işe gönderir.
- **Onaylanmadı** olarak işaretlenen mesaj aracıya zaten ulaşmış olabilir. Etkin tur yokken mesajın menüsünden açıkça gönderin. İstediğiniz zaman düzenleyebilir veya silebilirsiniz. Kuyruktaki diğer mesajlar önce devam edebilir.
- **Durdur** mevcut turu durdurur ve kuyruğu duraklatır. Okunmamış yönlendirmeler listeye döner. Hiçbir şey otomatik başlamaz. Mesaj alanındaki **Esc** de aynı işi yapar; ancak açık menü veya IME ile metin oluşturma sırasında durdurmaz. Esc önce bu mesaj alanının önerisini veya menüsünü kapatır.
- Etkin tur yokken **Şimdi gönder**, duraklatılmış kuyruğun ilk mesajını gönderir. Diğerleri sırayla devam eder. Normal tur bitişi de sonraki mesajı başlatır.
- **Durdur ve şimdi gönder** turu durdurup tüm listeyi tek bir yeni tur olarak gönderir. Okunmamış yönlendirmelerin dönmesini bekler, ardından bunları kuyruktaki mesajların önüne koyup boş satırlarla birleştirir. Okunmuş yönlendirmeler yeniden gönderilmez. Doğrulanmamış mesajlar listede kalır ve bu tura eklenmez. Diğer mesajlar normal şekilde devam eder.
- Oturum açıldığında, güvenli bir adım sınırı duyurulduktan sonra **Enter** belge asistanını yönlendirir. İleti, modelin zaten planlanan sonraki adımında okunur. **Tab** her zaman kuyruğa ekler. Duyurudan önce, oturum kapalıyken ve sunulan programların korumalı sohbetinde Enter ve Tab kuyruğa ekler. Belge asistanı istek sınırına ulaşınca kalan mesajları duraklatır.

## Ajan neler yapabilir

Ajan şunları okuyabilir:

- Çalıştırmalarınızı, günlüklerini ve test sonuçlarını.
- Analizleri ve cüzdan bakiyenizi.
- Kitaplığınızdaki veri kümelerini ve herkese açık arama sonuçlarını.
- Etiketleme oturumlarınızı (salt okunur).
- Örnek veri kümelerini, motor kataloğunu ve optimize edici kataloğunu.

Ajan işlem de yapabilir. Güvenli işlemler şunlardır:

- Çalıştırmaları yeniden adlandırmak ve sabitlemek, birçok çalıştırmayı tek seferde sabitlemek.
- Bir çalıştırmayı duraklatmak.
- Bir çalıştırmanın kodunu düzenlemek ve doğrulamak.
- Bir veri kümesinin profilini çıkarmak ve sütun rollerini belirlemek.
- Kullanılabilir modelleri bulmak.
- Gönderme sihirbazını ve tercihlerinizi güncellemek.
- Bir örnek veri kümesini hazırlamak.
- Bir klasör oluşturmak.

Yıkıcı işlemler şunlardır:

- Bir veya birçok çalıştırmayı silmek.
- Bir çalıştırmayı göndermek, iptal etmek, klonlamak, yeniden denemek, devam ettirmek veya baştan başlatmak ve birçok çalıştırmayı iptal etmek.
- Bir klasörü silmek, taşımak veya güncellemek ve çalıştırmaları klasörler arasında taşımak.

> [!NOTE]
> Web araçları (arama, bir URL getirme, bir siteyi tarama) yalnızca sunucuda bir web arama anahtarı kuruluysa kullanılabilir. Bunları görmüyorsanız Skynet örneğiniz bu araçları sunmuyordur.

## Güven modları

Güven modu, hangi işlemlerin onayınızı gerektirdiğini belirler. Değiştirmek için yazma alanındaki güven düğmesine tıklayın. Düğme üç mod arasında sırayla geçiş yapar.

| Mod                | Açıklama                                            | Güvenli işlemler | Yıkıcı işlemler |
| ------------------ | --------------------------------------------------- | ---------------- | --------------- |
| **Sor**            | Her işlemden önce onay ister.                       | Onay gerekir     | Onay gerekir    |
| **Otomatik**       | Güvenli işlemleri onay olmadan yapar.               | Hemen çalışır    | Onay gerekir    |
| **Kısıtlamasız**   | Her şeyi onay olmadan yapar.                        | Hemen çalışır    | Hemen çalışır   |

Varsayılan mod **Sor** modudur. Salt okunur araçlar hiçbir zaman onay gerektirmez.

> [!WARNING]
> **Kısıtlamasız** modda ajan, onay olmadan çalıştırmaları silebilir veya ücretli çalıştırmalar gönderebilir. Bu modu yalnızca göreve güvendiğinizde kullanın.

### Varsayılan modu belirleyin

1. **Ayarlar** sayfasını açın.
2. **Aracı paneli** sekmesini açın.
3. **Aracı güven düzeyi** altında **Önce sor**, **Yalnızca güvenli işlemler için otomatik** veya **Önceden onay yok** seçeneğini seçin.

### Bir işlemi onaylayın

Bir işlem onay gerektirdiğinde panel bir onay kartı gösterir. Kartta işlem için bir onay düğmesi ve **İptal** düğmesi bulunur. Tam araç çağrısını görmek için **Teknik ayrıntıları göster** düğmesine tıklayın.

15 dakika içinde yanıt vermezseniz ajan işlemi reddedilmiş sayar.

## Dosya ekleyin

Bir mesaja dosya ekleyebilirsiniz. Her dosya en fazla 20 MB olabilir. Tek seferde bir veri kümesi ekleyebilirsiniz.

Ajan aracılığıyla bir veri kümesi yüklemek için:

1. Bir CSV, JSON veya Excel dosyası ekleyin.
2. Yükleme kartını kontrol edin. Kart dosyanın sütunlarını gösterir.
3. Karttaki sütun rollerini belirleyin, ardından onaylayın.

Ajan dosyayı okuyamazsa kart "Dosya okunamadı. CSV, JSON veya XLSX dosyası kullanın." mesajını gösterir.

Dosya kuralları için [Veri kümesi hazırlayın](/docs/guides/datasets) sayfasına bakın.

## Model seçin

Yazma alanındaki model menüsünde şu gruplar vardır:

- **Auto**, modeli Skynet'in seçmesine izin verir.
- **Skynet kredileri**, bakiyenizden ödenen barındırılan bir model kullanır.
- **Kendi anahtarını kullan**, kendi sağlayıcı anahtarınız üzerinden bir model kullanır.

Bazı modeller akıl yürütme düzeyini (reasoning effort) belirlemenize izin verir. Düzeyler **Yok**, **Asgari**, **Düşük**, **Orta**, **Yüksek**, **Çok yüksek** ve **Maksimum** düzeyleridir. **Varsayılan**, modelin varsayılan düzeyini kullanır.

Yeni sohbetler için varsayılan bir model belirlemek üzere **Ayarlar** > **Aracı paneli** > **Varsayılan model** yolunu açın.

### Maliyet

Skynet kredileriyle yapılan ajan sohbetleri bakiyenizi kullanır. Bakiyeniz boşsa panel "Bakiyeniz boş. Ajanla sohbete devam etmek için bakiye ekleyin." mesajını gösterir. Fiyatlar için [Faturalandırma](/docs/concepts/billing) sayfasını okuyun.

## İpuçları

> [!TIP]
> Bir çalıştırmayı değiştirmeden önce ajandan onu açıklamasını isteyin. Örneğin: "42 numaralı çalıştırma neden 41 numaralıdan daha düşük puan aldı?"

> [!TIP]
> Yeni türde bir istek denerken **Sor** modunu kullanın. Ajanın ne yapacağını bildiğinizde **Otomatik** moda geçin.

## Ajan kullanmanın diğer yolları

Kendi yapay zekâ istemcinizi MCP sunucusu üzerinden Skynet'e de bağlayabilirsiniz. Bkz. [MCP](/docs/mcp). Betikler için [API](/docs/api) sayfasını kullanın.

## Sonraki adımlar

- [Çalıştırma gönderin](/docs/guides/submit).
- [Sonuçları okuyun](/docs/guides/results).
- Skynet'in anahtarlarınızı nasıl koruduğunu öğrenmek için [Güvenlik](/docs/security) sayfasını okuyun.
