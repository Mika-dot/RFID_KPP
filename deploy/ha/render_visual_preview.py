"""Render the actual Grafana Charts-function graphics with labelled synthetic data.

This preview evaluates the shipped JavaScript with a Grafana-shaped data frame,
then renders the returned graphic primitives as SVG. It is not a live Grafana
or factory screenshot. Node is a development dependency only.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from deploy.ha import install_monitoring as monitor


def sample(scenario="normal"):
    nodes = []
    for node in monitor.NODES:
        active = node == ("perimetr" if scenario == "physical-down" else "physical") and scenario != "no-sql"
        unavailable = node == "physical" and scenario == "physical-down"
        value = dict(node=node, label=monitor.LABELS[node], reachable=0 if unavailable else 1,
                     active=int(active), healthy=int(active), prepared=0 if unavailable or scenario == "no-sql" else 1,
                     faulted=int(unavailable), epoch=42, http=503 if unavailable else 200,
                     severity=1 if unavailable else 0, role="НЕДОСТУПЕН" if unavailable else "ВЕДУЩИЙ" if active else "РЕЗЕРВ",
                     detail="СИНТЕТИЧЕСКИЙ ПРИМЕР", release="a" * 40,
                     snapshot_fresh=True, bus_age=1, bus={"rfid_pending": 0, "video_pending": 0},
                     mirror={"enabled": True, "retention_days": 93,
                             "streams": {key: {"age_sec": 1, "caught_up": True, "records": 100}
                                         for key in monitor.MIRROR_STREAMS}},
                     controller={"owner": "comparator", "valid": scenario != "no-sql", "at": time.time()},
                     repair={"verification_required": False, "verified_sec": 0},
                     update={"pending": False, "quarantined": 0},
                     correlation={"mode": "shadow"})
        value["services"] = {name: {"ok": active, "observed": active,
                                   "dependencies": {key: "ok" for key in ("rfid_reader", "camera_0", "camera_1", "source_database", "database")}}
                             for name in ("RfidReader", "Yolo", "RusGuardSync", "Aggregator", "WebDashboard")}
        nodes.append(value)
    data = monitor.summarize(nodes, (0, "Без предупреждений"))
    data["gateway"] = {"ready": scenario != "no-sql", "observed": True}
    data["stale"] = scenario == "stale"
    return data


def evaluate(rows, width=1800, height=850):
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("NodeRequiredForDevelopmentPreview")
    script = Path(__file__).with_name("perimeter_topology.js").read_text(encoding="utf-8")
    names = ["id", "kind", "label", "detail", "state", "x", "y", "w", "h", "source", "target"]
    frame = {"length": len(rows), "fields": [{"name": name, "values": [row.get(name) for row in rows]} for name in names]}
    wrapper = """const fs=require('fs');const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context={panel:{data:{series:[input.frame]},chart:{getWidth:()=>input.width,getHeight:()=>input.height}}};
const result=new Function('context',input.script)(context);process.stdout.write(JSON.stringify(result));"""
    result = subprocess.run([node, "-e", wrapper], input=json.dumps({"script": script, "frame": frame, "width": width, "height": height}),
                            text=True, capture_output=True, check=True, timeout=10)
    return json.loads(result.stdout)


def render(options, output, width=1800, height=850):
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root = ET.Element("{http://www.w3.org/2000/svg}svg", width=str(width), height=str(height + 44), viewBox=f"0 0 {width} {height + 44}")
    ET.SubElement(root, "rect", width=str(width), height=str(height + 44), fill="#0d1622")
    primitives = options["graphic"][0]["children"]
    for item in sorted(primitives, key=lambda value: value.get("z", 0)):
        kind, shape, style = item["type"], item.get("shape", {}), item.get("style", {})
        attrs = {"fill": style.get("fill") or "none", "stroke": style.get("stroke") or "none",
                 "stroke-width": str(style.get("lineWidth", 0)), "opacity": str(style.get("opacity", 1))}
        if style.get("lineDash"):
            attrs["stroke-dasharray"] = " ".join(str(x) for x in style["lineDash"])
        if kind == "rect":
            attrs.update({key: str(shape[key]) for key in ("x", "y", "width", "height")})
            attrs["rx"] = str(shape.get("r", 0))
        elif kind == "circle":
            attrs.update({key: str(shape[key]) for key in ("cx", "cy", "r")})
        elif kind in {"polygon", "polyline"}:
            attrs["points"] = " ".join(",".join(str(number) for number in point) for point in shape["points"])
        elif kind == "text":
            attrs.update(x=str(item["x"]), y=str(item["y"]), style="font:" + style["font"], **{"dominant-baseline": "middle"})
        element = ET.SubElement(root, kind, attrs)
        if kind == "text":
            element.text = style["text"]
    note = ET.SubElement(root, "text", x="30", y=str(height + 25), fill="#8294a8", style="font:14px sans-serif")
    note.text = "Синтетический макет из штатного JS панели • не текущий статус оборудования"
    Path(output).write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=["normal", "physical-down", "no-sql", "stale"], default="normal")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(evaluate(monitor.diagram_rows(sample(args.scenario))), args.output)
    print("SYNTHETIC_VISUAL_PREVIEW_RENDERED")


if __name__ == "__main__":
    raise SystemExit(main())
