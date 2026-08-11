{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with listings_with_indices as (
    select
        l.salary_variants,
        unnest(range(0, json_array_length(l.salary_variants)::int)) as sv_idx
    from {{ ref('stg_listings') }} l
    where l.salary_variants is not null
      and l.salary_variants != '[]'
      and l.salary_variants != ''
      and json_valid(l.salary_variants)
      and json_array_length(l.salary_variants) > 0
),

employment_types as (
    select distinct
        json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].employment_type') as type
    from listings_with_indices li
    where json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].employment_type') is not null
      and json_extract_string(li.salary_variants, '$[' || li.sv_idx || '].employment_type') != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['type']) }} as employment_type_key,
    type
from employment_types
