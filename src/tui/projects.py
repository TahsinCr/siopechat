"""Account-scoped project forms, separate from conversation presentation."""

from accounts import origin
from providers import Capability
from providers.registry import PROVIDERS

from .file_selection import browse_form_field


class ProjectPanel:
    def __init__(self, view):
        self.view = view
        self.pending = False

    def accounts(self):
        service = self.view.account_service
        if not service:
            return []
        items = (
            service.active_accounts() if hasattr(service, "active_accounts") else service.items()
        )
        return [i for i in items if Capability.PROJECT_CREATE in service.capabilities(i["key"])]

    def can_create(self):
        return bool(self.accounts())

    def work(self, title, read, success):
        v = self.view
        if self.pending:
            v.status = self.view.tr("Proje işlemi sürüyor")
            v.app.invalidate()
            return
        self.pending = True
        v.status = title
        v.app.invalidate()

        def completed(result):
            self.pending = False
            success(result)
            v.app.invalidate()

        def failed(reason):
            self.pending = False
            v.status = self.view.tr("Proje işlemi: ") + str(reason)
            v.app.invalidate()

        v._read_in_background(read, completed, failed, session_bound=False)

    def label(self, key):
        account = next(i for i in self.view.account_service.items() if i["key"] == key)
        provider = PROVIDERS[account["provider"]]
        return provider[1] + " " + provider[0] + " · " + account["label"]

    def create(self):
        accounts = self.accounts()
        if not accounts:
            self.view.status = self.view.tr("Etkin hesaplarda proje oluşturma desteklenmiyor")
            return
        choices = [(i["key"], self.label(i["key"])) for i in accounts]
        selected = next((key for key, _ in choices if key == self.view.account_key), choices[0][0])
        self.view.forms.open(
            self.view.tr("Yeni proje · sağlayıcı seçin"),
            [("account", self.view.tr("Sağlayıcı / hesap"), selected, choices)],
            lambda values: lambda: self.create_form(values["account"]),
        )

    def create_form(self, key):
        def show(options):
            fields = [("name", self.view.tr("Proje adı"), "", None)]
            for name, title in (
                ("memory", self.view.tr("Bellek")),
                ("visibility", self.view.tr("Gizlilik")),
            ):
                choices = getattr(options, name)
                if len(choices) > 1:
                    fields.append((name, title, choices[0].id, [(c.id, c.title) for c in choices]))

            def submit(values):
                if not values["name"].strip():
                    raise ValueError(self.view.tr("Proje adı gerekir"))
                return lambda: self.work(
                    self.view.tr("Proje oluşturuluyor…"),
                    lambda: self.view.account_service.create_project(
                        key,
                        values["name"].strip(),
                        values.get("visibility", "private"),
                        values.get("memory", "default"),
                    ),
                    self.updated,
                )

            self.view.forms.open(self.view.tr("Yeni proje · ") + self.label(key), fields, submit)

        self.work(
            self.view.tr("Proje seçenekleri okunuyor…"),
            lambda: self.view.account_service.project_options(key),
            show,
        )

    def selected(self):
        v = self.view
        if (
            v.app.layout.current_control in (v.sidebar_control, v.sidebar_search.control)
            and v.sidebar_rows
        ):
            row = v.sidebar_rows[v.sidebar_index]
            value = row.get("id") if row["kind"] == "project" else row.get("project_id")
            if value:
                return value
        return v.current_project.get("id")

    def context(self, callback, value=None):
        v = self.view
        value = value or self.selected()
        if not v.account_service or not value:
            v.status = self.view.tr("Önce bir proje seçin")
            v.app.invalidate()
            return
        key, _ = origin(value, v.account_key)

        def read():
            service = v.account_service
            return service.project_details(key, value), service.project_options(key, value)

        self.work(
            self.view.tr("Proje seçenekleri okunuyor…"),
            read,
            lambda result: callback(key, value, *result),
        )

    def edit(self, value=None):
        if value is None:
            self.view.open_project_picker(purpose="edit")
            return
        self.view.show_project_details(value)

        def show(key, value, project, options):
            if self.view.tab_kind != "project" or self.view.current_project.get("id") != value:
                self.view.status = self.view.tr(
                    "Proje bilgileri hazır · düzenlemek için proje sekmesini açın"
                )
                return
            fields = [
                (name, title, project.get(name) or "", None)
                for name, title in (
                    ("name", self.view.tr("Proje adı")),
                    ("instructions", self.view.tr("Talimatlar")),
                )
                if name in options.fields
            ]
            if not fields:
                self.view.status = self.view.tr("Bu projenin bilgileri bu hesapta düzenlenemiyor")
                return

            def submit(values):
                if "name" in values and not values["name"].strip():
                    raise ValueError(self.view.tr("Proje adı gerekir"))
                return lambda: self.work(
                    self.view.tr("Proje kaydediliyor…"),
                    lambda: self.view.account_service.change_project(key, value, **values),
                    self.updated,
                )

            self.view.forms.open(
                self.view.tr("Projeyi düzenle · ") + self.label(key),
                fields,
                submit,
                inline_project=value,
            )

        self.context(show, value)

    def add_source(self):
        def show(key, value, project, options):
            if not options.sources:
                self.view.status = options.source_error or self.view.tr(
                    "Bu projede eklenebilir kaynak türü bulunamadı"
                )
                return
            choices = [(s["id"], s["title"]) for s in options.sources]

            def chosen(values):
                source = next(s for s in options.sources if s["id"] == values["kind"])
                return lambda: source_form(source)

            def source_form(source):
                kind = source["id"]
                fields = (
                    [
                        (
                            "path",
                            self.view.tr("Dosya yolu · ")
                            + (source.get("accept") or self.view.tr("Türü sağlayıcı doğrular")),
                            "",
                            None,
                        )
                    ]
                    if kind == "file"
                    else (
                        [
                            ("name", self.view.tr("Kaynak adı"), "", None),
                            ("text", self.view.tr("Metin"), "", None),
                        ]
                        if kind == "text"
                        else [("url", self.view.tr("Kaynak bağlantısı"), "", None)]
                    )
                )

                def submit(values):
                    if any(not str(v).strip() for v in values.values()):
                        raise ValueError(self.view.tr("Kaynak alanları boş olamaz"))
                    return lambda: self.work(
                        self.view.tr("Kaynak ekleniyor…"),
                        lambda: self.view.account_service.change_project(
                            key, value, "add_source", kind=kind, **values
                        ),
                        self.updated,
                    )

                actions = (
                    [
                        (
                            self.view.tr("Dosya yöneticisi"),
                            lambda: browse_form_field(
                                self.view,
                                "path",
                                source.get("accept"),
                                multiple=False,
                            ),
                        )
                    ]
                    if kind == "file"
                    else []
                )
                self.view.forms.open(
                    self.view.tr("Kaynak ekle · ") + self.label(key),
                    fields,
                    submit,
                    actions=actions,
                )

            self.view.forms.open(
                self.view.tr("Kaynak türü · ") + self.label(key),
                [("kind", self.view.tr("Desteklenen kaynaklar"), choices[0][0], choices)],
                chosen,
            )

        self.context(show)

    def remove_source(self):
        def show(key, value, project, options):
            resources = project.get("resources", [])
            if not options.remove_sources or not resources:
                self.view.status = self.view.tr("Silinebilecek proje kaynağı yok")
                return
            choices = [(r["id"], r.get("name") or r["id"]) for r in resources]

            def submit(values):
                if values["confirm"] != "remove":
                    return
                return lambda: self.work(
                    self.view.tr("Kaynak kaldırılıyor…"),
                    lambda: self.view.account_service.change_project(
                        key, value, "remove_source", resource_id=values["resource"]
                    ),
                    self.updated,
                )

            self.view.forms.open(
                self.view.tr("Kaynak kaldır · ") + self.label(key),
                [
                    ("resource", self.view.tr("Proje kaynağı"), choices[0][0], choices),
                    (
                        "confirm",
                        self.view.tr("Kaynak sağlayıcıdaki projeden de kaldırılır."),
                        "cancel",
                        [
                            ("cancel", self.view.tr("İptal")),
                            ("remove", self.view.tr("Kaynağı kaldır")),
                        ],
                    ),
                ],
                submit,
            )

        self.context(show)

    def updated(self, project):
        v = self.view
        key, _ = origin(project["id"], v.account_key)
        v._account_projects.setdefault(key, {})[project["id"]] = project
        v.project_items[project["id"]] = project
        for tab in v.tabs:
            if tab.get("project", {}).get("id") == project["id"]:
                tab["project"] = dict(project)
                if tab.get("kind") == "project":
                    tab.update(title=project["name"], messages=v.project_messages(project))
        if v.current_project.get("id") == project["id"]:
            v.current_project = dict(project)
            if v.tab_kind == "project":
                v.title, v.messages = project["name"], v.project_messages(project)
        v.filter_sidebar()
        v.status = (
            project.get("cache_warning") or self.view.tr("Proje güncellendi · ") + project["name"]
        )
