# sidera-agents

`sidera-agents` is a standalone Python package for agent profiles, runtime
capabilities, execution contracts, and run observations. Its public API uses
serializable contracts and generic authorization, context, secret, and tool
connection ports; it does not depend on SIDERA task or product internals.

Install the core package with `pydantic`. Install the `postgres` extra only
when PostgreSQL persistence is needed. The native Harness adapter contract is
internal and is not exported from the package root.
