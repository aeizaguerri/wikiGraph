# Domain documentation

Use [`README.md`](../../README.md) as the canonical domain and product reference when exploring wikiGraph. It documents implemented behavior with source/test evidence, input and graph semantics, API/run lifecycle, architecture decisions, security, deployment operations, and explicitly bounded verification status.

## Find the relevant section

- Crawl or graph behavior: **Product behavior** and **Architecture decisions**.
- API, persistence, recovery, or lifecycle: **API and run lifecycle**.
- Trust boundaries or credentials: **Security and privacy**.
- Local commands, hosted configuration, or cutover: **Local development and checks** and **Deployment and operations**.
- Production acceptance gaps: **Verification status and production gaps**.

Read the linked implementation/tests for precise behavior. README claims describe repository implementation and historical evidence; they do not imply tests were run during the current task or that production behavior has been verified. Preserve the distinction between source/test evidence, historical receipts, and current deployed proof.

Agent workflow instructions and skills remain under `docs/agents/` and `.agents/skills/`; they are not product-domain documentation. Local `.scratch/` issue history remains historical context, not a specification of currently implemented behavior.
