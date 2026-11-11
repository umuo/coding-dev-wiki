"""无模型的显式结构预览，不宣称理解业务或推断调用链。"""
from collections import defaultdict
from .schemas import Analysis, Group, Feature, Evidence, ArchitectureNode


def preview(files):
    groups = defaultdict(list)
    for path, file in files.items():
        prefix = path.split("/")[0] if "/" in path else "根目录"
        groups[prefix].append(file)
    if len(groups) > 25:
        names = list(groups)
        retained = {name: groups[name] for name in names[:24]}
        retained["其他目录（结构预览）"] = [file for name in names[24:] for file in groups[name]]
        groups = retained
    output = []
    nodes = []
    for prefix, entries in groups.items():
        features = []
        for file in entries[:25]:
            symbols = file["symbols"][:8]
            steps = [Evidence(path=file["path"], line=s["line"], end_line=s["end_line"],
                              symbol=s["name"], role=s["kind"], note=s.get("doc") or "源码定义，未分析业务含义") for s in symbols]
            if not steps:
                steps = [Evidence(path=file["path"], line=1, end_line=min(file["lines"], 60),
                                  symbol="", role="文件内容", note="结构预览：请查看完整文件")]
            features.append(Feature(title=file["path"], desc="结构预览：展示文件和符号，不代表业务功能分析。", steps=steps))
        output.append(Group(title=prefix, summary=f"{len(entries)} 个文件", features=features))
        if len(nodes) < 20:
            nodes.append(ArchitectureNode(title=prefix, description="源码目录分组（不是推断的架构关系）",
                                          evidence=features[0].steps[:1]))
    return Analysis(overview="这是未使用 LLM 的结构预览。配置模型后选择「Agent 分析」，生成业务功能、架构和实现链路说明。",
                                    readingGuide=["先查看 README 和配置，再浏览入口文件。"],
                                    featureGroups=output, architecture=nodes, edges=[],
                                    limitations=["未进行业务分析或调用关系验证；每个目录最多预览 25 个文件，完整源码仍可检索。"])
