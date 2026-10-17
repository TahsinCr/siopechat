"""One action dispatch path for command palette, keyboard and extension calls."""

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from utils.events import EventBus


@dataclass(frozen=True)
class Action:
    id: str
    title: str
    callback: Callable[..., Any]
    keybinding: str | None = None
    available: Callable[[], bool] = lambda: True


@dataclass(frozen=True)
class ActionChanged:
    id: str
    state: str


class ActionService:
    def __init__(self, events: EventBus | None = None):
        self.events = events
        self._actions: dict[str, tuple[str, Action]] = {}

    def register(self, owner: str, action: Action) -> None:
        if not action.id or action.id in self._actions:
            raise ValueError("action ID must be unique")
        self._actions[action.id] = (owner, action)

    def unregister(self, owner: str) -> None:
        self._actions = {key: value for key, value in self._actions.items() if value[0] != owner}

    @staticmethod
    def _available(action: Action) -> bool:
        try:
            return bool(action.available())
        except Exception:
            return False

    def search(self, query: str = "") -> tuple[Action, ...]:
        query = query.casefold()
        return tuple(
            action
            for _, action in self._actions.values()
            if query in f"{action.id} {action.title}".casefold() and self._available(action)
        )

    async def invoke(self, action_id: str, **arguments: Any) -> Any:
        action = self._actions[action_id][1]
        if not self._available(action):
            raise ValueError("action is currently unavailable")
        self._emit(action_id, "running")
        try:
            result = action.callback(**arguments)
            result = await result if inspect.isawaitable(result) else result
        except BaseException:
            self._emit(action_id, "failed")
            raise
        self._emit(action_id, "completed")
        return result

    def _emit(self, action_id: str, state: str) -> None:
        if self.events is not None and not self.events.closed:
            self.events.publish(ActionChanged(action_id, state))

    def keymap(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        overrides = overrides or {}
        result = {}
        for _, action in self._actions.values():
            key = overrides.get(action.id, action.keybinding)
            if key:
                if key in result:
                    raise ValueError(f"keybinding collision: {key}")
                result[key] = action.id
        return result


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
    "chat.copy_response": "Son yanıtı kopyala",
    "chat.reload": "Geçmişi yenile",
    "chat.clear_transcript": "Görüntülenen geçmişi temizle",
    "chat.paste_image": "Metin / görsel yapıştır",
    "chat.remove_attachment": "Son eki kaldır",
    "command.model": "Model seç",
    "command.effort": "Düşünme seviyesi seç",
    "command.rename": "Sohbeti yeniden adlandır",
    "command.project": "Proje bilgileri",
    "command.projects": "Projeleri listele",
    "projects.create": "Proje oluştur",
    "layout.split": "Yan yana böl",
    "layout.split_down": "Alt alta böl",
    "layout.unsplit": "Bölmeyi kaldır",
    "accounts.add": "Hesap ekle",
    "command.session": "Hesap ve oturum yönetimi",
    "ui.search": "Sohbetlerde ara",
    "ui.filter": "Sağlayıcı filtresi",
    "ui.toggle_sidebar": "Sol paneli aç / kapat",
    "command.refresh": "Sağlayıcı verilerini yenile",
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
