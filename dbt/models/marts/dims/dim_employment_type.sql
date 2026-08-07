{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with employment_types as (
    select distinct
        sv.employment_type as type
    from {{ ref('stg_listings') }} l
    lateral view explode(l.salary_variants) sv as employment_type, salary_min, salary_max, currency, is_gross
    where sv.employment_type is not null and sv.employment_type != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['type']) }} as employment_type_key,
    type
from employment_types
