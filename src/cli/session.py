"""Explicit session controls; authentication never asks the terminal for a password."""

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import DynamicContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, Frame

from .widgets import BoundedTextControl, fit_text, on_click


class SessionDialog:
    def __init__(self, view):
        self.view = view
        self.visible = False
        self.loading = False
        self.confirm_logout = False
        self.add_account_button = Button(
            self.view.tr("＋ Hesap ekle"), handler=self.view.add_account
        )
        self.message = ""
        self.account_index = 0
        self.account_control = BoundedTextControl(
            self.render_accounts,
            focusable=True,
            get_cursor_position=lambda: Point(0, self.account_index),
        )
        self.buttons = {
            "verify_profile": Button(
                self.view.text("verify_profile", "Profil ile doğrula"),
                width=24,
                handler=lambda: self.perform("verify_profile"),
            ),
            "logout": Button(self.view.tr("Çıkış"), width=24, handler=self.logout),
            "remove": Button(self.view.tr("Hesabı sil"), width=24, handler=self.remove),
        }
        self.account_actions = VSplit(
            [self.add_account_button, self.buttons["remove"]], padding=1, height=1
        )
        self.actions = VSplit(
            [self.buttons["verify_profile"], self.buttons["logout"]], padding=1, height=1
        )
        self.close_button = Button(self.view.tr("Kapat"), handler=self.close)
        keys = KeyBindings()
        account_focus = Condition(
            lambda: self.view.app.layout.current_control is self.account_control
        )
        keys.add("up", filter=account_focus)(lambda _: self.move_account(-1))
        keys.add("down", filter=account_focus)(lambda _: self.move_account(1))
        keys.add("enter", filter=account_focus)(
            lambda _: self.choose_account(
                self.view.account_service.items()[self.account_index]["key"]
            )
        )
        keys.add("escape")(lambda _: self.close())
        keys.add("tab")(lambda event: event.app.layout.focus_next())
        keys.add("s-tab")(lambda event: event.app.layout.focus_previous())
        self.container = Frame(
            HSplit(
                [
                    ConditionalContainer(
                        HSplit(
                            [
                                Window(
                                    content=self.account_control,
                                    right_margins=[ScrollbarMargin(display_arrows=True)],
                                    height=D(min=1, preferred=6, max=6),
                                ),
                                DynamicContainer(lambda: self.action_layout(accounts=True)),
                                Window(height=1, char="─", style="class:separator"),
                            ]
                        ),
                        filter=Condition(lambda: self.view.account_service is not None),
                    ),
                    Window(
                        height=2, content=BoundedTextControl(self.render_state), wrap_lines=True
                    ),
                    Window(
                        height=D(min=1, preferred=4, max=4),
                        content=BoundedTextControl(self.render_help),
                        wrap_lines=True,
                    ),
                    DynamicContainer(self.action_layout),
                    Window(
                        height=D(min=1, preferred=2, max=3),
                        content=BoundedTextControl(lambda: self.message),
                        wrap_lines=True,
                    ),
                ],
                modal=False,
                key_bindings=keys,
                padding=0,
            ),
            title=lambda: self.view.text("session", self.view.tr("Hesap ve oturum")),
        )

    def action_layout(self, accounts=False):
        width = max(1, self.view.settings_dialog.content_width() - 4)
        if accounts:
            buttons = [self.add_account_button, self.buttons["remove"]]
            labels = [self.view.tr("＋ Hesap ekle"), self.view.tr("Hesabı sil")]
            compact = [self.view.tr("＋ Ekle"), self.view.tr("Sil")]
        else:
            buttons = [self.buttons["verify_profile"], self.buttons["logout"]]
            labels = [
                self.view.text("verify_profile", "Profil ile doğrula"),
                self.view.tr("Oturumu unut") if self.confirm_logout else self.view.tr("Çıkış yap"),
            ]
            compact = [
                self.view.tr("Doğrula"),
                self.view.tr("Unut") if self.confirm_logout else self.view.tr("Çıkış"),
            ]
        if sum(get_cwidth(label) + 4 for label in labels) + 1 > width:
            labels = compact
        sizes = [get_cwidth(label) + 2 for label in labels]
        spare = max(0, width - sum(sizes) - 1)
        for index, button in enumerate(buttons):
            padding = min(2, spare)
            spare -= padding
            button.text = labels[index]
            button.width = button.window.width = sizes[index] + padding
        return self.account_actions if accounts else self.actions

    def render_accounts(self):
        if self.view.account_service is None:
            return []
        active_keys = (
            {item["key"] for item in self.view.account_service.active_accounts()}
            if hasattr(self.view.account_service, "active_accounts")
            else {self.view.account_key}
        )
        return [
            (
                "class:menu.selection"
                if index == self.account_index
                and self.view.app.layout.current_control is self.account_control
                else "class:tab.active"
                if item["key"] in active_keys
                else "class:button",
                self.account_line(item, active_keys),
                on_click(lambda key=item["key"]: self.choose_account(key)),
            )
            for index, item in enumerate(self.view.account_service.items())
        ]

    def account_line(self, item, active_keys):
        width = max(1, self.view.settings_dialog.content_width() - 5)
        label = (
            ("› " if item["key"] == self.view.account_key else "  ")
            + f"{item['icon']} {item['label']} · {item['provider']}"
            + (self.view.tr(" · etkin") if item["key"] in active_keys else "")
            + (self.view.tr(" · kapalı") if not item.get("enabled", True) else "")
        )
        label = fit_text(label, width)
        return label + " " * max(0, width - get_cwidth(label)) + "\n"

    def move_account(self, delta):
        if self.view.account_service:
            self.account_index = max(
                0, min(len(self.view.account_service.items()) - 1, self.account_index + delta)
            )
            self.view.app.invalidate()

    def choose_account(self, key):
        if self.loading or self.view.background_jobs:
            return
        if self.view.select_account(key) is False:
            return
        self.confirm_logout = False
        self.activate(focus=False)
        self.view.app.layout.focus(self.account_control)

    def open(self):
        self.view.settings_dialog.open("accounts")

    def activate(self, focus=True):
        self.add_account_button.text = self.view.tr("＋ Hesap ekle")
        self.visible = True
        self.confirm_logout = False
        if self.view.account_service:
            self.account_index = next(
                (
                    i
                    for i, item in enumerate(self.view.account_service.items())
                    if item["key"] == self.view.account_key
                ),
                0,
            )
        for key, button in self.buttons.items():
            label = {
                "verify_profile": ("verify_profile", "Profil ile doğrula"),
                "logout": ("logout", self.view.tr("Çıkış yap")),
                "remove": ("remove_account", self.view.tr("Hesabı sil")),
            }[key]
            button.text = self.view.text(*label)
        width = max(12, min(24, (self.view.terminal_width() - 26) // 2))
        for button in self.buttons.values():
            button.width = width
            button.window.width = width
        self.close_button.text = self.view.text("close", self.view.tr("Kapat"))
        if focus:
            self.view.app.layout.focus(self.buttons["verify_profile"])
        self.perform("status")

    def render_state(self):
        state = self.view.session_state
        verified = (
            self.view.text("verified", self.view.tr("Doğrulandı"))
            if state.get("authenticated")
            else self.view.text("not_verified", self.view.tr("Doğrulanmadı"))
        )
        saved = (
            self.view.text("yes", self.view.tr("var"))
            if state.get("saved_snapshot")
            else self.view.text("no", self.view.tr("yok"))
        )
        transport = self.view.tr("Bağlantı: kayıtlı web oturumu")
        return f"{self.view.session_state.get('account', 'ChatGPT')} · {verified}\n{self.view.text('saved_session', self.view.tr('Kayıtlı oturum'))}: {saved}\n{transport}"

    def render_help(self):
        return self.view.text(
            "login_help",
            self.view.tr(
                "Profil ile doğrula, varsayılan tarayıcının son kullanılan profilini okur.\nHesabınıza bu profilde önceden giriş yapmış olmalısınız.\nDoğrulama sırasında tarayıcı açılmaz.\nÇıkış yalnız uygulamadaki oturumu unutur."
            ),
        )

    def perform(self, action):
        if self.loading:
            return
        if self.view.session_action is None:
            self.message = self.view.text(
                "session_unavailable", self.view.tr("Oturum bağlantısı kurulmamış")
            )
            self.view.app.invalidate()
            return
        key = self.view.account_key
        callback = self.view.session_action
        account_busy = any(
            tab.get("busy") and tab.get("account_key", key) == key for tab in self.view.tabs
        )
        if action in ("logout", "verify_profile", "remove") and (
            account_busy
            or self.view.loading_conversation
            or self.view.uploading_image
            or self.view.background_jobs
        ):
            self.message = self.view.text(
                "wait_operation", self.view.tr("Önce devam eden işlemi tamamlayın veya durdurun")
            )
            self.view.app.invalidate()
            return
        self.loading = True
        self.message = self.view.text("working", self.view.tr("İşlem sürüyor…"))

        def success(result):
            self.loading = False
            if self.view.account_key != key:
                self.message = self.view.tr("Hesap işlemi tamamlandı")
                self.view.app.invalidate()
                return
            self.view.session_state.update(result)
            if action == "remove":
                self.view.after_logout()
                self.view.bind_account(result["active_account"])
                self.view.restore_account_preference()
                self.view._account_projects.pop(key, None)
                if self.view.provider_filter is not None:
                    self.view.provider_filter.intersection_update(
                        item["provider"] for item in self.view.account_service.items()
                    )
                    if not self.view.provider_filter:
                        self.view.provider_filter = None
                if self.view.account_filter is not None:
                    self.view.account_filter.discard(key)
                    if not self.view.account_filter:
                        self.view.account_filter = None
                self.view._save_active_tab()
                self.activate(focus=False)
                self.view.refresh_sidebar()
                self.view.refresh_provider_catalog()
                self.message = self.view.tr("Yerel hesap silindi")
            elif action == "logout":
                self.view.after_logout()
                self.message = self.view.text(
                    "logged_out", self.view.tr("Uygulamadaki oturum kapatıldı")
                )
            elif action == "verify_profile":
                self.view.session_state["authenticated"] = True
                self.view.refresh_sidebar()
                self.view.refresh_provider_catalog()
                self.message = self.view.text(
                    "profile_verified", "Profil doğrulandı: {browser}"
                ).format(browser=result.get("imported_browser", ""))
            else:
                self.message = ""
            self.view.app.invalidate()

        def failure(reason):
            self.loading = False
            self.message = self.view.tr(str(reason))
            self.view.app.invalidate()

        self.view._read_in_background(
            lambda: callback(action), success, failure, session_bound=False
        )

    def remove(self):
        if self.loading or self.view.background_jobs:
            return
        label = self.view.session_state.get("account", "")
        self.view.forms.confirm(
            self.view.tr("Yerel hesabı sil: {0}").format(label),
            lambda: self.perform("remove"),
        )

    def logout(self):
        if not self.confirm_logout:
            self.confirm_logout = True
            self.buttons["logout"].text = self.view.text(
                "confirm_logout", self.view.tr("Oturumu unut")
            )
            self.message = self.view.text(
                "logout_confirmation",
                self.view.tr(
                    "Kayıtlı uygulama oturumu unutulacak. Onaylamak için Oturumu unut'a basın."
                ),
            )
            self.view.app.invalidate()
        else:
            self.perform("logout")

    def close(self):
        self.visible = False
        self.view.settings_dialog.close()
