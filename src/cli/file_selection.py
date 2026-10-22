"""Shared desktop file selection for chat attachments and project sources."""

from utils.file_dialog import accepted_extensions, choose_files


def browse_form_field(view, field_name, accept=(), *, multiple=True, append=False):
    """Choose paths in a worker and place them into the current form field."""
    editor = view.forms.fields.get(field_name)
    if editor is None:
        return
    extensions = accepted_extensions(accept)

    def selected(paths):
        if view.forms.fields.get(field_name) is not editor:
            return
        if paths is None:
            view.forms.error = view.tr("Dosya seçici açılamadı · dosya yolunu yazabilirsiniz")
        elif paths:
            chosen = paths if multiple else paths[:1]
            current = editor.text.rstrip()
            editor.text = (
                current + ("\n" if current and append else "") + "\n".join(chosen)
                if append
                else "\n".join(chosen)
            )
            view.forms.error = ""
        view.app.invalidate()

    def failed(reason):
        view.forms.error = str(reason)
        view.app.invalidate()

    view._read_in_background(
        lambda: choose_files(view.tr("Dosya seç"), extensions, multiple=multiple),
        selected,
        failed,
        session_bound=False,
    )
