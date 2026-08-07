{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with employment_types as (
    select distinct
        sv.col.employment_type as type
    from {{ ref('stg_listings') }} l
    lateral view explode(l.salary_variants) sv
    where sv.col.employment_type is not null and sv.col.employment_type != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['type']) }} as employment_type_key,
    type
from employment_types
