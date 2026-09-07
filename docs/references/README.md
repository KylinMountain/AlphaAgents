# References

Vendored LLM-friendly documentation for dependencies an agent has to
reason about but cannot read from the network mid-run.

From the agent's point of view, anything it cannot reach in-context does
not exist. A dependency whose behaviour lives only in a website is a
dependency the agent will guess about.

Add a file here when an agent has guessed wrong about a library twice.
Name it `<library>-llms.txt`.

Currently empty — nothing has met that bar yet.
