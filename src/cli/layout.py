"""Terminal composition only; controls and actions belong to their components."""

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import Float, FloatContainer, HSplit, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import Frame

from .widgets import TranscriptGutter


def workspace_layout(view):
    def text_window(text, height=1, style=""):
        return Window(
            content=FormattedTextControl(text), height=height, style=style, wrap_lines=False
        )

    view.transcript_window.right_margins = [ScrollbarMargin(display_arrows=True)]
    view.transcript_window.left_margins = [TranscriptGutter(view)]
    view.transcript_window.style = "class:transcript"
    view.transcript_window.char = " "

    sidebar = ConditionalContainer(
        HSplit(
            [
                text_window(
                    lambda: view.text("library", "Sohbetler ve projeler"), 1, "class:sidebar.header"
                ),
                view.sidebar_search,
                view.sidebar_window,
            ],
            width=view.sidebar_width,
            style="class:sidebar",
        ),
        filter=Condition(lambda: view.sidebar_visible and view.tab_kind != "settings"),
    )
    conversation = HSplit(
        [
            view.transcript_window,
            Window(height=1, char="─", style="class:separator"),
            view.input,
            text_window(view.render_composer_actions, 1, "class:composer"),
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
                        conversation,
                    ]
                ),
                filter=Condition(lambda: view.tab_kind != "settings"),
            ),
            ConditionalContainer(
                HSplit(
                    [view.settings_dialog.container, Window(style="class:settings", char=" ")],
                    style="class:settings",
                ),
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
                filter=Condition(lambda: view.tab_kind != "settings"),
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
                    lambda: view.text("list_help", "Tıkla veya ↑/↓ ile seç · Enter aç · Esc kapat")
                ),
            ]
        ),
        title=lambda: view.text("resume", "Sohbet aç"),
    )
    palette = Frame(
        HSplit(
            [
                view.palette_search,
                view.palette_window,
                text_window(
                    lambda: view.text("list_help", "Tıkla veya ↑/↓ ile seç · Enter aç · Esc kapat")
                ),
            ]
        ),
        title=lambda: view.text(view.palette_mode, "Komutlar · modeller · efor"),
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
