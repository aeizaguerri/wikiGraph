# Truncated graphs keep referential integrity

When a Crawl run hits the Node cap, discovery stops mid-stream. A link found on a crawled Article may point at an Article that will never be added because the cap was already reached. ADR 0001 says every Article link found on a crawled Article becomes a directed edge — taken literally, that would add edges whose target node does not exist. We drop the edge together with the node: a truncated Graph contains no dangling edges, so the renderer never conjures phantom nodes from edge endpoints, and counters (discovered = node count) stay honest about what the Graph shows.

**Considered Options**: keep all found edges with dangling targets (rejected: the renderer would fabricate nodes beyond the cap and the payload would lie about what was discovered); drop node and edge together (chosen: partial Graph stays a consistent Graph).
