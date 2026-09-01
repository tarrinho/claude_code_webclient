"""QA: the diagrams in ARCHITECTURE.md must describe the system that exists.

A wrong diagram is worse than none: it is read as authoritative and it is not
executable, so nothing contradicts it. Two were wrong here at once.

The request-pipeline diagram defined `MW_C` twice -- the second label began with
the stray "K" of its intended id, `MW_CK[...]` having been typed as
`MW_C[K ...]` -- and its flow line referenced `MW_CK`, which therefore existed
nowhere. Mermaid does not complain: it silently overwrote the first node's
label and invented an empty one for the dangling reference. The rendered picture
was wrong in two places and the source looked fine.

Separately, both that diagram and the §3 prose listed the middleware as
CORS -> Security -> CSRF -> Auth. Starlette applies middleware in reverse
registration order, so the real chain is Security -> Auth -> CSRF -> CORS. The
documented order inverted the Auth/CSRF relationship, which is the one thing a
reader consults this section to settle.

These tests check structure and the claims that can be checked against code.
They do not check whether a diagram is a *good* picture.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "ARCHITECTURE.md"
SOURCE = ARCH.read_text(encoding="utf-8")

# ``` blocks tagged mermaid, with the fence stripped.
BLOCKS = re.findall(r"```mermaid\n(.*?)\n```", SOURCE, re.DOTALL)

# `id[Label]`, `id(Label)`, `id[(Label)]`, `id{Label}`, `id([Label])`
NODE_DEF = re.compile(r"(\w+)\s*(?:\[\(|\(\[|\[|\(|\{)")
# Edges: A --> B, A -->|text| B, A -.-> B, A --- B
EDGE = re.compile(r"(\w+)\s*(?:--+>|-\.-+>|--+|===+>)\s*(?:\|[^|]*\|\s*)?(\w+)")


def defined_ids(block: str) -> set[str]:
    ids = set()
    for line in block.splitlines():
        line = line.strip()
        if line.startswith(("style", "subgraph", "%%", "note", "end")):
            continue
        for m in NODE_DEF.finditer(line):
            ids.add(m.group(1))
    for m in re.finditer(r'subgraph\s+(\w+)\s*\[', block):
        ids.add(m.group(1))
    return ids


class DiagramsExistTests(unittest.TestCase):
    def test_there_are_diagrams_to_check(self):
        """A find-nothing bug would make every assertion below vacuous."""
        self.assertGreaterEqual(len(BLOCKS), 6)


class DiagramStructureTests(unittest.TestCase):
    def test_no_edge_points_at_an_undefined_node(self):
        """The exact defect that survived review: a dangling `MW_CK`.

        Mermaid renders an undefined reference as a blank node rather than
        failing, so this cannot be caught by looking at the picture either.
        """
        problems = []
        for index, block in enumerate(BLOCKS):
            head = block.strip().splitlines()[0]
            if not head.startswith(("graph", "flowchart")):
                continue        # sequence/state diagrams declare differently
            ids = defined_ids(block)
            # Blank out label contents first. `claude CLI\n--dangerously-skip`
            # and `systemd --user` parse as edges otherwise, and the resulting
            # "undefined node" is the scanner's invention, not the diagram's.
            stripped = re.sub(r"\[[^\]]*\]|\{[^}]*\}|\([^)]*\)|\|[^|]*\|",
                              " ", block)
            for m in EDGE.finditer(stripped):
                for ref in (m.group(1), m.group(2)):
                    if ref not in ids and not ref.isdigit():
                        problems.append(f"block {index}: `{ref}` is never defined")
        self.assertEqual(problems, [])

    def test_no_node_id_is_defined_twice_with_different_labels(self):
        """`MW_C` was defined twice; the second silently won."""
        problems = []
        for index, block in enumerate(BLOCKS):
            labels: dict[str, str] = {}
            for m in re.finditer(r"(\w+)\[([^\]]+)\]", block):
                node, label = m.group(1), m.group(2)
                if node in labels and labels[node] != label:
                    problems.append(
                        f"block {index}: `{node}` defined as "
                        f"{labels[node]!r} and {label!r}")
                labels[node] = label
        self.assertEqual(problems, [])

    def test_every_block_declares_a_diagram_type(self):
        for index, block in enumerate(BLOCKS):
            with self.subTest(block=index):
                head = block.strip().splitlines()[0].split()[0]
                self.assertIn(head.rstrip("TBLR"), {
                    "graph", "flowchart", "sequenceDiagram",
                    "stateDiagram-v2", "erDiagram", "classDiagram"})

    def test_fences_are_balanced(self):
        self.assertEqual(SOURCE.count("```") % 2, 0, "an unclosed code fence")


class DiagramClaimsTests(unittest.TestCase):
    """Claims a diagram makes that the code can confirm or deny."""

    def test_the_middleware_order_matches_the_application(self):
        """Starlette applies middleware in reverse registration order.

        Asserted against `app.py`'s registration rather than restating the
        list, so transposing two `add_middleware` calls fails here.
        """
        app_src = (ROOT / "app.py").read_text(encoding="utf-8")
        registered = re.findall(r"add_middleware\(\s*(\w+)", app_src)
        outermost_first = list(reversed(registered))
        documented = re.search(r'Middleware Stack\\n([^"]+)"', SOURCE)
        self.assertIsNotNone(documented, "the §2.1 stack label is gone")
        names = [n.strip().lower() for n in documented.group(1).split("→")]
        expected = [n.removesuffix("Middleware").lower() for n in outermost_first]
        self.assertEqual(
            names, expected,
            "the documented middleware order is not the applied one",
        )

    def test_the_storage_map_names_the_supervisor_tables(self):
        """It listed five tables for years while the schema grew to 22."""
        storage = SOURCE[SOURCE.index("### 2.4"):SOURCE.index("### 2.5")]
        for table in ("supervisors", "supervisor_tasks", "read_marks",
                      "system_samples", "turn_queue"):
            with self.subTest(table=table):
                self.assertIn(table, storage)

    def test_the_documented_tables_exist_in_the_schema(self):
        """The other direction: a diagram must not invent a table."""
        db_src = (ROOT / "db.py").read_text(encoding="utf-8")
        storage = SOURCE[SOURCE.index("### 2.4"):SOURCE.index("### 2.5")]
        for m in re.finditer(r"\[\((\w+)", storage):
            name = m.group(1)
            with self.subTest(table=name):
                self.assertIn(name, db_src,
                              f"the storage map names {name}, db.py does not")


if __name__ == "__main__":
    unittest.main()
