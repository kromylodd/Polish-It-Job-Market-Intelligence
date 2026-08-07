{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with seniorities as (
    select distinct seniority as level
    from {{ ref('stg_listings') }}
    where seniority is not null and seniority != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['level']) }} as seniority_key,
    level
from seniorities
