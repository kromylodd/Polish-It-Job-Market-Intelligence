{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with workplace_types as (
    select distinct workplace_type as type
    from {{ ref('stg_listings') }}
    where workplace_type is not null and workplace_type != ''
)

select
    {{ dbt_utils.generate_surrogate_key(['type']) }} as workplace_type_key,
    type
from workplace_types
