"""Agent 结构化输出及引用约束。"""
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    path: str
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    symbol: str = ""
    role: str
    note: str


class Feature(BaseModel):
    title: str
    desc: str
    steps: list[Evidence] = Field(min_length=1, max_length=20)


class Group(BaseModel):
    title: str
    summary: str
    features: list[Feature] = Field(min_length=1, max_length=30)


class ArchitectureNode(BaseModel):
    title: str
    description: str
    evidence: list[Evidence] = Field(min_length=1, max_length=8)


class ArchitectureEdge(BaseModel):
    source: int = Field(ge=0)
    target: int = Field(ge=0)
    label: str
    evidence: list[Evidence] = Field(min_length=1, max_length=5)


class Analysis(BaseModel):
    overview: str
    readingGuide: list[str] = Field(min_length=1, max_length=8)
    featureGroups: list[Group] = Field(min_length=1, max_length=25)
    architecture: list[ArchitectureNode] = Field(min_length=1, max_length=20)
    edges: list[ArchitectureEdge] = Field(default_factory=list, max_length=40)
    limitations: list[str] = Field(default_factory=list)


class Answer(BaseModel):
    answer: str
    citations: list[Evidence] = Field(default_factory=list, max_length=15)


def all_evidence(analysis):
    for group in analysis.featureGroups:
        for feature in group.features:
            yield from feature.steps
    for node in analysis.architecture:
        yield from node.evidence
    for edge in analysis.edges:
        yield from edge.evidence


def validate_evidence(evidence, files, read_ranges=None):
    for ref in evidence:
        file = files.get(ref.path)
        if not file or not (1 <= ref.line <= ref.end_line <= file["lines"]):
            raise ValueError(f"无效引用: {ref.path}:{ref.line}-{ref.end_line}")
        if read_ranges is not None:
            cursor = ref.line
            # 多次分页读取的连续区间也构成有效证据，但绝不跨过未读的间隙。
            for start, end in sorted(read_ranges.get(ref.path, [])):
                if start > cursor:
                    break
                if end >= cursor:
                    cursor = end + 1
                if cursor > ref.end_line:
                    break
            if cursor <= ref.end_line:
                raise ValueError(f"引用未通过 read_file 阅读，请先阅读: {ref.path}:{ref.line}-{ref.end_line}")



def validate_analysis(analysis, files, read_ranges=None):
    validate_evidence(all_evidence(analysis), files, read_ranges)
    for edge in analysis.edges:
        if edge.source >= len(analysis.architecture) or edge.target >= len(analysis.architecture):
            raise ValueError("架构关系指向不存在的节点")
