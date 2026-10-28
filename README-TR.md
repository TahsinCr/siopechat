[![Sürüm: v1.4.2-beta][version-shield]](CHANGELOG.md)
[![Python 3.11+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

[English](README.md) | **Türkçe** | [Русский](README-RU.md)

# SiopeChat

SiopeChat; ChatGPT, Gemini ve Claude sohbetlerini aynı terminalde kullanmanızı sağlar. Birden fazla sohbeti açık tutabilir, bölünmüş panellerde yanıtları karşılaştırabilir ve siteler arasında geçmeden projelerinizle çalışabilirsiniz.

Uygulama, giriş yaptığınız kendi web oturumlarınızı kullanır. API anahtarı gerektirmez; hesabınızın model erişimi ve kullanım sınırları geçerlidir. Bağımsız ve resmî olmayan bir istemcidir. Sağlayıcıların sitelerinde yapılan değişiklikler bazı özellikleri etkileyebilir.

![SiopeChat terminal çalışma alanı](assets/siopechat.png)

*Sabit varsayılan stilli ve örnek verili gerçek terminal görüntüsü.*

## Neler sunuyor?

- Ayrı taslak, kaydırma konumu ve yanıt göstergeleri olan sekmeler; grupta en fazla dört bölünmüş panel.
- Hesaba göre model ve düşünme eforu seçimi; her hesap için son seçimin hatırlanması.
- ChatGPT ve Claude projeleri, Gemini defterleri; desteklendiği ölçüde talimat ve kaynak yönetimi.
- Proje ve sohbetlere uygulanan arama, hesap filtresi, tarih ve isim sıralaması.
- Kayıtlı geçmişi önce gösterip arka planda eşitleyen SQLite arşivi.
- Komut paleti, değiştirilebilir kısayollar, fare desteği ve yerel eklentiler.
- İngilizce, Türkçe ve Rusça arayüz katalogları.

**v1.5-beta**, sağlayıcıların yayımladığı kaynakları, düşünme özetlerini ve arama etkinliğini yanıtlara ekler; üretilen dosyaları sohbet içinde indirmeyi ve sohbetleri yeniden adlandırmayı sağlar. Gemini efor seçenekleri hesabın web politikasından alınır. Arka plan geçmiş eşitlemesi ve yanıt sürümleri arasında gezinme iyileştirildi. Python dağıtım sürümü `1.5b0` olur.

## Kurulum ve giriş

SiopeChat için Python **3.11+** gerekir. Giriş yardımcıları ve ChatGPT istek imzalayıcısı Node.js **22+** kullanır; kurulum ve başlatma araçları iki sürümü de önceden denetler. Bağımsız sağlayıcı paketi ayrı olarak Python 3.10+ desteğini korur. Chrome/Chromium yalnızca açıkça başlatılan giriş işlemi için gerekir. Unicode destekli bir terminal kullanın; gerçek renk ve fare raporlama desteği önerilir.

Depoyu indirdiğiniz dizinde tek seferlik kurulum aracını çalıştırın. Python 3.11+ ve Node.js 22+ sürümlerini denetler, bulduğu uyumlu sanal ortamı kullanır ve SiopeChat ile Python bağımlılıklarını o ortama kurar. Ortam bulamazsa `python/` oluşturur; mevcut bir dizini silmez veya üzerine yazmaz.

| Sistem | Kurulum aracı |
| --- | --- |
| Linux / macOS terminali | `./install.sh` |
| macOS Finder | `install.command` |
| Windows PowerShell | `./install.ps1` |
| Windows Komut İstemi | `install.bat` |

| Sistem | Başlatma |
| --- | --- |
| Linux / macOS terminal | `./siopechat.sh` |
| macOS Finder | `siopechat.command` |
| Windows Komut İstemi | `siopechat.bat` |
| Windows PowerShell | `./siopechat.ps1` |
| Kurulu konsol komutu | `siopechat` |

Kurulum ve başlatma araçları ortamları şu sırayla arar: `python/`, `.venv/`, `venv/`, `env/`. Başka bir ortam kullanmak için `SIOPECHAT_VENV` değişkenini ortam dizinine ayarlayın; göreli yollar depo dizinine göre çözülür. Uygun ortam yoksa yalnızca kurulum aracı yenisini oluşturur. Bozuk veya uyumsuz mevcut dizinlere dokunulmaz. Depoyu güncelledikten sonra değişen bağımlılıkları kurmak için kurulum aracını yeniden çalıştırın. Başlatıcılar her açılışta paket kurmaz ve ağa çıkmaz; yalnızca gerekli sürümleri denetleyip ortamı başlatır.

Son kullanıcı kurulumu `pyproject.toml` içindeki sürüm aralıklarını pip ile kullanır; uv yüklemek gerekmez. `uv.lock`, CI ve geliştirme ortamlarında tam bağımlılık çözümünü sabitlemeye devam eder.

Yeni kurulum hesap eklenmeden başlar. İlk hesabınızı Ayarlar → Hesaplar bölümünden ekleyin; otomatik ChatGPT hesap kaydı oluşturulmaz.

**Ayarlar → Hesaplar** bölümünde bir hesap ekleyin veya mevcut hesabınızı seçin; ardından **Profil ile doğrula** seçeneğine basın. Varsayılan tarayıcınızın son kullanılan profilinde sağlayıcı hesabına önceden giriş yapmış olmalısınız. SiopeChat bu oturumu tarayıcı başlatmadan aktarır ve doğrular. Sohbet, geçmiş, model ve proje işlemleri HTTP üzerinden yürür. Oturumun süresi dolarsa normal tarayıcınızda yeniden giriş yapıp **Profil ile doğrula** işlemini tekrarlayın.

Hesap ekle, düzenle ve sil düğmeleri hesap listesinin altında yan yana durur. Düzenle yalnız seçili hesabın görünen adını değiştirir; oturumu ve geçmişi korunur. Profil doğrulama ve çıkış düğmeleri oturum bilgilerinin altındadır. Dar terminallerde düğme adları kısalır.

**Profil ile doğrula**, varsayılan tarayıcının son kullanılan profilinden seçili sağlayıcının açık oturumunu aktarır; tarayıcı başlatmaz ve hata ayıklama bayrağı gerektirmez. Chrome, Chromium, Brave/Brave Origin, Edge, Vivaldi, Firefox ve Zen’in standart profil konumları desteklenir. Yalnız seçili sağlayıcının kimlik doğrulama çerezleri alınır; Firefox’ta varsayılan kapsayıcı kullanılır. Yeni oturum, kayıtlı oturumun yerine yazılmadan önce sağlayıcıda doğrulanır. Sistem anahtarlığı erişim izni isteyebilir. Kilitli veritabanı, özel/yalıtılmış profil konumu veya desteklenmeyen şifreleme (bazı Windows Chromium yapılandırmaları dahil) aktarımı engelleyebilir; kayıtlı oturum silinmeden hata gösterilir. Bu aktarım seçeneği Safari’yi desteklemez. Aktarım hataları; eksik bağımlılık, dosya izni, veritabanı erişimi, anahtarlık erişimi ve çerez çözme sorunlarını ayrı gösterir. Bağımlılıkları SiopeChat’i başlatan Python ortamında güncelleyin. Firefox/Zen’in güncel kurtarma kaydındaki oturum çerezleri de okunur; ayrı container hesapları ve eski kurtarma kayıtları birleştirilmez. Çerez veritabanı kilitliyse WAL/journal dosyalarıyla birlikte özel geçici kopyadan yeniden okunur; asıl profil değiştirilmez.

Firefox ve Zen aynı salt okunur çerez okuyucusunu kullanır. Profil seçimi `profiles.ini` ve `installs.ini` üzerinden yapılır; belirsiz profiller birleştirilmeden hata bildirilir. Linux’ta Firefox’un Snap/Flatpak konumları ve Zen’in güncel XDG, eski ve Flatpak konumları bulunur.

Birden fazla hesap ekleyebilirsiniz. Gönderim için her sağlayıcıda bir hesap seçilidir; başka hesabın geçmişini görüntülemek bu seçimi değiştirmez. Farklı bir hesabı doğrulamak için önce varsayılan tarayıcınızda o hesabın açık olduğu profili seçin, ardından SiopeChat’te ilgili hesapta **Profil ile doğrula** seçeneğini kullanın.

Hesap satırına tıklayarak seçim yapın; ok işareti yönetilen hesabı gösterir. **Hesabı sil** (Hesaplar ekranında Alt+Backspace), onay aldıktan sonra yerel hesap kaydını, kayıtlı oturumu ve model tercihlerini kaldırır. Sağlayıcıdaki hesap ve arşivlenmiş sohbetler korunur. Silinen hesabın sekmeleri kapatılır, geçersiz kalan filtreler temizlenir ve başka etkin hesap seçilir. Son etkin hesabı silmeden önce başka hesap ekleyin.

## Kullanım

Mesaj alanına yazıp **Enter** ile gönderin. Destekleyen terminallerde **Shift+Enter** yeni satır ekler; uygulama açıkken genişletilmiş tuş bildirimi istenir. Terminal Enter ve Shift+Enter’ı ayırt etmiyorsa **Alt+Enter** veya **Ctrl+J** kullanın. Yazma alanı ayarlanan yüksekliğe kadar büyür, ardından kaydırılır. Ctrl+A metni seçer, Ctrl+C seçimi kopyalar, Ctrl+V yapıştırır. Bir mesaja tıklayarak içeriğini seçip kopyalayabilirsiniz.

Yeni sohbette, etkin sağlayıcıların seçili hesaplarına ait modeller gösterilir. Durum satırı kataloglardaki toplam model sayısını ve hazır katalog/sağlayıcı oranını gösterir; kalan kataloglar arka planda yüklenir. Mevcut sohbet kendi hesabına bağlı kalır. Model ve efor seçenekleri sağlayıcının kataloğundan alınır; erişilemeyen veya üst paket gerektiren modeller, bu bilgi mevcutsa elenir. Sunucu yine de listelenen bir modeli reddedebilir. Sağlayıcının yayımladığı düşünme özetleri yanıttan ayrı gösterilir; gizli düşünme verilerine erişilmez.

Mesajdaki Markdown web bağlantısına tıklayarak varsayılan tarayıcınızda açabilirsiniz. Mesajın diğer alanlarına tıklamak kopyalamak için mesajı seçer.

Her mesajın altında küçük bir araç çubuğu bulunur. Kopyalama yerel çalışır; kullanıcı mesajları düzenlenip yeniden gönderilebilir. ChatGPT ve Claude yanıtları yeniden denenebilir; ChatGPT ve Gemini yanıtlarından ayrı bir sohbet dalı açılabilir. Sağlayıcının yayımladığı kaynaklar, ilgili yanıtın düşünme özetleriyle birlikte gösterilir. Yanıta ait mevcut model ve efor bilgisi sağda, varsa kayıtlı sürüm kontrollerinin hemen solunda yer alır. Araçlar mesaja ve sağlayıcıya, model erişimi seçili hesaba bağlıdır. Düzenleme ve yeniden deneme, sağlayıcı doğrulamasından sonra aktif geçmişi günceller; dallandırma yeni bir sohbet açar. Sohbet silme ve bölme kontrolleri üstteki eşitleme düğmesinin yanındadır.

Üretilen dosyalar yanıtın içinde, mevcut ad, tür ve boyut bilgileriyle tıklanabilir dosya kartları olarak listelenir. Bir karta tıklamak indirme yöneticisinde arka plan aktarımı başlatır; dosya referansları ve metaverisi arşivlenmiş sohbette korunur. Korumalı kaynaklar ilgili sağlayıcı ve hesap üzerinden çözümlenir. Sağlayıcının saklama süresi ve dosyanın erişilebilirliği geçerlidir.

Dosya bağlantıları arka planda proje dizinindeki `downloads/` klasörüne indirilir. Konumu **Ayarlar → Genel → İndirme klasörü** bölümünden değiştirebilirsiniz; göreli yollar proje dizinine göre çözülür, tam yollar da desteklenir. Aynı anda en fazla iki aktarım çalışır, ilerleme durum çubuğunda gösterilir; mevcut dosyaların üzerine yazılmaz ve başarısız aktarımlar temizlenir. Normal web sayfaları varsayılan tarayıcıda açılır. ChatGPT sandbox dosyaları ait olduğu sohbet üzerinden, Gemini görsel yer tutucuları gerçek medya kayıtlarından çözümlenir; Claude sandbox/computer bağlantıları ait olduğu sohbetin dosya deposundan alınır. Korumalı indirmelerde seçili hesabın kayıtlı oturumu kullanılır; hesap bilgileri başka adreslere yönlendirilmez. Bulunamayan veya süresi dolmuş kaynaklar için hata gösterilir.

**Yardım** ile **Ayarlar** arasındaki **İndirmeler** düğmesini, `/downloads` komutunu veya **Alt+J** kısayolunu kullanın. Sayfada alınan baytlar, biliniyorsa toplam ve kalan boyut, tamamlanan/başarısız aktarımlar ve kaynak sohbet görünür. Tarih grupları katlanır; arama dosya adı, sohbet başlığı ve hesabı kapsar. Duruma göre filtreleyebilir; tarih, dosya adı ve boyuta göre sıralayabilirsiniz. **Eşitle** dosyaları arka planda kontrol eder; aynı kontrol on saniyede bir yapılır. Klasörden kaldırılan dosya **Silindi** olarak listede kalır; silindiği bilinen sohbetler de belirtilir. **Dosyayı sil** geçmiş kaydını korur, **Listeden kaldır** diskteki dosyaya dokunmaz. Arşiv eklentisi yeni indirme kayıtlarını SQLite’ta kalıcı tutar; eklenti kapalıyken geçmiş yalnız mevcut oturumda tutulur. Uygulama kapanırken yarıda kalan aktarımlar **Kesildi** olarak görünür. Bu özellik eklenmeden önce indirilen dosyalara otomatik kaynak sohbet kaydı oluşturulmaz.

İndirmelerde varsayılan sınır 512 MiB’tır. Genel ayarlardan bayt cinsinden boyut sınırını ve yerel ağdan indirme iznini değiştirebilirsiniz. Genel indirme yolu özel ve loopback adreslerini engeller, her yönlendirmeyi doğrular ve HTTPS bağlantısını HTTP’ye düşürmez. Bağlantıda doğrulanmış IP kullanılır; TLS ve HTTP için özgün alan adı korunur.

Yanıt veya düşünme özeti üzerinde sürükleyerek metnin bir kısmını seçip Ctrl+C ile kopyalayabilirsiniz. Mesaja tıkladıktan sonra Ctrl+A ile mesajın tamamını seçebilirsiniz.

Sohbet sekmesini kapatmak, devam eden yanıtı durdurmaz. Sol listede yeşil **●** çalışan sohbeti, **✓** arka planda tamamlanan yanıtı gösterir. Sohbete tıklayarak aynı yanıtı yeniden açabilirsiniz. Yanıt sürerken diğer sohbetleri açabilir ve geçmişi eşitleyebilirsiniz. İşlem yalnız SiopeChat açıkken sürer.

| Varsayılan kısayol | İşlev |
| --- | --- |
| F1 | Yardımı aç / kapat |
| Ctrl+P | Komut paleti |
| Ctrl+T / Ctrl+W | Yeni sekme / sekmeyi kapat |
| Alt+Sol / Alt+Sağ | Önceki / sonraki sekme |
| Alt+[ / Alt+] | Bölünmüş grupta önceki / sonraki panel |
| Alt+W | İşlem yürütmeyen tüm sekmeleri kapat |
| F7 | Sol paneli aç / kapat |
| F8 / F10 | Sohbeti / kütüphaneyi eşitle |
| F5 / F12 | Ayarlar / hesaplar |

Kısayol değişiklikleri arayüzün tamamında anında geçerli olur. Bütün eşleşmeler **Ayarlar → Kısayollar** bölümünden veya yerel `config.json` dosyasından değiştirilebilir. Komut paleti, eklenti aksiyonları dahil aynı işlemleri sunar.

Ayarlar normal bir çalışma alanı sekmesidir: Alt+Sol/Sağ ile sohbetlere geçebilir, F5 ile mevcut Ayarlar sekmesini yeniden açabilir ve Ctrl+W ile kapatabilirsiniz.

### Projeler ve bölünmüş görünüm

Yeni sohbetin projesini seçmek veya destekleniyorsa mevcut sohbeti taşımak için `/project` kullanın. **Projesiz** seçeneği üyeliği kaldırır. `/project-new` önce sağlayıcı ve hesabı, ardından desteklenen oluşturma alanlarını sorar. Proje bilgilerinde isim, talimat ve kaynaklar düzenlenebilir; satır içi düzenleyicide Ctrl+S kaydeder.

Proje hakkında ekranından doğrudan da yazabilirsiniz. Proje hesabına ait model ve eforu seçip gönderdiğinizde, proje içinde ayrı bir sohbet sekmesi açılır. Proje düzenlenirken mesaj alanı ve seçiciler gizlenir.

**Böl**, `/split` veya `/split-down` seçeneğinin ardından sol panelden başka bir sohbet seçin. Aktif olmayan panelin herhangi bir yerine tıklamak onu aktif eder. Pasif paneli fare tekerleğiyle kaydırmak odağı değiştirmez. `/unsplit` tek panele döner. Komut paletindeki `layout.create` ile açık sohbetleri, bölünme yönünü ve aktif paneli kaydedebilirsiniz. Sol panelden kaydedilmiş çalışma alanını açmak düzeni geri yükler. Yalnız sohbet referansları kaydedilir; geçici sohbetler, taslaklar ve çalışan istekler kayda alınmaz.

Sohbet silmek için yazma alanının yanındaki **Sil** düğmesini kullanın. `/project-delete` proje seçicisini açar; proje bilgilerinde de silme düğmesi vardır. Her ikisi kalıcı silme onayı ister; başlangıçta **Hayır** odaktadır. Yerel kayıt kaldırılmadan önce işlem sağlayıcıya gönderilir ve orada doğrulanır. Proje içindeki sohbetlerin durumu, sağlayıcının silme kurallarına bağlıdır. Sekme kapatmak sohbet silmez.

### Sağlayıcı farkları

| Sağlayıcı | Proje desteği | Mevcut sınırlar |
| --- | --- | --- |
| ChatGPT | Projeler, talimatlar, kaynaklar ve desteklenen sohbet taşıma işlemleri | Model, efor, geçici mod ve proje seçenekleri hesaba bağlıdır. |
| Gemini | Ayrı bir proje türü olarak Gemini defterleri; defter bağlamında sohbet, talimat ve metin kaynakları | Mevcut sohbet deftere taşınamaz. Dosya/URL kaynağı içe aktarma sunulmuyor. Düşünme seçenekleri ve varsayılanlar hesabın Gemini web model politikasından alınır. |
| Claude | Projeler, talimatlar, metin/dosya kaynakları ve desteklenen üyelik değişiklikleri | Projeler dışında geçici sohbet desteklenir; özel/varsayılan dışındaki gizlilik/bellek seçenekleri sunulmuyor. |

Gemini defterleri Gemini hesabının oturumunu kullanır; ayrı NotebookLM girişi gerekmez. İçeride kapsayıcı türleri ve sağlayıcı metaverileri korunur. Bütün sağlayıcılar aynı özelliklere sahipmiş gibi ele alınmaz.

Her dosya için yerel ek sınırı **20 MiB**’dır; sağlayıcı daha düşük sınır uygulayabilir. Geçici mod yalnız desteklendiğinde sunulur ve sohbet anlık görüntülerini yerelde kaydetmez. Sağlayıcının veri saklama kuralları geçerlidir.

## Ayarlar ve yerel veriler

`config.json` yerelde oluşturulur ve Git kapsamı dışındadır. Varsayılanlar `src/settings.py` içinde tanımlıdır. Dil, **Ayarlar → Genel → Dil** bölümünden (`en`, `tr`, `ru`); sağlayıcılar **Sağlayıcılar**, özellik paketleri **Eklentiler** bölümünden yönetilir. En az bir sağlayıcı etkin kalmalıdır.

Farklı bir ayar/veri dizini için `SIOPECHAT_HOME` kullanın. Yapılandırmayı, hesap oturumlarını ve `data/` dizinini gizli tutun; hata raporlarına eklemeyin.

Arşiv eklentisi; sohbetleri, sıralı mesajları, yayımlanmış düşünme verilerini, proje/defterleri, üyelikleri ve kaynak metaverilerini `data/archive.sqlite3` içinde tutar. Yüklenen dosyalar arşive kopyalanmaz; sağlayıcı kimliği ve mevcut dosya metaverisi saklanır, yerel dosya yolu veya içerik tutulmaz. Eski arşivlerdeki içerik adresli dosya kopyaları yükseltme sırasında kaldırılır; sohbet ve kaynak metaverisi korunur. İndirilen dosyalar seçilen indirme klasöründe kalır, arşivde yalnızca indirme geçmişi metaverisi bulunur. Uzak görsel ve kaynaklar referans olarak tutulur; her uzak dosya otomatik indirilmez.

**Yükseltme notu:** Şema v5 geçişi eski yükleme kopyalarını kalıcı olarak siler. Bu sürümle eski bir arşivi ilk kez açmadan önce çevrimdışı kopyalara ihtiyaç duyuyorsanız veya eski SiopeChat sürümüne dönme ihtimaliniz varsa `data/archive.sqlite3` ile `data/files/` klasörünü birlikte yedekleyin. Özel `SIOPECHAT_HOME` kullanıyorsanız bu yolları veri dizininize göre uyarlayın.

Arşiv açıkken sohbetler, SQLite okuması başına en fazla 80 görünür mesaj ve 262.144 karakterlik serileştirilmiş kayıt bütçesiyle açılır. Yukarı kaydırdıkça eski parçalar eklenir; önceden yüklenen mesajlar kaldırılmaz. Görünür konum kalıcı mesaj kimliğiyle korunur. Sekmede yalnız çizim için gereken metin ve metaveri tutulur; metinler görünümle paylaşılır. Render önce görünür alanı, sonra üstte ve altta ikişer ekranı hazırlar. Her görünümde render satırları için 1 MiB LRU, büyük ANSI kayıtlarını sıkıştıran ayrı bir 256 KiB önbellek vardır. Etkin olmayan ana sekme görünümleri ortak 4 MiB boşaltma bütçesine uyar. Kabul edilmiş yanıtlar yalnız sınırlı bir SQL son bölümünü günceller. Sekme kapatıldığında kaynak ve render referansları bırakılır; arka planda süren yanıtın mevcut turu kaydedilene kadar tutulur. Tek mesaj parça bütçesine sığdırılmak için kesilmez. Kaynak belleği gezilen geçmişle büyür; render önbellekleri sınırlı kalır. Python bırakılan süreç belleğini tutabildiğinden RSS hemen düşmeyebilir.

Daha önce yüklenmiş sohbetler önce yerel arşivden açılır, sağlayıcıyla arka planda yenilenir. Yerel geçmiş ve eşitleme sürerken yazıp gönderebilirsiniz; eski mesajlar sonradan geldiğinde yeni turla birleştirilir. Önbellekteki sol panel ve proje sayfaları, sonraki sayfalar arka planda alınırken kullanılabilir. Eşitleme göstergeleri alt çubukta ve sohbet listesinde görünür. Yenileme, aktif yanıtı ezmeden yeni yerel mesajları birleştirir. Başarısız eşitlemede son sağlam kayıt korunur. Sonucu belirsiz bir uzak işlem otomatik tekrarlanmaz; yeniden göndermeden önce eşitleyin.

Arşivin bellek ve iş kuyrukları sınırlıdır. Büyük geçmişlerin hazırlanması arayüz olay döngüsü dışında yapılır. Sol panel yalnızca ekranda görünen satırları çizer; süzülmüş ve sıralanmış liste, kütüphane verisi değişene kadar yeniden kullanılır. Arşivi devre dışı bırakmak dosyalarını korur; doğrudan sağlayıcıdan yükleme ve gönderim devam eder. Düzen eklentisini kapatmak, sağlayıcıları etkilemeden bölme araçlarını kaldırır.

## Eklenti geliştirme

`extensions/my_feature/plugin.py` oluşturun. Ortak API’ye katkı kaydeden bir etkinleştirme fonksiyonu ve `ExtensionSpec` dışa aktarın:

```python
from extension import Action, ExtensionSpec, SettingField


def activate(context):
    context.settings(SettingField("enabled", bool, True, "my_feature.enabled"))
    context.translations("en", {"enabled": "Enable my feature"})
    context.translations("tr", {"enabled": "Özelliğimi etkinleştir"})
    context.translations("ru", {"enabled": "Включить расширение"})
    context.action(Action("my_feature.hello", "Hello", lambda: "Hello"))


SPEC = ExtensionSpec("my_feature", activate, api_version=3)
```

Her eklentide `plugin.py` yanında bir `manifest.json` bulunmalıdır. Dosya, Python kodunu çalıştırmadan eklenti bilgilerini bildirir:

```json
{"id": "my_feature", "api_version": 3, "requires": []}
```

`requires` alanını `ExtensionSpec` ile aynı tutun. Kapalı eklentiler import edilmez; etkinleştirildiğinde Python modülü yüklenir. Manifest ve `ExtensionSpec` aynı API sürümünü belirtmelidir.

Eklentiye ait servisleri `context.provide()` ile kaydedin; diğer kaynakları `context.on_dispose()` ile temizleyin.

Paketin keşfedilmesi için yeniden başlatın; ardından **Ayarlar → Eklentiler** bölümünden açıp kapatın. Ayarları `extensions.my_feature` altında tutulur. Dil paketleri ad alanlarına ayrılır ve uygulama kataloğuyla birleştirilir. Gerektiğinde `requires=("archive",)` ile bağımlılık bildirin. Aksiyonlar komut paletine giriş alanları sunabilir; eklentiler ayrıca servis ve görünüm kaydedebilir.

Arşive `context.api.get("archive")` ile erişilir. `conversations`, `messages` ve `resources` metotları sağlayıcı/hesap kapsamlı, sayfalanabilir kayıtlar sunar. Değiştirilebilir repository sözleşmesi ve SQLite adaptörü `extensions/archive/repository.py`, sağlayıcı normalleştirmeleri `adapters.py` içindedir.

Eklentiler yerel kod çalıştırır. Yalnız güvendiğiniz paketleri yükleyin. Ağ işlerini çizim fonksiyonlarında yürütmeyin; kaynak temizliğinde ortak yaşam döngüsünü kullanın.

Arşiv listeleri uzak kimliğe göre kararlı sıradadır; arayüz sıralaması ayrıca uygulanır. `siopechat action archive.inspect`, eksik dosya referanslarını ve sahipsiz içerikleri bildirir. `siopechat action archive.collect` yalnız temizlik önizlemesi yapar; adayları silmek istediğinizde aksiyon parametresi olarak `'{"dry_run": false}'` verin. Ortak kullanılan dosyalar, bilinmeyen dosyalar ve son bir saatte kaydedilen içerikler korunur. Daha yeni şemaya ait veritabanı değiştirilmeden reddedilir.

## Geliştirme

Hesap oturumları, model katalogları, doğrulama kayıtları ve arşiv kapalıyken kullanılan sohbet önbelleği `data/accounts.sqlite3` içinde sağlayıcı/hesap ayrımıyla tutulur. Önbellek sohbet kayıtlarını, etkin sohbet işaretçisini ve silme işaretlerini kapsar; arşiv açıkken sohbet geçmişinin asıl deposu `data/archive.sqlite3` olmaya devam eder. Hesap veritabanı şeması eklemeli olarak yükseltilir; mevcut oturum/katalog tablosu korunur. Sağlayıcıya özel alanlar JSON olarak saklanır. Unix üzerinde dosya yalnız sahibine açıktır; veritabanı şifreli değildir. Hesap adları, seçimler ve tercihler uygulama yapılandırmasında kalır. Sağlayıcı kütüphanesi dışarıdan kimlik bilgisi deposu alır; uygulamanın SQLite kodunu bilmez. Gemini yerel HTTPS taşıyıcısını kullanır ve Gemini SDK bağımlılığı gerektirmez. Eski hesap dosyaları ancak veritabanı/arşiv karşılığı doğrulandıktan sonra kaldırılır.

Yerleşik sağlayıcılar sohbet ve proje işlemlerinde kendi HTTPS taşıyıcılarını kullanır. Paketlenen Node yardımcıları açıkça başlatılan giriş/oturum aktarımı ve ChatGPT istek imzalama işlemleriyle sınırlıdır.

Sağlayıcı kütüphanesi terminalden, arşivden ve uygulama çalışma zamanından bağımsızdır. `accounts.py` sağlayıcı/hesap işlerini yönlendirir; `runtime.py`, `actions.py` ve `extension.py` ortak servis ve katkıları yönetir. Komut satırı girişi `src/cli.py`, terminal arayüzünün uygulaması ise `src/tui/` altındadır. Yerleşik eklentiler genel eklenti sözleşmesini `extension.py` üzerinden, terminal arayüzüne katkı gerektiğinde yalnız açık `tui.api` arayüzü üzerinden kullanır; TUI iç modüllerine doğrudan bağlanmaz. İsteğe bağlı özellikler `extensions/`, ortak yardımcılar `src/utils/` altındadır. Aynı uygulama servisleri bir GUI tarafından da kullanılabilir; bu betada GUI bulunmuyor.

Sağlayıcı dizini ayrıca bağımsız kurulabilen bir pakettir: `python -m pip install ./src/providers`. `ProviderClient`, hesapları sağlayıcı ve hesap kimliğiyle yönlendirir, akış iptalini ve bağlantıların kapanmasını yönetir. Uygulama da aynı `create_provider` fonksiyonunu kullanır. Tipli `Backend` API’si modelleri, sohbetleri, projeleri, kaynakları, yüklemeleri ve indirme çözümlemesini kapsar. Kullanım ve uygulamadan bağımsız testler için [sağlayıcı paketi belgesine](src/providers/README.md) bakın; dizin ayrı bir depoya veya submodule’e taşınabilir.

Testler kişisel hesap oturumlarınızı gerektirmez. Önce [uv](https://docs.astral.sh/uv/) kurun; ilk komut kilit dosyasına bağlı geliştirme ortamını oluşturur:

```sh
uv sync --locked --all-groups
PYTHONPATH=src:. uv run --locked python -m unittest discover -s tests
node --test tests/js/*.test.cjs
uv run --locked python -m ruff check .
uv run --locked python -m ruff format --check .
uv run --locked python -m build
uv run --locked python -m twine check dist/*
```

SiopeChat'i kullanırken kaynak ölçümü almak için `uv run --group performance python tools/measure_performance.py` komutunu çalıştırın. CSV; süreç ağacının CPU, RSS/USS/PSS bellek, disk G/Ç, açık tanıtıcı, thread ve süreç sayılarını kaydeder. Yanındaki JSON özeti `data/` ve yapılandırılmış indirme klasöründeki dosya sayısı ile boyutları kategori bazında çalıştırma öncesi ve sonrası karşılaştırır; tarayıcı profili, önbellek, hesap verisi ve arşiv veritabanı ayrı görünür. Depolama taraması yalnız dosya metaverisini okur; dosya içeriklerini okumaz ve sembolik bağlantıları izlemez. Saniyede iki örnek için `--interval 0.5`, farklı klasörleri izlemek için tekrarlanabilir `--storage-root DİZİN`, klasör taramasını kapatmak için `--no-storage` kullanın. `--` sonrasındaki parametreler SiopeChat'e iletilir. Birden fazla çekirdek kullanılırsa süreç ağacının CPU toplamı %100'ü aşabilir; bazı işletim sistemlerinde USS/PSS veya disk G/Ç ölçümü bulunmayabilir ve boş bırakılır.

Hata bildirirken sağlayıcıyı, işlemi, terminali, işletim sistemini ve hata mesajını belirtin. Kimlik bilgilerini ve özel sohbet içeriğini çıkarın. Sürüm değişiklikleri [CHANGELOG.md](CHANGELOG.md) içinde yer alır.

`Accounts` gönderimleri hesap çözümleyicisi, yanıt sınırı ve hesap kuyruğu içindeki kayıt işlemiyle ortak `TaskService` üzerinden çalışır. İptal, taşıyıcı temizliği ve kabul edilmiş yanıtın yazımı bittikten sonra hesabı serbest bırakır. Sağlayıcı kaydındaki `account_factory`, hesaplara bağımsız adaptör örnekleri oluşturur. Terminal okumaları, değişiklik işlemleri ve Markdown hazırlığı ayrı, sınırlı iş havuzları kullanır; sekmeyi kapatmak yanıtı iptal etmez.

GitHub Actions, Linux, Windows ve macOS üzerinde uygulama için Python 3.11/3.14 ve Node 22/24 matrisini çalıştırır. Bağımsız sağlayıcı paketi kendi Python 3.10+ desteğini korur. Birim testleri veya CI başarısı, canlı sağlayıcı uyumluluğunu ve gerçek terminal etkileşimini tek başına doğrulamaz. Oturumlar işletim sistemi kasası yerine özel dosya izinleriyle saklanır. İmzalama yardımcısı sınırlı girdi ve ortamla ayrı süreçte çalışır; Node VM bir işletim sistemi güvenlik sandbox’ı değildir.

## Lisans

Telif hakkı © TahsinCr. [GPL-3.0-only](LICENSE) ile lisanslanmıştır. Dahil edilen üçüncü taraf bileşenlerin bildirimleri `src/utils/` altında korunur.

[version-shield]: https://img.shields.io/badge/version-v1.5--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge
