# ncdu.py - Python Disk Usage Analyzer

A lightweight, standalone, cross-platform Python implementation of **`ncdu`** (NCurses Disk Usage). It provides a fast directory scanner and an interactive NCurses-based terminal user interface (TUI) to inspect, sort, and manage disk space directly from the command line.

---

## Features

- **Multi-threaded Scanning (Default)**: Leverages a worker thread pool for high-throughput scanning on fast SSDs / NVMes, with customizable thread count (`-j / --threads`).
- **Fast Speed & Time Estimation**: Calibrates scanning rate based on the first 100 files to minimize computational overhead during large scans.
- **Interactive TUI Navigation**: Move effortlessly through directory trees with arrow keys or Vim keybindings (`h`/`j`/`k`/`l`).
- **Visual Relative Size Bars**: Instant visual indicator `[#####     ]` showing space consumption relative to the largest item in the current folder.
- **Fast & Resilient Scanning**: Recursively scans directories with real-time progress indicators, handling permission errors and broken symlinks gracefully.
- **Sorting Options**: Sort by **Size** (`s`), **Name** (`n`), or **Item Count** (`c`), with ascending/descending toggle (`a`).
- **In-place File Management**: Delete files or entire directories (`d`) with an interactive confirmation prompt and automatic tree size recalculation.
- **Detailed File Info**: Inspect full path, size, sub-item count, modified time, and status (`i`).
- **Shell Spawning**: Open a subshell in the currently selected directory (`b`).
- **Export & Import**: Save scans to JSON (`-o <file.json>`) and explore them later or on another machine without rescanning (`-f <file.json>`).
- **Filesystem Boundaries & Filtering**:
  - Stay on the same filesystem/mount point (`-x` / `--same-fs`).
  - Exclude patterns (`--exclude <pattern>`).

---

## Requirements & Installation

- **Python**: 3.8+
- **Linux / macOS**: Uses the built-in `curses` module (no extra dependencies required).
- **Windows**: Requires `windows-curses` for terminal UI support.

### Setup

```bash
# Clone or download the repository
cd tools

# Install dependencies (Windows only)
pip install -r requirements.txt
```

---

## Usage

### Basic Scan & Browse
```bash
# Scan and browse current directory (multi-threaded by default)
python ncdu.py

# Scan a specific directory with 16 worker threads
python ncdu.py -j 16 /path/to/folder

# Single-threaded mode (for legacy HDDs)
python ncdu.py -j 1 /path/to/folder
```

### Advanced Options

```bash
# Stay on the same filesystem and exclude node_modules and logs
python ncdu.py -x --exclude "node_modules" --exclude "*.log" /var/www

# Export scan results to a JSON file
python ncdu.py -o scan_backup.json /home/user

# Import and explore an existing JSON scan file
python ncdu.py -f scan_backup.json
```

---

## Keybindings Cheat Sheet

| Key | Action |
| :--- | :--- |
| `↑` / `k` | Move cursor up |
| `↓` / `j` | Move cursor down |
| `Enter` / `→` / `l` | Open selected directory |
| `←` / `h` | Go to parent directory |
| `g` / `Home` | Jump to top of list |
| `G` / `End` | Jump to bottom of list |
| `s` | Sort by size |
| `n` | Sort by name |
| `c` | Sort by item count |
| `a` | Toggle ascending / descending sort |
| `d` | Delete selected file or directory (with confirmation) |
| `i` | Show detailed metadata popup |
| `b` | Spawn a shell in the current directory |
| `?` | Show help modal |
| `q` | Quit or close modal |

---

## Running Tests

To run the automated unit test suite:

```bash
python test_ncdu.py
```

---

## License

MIT License. Free for open-source and personal use.
