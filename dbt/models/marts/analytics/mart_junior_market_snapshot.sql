{{
    config(
        materialized='table',
        schema='gold'
    )
}}

-- Junior-only slice of mart_market_snapshot, kept for backward compatibility
-- (older dashboards / the original junior-focused alert framing). The daily
-- alerts now source the all-seniorities mart_market_snapshot directly and apply
-- each user's seniority filter downstream.

select *
from {{ ref('mart_market_snapshot') }}
where seniority = 'junior'
