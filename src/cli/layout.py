"""Terminal composition only; controls and actions belong to their components."""

from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.layout import DynamicContainer, Float, FloatContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame

from .widgets import TranscriptGutter, fit_text, on_click


def workspace_layout(view):
    def text_window(text, height=1, style="", width=None):
        return Window(
            content=FormattedTextControl(text),
            height=height,
            style=style,
            wrap_lines=False,
            width=width,
        )

    view.transcript_window.right_margins = [ScrollbarMargin(display_arrows=True)]
    view.transcript_window.left_margins = [TranscriptGutter(view)]
    view.transcript_window.style = "class:transcript"
    view.transcript_window.char = " "

    sidebar = ConditionalContainer(
        HSplit(
            [
                VSplit(
                    [
                        text_window(
                            lambda: [
                                (
                                    "class:button",
                                    " ↻ ",
                                    on_click(lambda: view.synchronize("library")),
                                )
                            ],
                            width=3,
                        ),
                        text_window(
                            lambda: view.text("library", view.tr("Sohbetler ve projeler")),
                            1,
                            "class:sidebar.header",
                        ),
                        ConditionalContainer(
                            text_window(
                                lambda: [
                                    ("class:button", " + ", on_click(view.open_project_create))
                                ],
                                width=3,
                            ),
                            filter=Condition(view.project_panel.can_create),
                        ),
                    ]
                ),
                VSplit(
                    [
                        view.sidebar_search,
                        ConditionalContainer(
                            text_window(
                                lambda: [
                                    (
                                        "class:button",
                                        " ☷ ",
                                        on_click(view.open_filters),
                                    )
                                ],
                                width=3,
                            ),
                            filter=Condition(lambda: view.account_service is not None),
                        ),
                    ]
                ),
                view.sidebar_window,
            ],
            width=view.sidebar_width,
            style="class:sidebar",
        ),
        filter=Condition(lambda: view.sidebar_visible and view.tab_kind != "settings"),
    )
    conversation = HSplit(
        [
            VSplit(
                [
                    text_window(
                        lambda: [
                            (
                                "class:button",
                                view.tr(" ↻ Eşitle"),
                                on_click(lambda: view.synchronize("conversation")),
                            )
                        ],
                        width=10,
                    ),
                    Window(height=1),
                ],
                height=1,
            ),
            DynamicContainer(
                lambda: (
                    view.forms.container
                    if (
                        view.tab_kind == "project"
                        and view.forms.visible
                        and view.forms.inline_project == view.current_project.get("id")
                    )
                    else view.transcript_window
                )
            ),
            ConditionalContainer(
                HSplit(
                    [
                        Window(height=1, char="─", style="class:separator"),
                        view.input,
                        text_window(view.render_composer_actions, 1, "class:composer"),
                    ]
                ),
                filter=Condition(lambda: not view.editing_project()),
            ),
        ],
        style="class:transcript",
    )
    content = HSplit(
        [
            HSplit(
                [
                    text_window(view.render_tabs, 1, "class:tabs"),
                    Window(height=1, char="─", style="class:tabs.border"),
                ]
            ),
            ConditionalContainer(
                VSplit(
                    [
                        sidebar,
                        ConditionalContainer(
                            Window(width=1, char="│", style="class:separator"),
                            filter=view.sidebar_visible_filter,
                        ),
                        DynamicContainer(
                            lambda: (
                                view.layout_controller.container(conversation)
                                if view.layout_controller
                                else conversation
                            )
                        ),
                    ]
                ),
                filter=Condition(lambda: view.tab_kind != "settings"),
            ),
            ConditionalContainer(
                view.settings_dialog.container,
                filter=Condition(lambda: view.tab_kind == "settings"),
            ),
            ConditionalContainer(
                Window(
                    content=FormattedTextControl(view.render_chat_header),
                    height=lambda: (
                        1 + sum(fragment[1].count("\n") for fragment in view.render_chat_header())
                    ),
                    style="class:chat.header",
                    wrap_lines=False,
                ),
                filter=Condition(
                    lambda: view.tab_kind != "settings" and not view.editing_project()
                ),
            ),
            text_window(view.render_footer, 1, "class:help"),
        ]
    )
    resume = Frame(
        HSplit(
            [
                view.resume_search,
                view.resume_list_window,
                text_window(
                    lambda: view.text(
                        "list_help", view.tr("Tıkla veya ↑/↓ ile seç · Enter aç · Esc kapat")
                    )
                ),
            ]
        ),
        title=lambda: view.text("resume", view.tr("Sohbet aç")),
    )
    palette = Frame(
        HSplit(
            [
                view.palette_search,
                view.palette_window,
                text_window(
                    lambda: view.text(
                        "list_help", view.tr("Tıkla veya ↑/↓ ile seç · Enter aç · Esc kapat")
                    )
                ),
            ]
        ),
        title=lambda: view.text(view.palette_mode, view.tr("Komutlar · modeller · efor")),
    )
    return FloatContainer(
        content=content,
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                attach_to_window=view.input.window,
                allow_cover_cursor=True,
                content=CompletionsMenu(max_height=8, scroll_offset=1),
            ),
            Float(
                content=ConditionalContainer(
                    DynamicContainer(lambda: view.forms.container),
                    filter=Condition(lambda: view.forms.visible and not view.forms.inline_project),
                ),
                width=lambda: min(64, max(20, view.terminal_width() - 8)),
                top=3,
            ),
            Float(
                content=ConditionalContainer(resume, filter=view.resume_filter),
                left=3,
                right=3,
                top=3,
                bottom=3,
            ),
            Float(
                content=ConditionalContainer(palette, filter=view.palette_filter),
                left=3,
                right=3,
                top=3,
                bottom=3,
            ),
        ],
    )


def render_tab_bar(view):
    width = view.terminal_width()
    prefix = fit_text(" ☰ SiopeChat ", max(0, width - 12))
    fragments = [("class:brand", prefix, on_click(view.toggle_sidebar))]
    available = max(0, width - get_cwidth(prefix) - 12)
    entries = list(range(len(view.tabs)))
    group_titles = {}
    if view.layout_controller and view.layout_enabled:
        view.layout_controller.active_group()
        for group in view.layout_controller.groups:
            members = [i for i, tab in enumerate(view.tabs) if tab.get("tab_id") in group["tabs"]]
            if len(members) < 2:
                continue
            representative = view.active_tab if view.active_tab in members else members[0]
            group_titles[representative] = " ◫ ".join(
                view.title if i == view.active_tab else view.tabs[i].get("title", view.tr("Sohbet"))
                for i in members
            )
            entries = [i for i in entries if i not in members or i == representative]
    count = min(len(entries), max(1, available // 12))
    indices = sorted(sorted(entries, key=lambda i: (abs(i - view.active_tab), i))[:count])
    base, remainder = divmod(available, max(1, len(indices)))
    for position, i in enumerate(indices):
        tab_width = min(
            getattr(view, "theme_options", {}).get("tab_width", 26),
            base + (position < remainder),
        )
        tab = view.tabs[i]
        title = group_titles.get(i) or (
            view.title if i == view.active_tab else tab.get("title", view.tr("Sohbet"))
        )
        marker = (
            "● "
            if tab.get("busy")
            else "✓ "
            if tab.get("completed_pending")
            else "↻ "
            if tab.get("loading") or tab.get("syncing")
            else "◌ "
            if tab.get("temporary")
            else ""
        )
        label = fit_text(marker + title, max(0, tab_width - 4))
        padding = max(0, tab_width - 4 - get_cwidth(label))
        label = " " * (padding // 2) + label + " " * (padding - padding // 2)
        style = (
            "class:tab.active"
            if i == view.active_tab
            else "class:tab.running"
            if tab.get("busy")
            else "class:tab"
        )
        fragments.append(("class:tab.divider", "│"))
        fragments.append(
            ("class:tab.open " + style, label, on_click(lambda i=i: view.select_tab(i)))
        )
        fragments.append(
            ("class:tab.close " + style, " × ", on_click(lambda i=i: view.close_clicked_tab(i)))
        )
    used = sum(get_cwidth(fragment[1]) for fragment in fragments)
    fragments.append(("class:tabs", " " * max(0, width - used - 12)))
    fragments.extend(
        [
            ("class:button", " ‹ ", on_click(lambda: view.switch_tab(-1))),
            ("class:button", " › ", on_click(lambda: view.switch_tab(1))),
            ("class:button", " + ", on_click(view.new_tab)),
            ("class:button", " ⊗ ", on_click(view.close_all_tabs)),
        ]
    )
    return FormattedText(fragments)
