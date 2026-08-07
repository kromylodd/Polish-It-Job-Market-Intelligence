{{
    config(
        materialized='table',
        schema='gold'
    )
}}

with city_lookup as (
    select
        city_name,
        voivodeship
    from {{ ref('city_lookup') }}
)

select
    {{ dbt_utils.generate_surrogate_key(['city_name']) }} as city_key,
    city_name,
    voivodeship
from city_lookup
