"""UI-side page cache. The worker owns the index and performs all disk queries."""
from collections import OrderedDict
from array import array
import bisect


class RemoteTreeStore:
    lazy = True
    def __init__(self, send, total, root_id):
        self.send, self.total, self.root_id = send, total, root_id
        self.cache, self.directory_ids = OrderedDict(), OrderedDict()
        self.inflight = self.desired = None
        self.online, self.pending = True, False
        self.error = ""
        self.history_parent, self.history = ".", array("Q", [0])

    def __len__(self):
        return self.total

    def children(self, parent=".", after=0, size=81):
        key = (parent,after,size)
        self.desired = key
        if parent != self.history_parent:
            self.history_parent, self.history = parent, array("Q",[0])
        page = self.cache.setdefault(key, {"rows":[],"cursor":after,"pending":True})
        self.cache.move_to_end(key)
        while len(self.cache) > 8:
            self.cache.popitem(last=False)
        self.pending = page["pending"] and self.online
        self.error = page.get("error", "")
        if self.pending and self.inflight is None:
            command = {"kind":"browse", "key":key, "parent":parent, "after":page["cursor"],
                       "limit":size-len(page["rows"]),
                       "directory_id":self.root_id if parent == "." else self.directory_ids.get(parent)}
            if self.send(command):
                self.inflight = key
        if len(page["rows"]) >= size and size > 1:
            next_cursor = int(page["rows"][size-2]["entry_id"][1:])
            if next_cursor > self.history[-1]:
                self.history.append(next_cursor)
        return page["rows"]

    def accept(self, key, reply):
        key = tuple(key)
        if key != self.inflight:
            return
        self.inflight = None
        page = self.cache.get(key)
        if page is None:
            return
        page["rows"].extend(reply["rows"])
        page.update(cursor=reply["cursor"], pending=reply["pending"], error=reply.get("error", ""))
        for row in reply["rows"]:
            if row["is_dir"]:
                self.directory_ids[row["relative_path"]] = row["_directory_id"]
                self.directory_ids.move_to_end(row["relative_path"])
        while len(self.directory_ids) > 2048:
            self.directory_ids.popitem(last=False)

    def previous(self, parent, after, size=80):
        if parent != self.history_parent:
            return 0
        position = bisect.bisect_left(self.history, after)
        return self.history[max(0,position-1)]

    def pause(self):
        self.online, self.pending = False, False

    def close(self):
        self.pause()
        self.cache.clear()
        self.directory_ids.clear()
