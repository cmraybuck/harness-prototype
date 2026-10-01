# Implementation planning

Apply this workflow when the user asks for a plan, design, or task breakdown. Scale detail to the change; a small change may need only a few steps.

1. Identify the desired user-visible outcome, constraints, and explicit exclusions. Inspect relevant project guidance, interfaces, and tests when tools are available; otherwise work from the supplied context. Distinguish confirmed facts from assumptions. Ask about an unresolved decision only when it materially changes scope or design.
2. Choose an approach that fits existing modules and interfaces. Explain consequential tradeoffs and alternatives only where they affect the decision. Account for failure behavior, compatibility, and data migration when the proposed change touches them.
3. Produce ordered, concrete steps tied to affected files or responsibilities. Put prerequisite decisions before dependent work. Each step should state its intended behavior and a checkable completion condition. Include meaningful acceptance scenarios for the successful path and likely failures, with verification appropriate to the project.
4. Close with open decisions, material risks, and the first actionable step. Label suggested checks as proposed rather than executed. Keep optional improvements separate from the work needed to satisfy the request.

A planning request produces a plan; implementation follows when requested. Use available tools and existing permissions without assuming access to execution, deployment, or external services.
