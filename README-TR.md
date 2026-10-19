[![Sürüm: v1.0-beta][version-shield]](CHANGELOG.md)
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

**v1.0-beta**, ilk beta sürümüdür. Temel iş akışları otomatik testlerle kontrol edilir; sağlayıcı uyumluluğu ve farklı sistemlerdeki davranışlar için günlük kullanımdan gelecek geri bildirimlere ihtiyaç vardır. Python paket sürümü `1.0b0` olarak belirtilir.

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

**Ayarlar → Hesaplar** bölümünde hesap ekleyip **Bağlı tarayıcı** seçeneğini açın. Sağlayıcının sitesinde giriş yaptıktan sonra uygulamaya dönüp **Doğrula** seçeneğine basın. Oturum kaydedildiğinde giriş tarayıcısını, işlemiyle birlikte kapatabilirsiniz; sohbet, geçmiş, model ve proje işlemleri HTTP üzerinden yürür. Süresi dolan veya iptal edilen oturum yeniden doğrulanmalıdır. Yalnız varsayılan tarayıcıyı açmak, oturumu uygulamaya aktarmaz.

Birden fazla hesap eklenebilir. Her sağlayıcıda gönderim için bir hesap seçilir; başka bir hesabın geçmişine bakmak bu seçimi değiştirmez. Varsayılan olarak ayrı giriş profilleri kullanılır. Başka sağlayıcı için aynı profili kullanmak, hesap eklerken yapılan açık bir seçimdir.

## Kullanım

Mesaj alanına yazıp **Enter** ile gönderin. **Alt+Enter** veya **Ctrl+J** yeni satır ekler; terminal ayrı bildirdiğinde Shift+Enter da desteklenir. Yazma alanı ayarlanan yüksekliğe kadar büyür, ardından kaydırılır. Ctrl+A metni seçer, Ctrl+C seçimi kopyalar, Ctrl+V yapıştırır. Bir mesaja tıklayarak içeriğini seçip kopyalayabilirsiniz.

Yeni sohbette, etkin sağlayıcıların seçili hesaplarına ait modeller gösterilir. Mevcut sohbet kendi hesabına bağlı kalır. Model ve efor seçenekleri sağlayıcının kataloğundan alınır; erişilemeyen veya üst paket gerektiren modeller, bu bilgi mevcutsa elenir. Sunucu yine de listelenen bir modeli reddedebilir. Sağlayıcının yayımladığı düşünme özetleri yanıttan ayrı gösterilir; gizli düşünme verilerine erişilmez.

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

Bütün eşleşmeler **Ayarlar → Kısayollar** bölümünden veya yerel `config.json` dosyasından değiştirilebilir. Komut paleti, eklenti aksiyonları dahil aynı işlemleri sunar.

### Projeler ve bölünmüş görünüm

Yeni sohbetin projesini seçmek veya destekleniyorsa mevcut sohbeti taşımak için `/project` kullanın. **Projesiz** seçeneği üyeliği kaldırır. `/project-new` önce sağlayıcı ve hesabı, ardından desteklenen oluşturma alanlarını sorar. Proje bilgilerinde isim, talimat ve kaynaklar düzenlenebilir; satır içi düzenleyicide Ctrl+S kaydeder.

Proje hakkında ekranından doğrudan da yazabilirsiniz. Proje hesabına ait model ve eforu seçip gönderdiğinizde, proje içinde ayrı bir sohbet sekmesi açılır. Proje düzenlenirken mesaj alanı ve seçiciler gizlenir.

**Böl**, `/split` veya `/split-down` seçeneğinin ardından sol panelden başka bir sohbet seçin. Aktif olmayan panelin herhangi bir yerine tıklamak onu aktif eder. Pasif paneli fare tekerleğiyle kaydırmak odağı değiştirmez. `/unsplit` tek panele döner. Çalışma alanı metaverileri kaydedilebilir; canlı bölünmüş düzeni kaydetme ve geri yükleme bu sürümde bulunmuyor.

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

Arşivin bellek ve iş kuyrukları sınırlıdır. Büyük geçmişlerin hazırlanması arayüz olay döngüsü dışında yapılır. Arşivi devre dışı bırakmak dosyalarını korur; doğrudan sağlayıcıdan yükleme ve gönderim devam eder. Düzen eklentisini kapatmak, sağlayıcıları etkilemeden bölme araçlarını kaldırır.

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

Paketin keşfedilmesi için yeniden başlatın; ardından **Ayarlar → Eklentiler** bölümünden açıp kapatın. Ayarları `extensions.my_feature` altında tutulur. Dil paketleri ad alanlarına ayrılır ve uygulama kataloğuyla birleştirilir. Gerektiğinde `requires=("archive",)` ile bağımlılık bildirin. Aksiyonlar komut paletine giriş alanları sunabilir; eklentiler ayrıca servis, görünüm ve tema kaydedebilir.

Arşive `context.api.get("archive")` ile erişilir. `conversations`, `messages` ve `resources` metotları sağlayıcı/hesap kapsamlı, sayfalanabilir kayıtlar sunar. Değiştirilebilir repository sözleşmesi ve SQLite adaptörü `extensions/archive/repository.py`, sağlayıcı normalleştirmeleri `adapters.py` içindedir.

Eklentiler ve Python temaları yerel kod çalıştırır. Yalnız güvendiğiniz paketleri yükleyin. Ağ işlerini çizim fonksiyonlarında yürütmeyin; kaynak temizliğinde ortak yaşam döngüsünü kullanın.

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

Sağlayıcı kütüphanesi terminalden, arşivden ve uygulama çalışma zamanından bağımsızdır. `accounts.py` sağlayıcı/hesap işlerini yönlendirir; `runtime.py`, `actions.py` ve `extension.py` ortak servis ve katkıları yönetir. Terminal kodu `src/cli/`, isteğe bağlı özellikler `extensions/`, ortak yardımcılar `src/utils/` altındadır. Aynı servisler bir GUI tarafından kullanılabilir; bu betada GUI bulunmuyor.

Testler kişisel hesap oturumlarınızı gerektirmez:

```sh
PYTHONPATH=src:. python -m unittest discover -s tests
node --test tests/js/*.test.cjs
uvx ruff check src extensions tests
uvx ruff format --check src extensions tests
uv build
```

Hata bildirirken sağlayıcıyı, işlemi, terminali, işletim sistemini ve hata mesajını belirtin. Kimlik bilgilerini ve özel sohbet içeriğini çıkarın. Sürüm değişiklikleri [CHANGELOG.md](CHANGELOG.md) içinde yer alır.

## Lisans

Telif hakkı © TahsinCr. [GPL-3.0-only](LICENSE) ile lisanslanmıştır. Dahil edilen üçüncü taraf bileşenlerin bildirimleri `src/utils/` altında korunur.

[version-shield]: https://img.shields.io/badge/version-v1.0--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge
