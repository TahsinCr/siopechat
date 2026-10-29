"""ComposerComponent: composer behavior for ChatView."""

from __future__ import annotations

import json
import threading
import time
import uuid

from providers import Capability
from providers.models import FeatureState

from ..styles import Icon


class ComposerComponent:
    def submit_prompt(self) -> None:
        prompt = self.input.text.strip()
        if prompt.startswith("/"):
            self.input.text = ""
            self.handle_command(prompt)
            return
        if self.account_service and not self.account_key:
            self.status = self.tr("Önce hesap ekleyin")
            self.settings_dialog.open("accounts")
            return
        if (
            self.account_service
            and hasattr(self.account_service, "enabled")
            and not self.account_service.enabled(self.provider_id)
        ):
            self.status = (
                self.tr("Sağlayıcı devre dışı · taslak korundu · Ayarlar")
                + f" {Icon.RIGHT} "
                + self.tr("Sağlayıcılar")
            )
            self.app.invalidate()
            return
        self.normalize_model_selection()
        if self.editing_project():
            self.status = self.tr("Önce proje düzenlemesini kaydedin veya iptal edin")
            self.app.invalidate()
            return
        if not self.input.text.lstrip().startswith("/") and (
            self.model.casefold().endswith("-wm")
            or (
                self.catalog.state == FeatureState.AVAILABLE
                and not any(model.id == self.model for model in self.catalog.chat_models)
            )
        ):
            self.status = self.tr("Önce /model ile normal bir sohbet modeli seçin · taslak korundu")
            self.app.invalidate()
            return
        if self.loading_conversation or self.uploading_image:
            self.status = self.tr("Conversation or image still loading · draft kept")
            self.app.invalidate()
            return
        if self.tabs[self.active_tab].get("project_changing"):
            self.status = self.tr("Proje değiştiriliyor · taslak korundu")
            self.app.invalidate()
            return
        if self.temporary and Capability.TEMPORARY not in self.provider_features:
            self.status = self.tr(
                "Bu sağlayıcı geçici sohbeti desteklemiyor · yeni normal sohbet açın"
            )
            self.app.invalidate()
            return
        if (
            self.active_busy()
            or sum(bool(tab.get("busy")) for tab in self.tabs + self.background_tabs) >= 4
        ):
            self.status = self.tr(
                "Bu sohbetin yanıtını bekleyin · aynı anda en fazla 4 bekleyen istek"
            )
            self.app.invalidate()
            return
        if (
            self.account_service
            and hasattr(self.account_service, "active_accounts")
            and not self.input.text.lstrip().startswith("/")
            and self.account_key
            not in {item["key"] for item in self.account_service.active_accounts()}
        ):
            self.status = self.tr(
                "Bu sağlayıcıda başka hesap etkin · Hesaplar’dan bu hesabı seçin · taslak korundu"
            )
            self.app.invalidate()
            return
        if (
            self.current_project.get("metadata", {}).get("read_only")
            and not (
                self.current_project.get("metadata", {}).get("supports_chat")
                or (self.provider_id == "gemini" and self.current_project.get("kind") == "notebook")
            )
            and not self.input.text.lstrip().startswith("/")
        ):
            self.status = self.tr(
                "Bu Gemini defteri salt okunur · defter bağlamında gönderim henüz desteklenmiyor · taslak korundu"
            )
            self.app.invalidate()
            return
        prompt = self.input.text.strip()
        attachments = list(self.pending_attachments)
        if attachments and self._attachments_in_flight:
            self.status = self.tr("Ekler başka bir sohbete gönderiliyor · bekleyin")
            self.app.invalidate()
            return
        if not prompt and not attachments:
            return
        self.tabs[self.active_tab]["empty_workspace"] = False
        if self.tab_kind == "project":
            source_tab = self.tabs[self.active_tab]
            project, key = dict(self.current_project), self.account_key
            model, effort = self.model, self.effort
            count = len(self.tabs)
            self.new_tab()
            if len(self.tabs) == count:
                return
            if self.account_service:
                self.bind_account(key)
            self.model, self.effort = model, effort
            self.current_project = project
            self.temporary = False
            # The details tab keeps its content; its submitted draft belongs to the new chat.
            source_tab.update(draft="")
        self.input.text = ""
        if prompt.startswith("/"):
            self.handle_command(prompt)
            return
        for item in self.pending_attachments:
            item.setdefault("queue_id", uuid.uuid4().hex)
        attachments = list(self.pending_attachments)
        if attachments:
            submitted_ids = {str(item["queue_id"]) for item in attachments}
            self.pending_attachments = [
                item
                for item in self.pending_attachments
                if str(item.get("queue_id") or "") not in submitted_ids
            ]
            self._attachments_in_flight = True
            self._inflight_attachments = attachments
        prompt = prompt or self.tr("Please review the attached files.")
        display_prompt = prompt
        if attachments:
            labels = " ".join(f"[{Icon.ATTACHMENT} {item['name']}]" for item in attachments)
            display_prompt = f"{display_prompt}\n\n{labels}"
        tab = self.tabs[self.active_tab]
        window = tab.get("history_window")
        if window and window["end"] < window["total"]:
            self.messages = []
            tab["messages"] = self.messages
            tab.pop("history_anchor", None)
        with self.lock:
            self.messages.append({"role": "user", "text": display_prompt, "local_pending": True})
            self.messages.append({"role": "assistant", "text": "", "local_pending": True})
        self._save_active_tab()
        self.busy = True
        self.busy_tab_index = self.active_tab
        self.tabs[self.active_tab]["busy"] = True
        self.tabs[self.active_tab]["cancel_requested"] = False
        self.stop_confirmation_until = 0.0
        self.stop_confirmation_tab_id = None
        self._invalidate_sidebar_rows()
        if not self.conversation_id:
            self.title = self.tabs[self.active_tab]["title"] = prompt[:60]
        self.tabs[self.active_tab]["completed_pending"] = False
        self.active_started = time.monotonic()
        self.tabs[self.active_tab]["started"] = self.active_started
        self.tabs[self.active_tab]["connection"] = None
        self.last_elapsed = None
        self.status = self.tr("Yanıt bekleniyor…")
        self.scroll_bottom()
        threading.Thread(
            target=self.run_send,
            kwargs={"send_callback": self.send_prompt, "target_tab": self.tabs[self.active_tab]},
            args=(
                prompt,
                attachments,
                self.active_tab,
                self.messages,
                self.conversation_id,
                self.model,
                self.effort,
                self.current_project.get("id"),
                self.temporary,
            ),
            daemon=True,
        ).start()

    def handle_command(self, command: str) -> None:
        if command.strip() == "/downloads":
            self.downloads_panel.open()
            return
        if command.strip() == "/help" and self.tab_kind in ("settings", "downloads"):
            self.new_tab()
        if command.strip() in ("/login", "/logout", "/session"):
            self.session_dialog.open()
            return
        name, _, argument = command.partition(" ")
        if name == "/exit":
            self.request_quit()
        elif name == "/new":
            self.new_tab()
        elif name in ("/pane-next", "/pane-prev"):
            if self.layout_controller and self.layout_enabled:
                self.layout_controller.focus_pane(1 if name == "/pane-next" else -1)
        elif name == "/tab-next":
            self.switch_tab(1)
        elif name == "/tab-prev":
            self.switch_tab(-1)
        elif name == "/tabs-close":
            self.close_all_tabs()
        elif name == "/tab-close":
            self.close_tab()
        elif name == "/search":
            self.open_sidebar_search()
        elif name == "/project-new":
            self.open_project_create()
        elif name == "/project-edit":
            self.project_panel.edit()
        elif name == "/project-source-add":
            self.project_panel.add_source()
        elif name == "/project-source-remove":
            self.project_panel.remove_source()
        elif name == "/attach":
            self.open_attachment()
        elif name == "/filter":
            self.open_filters()
        elif name == "/account-add":
            self.add_account()
        elif name in ("/split", "/split-down"):
            self.begin_split("rows" if name == "/split-down" else "columns")
        elif name == "/unsplit":
            if self.layout_controller:
                self.layout_controller.unsplit()
        elif name == "/delete":
            self.delete_record("conversation", argument.strip() or None)
        elif name == "/project-delete":
            if argument.strip():
                self.delete_record("container", argument.strip())
            else:
                self.open_project_picker("delete")
        elif name == "/projects":
            self.show_projects()
        elif name == "/actions":
            self.open_palette()
        elif name == "/settings":
            self.settings_dialog.open()
        elif name == "/model":
            model = argument.strip()
            if not model:
                self.open_palette("models")
            else:
                self.select_model(model)
        elif name == "/effort":
            effort = argument.strip().casefold()
            if effort:
                self.select_effort(None if effort == "default" else effort)
            else:
                self.open_palette("effort")
        elif name == "/project":
            if argument.strip() == "info":
                self.show_project_details()
            elif argument.strip():
                self.choose_project(argument.strip())
            else:
                self.open_project_picker()
        elif name == "/temporary":
            self.new_temporary_chat()
        elif name == "/refresh":
            self.synchronize(argument.strip() or "all")
        elif name == "/action":
            action_id, _, raw_arguments = argument.strip().partition(" ")
            if action_id:
                try:
                    arguments = json.loads(raw_arguments) if raw_arguments else {}
                    if not isinstance(arguments, dict):
                        raise ValueError("action arguments must be a JSON object")
                    self.run_action(action_id, arguments)
                except (json.JSONDecodeError, ValueError) as error:
                    self.status = self.tr("Usage: /action ID [JSON_OBJECT] · {0}").format(error)
                    self.app.invalidate()
            else:
                self.open_palette()
        elif name == "/clear":
            with self.lock:
                self.messages.clear()
            self.status = self.tr("Transcript cleared")
            self.scroll_bottom()
        elif name == "/resume":
            if argument.strip():
                self.resume_conversation(argument.strip())
            else:
                self.open_resume()
        elif name == "/history":
            if self.temporary:
                self.status = self.tr("Geçici sohbet yalnız açık oturumda tutulur")
                self.app.invalidate()
                return
            if self.conversation_id:
                self.resume_conversation(self.conversation_id)
            else:
                self.status = self.tr("No saved conversation yet")
                self.app.invalidate()
        elif name == "/rename":
            self.rename_current_conversation(argument)
        elif name == "/copy":
            self.copy_latest_response()
        elif name == "/remove":
            if self.pending_attachments and not self._attachments_in_flight:
                removed = self.pending_attachments.pop()
                self._release_staged_attachments([removed])
                self.status = self.tr("Removed {0}").format(removed["name"])
            elif self._attachments_in_flight:
                self.status = self.tr("Dosyalar gönderilirken ek listesi değiştirilemiyor")
            else:
                self.status = self.tr("No pending image to remove")
            self.app.invalidate()
        elif name == "/help":
            existing = next(
                (message for message in self.messages if message.get("local_help")), None
            )
            if existing is not None:
                with self.lock:
                    self.messages.remove(existing)
                state = self.tabs[self.active_tab].pop("help_view", None)
                if state:
                    self.follow_output, self.transcript_window.vertical_scroll = state
                self.app.invalidate()
                return
            self.tabs[self.active_tab]["help_view"] = (
                self.follow_output,
                self.transcript_window.vertical_scroll,
            )
            labels = {
                "ui.command_palette": self.tr("Komut ve eylem paletini aç"),
                "ui.search": self.tr("Yüklenmiş sohbet listesini ara"),
                "navigation.focus_sidebar": self.tr("Sol paneli göster ve odağı al"),
                "ui.toggle_sidebar": self.tr("Sol paneli göster veya gizle"),
                "chat.new": self.tr("Yeni sekme aç"),
                "chat.close_tab": self.tr("Etkin sekmeyi kapat"),
                "chat.next_tab": self.tr("Sonraki sekmeye geç"),
                "chat.previous_tab": self.tr("Önceki sekmeye geç"),
                "chat.resume": self.tr("Son sohbetleri ara ve aç"),
                "chat.send": self.tr("Mesajı gönder"),
                "chat.newline": self.tr("Mesaja yeni satır ekle"),
                "edit.copy": self.tr("Seçimi kopyala"),
                "edit.paste": self.tr("Panodan yapıştır"),
                "edit.select_all": self.tr("Metnin veya odaktaki mesajın tümünü seç"),
                "edit.cut": self.tr("Seçili metni kes"),
                "chat.temporary": self.tr("Yeni geçici sohbet"),
                "chat.interrupt": self.tr("Yanıtı durdur veya taslağı temizle"),
                "chat.stop": self.tr("Yanıtı durdur"),
                "chat.clear_transcript": self.tr("Görünen konuşmayı temizle"),
                "chat.reload": self.tr("Sohbeti eşitle"),
                "chat.copy_response": self.tr("Son yanıtı kopyala"),
                "chat.remove_attachment": self.tr("Son eklenen görseli kaldır"),
                "command.help": self.tr("Kısayol ve komut yardımını göster"),
                "command.rename": self.tr("Etkin sohbeti yeniden adlandır"),
                "command.projects": self.tr("Proje bölümünü göster"),
                "command.project": self.tr("Seçili projenin bilgilerini aç"),
                "command.refresh": self.tr("Verileri eşitle"),
                "command.settings": self.tr("Ayarları göster"),
                "app.quit": self.tr("Uygulamadan çık"),
                "app.quit_eof": self.tr("Boş girişte uygulamadan çık"),
                "navigation.sidebar_up": self.tr("Sol panelde yukarı gezin"),
                "navigation.sidebar_down": self.tr("Sol panelde aşağı gezin"),
                "navigation.sidebar_toggle_group": self.tr(
                    "Seçili proje/bölümü daralt veya genişlet"
                ),
                "navigation.close_overlay": self.tr("Açık pencereyi kapat"),
                "navigation.composer_next": self.tr("Komut tamamlamasını seç"),
                "navigation.focus_composer": self.tr("Mesaj girişine odaklan"),
                "navigation.transcript_up": self.tr("Konuşmada yukarı kaydır"),
                "navigation.transcript_down": self.tr("Konuşmada aşağı kaydır"),
                "navigation.transcript_start": self.tr("Konuşmanın başına git"),
                "navigation.transcript_end": self.tr("Konuşmanın sonuna git"),
            }
            shortcut_lines = "\n".join(
                f"- `{self.shortcut(action_id)}` — {labels.get(action_id, action_id)} (`{action_id}`)"
                for action_id, _key in sorted(self.keybindings.items())
            )
            navigation_help = self.tr(
                "- `{0}`: Sol paneli aç ve odağı al\n- `{1}` / `{2}`: proje ve sohbetlerde gezin; `Enter` sohbeti açar veya grubu değiştirir\n- `{3}`: seçili proje/bölümü daralt veya genişlet\n- `{4}`: yüklenmiş sohbetleri ara; `Enter` seçili sohbeti açar\n- `{5}`: sol paneli gizle/göster\n- `{6}`: son 50 sohbeti ara; `Enter` seçileni sekmede açar\n- `{7}` yeni sekme; `{8}` / `{9}` sekme değiştirir; `{10}` sekmeyi kapatır\n- Sekmede `{11}` yanıtın sürdüğünü, `{12}` yanıtın tamamlandığını gösterir\n- Yanıt sürerken açık sekmelere geçilebilir; uyumluluk daemon'ı seri çalıştığı için başka sohbet yükleme/gönderme bekler\n"
            ).format(
                self.shortcut("navigation.focus_sidebar"),
                self.shortcut("navigation.sidebar_up"),
                self.shortcut("navigation.sidebar_down"),
                self.shortcut("navigation.sidebar_toggle_group"),
                self.shortcut("ui.search"),
                self.shortcut("ui.toggle_sidebar"),
                self.shortcut("chat.resume"),
                self.shortcut("chat.new"),
                self.shortcut("chat.previous_tab"),
                self.shortcut("chat.next_tab"),
                self.shortcut("chat.close_tab"),
                Icon.RUNNING,
                Icon.COMPLETE,
            )
            self.append_system(
                self.text(
                    "help_body",
                    f"**Commands**\n\n- `/new` start a conversation\n- `/tab-next` or `/tab-prev` switch tabs\n- `/tab-close` close the current tab\n- `/search` search saved conversations\n- `/projects` list projects; `/project [ID]` move this chat or choose its project; `/project info` opens details\n- `/temporary` starts a temporary chat without local conversation files\n- `/model [MODEL_ID]` choose from the account's model catalog\n- `/effort [ID|default]` choose an effort offered by the selected model\n- `/refresh` refresh models, projects and chats (F10 by default)\n- `/actions` open searchable actions and commands\n- `/settings` opens categorized settings and accounts in a workspace tab\n- `/action ID` run an application action\n- `/resume` search recent conversations\n- `/resume ID` open a conversation directly\n- `/history` reload this conversation\n- `/rename TITLE` rename this conversation\n- `/delete [ID]` delete a chat; `/project-delete [ID]` delete a project and its contents (confirmation required)\n- `/copy` copy the latest assistant response\n- `/remove` remove the latest pending image\n- `/clear` clear the displayed transcript\n- `/exit` quit\n\n**Text editing**\n\nCtrl+A selects composer text; Ctrl+C copies a selection; Ctrl+V pastes. Click a message, then Ctrl+A / Ctrl+C to copy that message.\nTemporary chat uses the provider history-disabled request; provider retention rules still apply.\n\n\n**Navigation**\n\n{navigation_help}\n**Configured shortcuts**\n\n{shortcut_lines}",
                ).format(navigation=navigation_help, shortcuts=shortcut_lines)
            )
            self.messages[-1]["local_help"] = True
        else:
            self.status = self.tr("Unknown command: {0}").format(name)
            self.app.invalidate()
