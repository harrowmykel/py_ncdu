#!/usr/bin/env python3
"""
ncdu.py - A Python implementation of ncdu (NCurses Disk Usage)
Cross-platform interactive disk usage analyzer with an NCurses TUI.
"""

import os
import sys
import time
import json
import shutil
import fnmatch
import argparse
from typing import List, Optional, Dict, Any, Tuple

try:
    import curses
except ImportError:
    curses = None

__version__ = "1.0.0"


def format_size(size_bytes: int) -> str:
    """Format bytes into human-readable binary unit strings (B, KiB, MiB, GiB, TiB)."""
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(size_bytes)
    for unit in units:
        if abs(size) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size):>5} B"
            return f"{size:>6.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def make_graph_bar(size: int, max_size: int, bar_width: int = 10) -> str:
    """Generate a visual graph bar like [#####     ]."""
    if max_size <= 0:
        filled = 0
    else:
        fraction = min(1.0, max(0.0, size / max_size))
        filled = int(round(fraction * bar_width))
    return f"[{'#' * filled}{' ' * (bar_width - filled)}]"


class Node:
    """Represents a file or directory node in the disk usage tree."""

    def __init__(
        self,
        name: str,
        path: str,
        is_dir: bool = False,
        size: int = 0,
        item_count: int = 0,
        mtime: float = 0.0,
        parent: Optional["Node"] = None,
    ):
        self.name = name
        self.path = path
        self.is_dir = is_dir
        self.size = size
        self.item_count = item_count
        self.mtime = mtime
        self.parent = parent
        self.children: List[Node] = []
        self.read_error: bool = False

    def add_child(self, child: "Node") -> None:
        child.parent = self
        self.children.append(child)

    def remove_child(self, child: "Node") -> None:
        if child in self.children:
            self.children.remove(child)
            child.parent = None

    def recalculate(self) -> None:
        """Recalculate recursive size and item count from children."""
        if self.is_dir:
            total_size = 0
            total_items = len(self.children)
            for child in self.children:
                if child.is_dir:
                    child.recalculate()
                    total_size += child.size
                    total_items += child.item_count
                else:
                    total_size += child.size
            self.size = total_size
            self.item_count = total_items

    def recalculate_upwards(self) -> None:
        """Recalculate self and propagate recalculations up to root."""
        curr: Optional[Node] = self
        while curr is not None:
            if curr.is_dir:
                curr.size = sum(c.size for c in curr.children)
                curr.item_count = len(curr.children) + sum(
                    c.item_count for c in curr.children if c.is_dir
                )
            curr = curr.parent

    def to_dict(self) -> Dict[str, Any]:
        """Serialize tree to a dictionary."""
        data: Dict[str, Any] = {
            "name": self.name,
            "path": self.path,
            "is_dir": self.is_dir,
            "size": self.size,
            "item_count": self.item_count,
            "mtime": self.mtime,
            "read_error": self.read_error,
        }
        if self.is_dir:
            data["children"] = [child.to_dict() for child in self.children]
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any], parent: Optional["Node"] = None) -> "Node":
        """Reconstruct tree from serialized dictionary."""
        node = cls(
            name=data.get("name", ""),
            path=data.get("path", ""),
            is_dir=data.get("is_dir", False),
            size=data.get("size", 0),
            item_count=data.get("item_count", 0),
            mtime=data.get("mtime", 0.0),
            parent=parent,
        )
        node.read_error = data.get("read_error", False)
        if node.is_dir and "children" in data:
            for child_data in data["children"]:
                child_node = cls.from_dict(child_data, parent=node)
                node.children.append(child_node)
        return node


import threading
from queue import Queue, Empty


class Scanner:
    """Multi-threaded recursive filesystem scanner with speed calibration and Ctrl+C support."""

    def __init__(
        self,
        root_path: str,
        same_fs: bool = False,
        excludes: Optional[List[str]] = None,
        threads: Optional[int] = None,
        progress_callback=None,
    ):
        self.root_path = os.path.abspath(root_path)
        self.same_fs = same_fs
        self.excludes = excludes or []
        if threads is None or threads <= 0:
            self.threads = min(32, (os.cpu_count() or 1) * 4)
        else:
            self.threads = threads
        self.progress_callback = progress_callback
        self.scanned_items = 0
        self.root_dev: Optional[int] = None
        self._lock = threading.Lock()
        self.start_time = 0.0
        self.calibrated_rate: Optional[float] = None  # items per second based on first 100 files
        self.calibrated_time_for_100: Optional[float] = None
        self.stop_event = threading.Event()
        self.latest_path = ""
        self._last_seq_progress_time = 0.0
        self.thread_status: Dict[int, Dict[str, Any]] = {
            i: {"status": "idle", "path": "", "items": 0} for i in range(self.threads)
        }

    def _is_excluded(self, path: str, name: str) -> bool:
        for pat in self.excludes:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def stop(self) -> None:
        """Signal all worker threads to cancel scanning."""
        self.stop_event.set()

    def get_thread_status(self) -> Tuple[int, Optional[float], float, Dict[int, Dict[str, Any]]]:
        """Return a snapshot of current scan stats and each thread's status."""
        with self._lock:
            return (
                self.scanned_items,
                self.calibrated_rate,
                time.time() - self.start_time if self.start_time > 0 else 0.0,
                {k: dict(v) for k, v in self.thread_status.items()},
            )

    def scan(self) -> Optional[Node]:
        """Perform directory scan with cancellation support."""
        try:
            stat_root = os.stat(self.root_path, follow_symlinks=False)
            self.root_dev = stat_root.st_dev
            mtime = stat_root.st_mtime
        except OSError:
            mtime = 0.0

        root_name = os.path.basename(self.root_path) or self.root_path
        root_node = Node(
            name=root_name,
            path=self.root_path,
            is_dir=os.path.isdir(self.root_path),
            mtime=mtime,
        )

        if not root_node.is_dir:
            try:
                root_node.size = os.path.getsize(self.root_path)
            except OSError:
                root_node.size = 0
            root_node.item_count = 1
            return root_node

        self.start_time = time.time()
        self.scanned_items = 0
        self._last_seq_progress_time = self.start_time

        try:
            if self.threads <= 1:
                self._scan_dir_sequential(root_node)
            else:
                self._scan_dir_multithreaded(root_node)
        except KeyboardInterrupt:
            self.stop()
            raise

        if self.stop_event.is_set():
            return None

        root_node.recalculate()
        return root_node

    def _scan_dir_sequential(self, dir_node: Node) -> None:
        if self.stop_event.is_set():
            return

        self._record_path(dir_node.path, thread_id=0)
        now = time.time()
        if self.progress_callback and (now - self._last_seq_progress_time >= 2.0):
            self._last_seq_progress_time = now
            self.progress_callback(
                self.latest_path,
                self.scanned_items,
                self.calibrated_rate,
                now - self.start_time,
            )

        try:
            with os.scandir(dir_node.path) as it:
                for entry in it:
                    if self.stop_event.is_set():
                        return

                    self._increment_item(thread_id=0)
                    name = entry.name
                    path = entry.path

                    if self._is_excluded(path, name):
                        continue

                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        child = Node(name=name, path=path, is_dir=False, size=0, parent=dir_node)
                        child.read_error = True
                        dir_node.add_child(child)
                        continue

                    if self.same_fs and self.root_dev is not None and stat.st_dev != self.root_dev:
                        continue

                    is_dir = entry.is_dir(follow_symlinks=False)
                    child = Node(
                        name=name,
                        path=path,
                        is_dir=is_dir,
                        size=stat.st_size if not is_dir else 0,
                        mtime=stat.st_mtime,
                        parent=dir_node,
                    )
                    dir_node.add_child(child)

                    if is_dir:
                        self._scan_dir_sequential(child)
        except (PermissionError, OSError):
            dir_node.read_error = True

    def _scan_dir_multithreaded(self, root_node: Node) -> None:
        work_queue: Queue = Queue()
        work_queue.put(root_node)
        active_lock = threading.Lock()
        active_count = 0
        all_done = threading.Event()

        def worker(thread_id: int):
            nonlocal active_count
            while not all_done.is_set() and not self.stop_event.is_set():
                try:
                    dir_node = work_queue.get(timeout=0.05)
                except Empty:
                    with active_lock:
                        if active_count == 0 and work_queue.empty():
                            all_done.set()
                            return
                    with self._lock:
                        self.thread_status[thread_id]["status"] = "idle"
                    continue

                if self.stop_event.is_set():
                    work_queue.task_done()
                    return

                with active_lock:
                    active_count += 1

                with self._lock:
                    self.thread_status[thread_id]["status"] = "scanning"
                    self.thread_status[thread_id]["path"] = dir_node.path

                try:
                    self._record_path(dir_node.path, thread_id=thread_id)
                    with os.scandir(dir_node.path) as it:
                        for entry in it:
                            if self.stop_event.is_set():
                                break

                            self._increment_item(thread_id=thread_id)
                            name = entry.name
                            path = entry.path

                            if self._is_excluded(path, name):
                                continue

                            try:
                                stat = entry.stat(follow_symlinks=False)
                            except OSError:
                                child = Node(name=name, path=path, is_dir=False, size=0, parent=dir_node)
                                child.read_error = True
                                with self._lock:
                                    dir_node.add_child(child)
                                continue

                            if self.same_fs and self.root_dev is not None and stat.st_dev != self.root_dev:
                                continue

                            is_dir = entry.is_dir(follow_symlinks=False)
                            child = Node(
                                name=name,
                                path=path,
                                is_dir=is_dir,
                                size=stat.st_size if not is_dir else 0,
                                mtime=stat.st_mtime,
                                parent=dir_node,
                            )
                            with self._lock:
                                dir_node.add_child(child)

                            if is_dir and not self.stop_event.is_set():
                                work_queue.put(child)
                except (PermissionError, OSError):
                    dir_node.read_error = True
                finally:
                    with active_lock:
                        active_count -= 1
                        if active_count == 0 and work_queue.empty():
                            all_done.set()
                    work_queue.task_done()
                    with self._lock:
                        self.thread_status[thread_id]["status"] = "idle"

        worker_threads = []
        for tid in range(self.threads):
            t = threading.Thread(target=worker, args=(tid,), daemon=True)
            t.start()
            worker_threads.append(t)

        # Main thread coordinates and prints progress every 2 seconds if callback exists
        try:
            while not all_done.wait(timeout=2.0):
                if self.stop_event.is_set():
                    break
                if self.progress_callback:
                    with self._lock:
                        count = self.scanned_items
                        rate = self.calibrated_rate
                        path = self.latest_path
                        elapsed = time.time() - self.start_time
                    self.progress_callback(path, count, rate, elapsed)
        except KeyboardInterrupt:
            self.stop()
            all_done.set()
            # Drain queue so blocked threads unblock
            while not work_queue.empty():
                try:
                    work_queue.get_nowait()
                    work_queue.task_done()
                except Empty:
                    break
            raise

        for t in worker_threads:
            t.join(timeout=0.5)

    def _increment_item(self, thread_id: int = 0) -> None:
        with self._lock:
            self.scanned_items += 1
            if thread_id in self.thread_status:
                self.thread_status[thread_id]["items"] += 1
            # Calibration: calculate rate on the first 100 items
            if self.scanned_items == 100 and self.calibrated_rate is None:
                elapsed = time.time() - self.start_time
                self.calibrated_time_for_100 = elapsed
                self.calibrated_rate = 100.0 / max(elapsed, 0.0001)

    def _record_path(self, path: str, thread_id: int = 0) -> None:
        with self._lock:
            self.latest_path = path
            if thread_id in self.thread_status:
                self.thread_status[thread_id]["path"] = path
                self.thread_status[thread_id]["status"] = "scanning"


def render_curses_scan(stdscr, scanner: Scanner) -> Optional[Node]:
    """Render live multi-threaded scanning progress dashboard in curses."""
    curses.curs_set(0)
    stdscr.timeout(50)
    curses.use_default_colors()

    if curses.has_colors():
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)    # Header/Footer
        curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)    # Active
        curses.init_pair(3, curses.COLOR_GREEN, -1)                  # Scanning status
        curses.init_pair(4, curses.COLOR_YELLOW, -1)                 # Idle status

    scan_done = threading.Event()
    root_box: List[Optional[Node]] = [None]
    error_box: List[Optional[Exception]] = [None]

    def scan_worker():
        try:
            root_box[0] = scanner.scan()
        except Exception as e:
            error_box[0] = e
        finally:
            scan_done.set()

    t = threading.Thread(target=scan_worker, daemon=True)
    t.start()

    while not scan_done.is_set():
        try:
            ch = stdscr.getch()
        except curses.error:
            ch = -1

        if ch in (ord('q'), ord('Q'), 27):  # 'q' or ESC
            scanner.stop()
            break

        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 5 or w < 30:
            stdscr.addstr(0, 0, "Window too small")
            stdscr.refresh()
            continue

        scanned_items, rate, elapsed, thread_status = scanner.get_thread_status()

        # 1. Header Bar
        hdr = f" ncdu.py {__version__} ~ Scanning: {scanner.root_path} (Threads: {scanner.threads})"
        hdr = hdr[:w - 1].ljust(w - 1)
        attr_hdr = curses.color_pair(1) if curses.has_colors() else curses.A_REVERSE
        try:
            stdscr.addstr(0, 0, hdr, attr_hdr)
        except curses.error:
            pass

        # 2. Stats bar
        rate_str = f"~{int(rate):,} items/s" if rate is not None else "calibrating (1st 100)..."
        stats_line = f" Scanned: {scanned_items:,} items | Speed (1st 100): {rate_str} | Elapsed: {elapsed:.1f}s"
        stats_line = stats_line[:w - 1].ljust(w - 1)
        try:
            stdscr.addstr(1, 0, stats_line)
            stdscr.addstr(2, 0, "-" * (w - 1))
        except curses.error:
            pass

        # 3. Thread Activity Lines
        avail_rows = h - 5
        visible_threads = min(len(thread_status), avail_rows)
        for i in range(visible_threads):
            info = thread_status.get(i, {"status": "idle", "path": "", "items": 0})
            st = info.get("status", "idle").upper()
            items_c = info.get("items", 0)
            path_str = info.get("path", "")

            prefix = f" Thread #{i:02d}: [{st:8s}] {items_c:>6,} items | "
            rem_w = max(0, w - 1 - len(prefix))
            if len(path_str) > rem_w:
                path_display = "..." + path_str[-(rem_w - 3):] if rem_w > 3 else path_str[:rem_w]
            else:
                path_display = path_str

            row_line = (prefix + path_display)[:w - 1].ljust(w - 1)
            row_y = 3 + i

            attr = curses.A_NORMAL
            if st == "SCANNING" and curses.has_colors():
                attr = curses.color_pair(3) | curses.A_BOLD
            elif st == "IDLE" and curses.has_colors():
                attr = curses.color_pair(4)

            try:
                stdscr.addstr(row_y, 0, row_line, attr)
            except curses.error:
                pass

        # 4. Footer
        footer_text = " Scanning in progress... Press 'q' or Ctrl+C to cancel"
        footer_text = footer_text[:w - 1].ljust(w - 1)
        try:
            stdscr.addstr(h - 1, 0, footer_text, attr_hdr)
        except curses.error:
            pass

        stdscr.refresh()

    t.join(timeout=1.0)
    if error_box[0]:
        raise error_box[0]

    return root_box[0]


class NcduApp:
    """TUI Application for browsing disk usage."""

    def __init__(self, root_node: Optional[Node] = None, scanner: Optional[Scanner] = None):
        self.root = root_node
        self.scanner = scanner
        self.current_dir = root_node
        self.cursor_idx = 0
        self.scroll_offset = 0
        self.sort_key = "size"  # 'size', 'name', 'count'
        self.sort_reverse = True
        self.items: List[Node] = []
        if root_node is not None:
            self._update_item_list()

    def _update_item_list(self) -> None:
        """Sort and refresh the list of children for the current directory."""
        if not self.current_dir or not self.current_dir.is_dir:
            self.items = []
            return

        items = list(self.current_dir.children)
        if self.sort_key == "size":
            items.sort(key=lambda x: (x.size, x.name.lower()), reverse=self.sort_reverse)
        elif self.sort_key == "name":
            items.sort(key=lambda x: x.name.lower(), reverse=self.sort_reverse)
        elif self.sort_key == "count":
            items.sort(key=lambda x: (x.item_count if x.is_dir else 0, x.name.lower()), reverse=self.sort_reverse)

        self.items = items
        if self.cursor_idx >= len(self.items):
            self.cursor_idx = max(0, len(self.items) - 1)

    def run(self) -> Optional[Node]:
        """Initialize curses and run event loop."""
        if curses is None:
            print("Error: curses library is not available. Please install windows-curses on Windows.", file=sys.stderr)
            sys.exit(1)
        return curses.wrapper(self._entry_loop)

    def _entry_loop(self, stdscr) -> Optional[Node]:
        # If scanner is provided, render curses live scan first
        if self.root is None and self.scanner is not None:
            self.root = render_curses_scan(stdscr, self.scanner)
            if self.root is None or self.scanner.stop_event.is_set():
                return None
            self.current_dir = self.root
            self._update_item_list()

        if self.root is not None:
            self._main_loop(stdscr)
        return self.root

    def _main_loop(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.timeout(100)
        curses.use_default_colors()

        # Define color pairs if possible
        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)    # Header/Footer
            curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLUE)    # Selected row
            curses.init_pair(3, curses.COLOR_YELLOW, -1)                  # Directory flag / warning
            curses.init_pair(4, curses.COLOR_GREEN, -1)                   # Graph bar
            curses.init_pair(5, curses.COLOR_RED, curses.COLOR_WHITE)     # Confirm / Alert

        while True:
            self._render(stdscr)
            try:
                ch = stdscr.getch()
            except curses.error:
                ch = -1

            if ch == -1:
                continue

            if ch in (ord('q'), ord('Q')):
                break
            elif ch in (curses.KEY_UP, ord('k'), ord('K')):
                if self.cursor_idx > 0:
                    self.cursor_idx -= 1
            elif ch in (curses.KEY_DOWN, ord('j'), ord('J')):
                if self.cursor_idx < len(self.items) - 1:
                    self.cursor_idx += 1
            elif ch in (curses.KEY_RIGHT, ord('l'), ord('L'), 10, 13, curses.KEY_ENTER):
                self._enter_selected()
            elif ch in (curses.KEY_LEFT, ord('h'), ord('H')):
                self._go_parent()
            elif ch in (ord('g'), curses.KEY_HOME):
                self.cursor_idx = 0
            elif ch in (ord('G'), curses.KEY_END):
                if self.items:
                    self.cursor_idx = len(self.items) - 1
            elif ch in (ord('s'), ord('S')):
                self._cycle_sort("size")
            elif ch in (ord('n'), ord('N')):
                self._cycle_sort("name")
            elif ch in (ord('c'), ord('C')):
                self._cycle_sort("count")
            elif ch in (ord('a'), ord('A')):
                self.sort_reverse = not self.sort_reverse
                self._update_item_list()
            elif ch in (ord('d'), ord('D')):
                self._handle_delete(stdscr)
            elif ch in (ord('i'), ord('I')):
                self._show_info_modal(stdscr)
            elif ch == ord('?'):
                self._show_help_modal(stdscr)
            elif ch in (ord('b'), ord('B')):
                self._spawn_shell(stdscr)

    def _cycle_sort(self, sort_type: str) -> None:
        if self.sort_key == sort_type:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = sort_type
            self.sort_reverse = True if sort_type in ("size", "count") else False
        self._update_item_list()

    def _enter_selected(self) -> None:
        if 0 <= self.cursor_idx < len(self.items):
            selected = self.items[self.cursor_idx]
            if selected.is_dir:
                self.current_dir = selected
                self.cursor_idx = 0
                self.scroll_offset = 0
                self._update_item_list()

    def _go_parent(self) -> None:
        if self.current_dir and self.current_dir.parent is not None:
            prev = self.current_dir
            self.current_dir = self.current_dir.parent
            self.scroll_offset = 0
            self._update_item_list()
            # Restore selection to previously visited folder
            for idx, item in enumerate(self.items):
                if item == prev:
                    self.cursor_idx = idx
                    break
            else:
                self.cursor_idx = 0

    def _render(self, stdscr) -> None:
        stdscr.erase()
        h, w = stdscr.getmaxyx()
        if h < 5 or w < 30 or not self.current_dir:
            stdscr.addstr(0, 0, "Window too small")
            stdscr.refresh()
            return

        # 1. Header Bar
        header_text = f" ncdu.py {__version__} ~ Use arrow keys to navigate, ? for help, q to quit"
        header_text = header_text[:w - 1].ljust(w - 1)
        attr_hdr = curses.color_pair(1) if curses.has_colors() else curses.A_REVERSE
        try:
            stdscr.addstr(0, 0, header_text, attr_hdr)
        except curses.error:
            pass

        # 2. Path & Sort status bar
        sort_desc = f"Sort: {self.sort_key} ({'desc' if self.sort_reverse else 'asc'})"
        path_str = f"--- {self.current_dir.path} "
        remaining_len = w - 1 - len(path_str) - len(sort_desc) - 5
        if remaining_len > 0:
            path_bar = path_str + ("-" * remaining_len) + f" [{sort_desc}] ---"
        else:
            path_bar = (path_str + f"[{sort_desc}]")[:w - 1]
        try:
            stdscr.addstr(1, 0, path_bar)
        except curses.error:
            pass

        # Calculate max size in current dir for bar graph
        max_size = max((item.size for item in self.items), default=0)

        # 3. Main Item List Area
        list_h = h - 4
        if self.cursor_idx < self.scroll_offset:
            self.scroll_offset = self.cursor_idx
        elif self.cursor_idx >= self.scroll_offset + list_h:
            self.scroll_offset = self.cursor_idx - list_h + 1

        for i in range(list_h):
            idx = self.scroll_offset + i
            row_y = 2 + i
            if idx >= len(self.items):
                break

            item = self.items[idx]
            is_selected = (idx == self.cursor_idx)

            size_str = format_size(item.size)
            graph = make_graph_bar(item.size, max_size, bar_width=10)
            prefix = "/" if item.is_dir else " "
            err_flag = "! " if item.read_error else "  "
            item_name = f"{prefix}{item.name}"

            # Format full line: [size] [graph] [err] [name]
            line = f" {size_str} {graph} {err_flag}{item_name}"
            line = line[:w - 1].ljust(w - 1)

            attr = curses.A_NORMAL
            if is_selected:
                attr = curses.color_pair(2) if curses.has_colors() else curses.A_REVERSE
            elif item.is_dir and curses.has_colors():
                attr = curses.color_pair(3) | curses.A_BOLD

            try:
                stdscr.addstr(row_y, 0, line, attr)
            except curses.error:
                pass

        # 4. Separator
        try:
            stdscr.addstr(h - 2, 0, "-" * (w - 1))
        except curses.error:
            pass

        # 5. Footer / Status Bar
        total_size_str = format_size(self.current_dir.size)
        total_items = self.current_dir.item_count
        item_pos = f"{self.cursor_idx + 1}/{len(self.items)}" if self.items else "0/0"
        footer_text = f" Total: {total_size_str} | Items: {total_items} | Pos: {item_pos}"
        footer_text = footer_text[:w - 1].ljust(w - 1)
        try:
            stdscr.addstr(h - 1, 0, footer_text, attr_hdr)
        except curses.error:
            pass

        stdscr.refresh()

    def _show_modal(self, stdscr, title: str, lines: List[str]) -> None:
        """Render a centered modal dialog and wait for any key."""
        h, w = stdscr.getmaxyx()
        box_w = min(max(max((len(l) for l in lines), default=20) + 6, len(title) + 6, 40), w - 4)
        box_h = min(len(lines) + 4, h - 2)

        start_y = max(0, (h - box_h) // 2)
        start_x = max(0, (w - box_w) // 2)

        # Draw box border
        for y in range(box_h):
            line_str = " " * box_w
            if y == 0 or y == box_h - 1:
                line_str = "+" + "-" * (box_w - 2) + "+"
            else:
                line_str = "|" + " " * (box_w - 2) + "|"
            try:
                stdscr.addstr(start_y + y, start_x, line_str, curses.A_BOLD)
            except curses.error:
                pass

        # Title
        title_str = f" [ {title} ] "
        title_x = start_x + max(1, (box_w - len(title_str)) // 2)
        try:
            stdscr.addstr(start_y, title_x, title_str, curses.A_BOLD)
        except curses.error:
            pass

        # Content
        for idx, line in enumerate(lines[:box_h - 3]):
            content = line[:box_w - 4]
            try:
                stdscr.addstr(start_y + 2 + idx, start_x + 2, content)
            except curses.error:
                pass

        # Prompt hint
        hint = "Press any key to continue"
        try:
            stdscr.addstr(start_y + box_h - 1, start_x + (box_w - len(hint) - 2) // 2, f" {hint} ")
        except curses.error:
            pass

        stdscr.refresh()
        stdscr.timeout(-1)
        stdscr.getch()
        stdscr.timeout(100)

    def _show_help_modal(self, stdscr) -> None:
        help_lines = [
            "Keys & Navigation:",
            "  up, k           Move cursor up",
            "  down, j         Move cursor down",
            "  enter, right, l Open directory",
            "  left, h         Open parent directory",
            "  g, Home         Jump to top",
            "  G, End          Jump to bottom",
            "",
            "Sorting & Actions:",
            "  s               Sort by size",
            "  n               Sort by name",
            "  c               Sort by item count",
            "  a               Toggle ascending / descending",
            "  d               Delete selected item",
            "  i               Show item information",
            "  b               Spawn shell in current directory",
            "  ?               Show this help screen",
            "  q               Quit ncdu.py",
        ]
        self._show_modal(stdscr, "Help & Keybindings", help_lines)

    def _show_info_modal(self, stdscr) -> None:
        if not self.items or not (0 <= self.cursor_idx < len(self.items)):
            return
        item = self.items[self.cursor_idx]
        mtime_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item.mtime)) if item.mtime else "Unknown"
        lines = [
            f"Name:       {item.name}",
            f"Path:       {item.path}",
            f"Type:       {'Directory' if item.is_dir else 'File'}",
            f"Size:       {format_size(item.size)} ({item.size:,} bytes)",
            f"Sub-items:  {item.item_count if item.is_dir else 'N/A'}",
            f"Modified:   {mtime_str}",
            f"Read Error: {'Yes' if item.read_error else 'No'}",
        ]
        self._show_modal(stdscr, "Item Information", lines)

    def _handle_delete(self, stdscr) -> None:
        if not self.items or not (0 <= self.cursor_idx < len(self.items)) or not self.current_dir:
            return
        item = self.items[self.cursor_idx]

        h, w = stdscr.getmaxyx()
        prompt = f"Delete '{item.name}'? [y/N]: "
        box_w = min(len(prompt) + 20, w - 4)
        box_h = 5
        start_y = max(0, (h - box_h) // 2)
        start_x = max(0, (w - box_w) // 2)

        # Draw confirmation box
        for y in range(box_h):
            line_str = "+" + "-" * (box_w - 2) + "+" if (y == 0 or y == box_h - 1) else "|" + " " * (box_w - 2) + "|"
            try:
                stdscr.addstr(start_y + y, start_x, line_str, curses.A_BOLD)
            except curses.error:
                pass

        try:
            stdscr.addstr(start_y + 2, start_x + 3, prompt, curses.A_BOLD)
        except curses.error:
            pass

        stdscr.refresh()
        stdscr.timeout(-1)
        ch = stdscr.getch()
        stdscr.timeout(100)

        if ch in (ord('y'), ord('Y')):
            try:
                if item.is_dir:
                    shutil.rmtree(item.path)
                else:
                    os.remove(item.path)

                self.current_dir.remove_child(item)
                self.current_dir.recalculate_upwards()
                self._update_item_list()
            except Exception as e:
                self._show_modal(stdscr, "Delete Error", [f"Failed to delete: {str(e)}"])

    def _spawn_shell(self, stdscr) -> None:
        """Spawn a subshell in the current directory."""
        if not self.current_dir:
            return
        curses.def_prog_mode()
        curses.endwin()
        try:
            shell = os.environ.get("SHELL")
            if not shell:
                shell = "powershell.exe" if sys.platform == "win32" else "/bin/sh"
            print(f"\n[ncdu.py] Spawning shell in {self.current_dir.path} (type 'exit' to return)...")
            os.system(f'cd "{self.current_dir.path}" && {shell}')
        finally:
            curses.reset_prog_mode()
            curses.curs_set(0)


def scan_progress_terminal(path: str, count: int, rate: Optional[float], elapsed: float) -> None:
    """Print a single progress line every 2 seconds before curses starts."""
    path_trunc = path if len(path) < 40 else "..." + path[-37:]
    if rate is not None:
        rate_str = f"~{int(rate):>5} items/s"
    else:
        rate_str = "calibrating..."

    line = f"\rScanning items: {count:>7}  |  Speed (1st 100): {rate_str:<15} |  Elapsed: {elapsed:>4.1f}s  |  {path_trunc:<40}"
    sys.stdout.write(line)
    sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ncdu.py",
        description="NCurses Disk Usage - A Python implementation",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory path to scan (default: current directory)",
    )
    parser.add_argument(
        "-j",
        "--threads",
        type=int,
        default=min(32, (os.cpu_count() or 1) * 4),
        help=f"Number of scanning worker threads (default: {min(32, (os.cpu_count() or 1) * 4)})",
    )
    parser.add_argument(
        "-x",
        "--same-fs",
        action="store_true",
        help="Stay on the same filesystem",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude files/directories matching glob pattern",
    )
    parser.add_argument(
        "-o",
        "--export",
        metavar="FILE",
        help="Export scanned directory tree to a JSON file",
    )
    parser.add_argument(
        "-f",
        "--import-file",
        metavar="FILE",
        help="Import and browse directory tree from an exported JSON file",
    )
    parser.add_argument(
        "--no-curses",
        action="store_true",
        help="Disable curses TUI during scan and run in terminal text mode",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    args = parser.parse_args()

    # If importing from JSON file
    if args.import_file:
        if not os.path.exists(args.import_file):
            print(f"Error: Import file not found: {args.import_file}", file=sys.stderr)
            sys.exit(1)
        try:
            with open(args.import_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            root_node = Node.from_dict(data)
            root_node.recalculate()
        except Exception as e:
            print(f"Error loading export file: {e}", file=sys.stderr)
            sys.exit(1)

        app = NcduApp(root_node=root_node)
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        return

    # Perform scan
    target_path = os.path.abspath(args.path)
    if not os.path.exists(target_path):
        print(f"Error: Path does not exist: {target_path}", file=sys.stderr)
        sys.exit(1)

    scanner = Scanner(
        root_path=target_path,
        same_fs=args.same_fs,
        excludes=args.exclude,
        threads=args.threads,
        progress_callback=scan_progress_terminal if args.no_curses else None,
    )

    # If text-only mode or curses not available
    if args.no_curses or curses is None:
        print(f"Scanning directory: {target_path} (threads: {args.threads}) ...")
        t0 = time.time()
        try:
            root_node = scanner.scan()
        except KeyboardInterrupt:
            sys.stdout.write("\r" + " " * 110 + "\r")
            print("[Scan cancelled by user (Ctrl+C)]")
            sys.exit(130)

        if root_node is None:
            print("[Scan aborted]")
            sys.exit(130)

        scan_time = time.time() - t0
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()

        rate_info = f", estimated speed: {int(scanner.calibrated_rate)} items/s (calibrated on first 100 files)" if scanner.calibrated_rate else ""
        print(f"Scan complete: {scanner.scanned_items} items in {scan_time:.2f}s ({format_size(root_node.size).strip()}{rate_info})")

        if args.export:
            try:
                with open(args.export, "w", encoding="utf-8") as f:
                    json.dump(root_node.to_dict(), f, indent=2)
                print(f"Exported scan results to: {args.export}")
            except Exception as e:
                print(f"Error exporting results: {e}", file=sys.stderr)
                sys.exit(1)
            return

        app = NcduApp(root_node=root_node)
        try:
            app.run()
        except KeyboardInterrupt:
            pass
        return

    # Default Curses Live Scan + Browser Mode
    app = NcduApp(scanner=scanner)
    try:
        root_node = app.run()
    except KeyboardInterrupt:
        root_node = None

    # Handle export if requested
    if args.export and root_node is not None:
        try:
            with open(args.export, "w", encoding="utf-8") as f:
                json.dump(root_node.to_dict(), f, indent=2)
            print(f"Exported scan results to: {args.export}")
        except Exception as e:
            print(f"Error exporting results: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
