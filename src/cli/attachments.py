"""Provider-aware file selection, preflight, and staged upload UI."""

import mimetypes
import os
import uuid
from pathlib import Path

from providers.attachments import fit_attachments

from .file_selection import browse_form_field


class AttachmentPicker:
    def __init__(self, view):
        self.view = view

    def open(self):
        view = self.view
        if getattr(view, "attachment_preflight", False):
            view.status = view.tr("Dosyalar kontrol ediliyor…")
            view.app.invalidate()
            return
        if view.attach_file_callback is None:
            if view.pending_attachments:
                view.forms.open(
                    view.tr("Ekli dosyalar"),
                    [
                        (
                            "files",
                            view.tr("Bu sağlayıcı ek gönderimini desteklemiyor"),
                            "\n".join(item.get("name", "") for item in view.pending_attachments),
                            None,
                        )
                    ],
                    lambda _values: None,
                    actions=[
                        (
                            view.tr("Tüm ekleri temizle"),
                            lambda: self._clear_pending(None),
                        )
                    ],
                )
                return
            view.status = view.tr("Bu sağlayıcıda dosya ekleme kullanılamıyor")
            view.app.invalidate()
            return
        if view.uploading_image or view.loading_conversation or view.busy:
            view.status = view.tr("Devam eden işlem tamamlanınca dosya ekleyin")
            view.app.invalidate()
            return
        try:
            policy = (view.attachment_policy_callback or (lambda: None))() or {}
        except Exception as error:
            view.status = view.tr("Dosya kuralları alınamadı: ") + str(error)
            view.app.invalidate()
            return
        self._open_path_form("", policy)

    def _open_path_form(self, value, policy):
        view = self.view
        extensions = tuple(policy.get("allowed_extensions") or ())
        pending = list(view.pending_attachments)
        if not value:
            value = "\n".join(item.get("path", "") for item in pending if item.get("path"))

        def browse():
            self._choose_files_into(extensions)

        actions = [(view.tr("Dosya yöneticisi"), browse)]
        if pending:
            actions.append((view.tr("Tüm ekleri temizle"), lambda: self._clear_pending(policy)))

        view.forms.open(
            view.tr("Dosya ekle"),
            [("files", view.tr("Dosya yolları (bekleyen ekler korunur)"), value, None)],
            lambda values: lambda: self._review(values["files"], policy),
            actions=actions,
        )

    def _clear_pending(self, policy):
        if self.view._attachments_in_flight or self.view.uploading_image:
            self.view.status = self.view.tr("Dosyalar gönderilirken ek listesi değiştirilemiyor")
            self.view.app.invalidate()
            return
        pending = list(self.view.pending_attachments)
        self.view._release_staged_attachments(pending)
        self.view.pending_attachments.clear()
        self.view.status = self.view.tr("Ekler temizlendi")
        self.view.app.invalidate()
        if policy is None:
            self.view.forms.close()
        else:
            self._open_path_form("", policy)

    def _prepare(self, value, policy, *, pending=None):
        view = self.view
        max_files = max(1, int(policy.get("max_files") or 1))
        max_bytes = max(1, int(policy.get("max_bytes") or 20 * 1024 * 1024))
        max_total = policy.get("max_total_bytes")
        extensions = {str(x).lower() for x in policy.get("allowed_extensions") or ()}
        pending = [dict(item) for item in view.pending_attachments] if pending is None else pending
        eligible_pending, _ = fit_attachments(pending, policy)
        remaining = max_files - len(eligible_pending)
        accepted, rejected, seen = [], [], set()
        for item in pending:
            path = item.get("path")
            if path:
                seen.add(self._path_identity(path))
        for raw in value.splitlines():
            raw = raw.strip().strip('"')
            if not raw:
                continue
            try:
                path = Path(raw).expanduser().resolve(strict=True)
                stat = path.stat()
            except (OSError, RuntimeError):
                rejected.append((raw, view.tr("dosya bulunamadı")))
                continue
            identity = self._path_identity(path)
            if identity in seen:
                continue
            seen.add(identity)
            if not path.is_file() or stat.st_size <= 0:
                rejected.append((path.name, view.tr("boş veya normal dosya değil")))
                continue
            if extensions and path.suffix.lower() not in extensions:
                rejected.append((path.name, view.tr("sağlayıcı biçimi desteklemiyor")))
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if mime.startswith("video/"):
                limit = policy.get("max_video_bytes")
            elif mime.startswith("image/"):
                limit = policy.get("max_image_bytes")
            elif path.suffix.lower() in {".csv", ".tsv", ".xls", ".xlsx"}:
                limit = policy.get("max_spreadsheet_bytes")
            else:
                limit = None
            if stat.st_size > (int(limit) if limit else max_bytes):
                rejected.append((path.name, view.tr("dosya boyutu sınırı aşıyor")))
                continue
            accepted.append({"path": str(path), "name": path.name, "size_bytes": stat.st_size})

        if remaining <= 0:
            rejected.extend(
                (item["name"], view.tr("dosya sayısı sınırı dolu")) for item in accepted
            )
            accepted = []
        elif len(accepted) > remaining:
            rejected.extend(
                (item["name"], view.tr("dosya sayısı sınırı · sondaki dosya çıkarıldı"))
                for item in accepted[remaining:]
            )
            accepted = accepted[:remaining]
        pending_bytes = sum(int(item.get("size_bytes") or 0) for item in eligible_pending)
        if max_total:
            while accepted and pending_bytes + sum(item["size_bytes"] for item in accepted) > int(
                max_total
            ):
                removed = accepted.pop()
                rejected.append(
                    (removed["name"], view.tr("toplam boyut sınırı · sondaki dosya çıkarıldı"))
                )
        return accepted, rejected

    @staticmethod
    def _path_identity(path):
        """Compare queued and selected files across symlinks and OS path rules."""
        return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))

    def _review(self, value, policy):
        self._prepare_in_background(
            value,
            policy,
            lambda result: self._show_review(result[0], result[1], policy, value),
        )

    def _prepare_in_background(self, value, policy, ready):
        view = self.view
        pending = [dict(item) for item in view.pending_attachments]
        view.attachment_preflight = True
        view.background_jobs += 1
        view.status = view.tr("Dosyalar kontrol ediliyor…")
        view.app.invalidate()
        finished = False

        def release():
            nonlocal finished
            if finished:
                return
            finished = True
            view.attachment_preflight = False
            view.background_jobs = max(0, view.background_jobs - 1)

        def success(result):
            release()
            ready(result)

        def failure(reason):
            release()
            view.status = str(reason)
            view.app.invalidate()

        view._read_in_background(
            lambda: self._prepare(value, policy, pending=pending),
            success,
            failure,
            session_bound=False,
        )

    def _show_review(self, accepted, rejected, policy, value):
        view = self.view
        if not accepted:
            if view.pending_attachments and not rejected:
                view.status = self.tr_count(len(view.pending_attachments))
                view.forms.close()
            else:
                self._open_path_form(value, policy)
                view.forms.error = "\n".join(
                    f"{name}: {reason}" for name, reason in rejected[:8]
                ) or view.tr("Uygun dosya kalmadı · yolları düzenleyin")
            view.app.invalidate()
            return

        total = sum(item["size_bytes"] for item in accepted)
        lines = [item["path"] for item in accepted]
        explanation = (
            f"{len(accepted)} yeni dosya · {self._size(total)} · "
            f"sağlayıcı sınırı {policy.get('max_files', '?')} dosya / "
            f"{self._size(int(policy.get('max_bytes') or 0))} dosya başına"
        )
        if view.pending_attachments:
            explanation += "\n" + view.tr(
                "Bekleyen ekler gönderilene veya temizlenene kadar korunur"
            )
        if policy.get("max_video_bytes"):
            explanation += f" · video {self._size(int(policy['max_video_bytes']))}"
        if policy.get("max_image_bytes"):
            explanation += (
                f" · {view.tr('Görsel sınırı')} {self._size(int(policy['max_image_bytes']))}"
            )
        if policy.get("max_spreadsheet_bytes"):
            explanation += (
                f" · {view.tr('Tablo sınırı')} {self._size(int(policy['max_spreadsheet_bytes']))}"
            )
        if policy.get("max_files_scope") == "conversation":
            explanation += " · " + view.tr("Mevcut sohbet dosyaları gönderimden önce sayılır")
        if rejected:
            explanation += "\nListeden çıkarılanlar: " + "; ".join(
                f"{name} ({reason})" for name, reason in rejected[:8]
            )
            if len(rejected) > 8:
                explanation += f" · +{len(rejected) - 8}"
        if policy.get("source"):
            explanation += "\nLimit kaynağı: " + str(policy["source"])
        # A multiline review lets the user remove or correct a path before upload.
        view.forms.open(
            view.tr("Dosyaları gözden geçir"),
            [("files", explanation, "\n".join(lines), None)],
            lambda values: self._upload(values["files"], policy),
            actions=[
                (view.tr("Dosya ekle"), lambda: self._browse_into_form(policy)),
                *(
                    [(view.tr("Tüm ekleri temizle"), lambda: self._clear_pending(policy))]
                    if view.pending_attachments
                    else []
                ),
            ],
        )

    def _browse_into_form(self, policy):
        self._choose_files_into(tuple(policy.get("allowed_extensions") or ()))

    def _choose_files_into(self, extensions):
        browse_form_field(self.view, "files", extensions, multiple=True, append=True)

    def tr_count(self, count):
        return self.view.tr("{0} dosya hazır").format(count)

    def _upload(self, value, policy):
        self._prepare_in_background(
            value,
            policy,
            lambda result: self._upload_selected(*result, policy=policy),
        )

    def _upload_selected(self, selected, rejected, *, policy):
        view = self.view
        if not selected:
            self._open_path_form("", policy)
            view.forms.error = "\n".join(
                f"{name}: {reason}" for name, reason in rejected[:8]
            ) or view.tr("Dosya seçilmedi")
            view.app.invalidate()
            return
        callback = view.attach_file_callback
        target = view.tabs[view.active_tab]
        target_tab_id = target.get("tab_id")
        source_account_key = view.account_key
        view.uploading_image = True
        view.background_jobs += 1
        view.status = view.tr("Dosya yükleniyor") + "…"

        def work():
            uploaded, errors = [], []
            for index, item in enumerate(selected, 1):
                view._dispatch_ui(
                    lambda index=index, name=item["name"]: (
                        setattr(
                            view,
                            "status",
                            f"{view.tr('Dosya yükleniyor')} {index}/{len(selected)} · {name}",
                        ),
                        view.app.invalidate(),
                    )
                )
                try:
                    result = callback(item["path"])
                    if not isinstance(result, dict):
                        raise TypeError("Sağlayıcı dosya kaydı döndürmedi")
                    result = dict(result)
                    result.setdefault("size_bytes", item["size_bytes"])
                    result.setdefault("name", item["name"])
                    result.setdefault("path", item["path"])
                    result["queue_id"] = uuid.uuid4().hex
                    result["source_account_key"] = source_account_key
                    result["source_tab_id"] = target_tab_id
                    uploaded.append(result)
                except Exception as error:
                    errors.append(f"{item['name']}: {error}")
            return uploaded, errors

        def success(result):
            uploaded, errors = result
            view.uploading_image = False
            view.background_jobs = max(0, view.background_jobs - 1)
            view.pending_attachments.extend(uploaded)
            status = [view.tr("{0} dosya eklendi").format(len(uploaded))] if uploaded else []
            if errors:
                status.append(view.tr("{0} dosya yüklenemedi").format(len(errors)))
            if rejected:
                status.append(view.tr("{0} dosya sınır nedeniyle eklenmedi").format(len(rejected)))
            view.status = " · ".join(status) or view.tr("Dosya yüklenemedi")
            view.app.invalidate()

        def failure(reason):
            view.uploading_image = False
            view.background_jobs = max(0, view.background_jobs - 1)
            view.status = str(reason)
            view.app.invalidate()

        view._read_in_background(work, success, failure, session_bound=False)

    @staticmethod
    def _size(value):
        if value <= 0:
            return "bilinmiyor"
        if value >= 1024 * 1024:
            return f"{value / (1024 * 1024):.0f} MiB"
        return f"{value / 1024:.0f} KiB"
