[![Sürüm: v1.2-beta][version-shield]](CHANGELOG.md)
[![Python 3.10+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

[English](README.md) | **Türkçe** | [Русский](README-RU.md)

# SiopeChat

SiopeChat; ChatGPT, Gemini ve Claude sohbetlerini aynı terminalde kullanmanızı sağlar. Birden fazla sohbeti açık tutabilir, bölünmüş panellerde yanıtları karşılaştırabilir ve siteler arasında geçmeden projelerinizle çalışabilirsiniz.

Uygulama, giriş yaptığınız kendi web oturumlarınızı kullanır. API anahtarı gerektirmez; hesabınızın model erişimi ve kullanım sınırları geçerlidir. Bağımsız ve resmî olmayan bir istemcidir. Sağlayıcıların sitelerinde yapılan değişiklikler bazı özellikleri etkileyebilir.

![SiopeChat terminal çalışma alanı](assets/siopechat.png)

*Graphite teması ve örnek verilerle alınmış gerçek terminal görüntüsü.*

## Neler sunuyor?

- Ayrı taslak, kaydırma konumu ve yanıt göstergeleri olan sekmeler; grupta en fazla dört bölünmüş panel.
- Hesaba göre model ve düşünme eforu seçimi; her hesap için son seçimin hatırlanması.
- ChatGPT ve Claude projeleri, Gemini defterleri; desteklendiği ölçüde talimat ve kaynak yönetimi.
- Proje ve sohbetlere uygulanan arama, hesap filtresi, tarih ve isim sıralaması.
- Kayıtlı geçmişi önce gösterip arka planda eşitleyen SQLite arşivi.
- Komut paleti, değiştirilebilir kısayollar, fare desteği, yerel eklentiler ve JSON/Python temaları.
- İngilizce, Türkçe ve Rusça arayüz katalogları.

**v1.2-beta**, canlı aktarım durumunu, kalıcı indirme geçmişini ve kaynak sohbete dönüşü içeren aranabilir bir İndirmeler çalışma alanı ekler. Bu sürüm ayrıca iş iptalini, sağlayıcı yanıt doğrulamasını, arşiv güvenliğini ve kaydedilmiş bölünmüş çalışma alanlarını geliştirir. Temel akışlar otomatik testlerle kontrol edilir; sağlayıcı uyumluluğu ve farklı sistemlerdeki yerel davranışlar günlük kullanımda da sınanmalıdır. Python paket sürümü `1.2b0` olur.

## Kurulum ve giriş

ChatGPT ve Claude için Python **3.10+**, Gemini için **3.11+** gerekir. Giriş yardımcıları ve ChatGPT istek imzalayıcısı Node.js **22+**, açıkça başlatılan giriş işlemi ise Chrome/Chromium kullanır. Unicode destekli bir terminal kullanın; gerçek renk ve fare raporlama desteği önerilir.

Projeyi indirdiğiniz dizinde, kendi Python ortamınızla kurun:

```sh
python -m pip install -e .
```

| Sistem | Başlatma |
| --- | --- |
| Linux / macOS terminal | `./siopechat.sh` |
| macOS Finder | `siopechat.command` |
| Windows Komut İstemi | `siopechat.bat` |
| Windows PowerShell | `./siopechat.ps1` |
| Kurulu konsol komutu | `siopechat` |

Başlatıcılar, varsa proje dizinindeki `python/` ortamını kullanır; yeni bir ortam oluşturmaz.

Yeni kurulum hesap eklenmeden başlar. İlk hesabınızı Ayarlar → Hesaplar bölümünden ekleyin; otomatik ChatGPT hesap kaydı oluşturulmaz.

**Ayarlar → Hesaplar** bölümünde bir hesap ekleyin veya mevcut hesabınızı seçin; ardından **Profil ile doğrula** seçeneğine basın. Varsayılan tarayıcınızın son kullanılan profilinde sağlayıcı hesabına önceden giriş yapmış olmalısınız. SiopeChat bu oturumu tarayıcı başlatmadan aktarır ve doğrular. Sohbet, geçmiş, model ve proje işlemleri HTTP üzerinden yürür. Oturumun süresi dolarsa normal tarayıcınızda yeniden giriş yapıp **Profil ile doğrula** işlemini tekrarlayın.

Hesap ekle, düzenle ve sil düğmeleri hesap listesinin altında yan yana durur. Düzenle yalnız seçili hesabın görünen adını değiştirir; oturumu ve geçmişi korunur. Profil doğrulama ve çıkış düğmeleri oturum bilgilerinin altındadır. Dar terminallerde düğme adları kısalır.

**Profil ile doğrula**, varsayılan tarayıcının son kullanılan profilinden seçili sağlayıcının açık oturumunu aktarır; tarayıcı başlatmaz ve hata ayıklama bayrağı gerektirmez. Chrome, Chromium, Brave/Brave Origin, Edge, Vivaldi, Firefox ve Zen’in standart profil konumları desteklenir. Yalnız seçili sağlayıcının kimlik doğrulama çerezleri alınır; Firefox’ta varsayılan kapsayıcı kullanılır. Yeni oturum, kayıtlı oturumun yerine yazılmadan önce sağlayıcıda doğrulanır. Sistem anahtarlığı erişim izni isteyebilir. Kilitli veritabanı, özel/yalıtılmış profil konumu veya desteklenmeyen şifreleme (bazı Windows Chromium yapılandırmaları dahil) aktarımı engelleyebilir; kayıtlı oturum silinmeden hata gösterilir. Bu aktarım seçeneği Safari’yi desteklemez. Aktarım hataları; eksik bağımlılık, dosya izni, veritabanı erişimi, anahtarlık erişimi ve çerez çözme sorunlarını ayrı gösterir. Bağımlılıkları SiopeChat’i başlatan Python ortamında güncelleyin. Firefox/Zen’in güncel kurtarma kaydındaki oturum çerezleri de okunur; ayrı container hesapları ve eski kurtarma kayıtları birleştirilmez. Çerez veritabanı kilitliyse WAL/journal dosyalarıyla birlikte özel geçici kopyadan yeniden okunur; asıl profil değiştirilmez.

Firefox ve Zen aynı salt okunur çerez okuyucusunu kullanır. Profil seçimi `profiles.ini` ve `installs.ini` üzerinden yapılır; belirsiz profiller birleştirilmeden hata bildirilir. Linux’ta Firefox’un Snap/Flatpak konumları ve Zen’in güncel XDG, eski ve Flatpak konumları bulunur.

Birden fazla hesap ekleyebilirsiniz. Gönderim için her sağlayıcıda bir hesap seçilidir; başka hesabın geçmişini görüntülemek bu seçimi değiştirmez. Farklı bir hesabı doğrulamak için önce varsayılan tarayıcınızda o hesabın açık olduğu profili seçin, ardından SiopeChat’te ilgili hesapta **Profil ile doğrula** seçeneğini kullanın.

Hesap satırına tıklayarak seçim yapın; ok işareti yönetilen hesabı gösterir. **Hesabı sil** (Hesaplar ekranında Alt+Backspace), onay aldıktan sonra yerel hesap kaydını, kayıtlı oturumu ve model tercihlerini kaldırır. Sağlayıcıdaki hesap ve arşivlenmiş sohbetler korunur. Silinen hesabın sekmeleri kapatılır, geçersiz kalan filtreler temizlenir ve başka etkin hesap seçilir. Son etkin hesabı silmeden önce başka hesap ekleyin.

## Kullanım

Mesaj alanına yazıp **Enter** ile gönderin. Destekleyen terminallerde **Shift+Enter** yeni satır ekler; uygulama açıkken genişletilmiş tuş bildirimi istenir. Terminal Enter ve Shift+Enter’ı ayırt etmiyorsa **Alt+Enter** veya **Ctrl+J** kullanın. Yazma alanı ayarlanan yüksekliğe kadar büyür, ardından kaydırılır. Ctrl+A metni seçer, Ctrl+C seçimi kopyalar, Ctrl+V yapıştırır. Bir mesaja tıklayarak içeriğini seçip kopyalayabilirsiniz.

Yeni sohbette, etkin sağlayıcıların seçili hesaplarına ait modeller gösterilir. Mevcut sohbet kendi hesabına bağlı kalır. Model ve efor seçenekleri sağlayıcının kataloğundan alınır; erişilemeyen veya üst paket gerektiren modeller, bu bilgi mevcutsa elenir. Sunucu yine de listelenen bir modeli reddedebilir. Sağlayıcının yayımladığı düşünme özetleri yanıttan ayrı gösterilir; gizli düşünme verilerine erişilmez.

Mesajdaki Markdown web bağlantısına tıklayarak varsayılan tarayıcınızda açabilirsiniz. Mesajın diğer alanlarına tıklamak kopyalamak için mesajı seçer.

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
| Gemini | Ayrı bir proje türü olarak Gemini defterleri; defter bağlamında sohbet, talimat ve metin kaynakları | Mevcut sohbet deftere taşınamaz. Dosya/URL kaynağı içe aktarma sunulmuyor. Efor seçenekleri taşıyıcıya bağlıdır. |
| Claude | Projeler, talimatlar, metin/dosya kaynakları ve desteklenen üyelik değişiklikleri | Sohbet eki, geçici sohbet ve özel/varsayılan dışındaki gizlilik/bellek seçenekleri sunulmuyor. |

Gemini defterleri Gemini hesabının oturumunu kullanır; ayrı NotebookLM girişi gerekmez. İçeride kapsayıcı türleri ve sağlayıcı metaverileri korunur. Bütün sağlayıcılar aynı özelliklere sahipmiş gibi ele alınmaz.

Her dosya için yerel ek sınırı **20 MiB**’dır; sağlayıcı daha düşük sınır uygulayabilir. Geçici mod yalnız desteklendiğinde sunulur ve sohbet anlık görüntülerini yerelde kaydetmez. Sağlayıcının veri saklama kuralları geçerlidir.

## Ayarlar ve yerel veriler

`config.json` yerelde oluşturulur ve Git kapsamı dışındadır. Varsayılanlar `src/settings.py` içinde tanımlıdır. Dil, **Ayarlar → Genel → Dil** bölümünden (`en`, `tr`, `ru`); sağlayıcılar **Sağlayıcılar**, özellik paketleri **Eklentiler** bölümünden yönetilir. En az bir sağlayıcı etkin kalmalıdır.

Farklı bir ayar/veri dizini için `SIOPECHAT_HOME` kullanın. Yapılandırmayı, hesap oturumlarını ve `data/` dizinini gizli tutun; hata raporlarına eklemeyin.

Arşiv eklentisi; sohbetleri, sıralı mesajları, yayımlanmış düşünme verilerini, proje/defterleri, üyelikleri ve kaynak metaverilerini `data/archive.sqlite3` içinde tutar. Dosyalar `data/files/` altında, hash ve konumları veritabanında saklanır. Erişilebilir yerel ekler, başarılı normal gönderimden sonra kopyalanır. Uzak görsel ve kaynakların bağlantıları korunur; her uzak dosya otomatik indirilmez.

Daha önce yüklenmiş sohbetler önce yerel arşivden açılır, arka planda yenilenir. Eşitleme göstergeleri alt çubukta ve sohbet listesinde görünür. Yeni yerel mesajlar, yenilenen geçmişle birleştirilir; aktif yanıtın üzerine yazılmaz. Başarısız eşitlemede son sağlam kayıt korunur. Sonucu belirsiz bir uzak işlem otomatik tekrarlanmaz; yeniden göndermeden önce eşitleyin.

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


SPEC = ExtensionSpec("my_feature", activate)
```

`plugin.py` yanındaki isteğe bağlı `manifest.json`, Python çalıştırmadan eklentinin bilgilerini bildirir:

```json
{"id": "my_feature", "api_version": 1, "requires": []}
```

`requires` alanını `ExtensionSpec` ile aynı tutun. Kapalı eklentiler import edilmez; etkinleştirildiğinde Python modülü yüklenir. Manifesti olmayan mevcut paketler de desteklenir.

Paketin keşfedilmesi için yeniden başlatın; ardından **Ayarlar → Eklentiler** bölümünden açıp kapatın. Ayarları `extensions.my_feature` altında tutulur. Dil paketleri ad alanlarına ayrılır ve uygulama kataloğuyla birleştirilir. Gerektiğinde `requires=("archive",)` ile bağımlılık bildirin. Aksiyonlar komut paletine giriş alanları sunabilir; eklentiler ayrıca servis, görünüm ve tema kaydedebilir.

Arşive `context.api.get("archive")` ile erişilir. `conversations`, `messages` ve `resources` metotları sağlayıcı/hesap kapsamlı, sayfalanabilir kayıtlar sunar. Değiştirilebilir repository sözleşmesi ve SQLite adaptörü `extensions/archive/repository.py`, sağlayıcı normalleştirmeleri `adapters.py` içindedir.

Eklentiler ve Python temaları yerel kod çalıştırır. Yalnız güvendiğiniz paketleri yükleyin. Ağ işlerini çizim fonksiyonlarında yürütmeyin; kaynak temizliğinde ortak yaşam döngüsünü kullanın.

Arşiv listeleri uzak kimliğe göre kararlı sıradadır; arayüz sıralaması ayrıca uygulanır. `siopechat action archive.inspect`, eksik dosya referanslarını ve sahipsiz içerikleri bildirir. `siopechat action archive.collect` yalnız temizlik önizlemesi yapar; adayları silmek istediğinizde aksiyon parametresi olarak `'{"dry_run": false}'` verin. Ortak kullanılan dosyalar, bilinmeyen dosyalar ve son bir saatte kaydedilen içerikler korunur. Daha yeni şemaya ait veritabanı değiştirilmeden reddedilir.

## Temalar

`themes/` altına bir JSON teması koyup `id` değerini **Ayarlar → Genel → Tema** bölümünden seçin:

```json
{
  "id": "my-theme",
  "tokens": {"background": "#202020", "foreground": "#dedede", "accent": "#b7a3e3"},
  "styles": {"tab.active": "bold #b7a3e3"},
  "options": {"sidebar_width": 28, "message_width": 100, "composer_max_lines": 6, "tab_width": 26}
}
```

Eksik değerler varsayılanlardan alınır. Renkler altı haneli hexadecimal gösterim, terminal stilleri Prompt Toolkit, `markdown` stilleri Rich sözdizimi kullanır. Desteklenen aralıklar: sol panel 16–40 hücre, mesaj genişliği 40–160 hücre, yazma alanı 3–16 satır ve sekme başına 12–40 hücre.

Hesaplanan değerler için aynı sözlüğü bir Python dosyasından `THEME` adıyla dışa aktarın. [Graphite](themes/graphite.py) örnek alınabilir. Eklentiler `context.theme(...)` kullanabilir. Hatalı isteğe bağlı temalar bildirilir; varsayılan temanın yüklenmesi engellenmez.

## Geliştirme

Uygulama ayarları ve önbellek kayıtları ortak atomik JSON yazıcısını kullanır. Bağımsız sağlayıcı kütüphanesi oturum kayıtlarını ve ortak proje kaynak doğrulamasını kendi içinde yönetir; uygulamanın yardımcı modüllerine bağımlı değildir.

Yerleşik sağlayıcılar sohbet ve proje işlemlerinde kendi HTTPS taşıyıcılarını kullanır. Paketlenen Node yardımcıları açıkça başlatılan giriş/oturum aktarımı ve ChatGPT istek imzalama işlemleriyle sınırlıdır.

Sağlayıcı kütüphanesi terminalden, arşivden ve uygulama çalışma zamanından bağımsızdır. `accounts.py` sağlayıcı/hesap işlerini yönlendirir; `runtime.py`, `actions.py` ve `extension.py` ortak servis ve katkıları yönetir. Terminal kodu `src/cli/` altında; sekme çizimi `layout.py`, kütüphane filtreleri ve satırları `library.py` içindedir. İsteğe bağlı özellikler `extensions/`, ortak yardımcılar `src/utils/` altındadır. Aynı servisler bir GUI tarafından kullanılabilir; bu betada GUI bulunmuyor.

Testler kişisel hesap oturumlarınızı gerektirmez:

```sh
PYTHONPATH=src:. python -m unittest discover -s tests
node --test tests/js/*.test.cjs
uvx ruff check src extensions tests
uvx ruff format --check src extensions tests
uv build
```

Hata bildirirken sağlayıcıyı, işlemi, terminali, işletim sistemini ve hata mesajını belirtin. Kimlik bilgilerini ve özel sohbet içeriğini çıkarın. Sürüm değişiklikleri [CHANGELOG.md](CHANGELOG.md) içinde yer alır.

`Accounts` gönderimleri hesap çözümleyicisi, yanıt sınırı ve hesap kuyruğu içindeki kayıt işlemiyle ortak `TaskService` üzerinden çalışır. İptal, taşıyıcı temizliği ve kabul edilmiş yanıtın yazımı bittikten sonra hesabı serbest bırakır. Sağlayıcı kaydındaki `account_factory`, hesaplara bağımsız adaptör örnekleri oluşturur. Terminal okumaları, değişiklik işlemleri ve Markdown hazırlığı ayrı, sınırlı iş havuzları kullanır; sekmeyi kapatmak yanıtı iptal etmez.

GitHub Actions tanımı Linux, Windows ve macOS üzerinde Python 3.10/3.14 ve Node 22/24 kontrollerini, ayrıca paket üretimini kapsar. Birim testleri veya CI başarısı, canlı sağlayıcı uyumluluğunu ve gerçek terminal etkileşimini tek başına doğrulamaz. Oturumlar işletim sistemi kasası yerine özel dosya izinleriyle saklanır. İmzalama yardımcısı sınırlı girdi ve ortamla ayrı süreçte çalışır; Node VM bir işletim sistemi güvenlik sandbox’ı değildir.

## Lisans

Telif hakkı © TahsinCr. [GPL-3.0-only](LICENSE) ile lisanslanmıştır. Dahil edilen üçüncü taraf bileşenlerin bildirimleri `src/utils/` altında korunur.

[version-shield]: https://img.shields.io/badge/version-v1.2--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge
