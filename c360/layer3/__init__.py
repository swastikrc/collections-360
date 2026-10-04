"""Layer 3: act on the Collections 360 record.

Two front ends over one set of engines:
  - the employee terminal (portfolio monitor, customer/case 360 with next best action and
    human approval, post-call summary and QA scoring), and
  - the customer self-service app (what I owe, hardship options, promises and payments, assistant).

The engines read the Collections 360 customer document (the shape served by `GET /customers/{key}`
in SPEC_FRONTEND.md and by `c360.store.C360Store`). Where that document comes from is a setting:
`C360_L3_SOURCE=local` assembles it from the Maple dataset in DuckDB; `C360_L3_SOURCE=api` reads it
from the Layer 2 HTTP API once that is running.
"""
