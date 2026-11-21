"""Phase 12 layout probe: repeatable offscreen rendering checks (NOT a DPI review).

Renders every core surface at several window sizes and logical scale factors
and records, for each (size, scale, surface) triple:

* labels clipped by their own layout (text cut off),
* widgets overflowing the window horizontally,
* the page's core actions being visible and inside the window,
* the investigation workspace's region sizes in both inspector modes,
* a PNG per surface for human review.

This is an automated, repeatable check from a clean checkout. It is NOT a
Windows DPI acceptance: the offscreen platform does not load native fonts
(Chinese text may render as boxes, which skews width metrics) and no native
control metrics are exercised. A green row here never replaces the human
Windows DPI review.

Repeatable commands (from the repository root, with the project venv)::

    # full standard matrix: 3 sizes x 5 scales, one child process per scale
    & .\\.venv\\Scripts\\python.exe -s tools/verification/phase12_layout_probe.py --matrix

    # one scale, several sizes, custom output directory
    & .\\.venv\\Scripts\\python.exe -s tools/verification/phase12_layout_probe.py \
        --scale 1.25 --sizes 1280x720 1920x1080 --out .verification/phase12-layout

Output (JSON rows + PNGs) is written under ``--out`` (default: the git-ignored
``.verification/phase12-layout`` directory). Screenshots are artifacts; they
are not meant to be committed.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIZES = [(1280, 720), (1920, 1080), (2560, 1440)]
DEFAULT_SCALES = [1.0, 1.25, 1.5, 1.75, 2.0]
PAGE_NAMES = ["dashboard", "people", "events", "workspace-timeline", "graph", "analysis", "settings"]


def parse_size(text):
    width, _, height = text.partition("x")
    return int(width), int(height)


# ------------------------------------------------------------------ checks

def clipped_labels(widget):
    """Labels whose own sizeHint does not fit their layout width (text cut)."""
    from PySide6.QtWidgets import QLabel
    offenders = []
    for child in widget.findChildren(QLabel):
        if not child.isVisible() or not child.text().strip():
            continue
        if child.wordWrap() or child.pixmap() is not None:
            continue
        needed = child.sizeHint().width()
        if needed > child.width() + 2:
            offenders.append({"object": child.objectName() or child.text()[:24],
                              "needed": needed, "actual": child.width()})
    return offenders


def horizontal_overflow(widget):
    """Scroll areas whose content needs horizontal scrolling on this window."""
    from PySide6.QtWidgets import QAbstractScrollArea
    reports = []
    for child in widget.findChildren(QAbstractScrollArea):
        if not child.isVisible():
            continue
        bar = child.horizontalScrollBar()
        if bar.isVisible() or bar.maximum() > 0:
            reports.append({"object": child.objectName() or type(child).__name__,
                            "overflow": bar.maximum()})
    return reports


def action_checks(page, name):
    """The page's core actions must be visible and inside the window."""
    expected = []
    if name == "people":
        expected = ["people_list", "search", "include_archived", "inspector_mode_button",
                    "edit_button", "archive_button", "analyze_button"]
    elif name == "events":
        # workspace_timeline is checked on its own surface (the other tab).
        expected = ["create_button", "edit_button", "delete_button", "tabs"]
    elif name == "dashboard":
        expected = ["refresh_button", "include_deleted", "dashboardRecentPeople",
                    "dashboardRecentEvents"]
    elif name == "graph":
        expected = ["refresh_button", "type_filter", "state_filter", "show_archived", "show_deleted"]
    elif name == "analysis":
        expected = ["history_table"]
    elif name == "settings":
        # The page-local workspace_combo is the documented hidden proxy
        # (Phase 11); the section navigation is the visible control surface.
        expected = ["section_nav"]
    elif name == "workspace-timeline":
        expected = ["workspace_timeline"]
    missing, hidden = [], []
    for attribute in expected:
        widget = getattr(page, attribute, None)
        if widget is None:
            missing.append(attribute)
            continue
        if not widget.isVisible():
            hidden.append(attribute)
    return missing, hidden


def region_sizes(page, name):
    """Investigation workspace regions must keep usable space."""
    if name != "people":
        return {}
    inner = getattr(page, "inner_split", None)
    outer = getattr(page, "outer_split", None)
    report = {"inspector_mode": getattr(page, "inspector_mode", None)}
    if inner is not None:
        report["inner_split"] = [inner.sizes()[i] if i < len(inner.sizes()) else 0
                                 for i in range(inner.count())]
    if outer is not None:
        report["outer_split"] = outer.sizes()
    return report


def inspect_surface(window, page, name):
    from PySide6.QtWidgets import QApplication
    QApplication.processEvents()
    missing, hidden = action_checks(page, name)
    return {
        "surface": name,
        "window": [window.width(), window.height()],
        "dpr": window.devicePixelRatioF(),
        "clipped_labels": clipped_labels(page),
        "horizontal_overflow": horizontal_overflow(page),
        "actions_missing": missing,
        "actions_hidden": hidden,
        "regions": region_sizes(page, name),
    }


def dialog_bounds(window):
    """Representative dialogs must fit the tested window width."""
    from app.ui.person_dialog import PersonDialog
    from app.ui.event_dialog import EventDialog
    reports = []
    for factory in (lambda: PersonDialog(), lambda: EventDialog([])):
        dialog = factory()
        hint = dialog.sizeHint()
        reports.append({"dialog": type(dialog).__name__,
                        "hint": [hint.width(), hint.height()],
                        "fits_window": hint.width() <= window.width()})
        dialog.close()
        dialog.deleteLater()
    return reports


# ------------------------------------------------------------------ driver

def run_matrix(args):
    """One child process per scale: QT_SCALE_FACTOR must precede QApplication."""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    sizes = " ".join(f"{w}x{h}" for w, h in args.sizes)
    aggregate = {"runs": [], "note": "offscreen rendering check - NOT a Windows DPI acceptance"}
    for scale in args.scales:
        command = [sys.executable, "-s", str(Path(__file__).resolve()),
                   "--scale", str(scale), "--sizes", *sizes.split(), "--out", str(out)]
        if args.strict:
            command.append("--strict")
        completed = subprocess.run(command, cwd=str(ROOT), capture_output=True, text=True)
        sys.stdout.write(completed.stdout)
        if completed.returncode != 0:
            sys.stderr.write(completed.stderr)
            aggregate["runs"].append({"scale": scale, "error": completed.stderr[-500:]})
        else:
            aggregate["runs"].append(json.loads(completed.stdout))
    summary_path = out / "summary.json"
    summary_path.write_text(json.dumps(aggregate, ensure_ascii=False, indent=1), encoding="utf-8")
    findings = sum(len(run.get("rows", [])) and sum(
        len(row["clipped_labels"]) + len(row["horizontal_overflow"])
        + len(row["actions_missing"]) + len(row["actions_hidden"])
        for row in run.get("rows", [])) or 0 for run in aggregate["runs"])
    print(f"summary -> {summary_path}")
    print(f"findings: {findings} (offscreen metrics are advisory; see --strict)")
    return 1 if (args.strict and findings) else 0


def run_one_scale(args):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = str(args.scale)
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    sys.path.insert(0, str(ROOT))
    from app.config.settings import AppConfig
    from app.services.application import create_application_services
    from app.services.database import initialize_database
    from app.ui.main_window import MainWindow

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    dialogs = []
    for width, height in args.sizes:
        data = out / "data"
        config = AppConfig(data_dir=data)
        runtime = initialize_database(config)
        services = create_application_services(config, runtime.sessions)
        window = MainWindow(config, services)
        window.resize(width, height)
        window.show()
        app.processEvents()
        try:
            for index, name in enumerate(PAGE_NAMES):
                if name == "workspace-timeline":
                    window.navigation.setCurrentRow(2)          # Events page
                    app.processEvents()
                    window.events_page.tabs.setCurrentIndex(1)  # Workspace Timeline tab
                    page = window.events_page
                elif name == "people":
                    window.navigation.setCurrentRow(1)
                    app.processEvents()
                    page = window.people_page
                    page.set_inspector_mode("side")
                    app.processEvents()
                else:
                    window.navigation.setCurrentRow(
                        {"dashboard": 0, "events": 2, "graph": 3, "analysis": 4,
                         "settings": 5}[name])
                    app.processEvents()
                    page = window.pages.currentWidget()
                row = inspect_surface(window, page, name)
                row["size"] = [width, height]
                row["scale"] = args.scale
                rows.append(row)
                image = window.grab()
                image.save(str(out / f"scale{args.scale:g}-{width}x{height}-{name}.png"))
            # The compact inspector mode is a second layout state of People.
            window.navigation.setCurrentRow(1)
            app.processEvents()
            window.people_page.set_inspector_mode("bottom")
            app.processEvents()
            compact = inspect_surface(window, window.people_page, "people")
            compact["surface"] = "people-bottom-inspector"
            compact["size"] = [width, height]
            compact["scale"] = args.scale
            rows.append(compact)
            window.grab().save(str(out / f"scale{args.scale:g}-{width}x{height}-people-bottom.png"))
            dialogs.extend(dialog_bounds(window))
        finally:
            window.close()
            runtime.close()
            app.processEvents()
    payload = {"scale": args.scale, "rows": rows, "dialogs": dialogs,
               "note": "offscreen rendering check - NOT a Windows DPI acceptance"}
    (out / f"rows-{args.scale:g}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    findings = sum(len(row["clipped_labels"]) + len(row["horizontal_overflow"])
                   + len(row["actions_missing"]) + len(row["actions_hidden"]) for row in rows)
    findings += sum(1 for item in dialogs if not item["fits_window"])
    return 1 if (args.strict and findings) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=float, default=1.0, help="QT_SCALE_FACTOR for this run")
    parser.add_argument("--sizes", nargs="+", default=[f"{w}x{h}" for w, h in DEFAULT_SIZES],
                        help="window sizes, e.g. 1280x720")
    parser.add_argument("--out", default=str(ROOT / ".verification" / "phase12-layout"),
                        help="output directory for JSON rows and PNGs (git-ignored by default)")
    parser.add_argument("--matrix", action="store_true",
                        help="run the standard matrix (3 sizes x 5 scales) in child processes")
    parser.add_argument("--scales", nargs="+", type=float, default=DEFAULT_SCALES,
                        help="scales for --matrix")
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any finding is reported")
    args = parser.parse_args()
    args.sizes = [parse_size(text) for text in args.sizes]
    if args.matrix:
        return run_matrix(args)
    return run_one_scale(args)


if __name__ == "__main__":
    sys.exit(main())
