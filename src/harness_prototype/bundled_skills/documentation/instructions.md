# Documentation

Apply this workflow when the user requests documentation or a written explanation. Match the audience and artifact requested.

1. Determine what the reader needs to accomplish and find the authoritative implementation, configuration, or supplied notes. Inspect the relevant sources using available tools. Treat existing documentation as a claim to check, especially for defaults, commands, limits, and error behavior.
2. Organize around the reader's task: prerequisites, the shortest useful path, expected outcomes, and recovery from likely failures. For API documentation, explain inputs, outputs, side effects, and material limits. For handoffs, record current state, decisions, verification, and outstanding work while preserving historical entries.
3. Write concrete examples that agree with the inspected interfaces. Use placeholders for credentials and personal paths. Label proposed behavior and uncertain details explicitly; separate current capabilities from future plans. Link to the authoritative source instead of duplicating large reference material.
4. If file edits are requested, update the relevant existing document and preserve unrelated content. Review the changed text for stale names, contradictions, broken relative references, and unsupported claims. State which examples were checked against source and which, if any, were actually run.

Use supported file tools and existing approvals for edits. Documentation examples do not authorize executing commands or tests. Finish with the requested artifact or explanation and any unresolved accuracy limits.
