"""Build a module-level NetworkX DiGraph from a RepoIndex."""
from __future__ import annotations

import networkx as nx

from .utils import RepoIndex, internal_imports, parse_imports


def build_import_graph(index: RepoIndex) -> nx.DiGraph:
    """Nodes are module qualnames; edges go from importer to imported."""
    g = nx.DiGraph()
    for qn, mi in index.modules.items():
        g.add_node(qn, file=str(mi.path), package=mi.package)
    for qn, mi in index.modules.items():
        raw = parse_imports(mi.path, qn)
        for target in internal_imports(qn, raw, index):
            if target == qn:
                continue
            g.add_edge(qn, target)
    return g


def graph_summary(g: nx.DiGraph) -> dict:
    return {
        "num_modules": g.number_of_nodes(),
        "num_edges": g.number_of_edges(),
        "num_packages": len({d.get("package") for _, d in g.nodes(data=True)}),
    }
