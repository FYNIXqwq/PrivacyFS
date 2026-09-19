"""Workflow state: scanning, review, resumable snapshots, and scoped export."""
import uuid

from ..gui.backend import ScanController
from .review_store import ReviewStore
from .files import FileJob, snapshot, save_session, load_session, export_report


class WorkbenchController:
    def __init__(self):
        self.scan = ScanController(store_factory=ReviewStore)
        self.job = None
        self.session_id = str(uuid.uuid4())
        self.dirty, self.reopened = False, False
        self.last_error, self.message = "", ""

    @property
    def rows(self):
        return self.scan.rows

    @property
    def report(self):
        return self.scan.report

    @property
    def busy(self):
        return self.scan.running or self.job is not None

    def idle(self):
        if self.busy:
            raise ValueError("operation already running")

    def start(self, options, discard=False):
        self.idle()
        if self.dirty and not discard:
            raise ValueError("unsaved review")
        self.scan.start(options)
        self.session_id = str(uuid.uuid4())
        self.dirty, self.reopened, self.last_error = True, False, ""

    def mark(self, identifiers, state, note=""):
        self.idle()
        self.rows.decide(identifiers, state, note)
        self.dirty = True

    def undo(self):
        self.idle()
        if self.rows.undo():
            self.dirty = True

    def save(self, path):
        self.idle()
        if not self.report:
            raise ValueError("no scan to save")
        self.last_error = ""
        self.job = FileJob("save", save_session(path, snapshot(self), self.rows.iter_reviewed()))

    def open(self, path, discard=False):
        self.idle()
        if self.dirty and not discard:
            raise ValueError("unsaved review")
        self.last_error = ""
        candidate, metadata = ReviewStore(), {}
        self.job = FileJob("open", load_session(path, candidate, metadata), candidate, metadata)

    def export(self, path, format, scope):
        self.idle()
        if not self.report or scope not in {"all", "confirmed"}:
            raise ValueError("invalid export")
        self.last_error = ""
        self.job = FileJob("export", export_report(path, snapshot(self, scope), self.rows.iter_reviewed(scope), format))

    def poll(self):
        changed = self.scan.poll()
        if changed:
            self.dirty = True
        if self.job is not None:
            job = self.job
            job.step()
            changed = True
            self.message = f"文件操作中：已处理 {job.processed:,} 条"
            if job.done:
                self.last_error = job.error
                if job.error:
                    self.message = ("目标文件已存在，请使用新文件名；已有文件未改动。" if job.error == "FileExistsError"
                        else "文件操作失败；请检查文件格式、账户权限及磁盘空间。当前会话与已有文件保留。")
                elif job.kind == "open":
                    if self.scan.inventory is not None:
                        self.scan.inventory.close()
                        self.scan.inventory = None
                    self.scan.rows.close()
                    self.scan.rows, job.candidate = job.candidate, None
                    self.scan.report = job.metadata
                    self.session_id = job.metadata["session_id"]
                    self.dirty, self.reopened = False, True
                    self.message = "已打开历史会话；这些是保存时的结果，并未重新扫描源目录。"
                elif job.kind == "save":
                    self.dirty = False
                    self.message = "会话已保存，可在当前账户重新打开继续复核。"
                else:
                    self.message = "报告已导出。报告含真实路径；继续复核请另存会话。"
                self.job = None
        return changed

    def cancel_file_job(self):
        if self.job is not None:
            self.job.cancel()
            self.last_error = self.job.error
            self.job = None
            self.message = ("文件操作已取消，目标文件未发布。" if not self.last_error
                            else "文件操作已停止；临时文件清理未确认完成，目标文件未发布。")

    def close(self):
        self.cancel_file_job()
        self.scan.close()
