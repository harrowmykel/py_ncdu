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


class Scanner:
    """Recursively scans directory trees."""

    def __init__(
        self,
        root_path: str,
        same_fs: bool = False,
        excludes: Optional[List[str]] = None,
        progress_callback=None,
    ):
        self.root_path = os.path.abspath(root_path)
        self.same_fs = same_fs
        self.excludes = excludes or []
        self.progress_callback = progress_callback
        self.scanned_items = 0
        self.root_dev: Optional[int] = None

    def _is_excluded(self, path: str, name: str) -> bool:
        for pat in self.excludes:
            if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def scan(self) -> Node:
        """Perform recursive directory scan."""
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

        if root_node.is_dir:
            self._scan_dir(root_node)
            root_node.recalculate()
        else:
            try:
                root_node.size = os.path.getsize(self.root_path)
            except OSError:
                root_node.size = 0
            root_node.item_count = 1

        return root_node

    def _scan_dir(self, dir_node: Node) -> None:
        if self.progress_callback and self.scanned_items % 50 == 0:
            self.progress_callback(dir_node.path, self.scanned_items)

        try:
            with os.scandir(dir_node.path) as it:
                for entry in it:
                    self.scanned_items += 1
                    name = entry.name
                    path = entry.path

                    if self._is_excluded(path, name):
                        continue

                    try:
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        # Broken symlink or inaccessible entry
                        child = Node(
                            name=name,
                            path=path,
                            is_dir=False,
                            size=0,
                            parent=dir_node,
                        )
                        child.read_error = True
                        dir_node.add_child(child)
                        continue

                    if self.same_fs and self.root_dev is not None:
                        if stat.st_dev != self.root_dev:
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
                        self._scan_dir(child)

        except (PermissionError, OSError):
            dir_node.read_error = True


class NcduApp:
    """TUI Application for browsing disk usage."""

    def __init__(self, root_node: Node):
        self.root = root_node
        self.current_dir = root_node
        self.cursor_idx = 0
        self.scroll_offset = 0
        self.sort_key = "size"  # 'size', 'name', 'count'
        self.sort_reverse = True
        self.items: List[Node] = []
        self._update_item_list()

    def _update_item_list(self) -> None:
        """Sort and refresh the list of children for the current directory."""
        if not self.current_dir.is_dir:
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

    def run(self) -> None:
        """Initialize curses and run event loop."""
        if curses is None:
            print("Error: curses library is not available. Please install windows-curses on Windows.", file=sys.stderr)
            sys.exit(1)
        curses.wrapper(self._main_loop)

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
        if self.current_dir.parent is not None:
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
        if h < 5 or w < 30:
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
        if not (0 <= self.cursor_idx < len(self.items)):
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
        if not (0 <= self.cursor_idx < len(self.items)):
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


def scan_progress_terminal(path: str, count: int) -> None:
    """Print scan progress to terminal before curses starts."""
    path_trunc = path if len(path) < 60 else "..." + path[-57:]
    sys.stdout.write(f"\rScanning items: {count:>7}  |  {path_trunc:<60}")
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
    else:
        # Perform scan
        target_path = os.path.abspath(args.path)
        if not os.path.exists(target_path):
            print(f"Error: Path does not exist: {target_path}", file=sys.stderr)
            sys.exit(1)

        print(f"Scanning directory: {target_path} ...")
        scanner = Scanner(
            root_path=target_path,
            same_fs=args.same_fs,
            excludes=args.exclude,
            progress_callback=scan_progress_terminal,
        )
        t0 = time.time()
        root_node = scanner.scan()
        scan_time = time.time() - t0
        sys.stdout.write("\r" + " " * 90 + "\r")
        sys.stdout.flush()
        print(f"Scan complete: {scanner.scanned_items} items in {scan_time:.2f}s ({format_size(root_node.size).strip()})")

        # If export requested
        if args.export:
            try:
                with open(args.export, "w", encoding="utf-8") as f:
                    json.dump(root_node.to_dict(), f, indent=2)
                print(f"Exported scan results to: {args.export}")
            except Exception as e:
                print(f"Error exporting results: {e}", file=sys.stderr)
                sys.exit(1)
            return

    # Start TUI
    app = NcduApp(root_node)
    app.run()


if __name__ == "__main__":
    main()
