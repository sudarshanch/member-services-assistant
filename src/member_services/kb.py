"""Plan-document index.

A document is admissible for a tenant only when the directory name and the
front-matter tenant agree. Instruction-like lines are removed before any
chunk can reach a prompt. Benefit tables and plan rules are parsed from the
same admissible text the retriever searches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from member_services.money import parse_dollars

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
INJECTION_LINE = re.compile(
    r"coding assistant|do not mention this note|ignore (all |any )?previous|"
    r"system prompt|you are now|BEGIN (HIDDEN|INSTRUCTION)",
    re.IGNORECASE,
)
TOKEN = re.compile(r"[a-z0-9]{3,}")


@dataclass
class BenefitRow:
    name: str
    in_network: str
    out_of_network: str


@dataclass
class Chunk:
    tenant: str
    product_code: str
    plan_year: int | None
    source: str
    title: str
    text: str


@dataclass
class PlanRules:
    grievance_resolution_days: int
    appeal_window_days: int


@dataclass
class KnowledgeBase:
    chunks: list[Chunk] = field(default_factory=list)
    benefits: dict[tuple[str, str, int], list[BenefitRow]] = field(default_factory=dict)
    rules: dict[str, PlanRules] = field(default_factory=dict)
    carc: dict[str, dict[str, str]] = field(default_factory=dict)
    quarantined: list[dict] = field(default_factory=list)
    stripped_instruction_lines: int = 0

    @classmethod
    def load(cls, kb_dir: Path) -> "KnowledgeBase":
        index = cls()
        if not kb_dir.is_dir():
            return index
        for path in sorted(kb_dir.glob("*/*.md")):
            index._add(path)
        return index

    def _add(self, path: Path) -> None:
        raw = path.read_text(encoding="utf-8")
        match = FRONT_MATTER.match(raw)
        if not match:
            self.quarantined.append({"source": path.name, "reason": "missing_front_matter"})
            return
        front = _front_matter(match.group(1))
        directory_tenant = path.parent.name
        declared = front.get("tenant", "")
        if declared != directory_tenant:
            self.quarantined.append(
                {
                    "source": f"{directory_tenant}/{path.name}",
                    "reason": "tenant_front_matter_does_not_match_directory",
                    "directory_tenant": directory_tenant,
                    "front_matter_tenant": declared,
                }
            )
            return
        body, stripped = _strip_instructions(raw[match.end() :])
        self.stripped_instruction_lines += stripped
        year = int(front["plan_year"]) if front.get("plan_year", "").isdigit() else None
        product = front.get("product_code", "ALL")
        title = _first_heading(body) or path.stem
        self.chunks.append(
            Chunk(
                tenant=declared,
                product_code=product,
                plan_year=year,
                source=f"{directory_tenant}/{path.name}",
                title=title,
                text=body.strip(),
            )
        )
        rows = _benefit_rows(body)
        if rows and product != "ALL" and year is not None:
            self.benefits[(declared, product, year)] = rows
        if path.name == "member_services_guide.md":
            self.rules[declared] = _rules(body)
        if path.name == "claims_reference.md":
            self.carc[declared] = _carc(body)

    def benefit_row(self, tenant: str, product: str, year: int, name: str) -> BenefitRow | None:
        for row in self.benefits.get((tenant, product, year), []):
            if row.name.lower() == name.lower():
                return row
        return None

    def member_text_for_carc(self, tenant: str, carc: str) -> str | None:
        return self.carc.get(tenant, {}).get(carc)

    def retrieve(
        self,
        tenant: str,
        query: str,
        product_code: str | None = None,
        year: int | None = None,
        limit: int = 3,
    ) -> list[dict]:
        wanted = set(TOKEN.findall(query.lower()))
        scored: list[tuple[int, Chunk]] = []
        for chunk in self.chunks:
            if chunk.tenant != tenant:
                continue
            if product_code and chunk.product_code not in {product_code, "ALL"}:
                continue
            if year is not None and chunk.plan_year not in {None, year} and chunk.product_code != "ALL":
                continue
            words = set(TOKEN.findall(chunk.text.lower()))
            score = len(wanted & words)
            if score:
                scored.append((score, chunk))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {
                "source": chunk.source,
                "title": chunk.title,
                "plan_year": chunk.plan_year,
                "product_code": chunk.product_code,
                "excerpt": _excerpt(chunk.text, wanted),
            }
            for _, chunk in scored[:limit]
        ]


def _front_matter(block: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _strip_instructions(body: str) -> tuple[str, int]:
    kept: list[str] = []
    stripped = 0
    for line in body.splitlines():
        if INJECTION_LINE.search(line):
            stripped += 1
            continue
        kept.append(line)
    return "\n".join(kept), stripped


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return ""


def _benefit_rows(body: str) -> list[BenefitRow]:
    rows: list[BenefitRow] = []
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        if cells[0].lower() == "benefit" or set(cells[0]) <= set("-: "):
            continue
        rows.append(BenefitRow(name=cells[0], in_network=cells[1], out_of_network=cells[2]))
    return rows


def _rules(body: str) -> PlanRules:
    grievance = re.search(r"Grievance resolution timeframe\s*\|\s*(\d+)\s+calendar days", body)
    appeal = re.search(r"Claim appeal filing window\s*\|\s*(\d+)\s+calendar days", body)
    if not grievance or not appeal:
        raise ValueError("Member services guide is missing a resolution or appeal window.")
    return PlanRules(int(grievance.group(1)), int(appeal.group(1)))


def _carc(body: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in body.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"CO-\d+", cells[0]):
            found[cells[0]] = cells[2]
    return found


def _excerpt(text: str, wanted: set[str], limit: int = 500) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    ranked = sorted(
        paragraphs,
        key=lambda part: len(wanted & set(TOKEN.findall(part.lower()))),
        reverse=True,
    )
    excerpt = "\n".join(ranked[:2]).strip()
    return excerpt[:limit]


def individual_and_family(cell: str) -> tuple:
    amounts = parse_dollars(cell)
    if len(amounts) < 2:
        raise ValueError(f"Expected individual and family amounts in {cell!r}")
    return amounts[0], amounts[1]


def visit_limit(cell: str) -> int:
    match = re.search(r"(\d+)\s+visits", cell)
    if not match:
        raise ValueError(f"Expected a visit limit in {cell!r}")
    return int(match.group(1))
