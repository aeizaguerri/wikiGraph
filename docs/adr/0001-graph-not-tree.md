# Graph, not tree

Wikipedia article links form cycles (A links to B, B links back to A), so a crawl result could be modeled as a tree — with articles duplicated under every parent that links to them — or as a directed graph. We chose the graph: each article is exactly one node, every article link found on a crawled article becomes a directed edge, and each node is crawled once no matter how many paths reach it. A tree would inflate the dataset with duplicates and hide the very cycles we set out to show.

**Considered Options**: tree model (rejected: duplicates nodes, hides cycles, misrepresents Wikipedia's structure).
