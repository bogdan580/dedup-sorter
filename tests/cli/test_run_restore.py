"""CLI wiring of --restore and --verify-manifest."""

from tidy.cli import args as cli_args
from tidy.cli import main as cli_main
from fakes import FakeFileSystem


class TestRestoreAndVerifyManifestCliWiring:
    """--restore/--verify-manifest bypass the normal scan pipeline entirely
    and operate on a manifest from a previous --apply run."""

    def _make_manifest(self, tmp_path):
        from tidy.moves import applier
        from datetime import datetime
        from tidy.duplicates.keeper import Discard, KeeperDecision
        from tidy.duplicates.tiers import DuplicateGroup

        root = str(tmp_path).replace("\\", "/")
        fs = FakeFileSystem()
        fs.add_dir(root)
        keeper_path = f"{root}/keep.jpg"
        discard_path = f"{root}/discard.jpg"
        fs.add_file(keeper_path, b"x", size=1)
        fs.add_file(discard_path, b"x", size=1)
        groups = [DuplicateGroup(size=1, full_hash="h", paths=(keeper_path, discard_path))]
        decisions = [KeeperDecision(
            keeper=keeper_path, discarded=(Discard(path=discard_path, reason="dup-exact"),)
        )]
        result = applier.apply_batch(
            fs, root, groups, decisions, clock=lambda: datetime(2026, 7, 26, 10, 30)
        )
        return fs, result.manifest_path, keeper_path, discard_path

    def test_restore_bypasses_scan_and_restores(self, tmp_path, capsys):
        fs, manifest_path, keeper_path, discard_path = self._make_manifest(tmp_path)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--restore", manifest_path, "--archive-root", str(tmp_path)])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        assert fs.exists(discard_path) is True
        captured = capsys.readouterr()
        assert captured.out.strip() != ""

    def test_verify_manifest_bypasses_scan_and_reports(self, tmp_path, capsys):
        fs, manifest_path, keeper_path, discard_path = self._make_manifest(tmp_path)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--verify-manifest", manifest_path])

        exit_code = cli_main.run(args, fs)

        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out.strip() != ""

    def test_restore_does_not_invoke_scanner(self, tmp_path, monkeypatch):
        from tidy.duplicates import scanner as scanner_module
        fs, manifest_path, *_ = self._make_manifest(tmp_path)

        def boom(*_a, **_k):
            raise AssertionError("scanner.scan() must not be called for --restore")

        monkeypatch.setattr(scanner_module, "scan", boom)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--restore", manifest_path, "--archive-root", str(tmp_path)])

        exit_code = cli_main.run(args, fs)
        assert exit_code == 0

    def test_verify_manifest_does_not_invoke_scanner(self, tmp_path, monkeypatch):
        from tidy.duplicates import scanner as scanner_module
        fs, manifest_path, *_ = self._make_manifest(tmp_path)

        def boom(*_a, **_k):
            raise AssertionError("scanner.scan() must not be called for --verify-manifest")

        monkeypatch.setattr(scanner_module, "scan", boom)
        parser = cli_args.build_arg_parser()
        args = parser.parse_args(["--verify-manifest", manifest_path])

        exit_code = cli_main.run(args, fs)
        assert exit_code == 0
