import os
import sys
import json
import shutil
import tempfile
import unittest

from ncdu import Node, Scanner, format_size, make_graph_bar, NcduApp


class TestNcdu(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ncdu_test_")

        # Create mock file hierarchy:
        # test_dir/
        #   file1.txt (100 bytes)
        #   subdir1/
        #     file2.txt (200 bytes)
        #     subsubdir/
        #       file3.txt (300 bytes)
        #   subdir2/
        #     file4.bin (400 bytes)

        self.file1 = os.path.join(self.test_dir, "file1.txt")
        with open(self.file1, "wb") as f:
            f.write(b"A" * 100)

        self.subdir1 = os.path.join(self.test_dir, "subdir1")
        os.makedirs(self.subdir1)
        self.file2 = os.path.join(self.subdir1, "file2.txt")
        with open(self.file2, "wb") as f:
            f.write(b"B" * 200)

        self.subsubdir = os.path.join(self.subdir1, "subsubdir")
        os.makedirs(self.subsubdir)
        self.file3 = os.path.join(self.subsubdir, "file3.txt")
        with open(self.file3, "wb") as f:
            f.write(b"C" * 300)

        self.subdir2 = os.path.join(self.test_dir, "subdir2")
        os.makedirs(self.subdir2)
        self.file4 = os.path.join(self.subdir2, "file4.bin")
        with open(self.file4, "wb") as f:
            f.write(b"D" * 400)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_format_size(self):
        self.assertEqual(format_size(0).strip(), "0 B")
        self.assertEqual(format_size(500).strip(), "500 B")
        self.assertEqual(format_size(1024).strip(), "1.0 KiB")
        self.assertEqual(format_size(1024 * 1024 * 5).strip(), "5.0 MiB")
        self.assertEqual(format_size(1024 * 1024 * 1024 * 3).strip(), "3.0 GiB")

    def test_make_graph_bar(self):
        self.assertEqual(make_graph_bar(0, 100, bar_width=10), "[          ]")
        self.assertEqual(make_graph_bar(50, 100, bar_width=10), "[#####     ]")
        self.assertEqual(make_graph_bar(100, 100, bar_width=10), "[##########]")

    def test_scanner_and_node_calculations(self):
        scanner = Scanner(self.test_dir)
        root = scanner.scan()

        self.assertTrue(root.is_dir)
        # Total size: 100 + 200 + 300 + 400 = 1000 bytes
        self.assertEqual(root.size, 1000)

        # Check children sizes
        subdir1_node = next((c for c in root.children if c.name == "subdir1"), None)
        self.assertIsNotNone(subdir1_node)
        self.assertEqual(subdir1_node.size, 500)  # 200 + 300

        subdir2_node = next((c for c in root.children if c.name == "subdir2"), None)
        self.assertIsNotNone(subdir2_node)
        self.assertEqual(subdir2_node.size, 400)

        file1_node = next((c for c in root.children if c.name == "file1.txt"), None)
        self.assertIsNotNone(file1_node)
        self.assertEqual(file1_node.size, 100)

    def test_exclude_filter(self):
        scanner = Scanner(self.test_dir, excludes=["*.bin", "subsubdir"])
        root = scanner.scan()

        # With file4.bin (400) and subsubdir (300) excluded, size should be 100 + 200 = 300 bytes
        self.assertEqual(root.size, 300)

    def test_json_export_and_import(self):
        scanner = Scanner(self.test_dir)
        root = scanner.scan()

        exported_dict = root.to_dict()
        json_str = json.dumps(exported_dict)
        imported_dict = json.loads(json_str)

        imported_root = Node.from_dict(imported_dict)
        imported_root.recalculate()

        self.assertEqual(imported_root.size, root.size)
        self.assertEqual(imported_root.name, root.name)
        self.assertEqual(len(imported_root.children), len(root.children))

    def test_sorting_logic(self):
        scanner = Scanner(self.test_dir)
        root = scanner.scan()
        app = NcduApp(root)

        # Sort by size desc (default)
        app.sort_key = "size"
        app.sort_reverse = True
        app._update_item_list()
        self.assertEqual(app.items[0].name, "subdir1")  # 500 bytes
        self.assertEqual(app.items[1].name, "subdir2")  # 400 bytes
        self.assertEqual(app.items[2].name, "file1.txt")  # 100 bytes

        # Sort by size asc
        app.sort_reverse = False
        app._update_item_list()
        self.assertEqual(app.items[0].name, "file1.txt")
        self.assertEqual(app.items[1].name, "subdir2")
        self.assertEqual(app.items[2].name, "subdir1")

        # Sort by name asc
        app.sort_key = "name"
        app.sort_reverse = False
        app._update_item_list()
        self.assertEqual([i.name for i in app.items], ["file1.txt", "subdir1", "subdir2"])

    def test_node_deletion_recalculation(self):
        scanner = Scanner(self.test_dir)
        root = scanner.scan()
        app = NcduApp(root)

        # Find file1.txt and remove
        file1 = next(c for c in root.children if c.name == "file1.txt")
        root.remove_child(file1)
        root.recalculate_upwards()

        self.assertEqual(root.size, 900)

    def test_multithreaded_vs_sequential(self):
        scanner_seq = Scanner(self.test_dir, threads=1)
        root_seq = scanner_seq.scan()

        scanner_mt = Scanner(self.test_dir, threads=4)
        root_mt = scanner_mt.scan()

        self.assertEqual(root_seq.size, root_mt.size)
        self.assertEqual(root_seq.item_count, root_mt.item_count)
        self.assertEqual(len(root_seq.children), len(root_mt.children))

    def test_100_files_calibration(self):
        # Create a directory with 120 files to verify calibration trigger
        bulk_dir = os.path.join(self.test_dir, "bulk")
        os.makedirs(bulk_dir)
        for i in range(120):
            with open(os.path.join(bulk_dir, f"file_{i}.txt"), "wb") as f:
                f.write(b"X" * 10)

        scanner = Scanner(bulk_dir, threads=2)
        root = scanner.scan()

        self.assertIsNotNone(scanner.calibrated_rate)
        self.assertGreater(scanner.calibrated_rate, 0)
        self.assertEqual(root.item_count, 120)

    def test_cancellation_with_stop_event(self):
        # Create a large tree
        nested_dir = os.path.join(self.test_dir, "nested_cancel")
        os.makedirs(nested_dir)
        for i in range(50):
            sub = os.path.join(nested_dir, f"sub_{i}")
            os.makedirs(sub)
            with open(os.path.join(sub, "data.bin"), "wb") as f:
                f.write(b"0" * 100)

        scanner = Scanner(nested_dir, threads=4)
        # Pre-set stop event or trigger stop immediately
        scanner.stop()
        result = scanner.scan()
        self.assertIsNone(result)
        self.assertTrue(scanner.stop_event.is_set())

    def test_thread_status_tracking(self):
        scanner = Scanner(self.test_dir, threads=3)
        scanner.scan()
        items, rate, elapsed, thread_status = scanner.get_thread_status()
        self.assertGreater(items, 0)
        self.assertEqual(len(thread_status), 3)
        for tid in range(3):
            self.assertIn(tid, thread_status)
            self.assertIn("status", thread_status[tid])
            self.assertIn("items", thread_status[tid])
            self.assertIn("path", thread_status[tid])


if __name__ == "__main__":
    unittest.main()
