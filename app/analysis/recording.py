"""逐条保存功能和架构，避免在一次最终工具调用中重写整份项目。"""
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic_ai import ModelRetry, RunContext
from .schemas import Evidence, Feature, Group, ArchitectureNode, ArchitectureEdge, Analysis, validate_evidence
from .tools import RepositoryContext


class CompactEvidence(Evidence):
    path: str = Field(max_length=500)
    role: str = Field(min_length=1, max_length=80)
    symbol: str = Field(default="", max_length=200)
    note: str = Field(min_length=1, max_length=160)


def summary_type(budget):
    short = min(170, budget.max_output_tokens // 48)
    text = Annotated[str, Field(max_length=short)]
    return create_model("ProjectSummary", __config__=ConfigDict(extra="forbid"),
                        overview=(str, Field(max_length=min(1200, budget.max_output_tokens // 5))),
                        readingGuide=(list[text], Field(min_length=1, max_length=6)),
                        limitations=(list[text], Field(default_factory=list, max_length=6)))


def reject(ctx, message):
    ctx.deps.validation_error = message
    ctx.deps.log("记录校验未通过：" + message[:250])
    if ctx.deps.checkpoint:
        ctx.deps.checkpoint()
    raise ModelRetry(message)


def verify(ctx, evidence):
    try:
        validate_evidence(evidence, ctx.deps.files, ctx.deps.reads)
    except ValueError as exc:
        reject(ctx, str(exc))


def saved(ctx, message):
    ctx.deps.validation_error = ""
    ctx.deps.log(message)
    if ctx.deps.checkpoint:
        ctx.deps.checkpoint()


def register_record_tools(agent, budget):
    evidence_type = create_model("BudgetEvidence", __base__=CompactEvidence,
        note=(str, Field(min_length=1, max_length=min(160, budget.max_output_tokens // 32))),
        role=(str, Field(min_length=1, max_length=min(80, budget.max_output_tokens // 64))))
    record_type = create_model("FeatureRecord", __base__=Feature,
        title=(str, Field(min_length=1, max_length=160)), desc=(str, Field(min_length=1, max_length=min(600, budget.max_output_tokens // 8))),
        steps=(list[evidence_type], Field(min_length=1, max_length=8 if budget.max_output_tokens >= 4096 else 3)))
    key_type = Annotated[str, Field(min_length=1, max_length=100)]
    label_type = Annotated[str, Field(min_length=1, max_length=160)]
    description_type = Annotated[str, Field(max_length=min(240, budget.max_output_tokens // 16))]
    references_type = Annotated[list[evidence_type], Field(min_length=1, max_length=2)]

    @agent.tool
    def save_feature(ctx: RunContext[RepositoryContext], key: key_type, group: label_type,
                     summary: description_type, feature: record_type) -> dict:
        """保存一个已验证的业务功能及其实现步骤；每次仅保存一项，固定 key 可幂等更新。"""
        verify(ctx, feature.steps)
        other = {k: v for k, v in ctx.deps.features.items() if k != key}
        groups = {item["group"] for item in other.values()} | {group}
        if len(groups) > 25 or sum(item["group"] == group for item in other.values()) >= 30:
            reject(ctx, "最多 25 个功能分组，每组最多 30 项；请合理合并功能")
        ctx.deps.features[key] = {"group": group, "summary": summary, "feature": feature.model_dump()}
        saved(ctx, f"已保存功能 {len(ctx.deps.features)}：{feature.title}")
        return {"saved": key, "kind": "feature", "total": len(ctx.deps.features)}

    @agent.tool
    def save_node(ctx: RunContext[RepositoryContext], key: key_type, title: label_type,
                  description: description_type, evidence: references_type) -> dict:
        """保存一个架构模块与源码依据。固定 key 可幂等更新，最多 20 个模块。"""
        verify(ctx, evidence)
        if key not in ctx.deps.nodes and len(ctx.deps.nodes) >= 20:
            reject(ctx, "架构最多 20 个模块，请合并次要模块")
        ctx.deps.nodes[key] = {"title": title, "description": description,
                               "evidence": [ref.model_dump() for ref in evidence]}
        saved(ctx, "已保存架构模块：" + title)
        return {"saved": key, "kind": "node"}

    @agent.tool
    def save_edge(ctx: RunContext[RepositoryContext], key: key_type, source: key_type,
                  target: key_type, label: label_type, evidence: evidence_type) -> dict:
        """保存有代码依据的有向架构关系。source/target 必须是已保存节点的 key。"""
        verify(ctx, [evidence])
        if source not in ctx.deps.nodes or target not in ctx.deps.nodes:
            reject(ctx, "请先用 save_node 保存关系两端的架构节点")
        if key not in ctx.deps.edges and len(ctx.deps.edges) >= 40:
            reject(ctx, "最多 40 条架构关系，请保留主要关系")
        ctx.deps.edges[key] = {"source": source, "target": target, "label": label, "evidence": [evidence.model_dump()]}
        saved(ctx, "已保存架构关系：" + label)
        return {"saved": key, "kind": "edge"}

    @agent.tool
    def list_saved(ctx: RunContext[RepositoryContext], offset: int = 0) -> dict:
        """查看已保存功能、模块和关系的 key 与标题，恢复时避免重复工作；每页 20 项。"""
        entries = ([{"key": key, "kind": "feature", "title": item["feature"]["title"]} for key, item in ctx.deps.features.items()]
                   + [{"key": key, "kind": "node", "title": item["title"]} for key, item in ctx.deps.nodes.items()]
                   + [{"key": key, "kind": "edge", "title": item["label"]} for key, item in ctx.deps.edges.items()])
        selected = []
        size = 0
        from .budget import count_tokens
        import json
        offset = max(0, offset)
        for item in entries[offset:offset + 20]:
            cost = count_tokens(json.dumps(item, ensure_ascii=False)) + 2
            if selected and size + cost > budget.tool_tokens:
                break
            selected.append(item)
            size += cost
        next_offset = offset + len(selected)
        return {"total": len(entries), "items": selected,
                "next_offset": next_offset if next_offset < len(entries) else None}



def assemble(summary, context):
    groups = {}
    for record in context.features.values():
        group = groups.setdefault(record["group"], {"title": record["group"], "summary": record["summary"], "features": []})
        group["features"].append(Feature.model_validate(record["feature"]))
    indices = {key: index for index, key in enumerate(context.nodes)}
    edges = [ArchitectureEdge(source=indices[item["source"]], target=indices[item["target"]],
                              label=item["label"], evidence=item["evidence"]) for item in context.edges.values()]
    return Analysis(**summary.model_dump(), featureGroups=[Group.model_validate(group) for group in groups.values()],
                    architecture=[ArchitectureNode.model_validate(node) for node in context.nodes.values()], edges=edges)
