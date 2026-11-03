Bu sayfa, Skynet'in hesabınızı, anahtarlarınızı ve verilerinizi nasıl koruduğunu açıklar. Ayrıca bir güvenlik sorununu nasıl bildireceğinizi anlatır.

## Bir güvenlik açığı bildirme

Güvenlik sorunlarını gizli olarak bildirin. Herkese açık bir issue açmayın.

- GitHub'da Skynet deposunu açın ve **Security** > **Report a vulnerability** bölümüne gidin.
- Veya `security@skynetml.com` adresine e-posta gönderin.

Ekip her bildirimi beş iş günü içinde yanıtlamayı hedefler.

## Hesabınız

### Giriş yöntemleri

Şunlarla giriş yapabilirsiniz:

- Bir e-posta adresi ve parola.
- **Google ile devam et** veya **GitHub ile devam et**.
- **Geçiş anahtarıyla giriş yap**. Geçiş anahtarı Face ID, Touch ID veya bir donanım anahtarı kullanır.

Kullandığınız sağlayıcıları **Bağlı hesaplar** altında yönetebilirsiniz.

### Parola kuralları

Bir parola:

- En az 8 karakter olmalıdır.
- En fazla 128 karakter olmalıdır.
- E-posta adresinizi içermemelidir.

Yeni bir hesap, e-posta adresini bir kodla doğrulamalıdır. Parola sıfırlama da Skynet'in e-postayla gönderdiği bir kodu kullanır.

Tekrarlanan başarısız denemelerden sonra Skynet, bir e-posta adresi için girişi bir süreliğine kilitler.

### İki adımlı doğrulama

Daha fazla koruma için iki adımlı doğrulamayı açın. Bir yöntem seçin:

| Yöntem                          | Nasıl çalışır                                                       |
| ------------------------------- | ------------------------------------------------------------------- |
| **Kimlik doğrulama uygulaması** | Kimlik doğrulama uygulamanızdaki 6 haneli kodu girin.               |
| **E-posta kodları**             | Skynet'in her girişte gönderdiği 6 haneli kodu girin.               |

Skynet size ayrıca kurtarma kodları verir. Bunları güvenli bir yerde saklayın. Yönteminize erişimi kaybederseniz bunlardan birini kullanın.

> [!NOTE]
> Google veya GitHub ile giriş yaparsanız iki adımlı doğrulamanızı o sağlayıcı yönetir.

## Kodunuz nerede çalışır

Kullanıcı iş yükleri yalıtılmış sandbox'larda çalışır. Barındırılan üründe yürütme sınırı Vercel Sandbox'tır. Bu; kurulum kontrollerini, optimizasyon çalıştırmalarını, kurtarmayı ve tamamlanmış çalıştırmalar üzerindeki işleri kapsar.

Skynet çalışanları (worker) kodunuzu çalıştırmaz. Onlar kontrol düzlemidir. İşleri başlatır, bütçeleri ayırır ve kapatır, sandbox'lara kapsamı sınırlı kimlik bilgileri verir ve kontrol noktalarını kaydeder.

Skynet ücretli işe başlamadan önce cüzdanınızda ve çalıştırma bütçesinde pay ayırır. [Faturalandırma](/docs/concepts/billing) sayfasına bakın.

[Mac uygulamasında](/docs/guides/mac-app) işler yerel Docker kapsayıcılarında çalışır. Tüm yerel hizmetler yalnızca `127.0.0.1` üzerinde dinler.

## Sağlayıcı anahtarları (BYOK)

Kendi model sağlayıcı anahtarlarınızı ekleyebilirsiniz.

- Skynet her anahtarı bekleme sırasında şifreler.
- Skynet tam anahtarı API üzerinden hiçbir zaman döndürmez.
- Skynet yalnızca son dört karakteri ve bir durumu saklar: doğrulanmamış, doğrulanmış veya geçersiz.
- Bir anahtar girdiğinizde Skynet onu kontrol etmek için küçük bir test isteği gönderir.

Sağlayıcınız model belirteçlerini kendi anahtarınıza faturalandırır. Skynet model ücreti almaz. Çalışma bütçesi yalnızca sandbox kullanımını kapsar.

Skynet ayrıca günlüklerden ve hata mesajlarından gizli bilgileri kaldırır. Bunları `[REDACTED]` ile değiştirir.

## API belirteçleri

API belirteçleri, betiklerin ve araçların Skynet API'sini çağırmasını sağlar.

- Her kullanıcının en fazla bir geçerli belirteci olur. Yeni bir belirteç oluşturduğunuzda eski belirteç çalışmayı bırakır.
- Belirteçler `skyd_` ile başlar.
- Skynet belirtecin yalnızca bir karmasını (hash) saklar.
- Skynet tam belirteci yalnızca bir kez gösterir. Belirteci hemen kopyalayın ve saklayın.
- Bundan sonra Skynet yalnızca maskelenmiş bir biçim gösterir: `skyd_…` ve son dört karakter.

Belirteci `Authorization` başlığında gönderin:

```http
Authorization: Bearer skyd_...
```

Bir belirteci devre dışı bırakmak için **Belirteci iptal et**'e tıklayın. API'nin tamamı için [API](/docs/api) sayfasını okuyun.

> [!WARNING]
> Belirteci bir parola gibi koruyun. Belirtece sahip olan herkes API üzerinden sizin adınıza işlem yapabilir. Belirteci bir depoya commit etmeyin.

## Model veri gizliliği

**Model veri gizliliği** ayarı, model sağlayıcılarının verilerinizle neler yapabileceğini denetler. Skynet'in bakiyenizden faturalandırdığı optimizasyonlar için geçerlidir.

| Seçenek              | Etkisi                                                                                                   |
| -------------------- | -------------------------------------------------------------------------------------------------------- |
| **Tümüne izin ver**  | İstemlerinizle eğitim yapabilen veya bunları saklayabilen sağlayıcılar dahil, her sağlayıcı.             |
| **Eğitim yok**       | Yalnızca istemlerinizle eğitim yapmayan ve bunları saklamayan sağlayıcılar. Varsayılan budur.            |
| **Sıfır saklama**    | Yalnızca sıfır veri saklama sözleşmesi olan uç noktalar. Bazı modeller kullanılamayabilir.               |

> [!NOTE]
> Kendi anahtarınızla yapılan optimizasyonlar OpenRouter hesabınızın gizlilik ayarlarına uyar.

## Çalıştırmaların görünürlüğü

> [!WARNING]
> Yeni çalıştırmalar varsayılan olarak **Herkese açık**'tır. Herkese açık bir çalıştırma genel aramada görünür. Herkes onu bulabilir ve sonuçlarına göz atabilir.

Bir çalıştırmayı genel aramanın dışında tutmak için gönderirken **Görünürlük**'ü **Özel** olarak ayarlayın. Görünürlüğü daha sonra paylaşım iletişim kutusunda değiştirebilirsiniz. [Paylaşım](/docs/guides/sharing) sayfasına bakın.

Yeni veri kümeleri **Kısıtlı**'dır. Bağlantı erişimini değiştirene kadar yalnızca siz ve davet ettiğiniz kişiler bunları açabilir. [Veri kümesi hazırlama](/docs/guides/datasets) sayfasına bakın.

## Verileriniz

Verilerinizi yönetmek için **Ayarlar**'da **Gizlilik ve veri** sekmesini açın.

### Verilerinizi dışa aktarma

**Verilerimi dışa aktar** altında **Dışa aktar**'a tıklayın. Dışa aktarma, hesabınızdaki tüm verileri içerir. Bir biçim seçin: JSON, CSV, Excel, Pickle, Parquet veya Feather.

### Hesabınızı silme

**Hesabı sil**'e tıklayın. Onaylamak için mevcut parolanızı girin ve e-posta adresinizi yazın.

> [!WARNING]
> Bu işlem hesabınızı ve tüm verilerini kalıcı olarak siler. Geri alamazsınız.

### Kullanım analizleri

**Anonim kullanım analizleri gönder** ayarı, anonim ürün analizlerini açar veya kapatır.

## Sonraki adımlar

- [Limitler](/docs/limits) sayfasını okuyun.
- [SSS](/docs/faq) sayfasını okuyun.
