"""Update the existing ub22 wallboard, add a visual page and a one-minute playlist.

--render-only builds reviewable JSON without server access. --plan reads the live
configuration. --apply uses the existing local credential, checks a clean exact
release, backs up the configuration and restores our writes on verification
failure. It never stops Guardian/workers or connects to production SQL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from deploy.ha import grafana_visual as visual
from deploy.ha import install_monitoring as monitor

GRAFANA = "http://127.0.0.1:3000"
PLAYLIST_API = "/apis/playlist.grafana.app/v1/namespaces/default/playlists"


def optional(api, route):
    try:
        return api(route)
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def save_dashboard(api, dashboard, metadata):
    body = {"dashboard": dashboard, "overwrite": False, "message": visual.MANAGED_TAG}
    if metadata.get("folderUid"):
        body["folderUid"] = metadata["folderUid"]
    elif "folderId" in metadata:
        body["folderId"] = metadata["folderId"]
    return api("/api/dashboards/db", body)


def query_panel(api, panel):
    now = int(time.time() * 1000)
    body = {"from": str(now - 300000), "to": str(now), "queries": panel["targets"]}
    value = api("/api/ds/query", body)
    for target in panel["targets"]:
        result = value.get("results", {}).get(target["refId"], {})
        if result.get("error") or not result.get("frames"):
            raise RuntimeError("GrafanaBackendFramesMissing")


def backup_json(folder, name, value):
    path = folder / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)


def restore_dashboard(api, old, uid, written_version):
    current = api("/api/dashboards/uid/" + uid)
    if current["dashboard"].get("version") != written_version:
        raise RuntimeError("RollbackSkippedConcurrentDashboardEdit")
    if old is None:
        api("/api/dashboards/uid/" + uid, method="DELETE")
    else:
        dashboard = dict(old["dashboard"], version=written_version)
        save_dashboard(api, dashboard, old.get("meta", {}))


def update(api, apply=False, banner_panel_id=None, backup_root=None, collector_update=None):
    overview = api("/api/dashboards/uid/" + visual.OVERVIEW_UID)
    if not overview.get("meta", {}).get("canSave"):
        raise RuntimeError("ExistingDashboardNotWritable")
    datasource = api("/api/datasources/uid/wallboard-api")
    if datasource.get("type") != visual.DS["type"] or "http://127.0.0.1:19150" not in datasource.get("jsonData", {}).get("allowedHosts", []):
        raise RuntimeError("ExistingInfinityDatasourceRequired")
    plugins = api("/api/plugins")
    if not any(plugin.get("id") == "volkovlabs-echarts-panel" for plugin in plugins):
        raise RuntimeError("ExistingBusinessChartsPluginRequired")
    old_visual = optional(api, "/api/dashboards/uid/" + visual.VISUAL_UID)
    old_playlist = optional(api, PLAYLIST_API + "/" + visual.PLAYLIST_UID)
    cleaned, removed = visual.remove_top_banner(overview["dashboard"], banner_panel_id)
    new_overview = monitor.make_panels(cleaned)
    links = new_overview.setdefault("links", [])
    managed_urls = {"/d/" + visual.VISUAL_UID + "?kiosk", "/playlists/play/" + visual.PLAYLIST_UID + "?kiosk"}
    new_overview["links"] = [link for link in links if link.get("url") not in managed_urls] + [
        {"title": "Периметр • схема", "type": "link", "url": "/d/" + visual.VISUAL_UID + "?kiosk", "targetBlank": False},
        {"title": "Чередование • 1 мин", "type": "link", "url": "/playlists/play/" + visual.PLAYLIST_UID + "?kiosk", "targetBlank": False},
    ]
    new_visual = visual.visual_dashboard(old_visual["dashboard"] if old_visual else None)
    new_playlist = visual.playlist(old_playlist)
    report = {"applied": False, "removed_banner_ids": removed,
              "banner_detection": "removed_text_panel" if removed else "no_top_text_panel_detected; kiosk hides Grafana header",
              "interval": "1m", "refresh": "5s", "visual_panels": len(new_visual["panels"]),
              "overview_url": "/d/" + visual.OVERVIEW_UID + "?kiosk",
              "perimeter_url": "/d/" + visual.VISUAL_UID + "?kiosk",
              "rotation_url": "/playlists/play/" + visual.PLAYLIST_UID + "?kiosk"}
    if not apply:
        return report
    folder = Path(backup_root or "/var/lib/perimeter-ha-monitor/backups") / ("visual-" + str(time.time_ns()))
    folder.mkdir(parents=True, mode=0o700)
    for name, value in (("overview.json", overview), ("perimeter.json", old_visual), ("playlist.json", old_playlist)):
        backup_json(folder, name, value)
    written = []
    collector_rollback = None
    playlist_written = None
    try:
        if collector_update:
            collector_rollback = collector_update(folder)
        current = api("/api/dashboards/uid/" + visual.OVERVIEW_UID)
        if current["dashboard"].get("version") != overview["dashboard"].get("version"):
            raise RuntimeError("ConcurrentOverviewEdit")
        result = save_dashboard(api, new_overview, overview["meta"])
        written.append((overview, visual.OVERVIEW_UID, result["version"]))
        result = save_dashboard(api, new_visual, (old_visual or overview).get("meta", {}))
        written.append((old_visual, visual.VISUAL_UID, result["version"]))
        playlist_written = api(PLAYLIST_API + ("/" + visual.PLAYLIST_UID if old_playlist else ""),
                               new_playlist, method="PUT" if old_playlist else "POST")
        saved_playlist = api(PLAYLIST_API + "/" + visual.PLAYLIST_UID)
        if saved_playlist.get("spec") != new_playlist["spec"]:
            raise RuntimeError("PlaylistSaveNotConfirmed")
        saved = api("/api/dashboards/uid/" + visual.VISUAL_UID)
        if saved["dashboard"].get("tags") != new_visual["tags"]:
            raise RuntimeError("VisualDashboardSaveNotConfirmed")
        for panel in saved["dashboard"]["panels"]:
            query_panel(api, panel)
        report.update(applied=True, backend_queries_verified=True, backup=str(folder),
                      versions={uid: version for _old, uid, version in written})
        backup_json(folder, "receipt.json", report)
        return report
    except Exception:
        rollback_errors = []
        if playlist_written:
            try:
                current = api(PLAYLIST_API + "/" + visual.PLAYLIST_UID)
                if current["metadata"]["resourceVersion"] != playlist_written["metadata"]["resourceVersion"]:
                    raise RuntimeError("RollbackSkippedConcurrentPlaylistEdit")
                if old_playlist:
                    restored = dict(old_playlist, metadata={**old_playlist["metadata"], "resourceVersion": current["metadata"]["resourceVersion"]})
                    api(PLAYLIST_API + "/" + visual.PLAYLIST_UID, restored, method="PUT")
                else:
                    api(PLAYLIST_API + "/" + visual.PLAYLIST_UID, method="DELETE")
            except Exception as exc:
                rollback_errors.append(type(exc).__name__)
        for old, uid, version in reversed(written):
            try:
                restore_dashboard(api, old, uid, version)
            except Exception as exc:
                rollback_errors.append(type(exc).__name__)
        if collector_rollback:
            try:
                collector_rollback()
            except Exception as exc:
                rollback_errors.append(type(exc).__name__)
        backup_json(folder, "rollback.json", {"errors": rollback_errors, "complete": not rollback_errors})
        if rollback_errors:
            print("WALLBOARD_ROLLBACK_INCOMPLETE backup=" + str(folder), file=sys.stderr)
        raise


def update_collector(folder):
    destination = monitor.SCRIPT
    if destination.is_symlink() or not destination.is_file() or monitor.MARKER not in destination.read_text():
        raise RuntimeError("ExistingManagedCollectorRequired")
    dropin = Path("/etc/systemd/system/mositlab-wallboard.service.d/90-perimeter-ha.conf")
    if not dropin.is_file() or monitor.MARKER not in dropin.read_text():
        raise RuntimeError("ExistingWallboardProxyRequired")
    original = destination.read_bytes()
    incoming = Path(monitor.__file__).read_bytes()
    (folder / "monitor.py").write_bytes(original)
    baseline = monitor.http_json("http://127.0.0.1:19150/projects")[1]
    def restore():
        if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(incoming).digest():
            raise RuntimeError("CollectorChangedDuringUpdate")
        destination.write_bytes(original)
        destination.chmod(0o644)
        monitor.command(["systemctl", "restart", monitor.UNIT])
        monitor.command(["systemctl", "restart", "mositlab-wallboard.service"])
    try:
        destination.write_bytes(incoming)
        destination.chmod(0o644)
        monitor.command(["systemctl", "restart", monitor.UNIT])
        monitor.command(["systemctl", "restart", "mositlab-wallboard.service"])
        deadline = time.monotonic() + 25
        while True:
            try:
                current = monitor.http_json("http://127.0.0.1:19150/projects")[1]
                diagram = monitor.http_json(monitor.WALLBOARD_BASE + "/diagram")[1]
                if {row.get("project") for row in baseline} != {row.get("project") for row in current}:
                    raise RuntimeError("ExistingProjectsChanged")
                if not isinstance(diagram, list) or not any(row.get("kind") == "header" for row in diagram):
                    raise RuntimeError("DiagramCollectorNotReady")
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
    except Exception:
        restore()
        raise
    return restore


def main(argv=None):
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--render-only", action="store_true")
    modes.add_argument("--plan", action="store_true")
    modes.add_argument("--apply", action="store_true")
    parser.add_argument("--release")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--banner-panel-id", type=int)
    args = parser.parse_args(argv)
    if args.render_only:
        if not args.output_dir:
            parser.error("--render-only requires --output-dir")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "perimeter-visual.json").write_text(json.dumps(visual.visual_dashboard(), ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "playlist.json").write_text(json.dumps(visual.playlist(), ensure_ascii=False, indent=2), encoding="utf-8")
        print("VISUAL_JSON_RENDERED")
        return 0
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("RunOnUb22WithSudo")
    if args.apply:
        if not args.release or not re.fullmatch(r"[0-9a-f]{40}", args.release):
            raise ValueError("ExactReleaseRequired")
        if monitor.command(["git", "-C", str(ROOT), "rev-parse", "HEAD"]) != args.release or monitor.command(["git", "-C", str(ROOT), "status", "--porcelain"]):
            raise RuntimeError("CleanExactReleaseRequired")
    token, _ = monitor.read_env()
    def api(route, body=None, method=None):
        return monitor.http_json(GRAFANA + route, token=token, body=body, method=method)[1]
    result = update(api, args.apply, args.banner_panel_id, collector_update=update_collector if args.apply else None)
    result["release"] = args.release
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("VISUAL_WALLBOARD_FAILED", type(exc).__name__, file=sys.stderr)
        raise SystemExit(2)
