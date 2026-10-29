"""ControlsComponent: controls behavior for ChatView."""

from __future__ import annotations

from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.utils import get_cwidth

from accounts import origin
from providers import Capability

from ..layout import render_tab_bar
from ..styles import ICONS
from ..widgets import (
    fit_text,
    on_click,
)


class ControlsComponent:
    def render_tabs(self):
        return render_tab_bar(self)

    def close_clicked_tab(self, index):
        self.select_tab(index)
        if self.active_tab == index:
            self.close_tab()

    def select_tab(self, index):
        self.switch_tab(index - self.active_tab)

    def editing_project(self):
        return bool(
            self.tab_kind == "project"
            and self.forms.visible
            and self.forms.inline_project == self.current_project.get("id")
        )

    def render_project_actions(self):
        return FormattedText(
            [
                (
                    "class:button",
                    f" {ICONS['edit'].icon} {self.tr('Düzenle')} ",
                    on_click(lambda: self.project_panel.edit(self.current_project.get("id"))),
                ),
                (
                    "class:button",
                    f" {ICONS['add'].icon} {self.tr('Kaynak')} ",
                    on_click(self.project_panel.add_source),
                ),
                (
                    "class:button",
                    f" {ICONS['minus'].icon} {self.tr('Kaynak')} ",
                    on_click(self.project_panel.remove_source),
                ),
                (
                    "class:button",
                    self.tr(" Sil "),
                    on_click(
                        lambda: self.delete_record("container", self.current_project.get("id"))
                    ),
                ),
                (
                    "class:button",
                    f" {ICONS['retry'].icon} {self.tr('Eşitle')} ",
                    on_click(self.show_project_details),
                ),
            ]
        )

    def render_chat_header(self):
        if self.tab_kind == "settings":
            return FormattedText([("class:settings.title", self.tr(" Ayarlar"))])
        if self.editing_project():
            return FormattedText([])
        selected = self.catalog.chat_model(self.model)
        model = (
            (selected.chat_title or selected.title)
            if selected
            else self.model
            or (self.tr("Yükleniyor…") if self.catalog_loading else self.tr("Seçilmedi"))
        )
        effort = (
            next(
                (choice.title for choice in selected.efforts if choice.id == self.effort),
                self.effort or self.tr("Sağlayıcı varsayılanı"),
            )
            if selected
            else self.effort or self.tr("Sağlayıcı varsayılanı")
        )
        if selected and not selected.efforts:
            effort = (
                self.tr("Okunamadı · varsayılan")
                if selected.metadata.get("effort_error")
                else self.tr("Desteklenmiyor")
            )
        model_text = f" {model} {ICONS['dropdown'].icon} "
        effort_text = f" {effort} {ICONS['dropdown'].icon} "
        project_text = (
            " "
            + fit_text(self.current_project.get("name") or self.tr("Projesiz"), 22)
            + f" {ICONS['dropdown'].icon} "
        )
        temporary_supported = Capability.TEMPORARY in self.provider_features
        if self.temporary:
            temporary_style = "class:metadata.temporary"
            temporary_text = f" {self.tr('Açık')} {ICONS['running'].icon} "
        elif temporary_supported:
            temporary_style = "class:metadata.temporary.disabled"
            temporary_text = f" {self.tr('Kapalı')} {ICONS['disabled'].icon} "
        else:
            temporary_style = "class:metadata.temporary.disabled"
            temporary_text = f" {self.tr('Desteklenmiyor')} "
        fragments = (
            list(self.render_project_actions()) + [("", "\n")] if self.tab_kind == "project" else []
        )
        used = 0
        width = self.terminal_width()
        for style, label, callback in (
            ("class:metadata.model", model_text, lambda: self.handle_command("/model")),
            ("class:metadata.effort", effort_text, lambda: self.handle_command("/effort")),
            ("class:metadata.project", project_text, self.open_project_picker),
            (temporary_style, temporary_text, self.toggle_temporary_chat),
        ):
            if self.tab_kind == "project" and style in (
                "class:metadata.project",
                "class:metadata.temporary",
            ):
                continue
            if (
                style == "class:metadata.project"
                and Capability.PROJECTS not in self.provider_features
            ):
                continue
            label = fit_text(label, width)
            if used:
                separator = " · " if used + 3 + get_cwidth(label) <= width else "\n"
                fragments.append(("class:help", separator))
                used = 0 if separator == "\n" else used + 3
            fragments.append((style, label, on_click(callback)))
            used += get_cwidth(label)
        return FormattedText(fragments)

    def toggle_temporary_chat(self):
        if not self.temporary and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr("Bu sağlayıcı geçici sohbeti desteklemiyor")
        elif self.busy or self.loading_conversation or self.background_jobs:
            self.status = self.tr("Devam eden işlem tamamlandıktan sonra geçici modu değiştirin")
        elif self.conversation_id or self.messages:
            self.status = self.tr(
                "Geçici mod yalnız ilk mesajdan önce değiştirilebilir · yeni sohbet açın"
            )
        else:
            self.temporary = not self.temporary
            if self.temporary:
                self.current_project = {}
            self.title = self.tr("Geçici sohbet") if self.temporary else self.tr("Yeni sohbet")
            self.status = (
                self.tr("Geçici sohbet · yerel kayıt kapalı")
                if self.temporary
                else self.tr("Normal sohbet")
            )
            self._save_active_tab()
        self.app.invalidate()

    def new_temporary_chat(self):
        if self.account_service and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr("Bu sağlayıcı geçici sohbeti desteklemiyor")
            self.app.invalidate()
            return
        count = len(self.tabs)
        self.new_tab()
        if len(self.tabs) == count:
            return
        self.temporary = True
        self.current_project = {}
        self.title = self.tr("Geçici sohbet")
        self.status = self.tr("Geçici sohbet · yerel kayıt kapalı")
        self._save_active_tab()
        self.app.invalidate()

    def open_project_picker(self, purpose="select"):
        self.project_picker_purpose = purpose
        query = (
            self.palette_search.text
            if self.palette_visible and self.palette_mode == "projects"
            else ""
        )
        self.open_palette("projects")
        self.palette_items = [
            {
                "id": "project.select:",
                "title": self.tr("Projesiz sohbet"),
                "detail": self.tr("Projeden çıkar / projesiz başlat"),
            }
        ] + [
            {
                "id": (
                    "project." + purpose + ":"
                    if purpose in ("edit", "delete")
                    else "project.select:"
                )
                + project_id,
                "title": item.get("name") or project_id,
                "detail": origin(project_id, self.account_key)[0]
                if purpose in ("edit", "delete")
                else self.tr("Sohbeti taşı / yeni sohbete bağla"),
            }
            for project_id, item in self.project_items.items()
            if purpose in ("edit", "delete")
            or origin(project_id, self.account_key)[0] == self.account_key
        ]
        self.palette_items.append(
            {
                "id": "command.temporary",
                "title": self.tr("Yeni geçici sohbet"),
                "detail": self.tr("Yerel kayıt yok"),
            }
        )
        if self.project_cursor:
            self.palette_items.append(
                {"id": "project.more", "title": self.tr("Daha fazla proje…"), "detail": ""}
            )
        if purpose in ("edit", "delete"):
            self.palette_items = [
                i
                for i in self.palette_items
                if i["id"].startswith("project." + purpose + ":") or i["id"] == "project.more"
            ]
        self.palette_search.text = query
        self.filter_palette()
        if not self.projects_loaded and not self.projects_loading:
            self.refresh_projects()

    def choose_project(self, project_id):
        if project_id and project_id not in self.project_items:
            self.status = self.tr("Listeden erişilebilir bir proje seçin")
            self.app.invalidate()
            return
        if self.temporary:
            self.status = self.tr("Geçici sohbet bir projeye bağlanamaz")
            self.app.invalidate()
            return
        if self.busy or self.loading_conversation or self.background_jobs:
            self.status = self.tr("Devam eden işlem tamamlandıktan sonra proje değiştirin")
            self.app.invalidate()
            return
        if self.tab_kind != "chat":
            self.status = self.tr("Önce bir sohbet sekmesi seçin")
            self.app.invalidate()
            return
        project = dict(self.project_items[project_id]) if project_id else {}
        if project_id and origin(project_id, self.account_key)[0] != self.account_key:
            self.status = self.tr(
                "Proje başka bir sağlayıcı hesabına ait; önce o hesapta sohbet açın"
            )
            self.app.invalidate()
            return
        if project.get("metadata", {}).get("read_only") and not (
            project.get("metadata", {}).get("supports_chat")
            or (self.provider_id == "gemini" and project.get("kind") == "notebook")
        ):
            self.status = self.tr("Gemini defteri şu anda salt okunur · F9 ile ayrıntıları açın")
            self.app.invalidate()
            return
        if not self.conversation_id:
            self.current_project = project
            self._save_active_tab()
            self.status = self.tr("Yeni sohbet · ") + (project.get("name") or self.tr("Projesiz"))
            self.app.invalidate()
            return
        if project_id == (self.current_project or {}).get("id"):
            self.status = (
                self.tr("Sohbet zaten bu projede")
                if project_id
                else self.tr("Sohbet zaten projesiz")
            )
            self.app.invalidate()
            return
        if self.move_conversation is None:
            self.status = self.tr("Sağlayıcı bu sohbeti taşımayı desteklemiyor")
            self.app.invalidate()
            return
        remote_id = self.conversation_id
        target_tab = self.tabs[self.active_tab]
        target_tab["project_changing"] = True
        move = self.move_conversation
        self.background_jobs += 1
        self.status = self.tr("Proje değiştiriliyor…")
        self.app.invalidate()

        def completed(_result):
            self.background_jobs -= 1
            target_tab["project_changing"] = False
            for tab in self.tabs:
                if tab.get("conversation_id") == remote_id:
                    tab["project"] = dict(project)
            if self.conversation_id == remote_id:
                self.current_project = dict(project)
                self._save_active_tab()
            for item in self.sidebar_items:
                if item.get("id") == remote_id:
                    item.update(
                        project_id=project_id or None,
                        project_name=project.get("name"),
                        project=dict(project),
                    )
            self.filter_sidebar()
            self.status = self.tr("Sohbet · ") + (project.get("name") or self.tr("Projesiz"))
            self.app.invalidate()

        def failed(reason):
            self.background_jobs -= 1
            target_tab["project_changing"] = False
            self.status = self.tr("Proje değiştirilemedi: ") + str(reason)
            self.app.invalidate()

        self._read_in_background(
            lambda: move(remote_id, project_id or None), completed, failed, session_bound=False
        )

    def scroll_transcript(self, direction):
        self.transcript_window.scroll_direction = direction
        self.pause_following()
        page = self.transcript_page_size()
        layout = getattr(self, "_virtual_transcript", None)
        line_count = layout.line_count if layout is not None else self.transcript_line_count
        last = max(0, line_count - (page + 3))
        self.transcript_window.vertical_scroll = min(
            last, max(0, min(last, self.transcript_window.vertical_scroll) + direction * page)
        )
        self.app.invalidate()

    def scroll_top(self):
        self.transcript_window.scroll_direction = 0
        if self.history_pager.request("top"):
            return
        self.pause_following()
        self.transcript_window.vertical_scroll = 0
        self.app.invalidate()

    def render_conversation_actions(self):
        controls = [
            (
                "class:button",
                f" {ICONS['retry'].icon} {self.tr('Eşitle')} ",
                on_click(lambda: self.synchronize("conversation")),
            )
        ]
        if self.conversation_id:
            if self.rename_conversation and Capability.RENAME in self.provider_features:
                controls.append(
                    (
                        "class:button",
                        f" {ICONS['edit'].icon} " + self.tr("Yeniden Adlandır") + " ",
                        on_click(
                            lambda: self.open_conversation_rename(self.conversation_id, self.title)
                        ),
                    )
                )
            controls.append(
                (
                    "class:button",
                    f" {ICONS['delete'].icon}" + self.tr(" Sil "),
                    on_click(lambda: self.delete_record(value=self.conversation_id)),
                )
            )
        if self.layout_enabled:
            controls.append(
                (
                    "class:button",
                    f" {ICONS['split'].icon} {self.tr('Böl')} ",
                    on_click(self.begin_split),
                )
            )
        return controls

    def render_composer_actions(self):
        compact = self.main_width() < 42
        controls = []
        if self.attach_file_callback or self.pending_attachments:
            controls.append(
                (
                    "class:button",
                    f" {len(self.pending_attachments)} {ICONS['attachment'].icon} "
                    if self.pending_attachments
                    else f" {ICONS['add'].icon} "
                    if compact
                    else f" {ICONS['add'].icon} {self.tr('Dosya')} ",
                    on_click(self.open_attachment),
                )
            )
        if not self.follow_output:
            controls.append(
                ("class:button", f" {ICONS['down'].icon} ", on_click(self.scroll_bottom))
            )
        if self.active_busy():
            controls.append(
                (
                    "class:button",
                    f" {ICONS['stop'].icon} "
                    if compact
                    else f" {ICONS['stop'].icon} " + self.tr("Durdur") + " ",
                    on_click(self.stop_active_response),
                )
            )
        else:
            controls.append(
                (
                    "class:button",
                    f" {ICONS['up'].icon} "
                    if compact
                    else self.tr(" Gönder") + f" {ICONS['up'].icon} ",
                    on_click(self.submit_prompt),
                )
            )
        used = sum(get_cwidth(f[1]) for f in controls)
        return FormattedText([("", " " * max(0, self.main_width() - used))] + controls)

    def render_footer(self):
        controls = [
            (
                self.tr(" {0} Yardım ").format(self.shortcut("command.help")),
                lambda: self.handle_command("/help"),
            ),
            (self.downloads_panel.footer_label(), self.downloads_panel.open),
            (
                f" {ICONS['settings'].icon} " + self.text("settings", self.tr("Ayarlar")) + " ",
                self.settings_dialog.open,
            ),
            (
                f" {ICONS['running'].icon} " + self.tr("Hesaplar") + " "
                if self.session_state.get("authenticated")
                else f" {ICONS['disabled'].icon} " + self.tr("Hesaplar") + " ",
                self.session_dialog.open,
            ),
        ]
        available = max(0, self.terminal_width() - sum(get_cwidth(label) for label, _ in controls))
        activity = "".join(fragment[1] for fragment in self.render_activity())
        status = fit_text(activity or " " + self.status, available)
        status += " " * max(0, available - get_cwidth(status))
        return FormattedText(
            [("class:help", status)]
            + [
                (
                    (
                        "class:connection.connected"
                        if self.session_state.get("authenticated")
                        else "class:connection.disconnected"
                    )
                    if callback == self.session_dialog.open
                    else "class:button",
                    label,
                    on_click(callback),
                )
                for label, callback in controls
            ]
        )

    def settings_rows(self):
        if self.settings_rows_callback is None:
            raise ValueError(self.tr("Ayar bağlantısı kurulmamış"))
        rows = self.settings_rows_callback()
        for row in rows:
            if row["section"] == "application" and row["key"] == "model":
                row["choices"] = ("", *(m.id for m in self.catalog.chat_models))
                row["choice_labels"] = {
                    m.id: m.chat_title or m.title for m in self.catalog.chat_models
                }
            if row["section"] == "application" and row["key"] == "effort":
                selected = self.catalog.chat_model(self.model)
                row["choices"] = tuple(e.id for e in selected.efforts) if selected else ()
                row["choice_labels"] = {e.id: e.title for e in selected.efforts} if selected else {}
        return rows
