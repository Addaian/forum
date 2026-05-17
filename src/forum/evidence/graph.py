"""Build a module-level NetworkX DiGraph from a RepoIndex."""
from __future__ import annotations

import networkx as nx

from .languages import Language, get_language
from .utils import RepoIndex


def build_import_graph(index: RepoIndex, language: Language | None = None) -> nx.DiGraph:
    """Nodes are module qualnames; edges go from importer to imported.

    Dispatches import parsing to the Language that built the index.
    """
    lang = language or get_language(index.language)
    g = nx.DiGraph()
    for qn, mi in index.modules.items():
        g.add_node(qn, file=str(mi.path), package=mi.package)
    for qn, mi in index.modules.items():
        raw = lang.parse_imports(mi.path, qn)
        for target in lang.internal_imports(qn, raw, index):
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
