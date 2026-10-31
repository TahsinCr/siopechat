[![Sürüm: v1.5.2-beta][version-shield]](CHANGELOG.md)
[![Python 3.11+][python-shield]](https://www.python.org/downloads/)
[![GPL-3.0-only][license-shield]](LICENSE)

[English](README.md) | **Türkçe** | [Русский](README-RU.md)

# SiopeChat

### ChatGPT, Gemini ve Claude web sohbetleri — tek terminalde.

Sohbetlerinizi, projelerinizi ve farklı modellerin yanıtlarını tek bir çalışma alanında bir araya getirin. Sekmeler arasında geçin, yanıtları yan yana karşılaştırın ve terminalden ayrılmadan çalışmaya devam edin.

> SiopeChat, ChatGPT, Gemini ve Claude’un **web sohbetleri** için geliştirilmiş bağımsız, resmî olmayan bir istemcidir. Tarayıcıdaki oturumunuzu kullanır; API anahtarı gerektirmez. Aboneliğinizin model erişimi ve kullanım sınırları geçerlidir. Resmî geliştirici API’lerine, Codex’e veya Claude Code’a bağlanmaz. Sağlayıcı sitelerindeki değişiklikler uyumluluğu etkileyebilir.

![SiopeChat TUI ekran görüntüsü](assets/siopechat.png)

*Örnek sohbet verileriyle çalışan gerçek SiopeChat terminal arayüzü.*

<p align="center">
  <a href="#kurulum-ve-giriş"><strong>Kurulum</strong></a> &nbsp; | &nbsp;
  <a href="#kullanım"><strong>Kullanım</strong></a> &nbsp; | &nbsp;
  <a href="#eklenti-geliştirme"><strong>Eklentiler</strong></a> &nbsp; | &nbsp;
  <a href="#geliştirme"><strong>Geliştirme</strong></a>
</p>

## Neler yapabilirsiniz?

| Sohbetler | Projeler ve bağlam | Yerel geçmiş |
| --- | --- | --- |
| Taslak ve kaydırma konumlarını koruyan sekmeler kullanın; bir grupta en fazla dört paneli karşılaştırın. | ChatGPT ve Claude projeleri ya da Gemini defterleriyle çalışın; sağlayıcının desteklediği talimat ve kaynakları yönetin. | Kayıtlı sohbetleri hemen açın, geçmişi arka planda eşitleyin. |
| Hesaba göre model ve düşünme eforu seçin; desteklenen mesajları düzenleyin, yanıtı yeniden üretin veya sohbeti dallandırın. | Proje ve sohbetleri arayın, sıralayın ve sağlayıcı hesabına göre filtreleyin. | Sohbet geçmişini ve kaynak bilgilerini yerel SQLite arşivinde tutun. |

| Dosyalar ve indirmeler | Kısayollar ve eklentiler | Diliniz |
| --- | --- | --- |
| Yanıtlarda üretilen dosyaları indirin; ilerlemeyi indirme yöneticisinde izleyin. | Komut paleti, değiştirilebilir kısayollar, fare kontrolleri ve yerel eklentileri kullanın. | İngilizce, Türkçe veya Rusça arayüzü seçin. |

## Daha Az Bekleme, Daha Akıcı Sohbet

SiopeChat önceki yanıtı tamamlarken siz yazmaya devam edin. Ortak sohbet iyileştirmeleri ve sağlayıcılara özel optimizasyonlar, gereksiz beklemeleri azaltır.

| Kapsam | Neler iyileşti? |
| --- | --- |
| **ChatGPT, Gemini ve Claude** | Kayıtlı sohbetler yerel arşivden açılırken geçmiş arka planda eşitlenir. Geçerli model katalogları yeniden kullanılır; büyük geçmişlerin hazırlanması arayüzü meşgul etmez. Sağlayıcı metin üretiminin bittiğini bildirdiğinde durum ve süre göstergesi tamamlanır; bağlantının son işlemleri arka planda sürebilir. Mevcut gönderim tamamlanırken bir sonraki mesajı kuyruğa alabilirsiniz. |
| **ChatGPT** | Uygun sohbet kayıtları ve tamamlanmış akış mesajları yeniden kullanılarak yinelenen geçmiş istekleri azaltılır. Sentinel başlıkları sonraki gönderim için önceden hazırlanır. Kayıtlı çevrimdışı ölçümde saf Python ile başlangıç kanıtı üretimi **0,329 ms medyan** sürdü: aynı aşamada, yeniden kullanılan QuickJS-NG bağlamından yaklaşık **10,7 kat**, açık Node/V8 sürecinde her örnekte yeni VM kullanılan yoldan **12,6 kat hızlı**. |
| **Gemini** | Flash yönlendirmesi düzeltildi; sağlayıcının varsayılan düşünme tercihi korunuyor. Geçerli sohbet devam kimlikleri yeniden kullanılıyor, etkinlik RPC’si beş dakika içinde tekrarlanmıyor. Önceki sınırlı canlı denemelerde ilk metne ulaşma süresi **65–110 saniyeden 1,89–3,80 saniye aralığına** indiği şeklinde kaydedildi. Bunlar kontrollü benchmark sonuçları veya her model ve hesap için verilmiş bir garanti değildir. |

**Rakamların kapsamı:** ChatGPT oranları yalnızca yerel başlangıç kanıtı üretiminin medyanlarını karşılaştırır; mesajın tamamının o kadar hızlandığı anlamına gelmez. Gemini denemelerinde yayımlanmış eşleştirilmiş örnekler ve p95 bulunmadığından güvenilir bir “kaç kat” oranı çıkarılamaz. Ortak arayüz iyileştirmeleri için de ölçülmüş bir katsayı yoktur. Ağ, model ve sunucu yükü yanıt süresini etkilemeye devam eder. Ayrıntılar aşağıda ve [değişiklik günlüğünde](CHANGELOG.md). Python paket sürümü `1.5.2b0`.

## Kurulum ve giriş

### Gereksinimler

Python **3.11+** ve Unicode destekli bir terminal gerekir. Gerçek renk ve fare desteği önerilir. Kurulum araçları Python sürümünü denetler. Aşağıda anlatıldığı gibi desteklenen bir tarayıcı profilinden mevcut oturumunuzu aktarabilirsiniz. Günlük sohbet işlemleri HTTP üzerinden yürür; tarayıcının açık kalması gerekmez.

ChatGPT’nin desteklenen Sentinel kontrolleri, paketteki `SentinelSpec` profiliyle Python’da çalışır. **Node.js veya JavaScript motoru gerekmez.**

<details>
<summary>Performans verileri: tam olarak ne ölçüldü?</summary>

Kayıtlı çevrimdışı benchmark, Linux x86_64 üzerinde Python 3.14.8 ve Node.js 26.12.1 ile 60 örnek kullanılarak yapıldı. İki JavaScript yolunda aynı 33.820 baytlık SDK kullanıldı; Python ise SDK’yı çalıştırmak yerine karşılık gelen Sentinel profilini kullandı.

| Başlangıç kanıtı üretimi | Medyan | p95 | Medyan süre / Python süresi |
| --- | --- | --- | --- |
| Saf Python, yeniden kullanılan çalışma zamanı | 0,329 ms | 1,925 ms | 1× |
| QuickJS-NG, yeniden kullanılan bağlam | 3,526 ms | 11,220 ms | 10,7× |
| QuickJS-NG, yeni bağlam | 8,831 ms | 17,285 ms | 26,8× |
| Node/V8, açık süreç ve yeni VM | 4,140 ms | 6,742 ms | 12,6× |
| Node/V8, her işlemde yeni süreç | 57,944 ms | 62,409 ms | 176,1× |

Son satır süreç başlatma maliyetini de içerir; boş Node süreci başlatmak tek başına 32,004 ms medyan / 32,448 ms p95 sürdü. Bu yol, açık bir süreci yeniden kullanmakla eşdeğer değildir. Bu örnekte yeni bağlamlarla Node/V8, QuickJS-NG’den hızlıydı; en düşük kanıt üretimi medyanı Python’da ölçüldü. Ağ istekleri, modelin yanıt üretimi ve arayüz çizimi bu ölçüme dahil değildir. Bunlar önceki ölçümlerdir; mevcut revizyonda yeniden alınmış sonuçlar değildir.

</details>

### Kurulum ve başlatma

Depoyu indirdiğiniz dizinde tek seferlik kurulum aracını çalıştırın. Python 3.11+ sürümünü denetler, bulduğu uyumlu sanal ortamı kullanır ve SiopeChat ile Python bağımlılıklarını o ortama kurar. Ortam bulamazsa `python/` oluşturur; mevcut bir dizini silmez veya üzerine yazmaz.

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

<details>
<summary>Sanal ortam ve bağımlılık ayrıntıları</summary>

Kurulum ve başlatma araçları ortamları şu sırayla arar: `python/`, `.venv/`, `venv/`, `env/`. Başka bir ortam kullanmak için `SIOPECHAT_VENV` değişkenini ortam dizinine ayarlayın; göreli yollar depo dizinine göre çözülür. Uygun ortam yoksa yalnızca kurulum aracı yenisini oluşturur. Bozuk veya uyumsuz mevcut dizinlere dokunulmaz. Depoyu güncelledikten sonra değişen bağımlılıkları kurmak için kurulum aracını yeniden çalıştırın. Başlatıcılar her açılışta paket kurmaz ve ağa çıkmaz; yalnızca gerekli sürümleri denetleyip ortamı başlatır.

Son kullanıcı kurulumu `pyproject.toml` içindeki sürüm aralıklarını pip ile kullanır; uv yüklemek gerekmez. `uv.lock`, CI ve geliştirme ortamlarında tam bağımlılık çözümünü sabitlemeye devam eder.

</details>

### Hesap bağlama

Yeni kurulumlarda hesap listesi boştur. **Ayarlar → Hesaplar** bölümünden bir hesap ekleyin veya seçin ve **Profil ile doğrula** seçeneğine basın. Önce varsayılan tarayıcınızın son kullanılan profilinde sağlayıcıya giriş yapmış olun. SiopeChat oturumu tarayıcı başlatmadan aktarır ve doğrular. Sohbet, geçmiş, model ve proje işlemleri HTTP üzerinden yürür. Oturum süresi dolarsa normal tarayıcınızda yeniden giriş yapıp doğrulamayı tekrarlayın.

Hesap ekle, düzenle ve sil düğmeleri hesap listesinin altında yan yana durur. Düzenle yalnız seçili hesabın görünen adını değiştirir; oturumu ve geçmişi korunur. Profil doğrulama ve çıkış düğmeleri oturum bilgilerinin altındadır. Dar terminallerde düğme adları kısalır.

<details>
<summary>Desteklenen tarayıcı profilleri ve sorun giderme</summary>

**Profil ile doğrula**, varsayılan tarayıcının son kullanılan profilinden seçili sağlayıcının oturumunu okur. Tarayıcıyı başlatmaz ve hata ayıklama bayrağı gerektirmez. Yalnızca ilgili sağlayıcının kimlik doğrulama çerezleri alınır; yeni oturum, kayıtlı oturumun yerine yazılmadan önce doğrulanır.

Chrome, Chromium, Brave/Brave Origin, Edge ve Vivaldi ortak Chromium okuyucusunu kullanır. Firefox ve Zen için `profiles.ini` ve `installs.ini` üzerinden çalışan salt okunur Gecko okuyucusu, macOS’taki Safari için ayrı bir WebKit okuyucusu bulunur. Çerezler tarayıcıya özgü sistem anahtarlığı bilgileriyle çözülür; anahtarlık erişim izni isteyebilir.

Kilitli veritabanları, özel veya yalıtılmış profil dizinleri ve desteklenmeyen şifreleme aktarımı engelleyebilir. Bazı Windows Chromium yapılandırmaları da buna dahildir. Hatalar; eksik bağımlılık, dosya izni, veritabanı erişimi, anahtarlık ve şifre çözme sorunlarını ayrı gösterir. Başarısız aktarım kayıtlı oturumunuzu silmez. Bağımlılıkları SiopeChat’i başlatan ortamda güncelleyin.

Firefox/Zen’in güncel kurtarma kaydındaki oturum çerezleri de desteklenir; ayrı kapsayıcılar ve eski kayıtlar birleştirilmez. Çerez veritabanı kilitliyse WAL/journal dosyalarıyla birlikte özel bir geçici kopyadan yeniden okunur. Tarayıcı profiliniz değiştirilmez.

Firefox ve Zen aynı salt okunur Gecko profil ve çerez okuyucusunu kullanır. Profil seçimi `profiles.ini` ve `installs.ini` üzerinden yapılır; belirsiz profiller birleştirilmeden hata bildirilir. Linux’ta Firefox’un Snap/Flatpak konumları ve Zen’in güncel XDG, eski ve Flatpak konumları bulunur. Taşınabilir veya özel profil dizinleri tahmin edilmez; tarayıcıyı sistem varsayılanı yapıp standart profil konumunu kullanın.

</details>

### Birden fazla hesap

Birden fazla hesap ekleyebilirsiniz. Gönderim için her sağlayıcıda bir hesap seçilidir; başka hesabın geçmişini görüntülemek bu seçimi değiştirmez. Farklı bir hesabı doğrulamak için önce varsayılan tarayıcınızda o hesabın açık olduğu profili seçin, ardından SiopeChat’te ilgili hesapta **Profil ile doğrula** seçeneğini kullanın.

Hesap satırına tıklayarak seçim yapın; ok işareti yönetilen hesabı gösterir. **Hesabı sil** (Hesaplar ekranında Alt+Backspace), onay aldıktan sonra yerel hesap kaydını, kayıtlı oturumu ve model tercihlerini kaldırır. Sağlayıcıdaki hesap ve arşivlenmiş sohbetler korunur. Silinen hesabın sekmeleri kapatılır, geçersiz kalan filtreler temizlenir ve başka etkin hesap seçilir. Son etkin hesabı silmeden önce başka hesap ekleyin.

## Kullanım

### Sohbet başlatma ve gezinme

Mesaj alanına yazıp **Enter** ile gönderin. Destekleyen terminallerde **Shift+Enter** yeni satır ekler; uygulama açıkken genişletilmiş tuş bildirimi istenir. Terminal Enter ve Shift+Enter’ı ayırt etmiyorsa **Alt+Enter** veya **Ctrl+J** kullanın. Yazma alanı ayarlanan yüksekliğe kadar büyür, ardından kaydırılır. Ctrl+A metni seçer, Ctrl+C seçimi kopyalar, Ctrl+V yapıştırır. Bir mesaja tıklayarak içeriğini seçip kopyalayabilirsiniz.

Yeni bir sohbette, etkin sağlayıcıların seçili hesaplarına ait modeller gösterilir. Durum satırında toplam model sayısını ve kaç sağlayıcının kataloğunun hazır olduğunu görebilirsiniz; diğer kataloglar arka planda yüklenir. Mevcut sohbet kendi hesabına bağlı kalır. Model ve düşünme eforu seçenekleri sağlayıcının kataloğundan alınır. Erişim bilgisi mevcutsa kullanılamayan veya üst abonelik gerektiren modeller elenir; yine de son karar sunucuya aittir. Sağlayıcının yayımladığı düşünme özetleri yanıttan ayrı gösterilir; gizli düşünce zincirine erişilmez.

Mesajdaki Markdown web bağlantısına tıklayarak varsayılan tarayıcınızda açabilirsiniz. Mesajın diğer alanlarına tıklamak kopyalamak için mesajı seçer.

### Yanıt araçları ve oluşturulan dosyalar

Her mesajın altında küçük bir araç çubuğu bulunur. Kopyalama yerel olarak çalışır; desteklenen kullanıcı mesajları düzenlenip yeniden gönderilebilir. Yanıtı yeniden üretme seçeneği, mesaja ve sağlayıcıya göre sunulur. ChatGPT ve Gemini’de yanıttan ayrı bir sohbet dalı da açabilirsiniz. Sağlayıcının yayımladığı kaynaklar ilgili yanıtla birlikte tutulur. Mevcut model ve efor bilgisi sağda, kayıtlı sürüm kontrollerinin önünde yer alır. Düzenleme ve yeniden üretme işlemleri, sağlayıcı doğrulamasından sonra etkin geçmişi günceller. Sohbet silme ve bölünmüş görünüm kontrolleri üstteki eşitleme düğmesinin yanındadır.

Üretilen dosyalar yanıtın içinde, mevcut ad, tür ve boyut bilgileriyle tıklanabilir dosya kartları olarak listelenir. Bir karta tıklamak indirme yöneticisinde arka plan aktarımı başlatır; dosya referansları ve metaverisi arşivlenmiş sohbette korunur. Korumalı kaynaklar ilgili sağlayıcı ve hesap üzerinden çözümlenir. Sağlayıcının saklama süresi ve dosyanın erişilebilirliği geçerlidir.

### İndirme yöneticisi

Dosya bağlantıları arka planda proje dizinindeki `downloads/` klasörüne indirilir. Konumu **Ayarlar → Genel → İndirme klasörü** bölümünden değiştirebilirsiniz; göreli yollar proje dizinine göre çözülür, tam yollar da desteklenir. Aynı anda en fazla iki aktarım çalışır, ilerleme durum çubuğunda gösterilir; mevcut dosyaların üzerine yazılmaz ve başarısız aktarımlar temizlenir. Normal web sayfaları varsayılan tarayıcıda açılır. ChatGPT sandbox dosyaları ait olduğu sohbet üzerinden, Gemini görsel yer tutucuları gerçek medya kayıtlarından çözümlenir; Claude sandbox/computer bağlantıları ait olduğu sohbetin dosya deposundan alınır. Korumalı indirmelerde seçili hesabın kayıtlı oturumu kullanılır; hesap bilgileri başka adreslere yönlendirilmez. Bulunamayan veya süresi dolmuş kaynaklar için hata gösterilir.

**Yardım** ile **Ayarlar** arasındaki **İndirmeler** düğmesini, `/downloads` komutunu veya **Alt+J** kısayolunu kullanın. Sayfada alınan baytlar, biliniyorsa toplam ve kalan boyut, tamamlanan/başarısız aktarımlar ve kaynak sohbet görünür. Tarih grupları katlanır; arama dosya adı, sohbet başlığı ve hesabı kapsar. Duruma göre filtreleyebilir; tarih, dosya adı ve boyuta göre sıralayabilirsiniz. **Eşitle** dosyaları arka planda kontrol eder; aynı kontrol on saniyede bir yapılır. Klasörden kaldırılan dosya **Silindi** olarak listede kalır; silindiği bilinen sohbetler de belirtilir. **Dosyayı sil** geçmiş kaydını korur, **Listeden kaldır** diskteki dosyaya dokunmaz. Arşiv eklentisi yeni indirme kayıtlarını SQLite’ta kalıcı tutar; eklenti kapalıyken geçmiş yalnız mevcut oturumda tutulur. Uygulama kapanırken yarıda kalan aktarımlar **Kesildi** olarak görünür. Bu özellik eklenmeden önce indirilen dosyalara otomatik kaynak sohbet kaydı oluşturulmaz.

Varsayılan indirme sınırı 512 MiB’tır. Genel ayarlardan boyut sınırını bayt cinsinden değiştirebilir ve yerel ağdan indirmeye izin verebilirsiniz. İnternetten indirmelerde özel IP ve geri döngü adresleri engellenir, her yönlendirme doğrulanır ve HTTPS’ten HTTP’ye geçilmez. Bağlantı doğrulanmış IP’ye kurulur; TLS ve HTTP için özgün alan adı korunur.

### Kısayollar, seçim ve arka plan yanıtları

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

Proje ayrıntılarından doğrudan sohbet başlatabilirsiniz. Projenin bağlı olduğu hesaptan model ve efor seçip mesaj gönderdiğinizde, o proje içinde yeni bir sohbet sekmesi açılır. Projenin kendisini düzenlerken mesaj alanı ve seçim kontrolleri gizlenir.

**Böl**, `/split` veya `/split-down` seçeneğinin ardından sol panelden başka bir sohbet seçin. Aktif olmayan panelin herhangi bir yerine tıklamak onu aktif eder. Pasif paneli fare tekerleğiyle kaydırmak odağı değiştirmez. `/unsplit` tek panele döner. Komut paletindeki `layout.create` ile açık sohbetleri, bölünme yönünü ve aktif paneli kaydedebilirsiniz. Sol panelden kaydedilmiş çalışma alanını açmak düzeni geri yükler. Yalnız sohbet referansları kaydedilir; geçici sohbetler, taslaklar ve çalışan istekler kayda alınmaz.

Sohbet silmek için yazma alanının yanındaki **Sil** düğmesini kullanın. `/project-delete` proje seçicisini açar; proje bilgilerinde de silme düğmesi vardır. Her ikisi kalıcı silme onayı ister; başlangıçta **Hayır** odaktadır. Yerel kayıt kaldırılmadan önce işlem sağlayıcıya gönderilir ve orada doğrulanır. Proje içindeki sohbetlerin durumu, sağlayıcının silme kurallarına bağlıdır. Sekme kapatmak sohbet silmez.

### Sağlayıcı farkları

| Sağlayıcı | Proje desteği | Mevcut sınırlar |
| --- | --- | --- |
| ChatGPT | Projeler, talimatlar, kaynaklar ve desteklenen sohbet taşıma işlemleri | Model, efor, geçici mod ve proje seçenekleri hesaba bağlıdır. |
| Gemini | Ayrı bir proje türü olarak Gemini defterleri; defter bağlamında sohbet, talimat ve metin kaynakları | Mevcut sohbet deftere taşınamaz. Dosya/URL kaynağı içe aktarma sunulmuyor. Düşünme seçenekleri ve varsayılanlar hesabın Gemini web model politikasından alınır. |
| Claude | Projeler, talimatlar, metin/dosya kaynakları ve desteklenen üyelik değişiklikleri | Projeler dışında geçici sohbet desteklenir; özel/varsayılan dışındaki gizlilik/bellek seçenekleri sunulmuyor. |

Gemini defterleri mevcut Gemini oturumunuzu kullanır; ayrı NotebookLM girişi gerekmez. Her sağlayıcının proje türü ve kaynak bilgileri kendi biçiminde korunur. Kullanabildiğiniz özellikler sağlayıcıya ve hesabınıza göre değişir.

Her dosya için yerel ek sınırı **20 MiB**’dır; sağlayıcı daha düşük sınır uygulayabilir. Geçici mod yalnız desteklendiğinde sunulur ve sohbet anlık görüntülerini yerelde kaydetmez. Sağlayıcının veri saklama kuralları geçerlidir.

## Ayarlar ve yerel veriler

### Ayarlar ve gizlilik

`config.json` yerelde oluşturulur ve Git kapsamı dışındadır. Varsayılanlar `src/settings.py` içinde tanımlıdır. Dil, **Ayarlar → Genel → Dil** bölümünden (`en`, `tr`, `ru`); sağlayıcılar **Sağlayıcılar**, özellik paketleri **Eklentiler** bölümünden yönetilir. En az bir sağlayıcı etkin kalmalıdır.

Farklı bir ayar/veri dizini için `SIOPECHAT_HOME` kullanın. Yapılandırmayı, hesap oturumlarını ve `data/` dizinini gizli tutun; hata raporlarına eklemeyin.

### Arşiv ve dosya saklama

Arşiv eklentisi; sohbetleri, sıralı mesajları, yayımlanmış düşünme verilerini, proje/defterleri, üyelikleri ve kaynak metaverilerini `data/archive.sqlite3` içinde tutar. Yüklenen dosyalar arşive kopyalanmaz; sağlayıcı kimliği ve mevcut dosya metaverisi saklanır, yerel dosya yolu veya içerik tutulmaz. Eski arşivlerdeki içerik adresli dosya kopyaları yükseltme sırasında kaldırılır; sohbet ve kaynak metaverisi korunur. İndirilen dosyalar seçilen indirme klasöründe kalır, arşivde yalnızca indirme geçmişi metaverisi bulunur. Uzak görsel ve kaynaklar referans olarak tutulur; her uzak dosya otomatik indirilmez.

**Yükseltme notu:** Şema v5 geçişi eski yükleme kopyalarını kalıcı olarak siler. Bu sürümle eski bir arşivi ilk kez açmadan önce çevrimdışı kopyalara ihtiyaç duyuyorsanız veya eski SiopeChat sürümüne dönme ihtimaliniz varsa `data/archive.sqlite3` ile `data/files/` klasörünü birlikte yedekleyin. Özel `SIOPECHAT_HOME` kullanıyorsanız bu yolları veri dizininize göre uyarlayın.

### Yükleme, bellek ve eşitleme

Kayıtlı sohbetler önce arşivden açılır, ardından sağlayıcıyla arka planda eşitlenir. Geçmiş yüklenirken yazmaya ve mesaj göndermeye devam edebilirsiniz. Eski mesajlar, devam eden yanıtın üzerine yazılmadan yeni yerel mesajlarla birleştirilir. Sol panelde ve projelerde kayıtlı sayfalar yenileme sırasında kullanılabilir; yenileme başarısız olursa son sağlam veri korunur.

Büyük geçmişler arayüz olay döngüsü dışında hazırlanır; sol panel yalnızca görünür satırları çizer. Arşiv kuyrukları ve çizim önbellekleri sınırlıdır. Arşivi kapatmak dosyalarını silmez; doğrudan sağlayıcıyla çalışmaya devam edebilirsiniz. Düzen eklentisini kapatmak, sağlayıcıları etkilemeden bölünmüş görünüm kontrollerini kaldırır.

<details>
<summary>Geçmiş sayfalama ve bellek sınırları</summary>

İlk SQLite okuması, 262.144 karakterlik serileştirilmiş kayıt bütçesi içinde en fazla 80 görünür mesaj getirir. Yukarı kaydırdıkça eski parçalar eklenir; yüklenmiş mesajlar kaldırılmaz. Konumunuz kalıcı mesaj kimliğiyle korunur. Tek bir mesaj, parça sınırına sığması için kesilmez.

Önce görünür alan, ardından üstte ve altta ikişer ekran hazırlanır. Her görünümde çizilmiş satırlar için 1 MiB LRU önbellek ve büyük kayıtları sıkıştıran ayrı bir 256 KiB ANSI önbelleği bulunur. Etkin olmayan ana sekme görünümleri ortak 4 MiB sınırını paylaşır. Metinler mümkün olduğunda görünümle paylaşılır; kabul edilmiş yanıtlar SQL geçmişinin yalnızca sınırlı bir son bölümünü günceller.

Sekme kapatıldığında mesaj ve çizim referansları serbest bırakılır. Arka planda devam eden yanıtın mevcut turu, kayıt tamamlanana kadar tutulur. Kaynak belleği görüntülenen geçmişle büyür; çizim önbellekleri sınırlı kalır. Python serbest bırakılan belleği yeniden kullanmak üzere tutabildiğinden süreç belleği (RSS) hemen düşmeyebilir.

</details>

Uzak bir yazma işleminin sonucu belirsizse yeniden göndermeden önce eşitleyin. SiopeChat, sağlayıcıya ulaşmış olabilecek bir işlemi otomatik olarak tekrarlamaz.



## Eklenti geliştirme

`extensions/my_feature/plugin.py` oluşturun. Ortak API üzerinden özellik kaydeden bir etkinleştirme fonksiyonu tanımlayın ve bunu bir `ExtensionSpec` ile dışa aktarın:

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

Arşive `context.api.get("archive")` ile erişilir. `conversations`, `messages` ve `resources` metotları kayıtları sağlayıcı ve hesap kapsamında, sayfalar halinde döndürür. Veri deposu sözleşmesi ve SQLite uygulaması `extensions/archive/repository.py`, sağlayıcı verilerinin normalleştirilmesi ise `adapters.py` içindedir.

Eklentiler yerel kod çalıştırır. Yalnız güvendiğiniz paketleri yükleyin. Ağ işlerini çizim fonksiyonlarında yürütmeyin; kaynak temizliğinde ortak yaşam döngüsünü kullanın.

Arşiv listeleri uzak kimliğe göre kararlı sıradadır; arayüz sıralaması ayrıca uygulanır. `siopechat action archive.inspect`, eksik dosya referanslarını ve sahipsiz içerikleri bildirir. `siopechat action archive.collect` yalnız temizlik önizlemesi yapar; adayları silmek istediğinizde aksiyon parametresi olarak `'{"dry_run": false}'` verin. Ortak kullanılan dosyalar, bilinmeyen dosyalar ve son bir saatte kaydedilen içerikler korunur. Daha yeni şemaya ait veritabanı değiştirilmeden reddedilir.

## Geliştirme

### Ortamı hazırlama ve kontroller

Uygulama geliştirmek için Python 3.11+ kullanın. [uv](https://docs.astral.sh/uv/) kuruluysa aşağıdaki komutları depo kökünde çalıştırın. Başlatıcılarla aynı bağımlılıkları kullanmak için projenin mevcut `python/` ortamını seçin:

```sh
# Linux / macOS
UV_PROJECT_ENVIRONMENT=python uv sync --locked --all-groups
python/bin/python run_tests.py
python/bin/python -m ruff check .
python/bin/python -m ruff format --check .
python/bin/python -m build
python/bin/python -m twine check dist/*
```

Windows PowerShell’de `$env:UV_PROJECT_ENVIRONMENT = "python"` ayarından sonra `uv sync --locked --all-groups` çalıştırın. Diğer komutlarda `python/bin/python` yerine `./python/Scripts/python.exe` kullanın.

`run_tests.py`; uygulama testlerini, sağlayıcı testlerinin yalıtılmış bir kopyasını ve sağlayıcı paketinin temel çalışma kontrolünü çalıştırır. Testler sahte istemciler ve örnek veriler kullanır; kişisel hesap oturumları gerekmez. Çalıştırıcı önce etkin sanal ortamı, ardından proje ortamlarını (`python/`, sonra `.venv/`), uv’yi ve son olarak kendisini başlatan yorumlayıcıyı seçer. Hangi ortamın kullanılacağını kesinleştirmek için komutu doğrudan istediğiniz Python ile çalıştırın.

Kod denetimi, biçim kontrolü ve paket derlemesi ayrı adımlardır. GitHub Actions uygulamayı Linux, Windows ve macOS’ta Python 3.11/3.14 ile; sağlayıcı paketini ayrıca Python 3.10/3.14 ile sınar. Çevrimdışı testlerin geçmesi, canlı sağlayıcı uyumluluğunu veya gerçek terminal davranışını tek başına doğrulamaz.

### Hangi kod nerede?

| Konum | Sorumluluk |
| --- | --- |
| `src/bootstrap.py`, `src/cli.py` | Uygulamanın kurulumu ve komut satırı girişleri |
| `src/accounts.py`, `src/tasks.py`, `src/runtime.py` | Hesap yönlendirme, görev sahipliği ve servis yaşam döngüsü |
| `src/providers/` | Bağımsız web oturumu istemcileri, taşıma katmanları ve sağlayıcı sözleşmeleri |
| `src/tui/` | Terminal görünümleri, kullanıcı girdisi ve çizim |
| `src/extension.py`, `src/tui/api.py` | Eklenti yaşam döngüsü ve eklentilere açık terminal API’si |
| `extensions/archive/`, `extensions/layout/` | Yerel geçmiş ve kaydedilmiş çalışma alanları |
| `tests/`, `src/providers/tests/` | Uygulama regresyonları ve bağımsız sağlayıcı testleri |

Protokol ayrıntılarını ilgili sağlayıcıda tutun. Sağlayıcı kodu uygulamanın veritabanını, ayarlarını, eklentilerini veya terminal arayüzünü içe aktarmamalıdır. Eklentiler servislerini `extension.py` üzerinden kaydeder; terminale katkı için `tui.api` kullanır. Ağ isteklerini ve maliyetli Markdown işlemlerini çizim fonksiyonlarının dışında yürütün.

Gönderimler `TaskService` üzerinden hesap bazında sıraya alınır. İptalde hesap, bağlantı temizliği ve kabul edilmiş yanıtın kaydı tamamlandıktan sonra serbest bırakılır. Sekme kapatıldığında yanıt, uygulama açık olduğu sürece arka planda devam eder. Değişiklikler sağlayıcı/hesap kimliğini, mesajların üst mesaj ilişkilerini, dallanmayı ve iptal davranışını korumalıdır. Sonucu belirsiz uzak yazma işlemleri otomatik tekrarlanmamalıdır.

### Sağlayıcı kütüphanesini bağımsız kullanma

Seçtiğiniz ortamda `python -m pip install ./src/providers` ile kurun. `ProviderClient` hesap adaptörlerini, akışları ve bağlantı temizliğini yönetir. `Backend`; model, sohbet, proje, kaynak, dosya yükleme ve indirme çözümleme işlemlerini sunar. Uygulama da aynı `create_provider` fonksiyonunu kullanır. Örnekler ve bağımsız testler için [sağlayıcı kılavuzuna](src/providers/README.md) bakın.

Taşıma katmanları, tarayıcı profili bulma ve yerel CDP erişimi Python ile uygulanmıştır. Gemini, ek bir Gemini SDK’sı olmadan paketteki HTTPS taşıma katmanını kullanır. ChatGPT’nin desteklenen Sentinel kontrolleri Python çalışma zamanı ve paketteki profille yürür. Değişen veya desteklenmeyen sunucu gereksinimleri ayrıca doğrulanmalıdır; çevrimdışı test verileri tüm canlı kontrolleri kapsamaz.

### Yerel verilerin yapısı

| Dosya | İçerik |
| --- | --- |
| `config.json` | Tercihler, hesap adları ve seçimi |
| `data/accounts.sqlite3` | Hesap oturumları, model katalogları, doğrulama kayıtları ve yedek sohbet önbelleği |
| `data/archive.sqlite3` | Arşivlenmiş geçmiş, kaynak bilgileri, indirme kayıtları ve yerel yanıt sürümleri |
| `data/workspaces.sqlite3` | Kaydedilmiş sohbet düzenleri |

Bu yollar yapılandırılmış uygulama dizinine göredir. Sağlayıcıya özel alanlar ilgili veritabanında JSON olarak tutulur. Sağlayıcı kütüphanesi, çağıranın verdiği kimlik bilgisi deposunu kullanır; bu SQLite şemalarına bağımlı değildir. Hesap deposu şifreli değildir; Unix dosya izinleri erişimi sahibiyle sınırlar. Şema geçişlerini transaction içinde yürütün ve mevcut kullanıcı verisini koruyun. Eski bir arşivi açmadan önce yukarıdaki yükseltme uyarısını okuyun.

### Performans ölçümü ve hata bildirimi

Performans bağımlılık grubunu kurduktan sonra çalıştırın:

```sh
python/bin/python tools/measure_performance.py --interval 0.5
```

CSV çıktısı süreç ağacının CPU kullanımını, RSS/USS/PSS belleğini, disk G/Ç’sini, açık tanıtıcıları, iş parçacıklarını ve süreç sayılarını kaydeder. Yanındaki JSON dosyası çalıştırma öncesi ve sonrası depolama sayılarıyla boyutlarını karşılaştırır. Tarama yalnızca dosya bilgilerini okur; sembolik bağlantıları izlemez. Başka dizinler için `--storage-root DİZİN` seçeneğini tekrarlayın; taramayı kapatmak için `--no-storage`, uygulamaya parametre iletmek için `--` kullanın. Birden fazla çekirdek kullanıldığında CPU toplamı %100’ü aşabilir; işletim sisteminin sağlamadığı ölçümler boş kalır.

ChatGPT gönderiminin aşamalarını ölçmek için başlatmadan önce `SIOPECHAT_SEND_TIMINGS=1` ayarlayın. Aynı hesap, model, efor, sohbet durumu ve ağ koşullarını karşılaştırın. Bağlantı kurulmasını, sağlayıcıdan ilk metni, arayüzde ilk görünür metni ve son işlemleri ayrı değerlendirin; örnek sayısını, medyanı ve p95’i raporlayın. Tanılama kayıtlarına oturum bilgisi, mesaj metni veya token içeriği eklemeyin.

Hata bildirirken sürümü, sağlayıcıyı, işlemi, terminali, işletim sistemini ve gizli bilgilerden arındırılmış hata mesajını paylaşın. Sorunun çevrimdışı testte mi yoksa canlı oturumda mı yaşandığını belirtin. Sürüm değişiklikleri [CHANGELOG.md](CHANGELOG.md) içinde yer alır.

## Lisans

Telif hakkı © TahsinCr. [GPL-3.0-only](LICENSE) ile lisanslanmıştır. Dahil edilen üçüncü taraf bileşenlerin bildirimleri `src/utils/` altında korunur.

[version-shield]: https://img.shields.io/badge/version-v1.5.2--beta-b7a3e3.svg?style=for-the-badge
[python-shield]: https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?style=for-the-badge&logo=python&logoColor=white
[license-shield]: https://img.shields.io/badge/license-GPL--3.0--only-blue.svg?style=for-the-badge
