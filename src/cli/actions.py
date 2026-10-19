"""Terminal action titles and command ordering."""

_ACTION_NAMES = {
    "chat.new": "Yeni sohbet",
    "chat.temporary": "Geçici sohbet",
    "chat.send": "Mesaj gönder",
    "chat.attach": "Dosya ekle",
    "chat.resume": "Geçmişten sohbet aç",
    "chat.interrupt": "Yanıtı durdur / seçimi kopyala",
    "chat.newline": "Yeni satır · Alt+Enter",
    "chat.newline_shift": "Yeni satır · Shift+Enter",
    "chat.newline_lf": "Yeni satır · Ctrl+J",
    "chat.next_tab": "Sonraki sekme",
    "chat.previous_tab": "Önceki sekme",
    "chat.close_tab": "Sekmeyi kapat",
    "chat.close_all_tabs": "Tüm sekmeleri kapat",
    "chat.copy_response": "Son yanıtı kopyala",
    "chat.reload": "Sohbeti eşitle",
    "chat.clear_transcript": "Görüntülenen geçmişi temizle",
    "chat.paste_image": "Metin / görsel yapıştır",
    "chat.remove_attachment": "Son eki kaldır",
    "command.model": "Model seç",
    "command.effort": "Düşünme seviyesi seç",
    "command.rename": "Sohbeti yeniden adlandır",
    "command.project": "Proje bilgileri",
    "command.projects": "Projeleri listele",
    "projects.create": "Proje oluştur",
    "projects.edit": "Proje adı ve talimatlarını düzenle",
    "projects.add_source": "Projeye kaynak ekle",
    "projects.remove_source": "Proje kaynağını kaldır",
    "chat.delete": "Sohbeti sağlayıcıdan sil",
    "projects.delete": "Projeyi sağlayıcıdan sil",
    "layout.next_pane": "Sonraki bölünmüş panele geç",
    "layout.previous_pane": "Önceki bölünmüş panele geç",
    "layout.split": "Yan yana böl",
    "layout.split_down": "Alt alta böl",
    "layout.unsplit": "Bölmeyi kaldır",
    "accounts.add": "Hesap ekle",
    "command.session": "Hesap ve oturum yönetimi",
    "ui.search": "Sohbetlerde ara",
    "ui.filter": "Sağlayıcı filtresi",
    "ui.toggle_sidebar": "Sol paneli aç / kapat",
    "command.refresh": "Verileri eşitle",
    "command.settings": "Ayarlar",
    "command.help": "Yardım",
    "ui.command_palette": "Komut paleti",
    "edit.select_all": "Tüm metni / mesajı seç",
    "edit.cut": "Metni kes",
    "app.quit": "Uygulamadan çık",
    "app.quit_eof": "Boş mesajla çık",
    "chat.scroll_top": "İlk mesaja git",
    "chat.scroll_bottom": "Son mesaja git",
}
_GROUPS = (
    "Sohbet",
    "Model",
    "Projeler",
    "Düzen",
    "Hesaplar",
    "Görünüm",
    "Düzenleme",
    "Gezinme",
    "Uygulama",
    "Eklentiler",
)


def action_group(action_id):
    if action_id in ("command.model", "command.effort"):
        return "Model"
    if action_id in ("command.project", "command.projects") or action_id.startswith("projects."):
        return "Projeler"
    if action_id == "command.session" or action_id.startswith("accounts."):
        return "Hesaplar"
    if action_id.startswith("layout."):
        return "Düzen"
    if action_id.startswith("navigation."):
        return "Gezinme"
    if action_id.startswith("edit."):
        return "Düzenleme"
    if action_id.startswith("chat.") or action_id == "command.rename":
        return "Sohbet"
    if action_id.startswith("ui.") and action_id != "ui.command_palette":
        return "Görünüm"
    if action_id.startswith(("app.", "command.", "ui.")):
        return "Uygulama"
    return "Eklentiler"


def action_title(action_id):
    title = _ACTION_NAMES.get(action_id, action_id.partition(".")[2].replace("_", " ").capitalize())
    return action_group(action_id) + " · " + title


def action_order(action_id):
    return _GROUPS.index(action_group(action_id)), 0 if action_id == "chat.new" else 1, action_id
