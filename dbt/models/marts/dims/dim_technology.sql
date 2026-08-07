{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with tech_lookup as (
    select
        canonical_name,
        tech_category
    from {{ ref('technology_lookup') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['canonical_name']) }} as technology_key,
    canonical_name,
    tech_category
from tech_lookup
