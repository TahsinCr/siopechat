"""Explicit session controls; authentication never asks the terminal for a password."""

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.widgets import Button, Frame


class SessionDialog:
    def __init__(self, view):
        self.view = view
        self.visible = False
        self.loading = False
        self.confirm_logout = False
        self.message = ""
        self.buttons = {
            "verify": Button("Doğrula", width=24, handler=lambda: self.perform("verify")),
            "default_browser": Button(
                "Tarayıcıda aç", width=24, handler=lambda: self.perform("default_browser")
            ),
            "connect_browser": Button(
                "Bağlı tarayıcı", width=24, handler=lambda: self.perform("connect_browser")
            ),
            "logout": Button("Çıkış", width=24, handler=self.logout),
        }
        self.close_button = Button("Kapat", handler=self.close)
        keys = KeyBindings()
        keys.add("escape")(lambda _: self.close())
        keys.add("tab")(lambda event: event.app.layout.focus_next())
        keys.add("s-tab")(lambda event: event.app.layout.focus_previous())
        self.container = Frame(
            HSplit(
                [
                    Window(
                        height=2, content=FormattedTextControl(self.render_state), wrap_lines=True
                    ),
                    Window(
                        height=D(min=1, preferred=4, max=4),
                        content=FormattedTextControl(self.render_help),
                        wrap_lines=True,
                    ),
                    VSplit(
                        [self.buttons["verify"], self.buttons["default_browser"]],
                        padding=2,
                        height=1,
                    ),
                    VSplit(
                        [self.buttons["connect_browser"], self.buttons["logout"]],
                        padding=2,
                        height=1,
                    ),
                    Window(
                        height=D(min=1, preferred=2, max=3),
                        content=FormattedTextControl(lambda: self.message),
                        wrap_lines=True,
                    ),
                    self.close_button,
                ],
                modal=False,
                key_bindings=keys,
                padding=0,
            ),
            title=lambda: self.view.text("session", "Hesap ve oturum"),
        )

    def open(self):
        self.view.settings_dialog.open("accounts")

    def activate(self, focus=True):
        self.visible = True
        self.confirm_logout = False
        for key, button in self.buttons.items():
            label = {
                "verify": ("verify_session", "Oturumu doğrula"),
                "default_browser": ("open_browser", "Tarayıcıda aç"),
                "connect_browser": ("connect_browser", "Bağlı tarayıcı"),
                "logout": ("logout", "Çıkış yap"),
            }[key]
            button.text = self.view.text(*label)
        width = max(12, min(24, (self.view.terminal_width() - 26) // 2))
        for button in self.buttons.values():
            button.width = width
            button.window.width = width
        if width < 22:
            self.buttons["default_browser"].text = "Tarayıcıda aç"
        self.close_button.text = self.view.text("close", "Kapat")
        if focus:
            self.view.app.layout.focus(self.buttons["verify"])
        self.perform("status")

    def render_state(self):
        state = self.view.session_state
        verified = (
            self.view.text("verified", "Doğrulandı")
            if state.get("authenticated")
            else self.view.text("not_verified", "Doğrulanmadı")
        )
        saved = (
            self.view.text("yes", "var")
            if state.get("saved_snapshot")
            else self.view.text("no", "yok")
        )
        return f"ChatGPT · {verified}\n{self.view.text('saved_session', 'Kayıtlı oturum')}: {saved}"

    def render_help(self):
        return self.view.text(
            "login_help",
            "Kayıtlı oturum: tarayıcı açmadan doğrulayın.\nİlk giriş: Bağlı tarayıcı → giriş → Doğrula.\nVarsayılan tarayıcı, oturumu otomatik aktarmaz.\nÇıkış yalnız uygulamadaki oturumu unutur.",
        )

    def perform(self, action):
        if self.loading:
            return
        if self.view.session_action is None:
            self.message = self.view.text("session_unavailable", "Oturum bağlantısı kurulmamış")
            self.view.app.invalidate()
            return
        if action in ("logout", "verify", "connect_browser") and (
            self.view.busy
            or self.view.loading_conversation
            or self.view.uploading_image
            or self.view.background_jobs
        ):
            self.message = self.view.text(
                "wait_operation", "Önce devam eden işlemi tamamlayın veya durdurun"
            )
            self.view.app.invalidate()
            return
        self.loading = True
        self.message = self.view.text("working", "İşlem sürüyor…")

        def success(result):
            self.loading = False
            self.view.session_state.update(result)
            if action == "logout":
                self.view.after_logout()
                self.message = self.view.text("logged_out", "Uygulamadaki oturum kapatıldı")
            elif action == "verify":
                self.view.session_state["authenticated"] = True
                self.view.refresh_sidebar()
                self.view.refresh_provider_catalog()
                self.message = self.view.text("verified", "Oturum doğrulandı")
            elif action == "default_browser":
                self.message = self.view.text(
                    "default_opened",
                    "Varsayılan tarayıcı açıldı. Oturumu uygulamaya bağlamak için bağlı tarayıcı akışını kullanın.",
                )
            elif action == "connect_browser":
                self.message = self.view.text(
                    "finish_login", "Açılan tarayıcıda giriş yapın, sonra Oturumu doğrula'ya basın."
                )
            else:
                self.message = ""
            self.view.app.invalidate()

        def failure(reason):
            if action == "verify":
                self.view.session_state["authenticated"] = False
            self.loading = False
            self.message = str(reason)
            self.view.app.invalidate()

        self.view._read_in_background(
            lambda: self.view.session_action(action), success, failure, session_bound=False
        )

    def logout(self):
        if not self.confirm_logout:
            self.confirm_logout = True
            self.buttons["logout"].text = self.view.text("confirm_logout", "Oturumu unut")
            self.message = self.view.text(
                "logout_confirmation",
                "Kayıtlı uygulama oturumu unutulacak. Onaylamak için Oturumu unut'a basın.",
            )
            self.view.app.invalidate()
        else:
            self.perform("logout")

    def close(self):
        self.visible = False
        self.view.settings_dialog.close()
