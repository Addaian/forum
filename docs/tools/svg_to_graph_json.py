#!/usr/bin/env python3
"""Convert each data/<slug>/graph.svg (Graphviz output) into graph.json
suitable for the Sigma+graphology renderer in app.js.

Output schema:
  {
    "nodes": [{"id": "flask.logging", "label": "logging", "pkg": "flask", "x": 27.0, "y": -424.0}, ...],
    "edges": [{"source": "flask.logging", "target": "flask.globals"}, ...]
  }

Coords come from the polygon centroids in the SVG so forceatlas2 can warm-start
from Graphviz's hierarchical layout instead of cold random.
"""
from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"

NODE_RE = re.compile(
    r'<g id="node\d+" class="node">\s*<title>([^<]+)</title>\s*'
    r'<(?:polygon|ellipse)[^/]*(?:points="([^"]+)"|cx="([^"]+)"\s+cy="([^"]+)")',
    re.DOTALL,
)
EDGE_RE = re.compile(
    r'<g id="edge\d+" class="edge">\s*<title>([^<]+)</title>',
    re.DOTALL,
)


def centroid_from_points(points: str) -> tuple[float, float]:
    coords = [p.split(",") for p in points.split()]
    xs = [float(c[0]) for c in coords]
    ys = [float(c[1]) for c in coords]
    return sum(xs) / len(xs), sum(ys) / len(ys)


def parse_svg(svg: str) -> dict:
    nodes: list[dict] = []
    seen: set[str] = set()
    for m in NODE_RE.finditer(svg):
        name = unescape(m.group(1)).strip()
        if name in seen:
            continue
        seen.add(name)
        if m.group(2):  # polygon points
            x, y = centroid_from_points(m.group(2))
        else:  # ellipse cx/cy
            x, y = float(m.group(3)), float(m.group(4))
        pkg = name.split(".", 1)[0] if "." in name else name
        label = name.rsplit(".", 1)[-1]
        nodes.append({"id": name, "label": label, "pkg": pkg, "x": x, "y": -y})

    edges: list[dict] = []
    for m in EDGE_RE.finditer(svg):
        title = unescape(m.group(1)).strip()
        if "->" not in title:
            continue
        src, dst = (s.strip() for s in title.split("->", 1))
        if src in seen and dst in seen:
            edges.append({"source": src, "target": dst})

    return {"nodes": nodes, "edges": edges}


def main() -> None:
    for svg_path in sorted(DATA_DIR.glob("*/graph.svg")):
        graph = parse_svg(svg_path.read_text(encoding="utf-8"))
        out_path = svg_path.with_name("graph.json")
        out_path.write_text(json.dumps(graph, separators=(",", ":")), encoding="utf-8")
        print(f"{svg_path.parent.name}: {len(graph['nodes'])} nodes, "
              f"{len(graph['edges'])} edges -> {out_path.name}")


if __name__ == "__main__":
    main()
