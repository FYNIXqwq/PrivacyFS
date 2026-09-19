"""Interactive TUI browser (textual). `privacyfs tui PATH`.

Left column = tree (indented), right column = size (right-aligned) -- a
DataTable gives true column alignment at any terminal width/zoom, with
scrolling instead of line wrapping. The scan runs in a worker thread and
reports progress through a shared holder; only the sanitized listing ever
enters the UI.
"""

from __future__ import annotations

from .listing import to_tree_lines


def create_app(holder: dict):
    """holder: {'result': ScanResult|None, 'msg': str}. Returns a textual App."""
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Footer, Label

    class _App(App):
        CSS = "DataTable { height: 1fr; }"
        BINDINGS = [("q", "quit", "退出"), ("ctrl+q", "quit", "退出")]

        def action_quit(self) -> None:
            # quit must stop the background scan: set the cancel event AND
            # kill any detection workers, otherwise executor/atexit joins
            # keep the process alive doing IO
            from ..core import kill_active_pool

            cancel = holder.get("cancel")
            if cancel is not None:
                cancel.set()
            kill_active_pool()
            self.exit()

        def on_unmount(self) -> None:
            from ..core import kill_active_pool

            cancel = holder.get("cancel")
            if cancel is not None:
                cancel.set()
            kill_active_pool()

        def compose(self) -> ComposeResult:
            yield Label("privacyfs 扫描中…", id="status")
            yield DataTable(zebra_stripes=True, cursor_type="row")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one(DataTable)
            table.add_column("名称")
            table.add_column("大小")
            self.set_interval(0.15, self._poll)

        def _poll(self) -> None:
            msg = holder.get("msg")
            if msg:
                self.query_one("#status", Label).update(msg)
            result = holder.get("result")
            if result is not None and self.query_one(DataTable).row_count == 0:
                table = self.query_one(DataTable)
                for text, size in to_tree_lines(result, show_size=True):
                    table.add_row(text, size.rjust(10))
                self.query_one("#status", Label).update(
                    f"{result.files} 个文件, {result.dirs} 个目录, "
                    f"{result.hidden_dirs} 个隐藏子树 — q 退出, ↑↓ 浏览"
                )

    return _App()


def run_tui(holder: dict) -> None:
    """Blocks until the user quits."""
    create_app(holder).run()
